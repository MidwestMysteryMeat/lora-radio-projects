"""
main.py -- Tag firmware for the XIAO ESP32-S3 (MicroPython)

Complete tag firmware. On every wake from deep sleep it:
  1. reads 6 bytes of state + a cached GPS fix from RTC memory (survives sleep)
  2. checks the charge pin -- if charging, deep-sleeps without advancing state
     (and detects USB-unplug as a "deploy" trigger that resets to Phase 0)
  3. advances the operating phase based on cumulative cycle count
  4. reads GPS every Nth cycle (caches the fix; uses the cache on skip cycles)
  5. fires the phase-appropriate SOS burst over BLE + LoRa
  6. deep-sleeps until the next cycle

Runs on the ESP32-S3. The paired ESP32-C3 runs Meshtastic and owns the
LoRa mesh; the LoRa transmit here drives an RFM95W directly over SPI.

Reconstructed from conversation history -- this is the full original
firmware including charge handling, GPS caching, and deploy-on-unplug.
Verify pin assignments and 915MHz register values against your wiring.

GPIO map (XIAO ESP32-S3):
  D0/GPIO1  -> RFM95W DIO0  (TX-done IRQ)
  D1/GPIO2  -> RFM95W RST
  D2/GPIO3  -> TP4056 CHRG  (pull-up; LOW = charging)
  D3/GPIO4  -> RFM95W NSS/CS
  D6/GPIO43 -> GPS RX (config to GPS, optional)
  D7/GPIO44 -> GPS TX (NMEA in)
  D8/GPIO7  -> RFM95W SCK
  D9/GPIO8  -> RFM95W MISO
  D10/GPIO9 -> RFM95W MOSI
"""

import machine
import time
import bluetooth
from machine import UART, Pin, SPI

# ── Hardware pins ────────────────────────────────────────────────────────
CHRG_PIN = Pin(3, Pin.IN, Pin.PULL_UP)     # LOW = charging

# GPS on UART1 (TX=GPIO43, RX=GPIO44)
gps = UART(1, baudrate=9600, tx=Pin(43), rx=Pin(44))

# RFM95W on SPI2 (SCK=GPIO7, MOSI=GPIO9, MISO=GPIO8)
spi = SPI(2, baudrate=5_000_000, sck=Pin(7), mosi=Pin(9), miso=Pin(8))
cs = Pin(4, Pin.OUT, value=1)    # NSS / chip select
rst = Pin(2, Pin.OUT, value=1)   # RST
dio0 = Pin(1, Pin.IN)            # TX-done interrupt

# BLE
ble = bluetooth.BLE()
ble.active(True)

DEVICE_ID = b'TAG1'

# ── Phase configuration ──────────────────────────────────────────────────
# Phase 0 -- first ~8 hours   -- 3-packet mini-burst -- 10s/20s sleep
# Phase 1 -- 8 hrs to day 5   -- 9-packet SOS burst  -- 1min/3min sleep
# Phase 2 -- day 5+           -- 9-packet SOS burst  -- 15min/30min sleep
SLEEP_PHASES = [
    [10_000,    20_000],         # Phase 0: 10s / 20s
    [60_000,   180_000],         # Phase 1: 1min / 3min
    [900_000, 1_800_000],        # Phase 2: 15min / 30min
]
BURST_PACKETS = [3, 9, 9]        # mini-burst in phase 0, full SOS in 1 and 2
GPS_EVERY_N = [3, 3, 3]          # GPS read every 3rd cycle in all phases

# Phase transitions based on cumulative cycle counts:
#   Phase 0 -> 1: ~8hrs / 15s avg cycle  = ~1920 cycles
#   Phase 1 -> 2: day 5 (112hrs) / 120s avg = ~3360 more -> 5280 cumulative
PHASE_THRESHOLDS = [1920, 5280]

# ── SX1276 / RFM95W registers ────────────────────────────────────────────
REG_FIFO        = 0x00
REG_OP_MODE     = 0x01
REG_PA_CONFIG   = 0x09
REG_FIFO_ADDR   = 0x0D
REG_TX_BASE     = 0x0E
REG_RX_BASE     = 0x0F
REG_PAYLOAD_LEN = 0x22
REG_IRQ_FLAGS   = 0x12
MODE_LORA_SLEEP = 0x80
MODE_LORA_STDBY = 0x81
MODE_LORA_TX    = 0x83


def sx_write(reg, val):
    cs(0)
    spi.write(bytes([reg | 0x80, val]))
    cs(1)


def sx_read(reg):
    cs(0)
    spi.write(bytes([reg & 0x7F]))
    v = spi.read(1)[0]
    cs(1)
    return v


def lora_init():
    rst(0); time.sleep_ms(10); rst(1); time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_SLEEP)   # sleep (required to set LoRa bit)
    time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)   # LoRa standby
    sx_write(0x06, 0xE4)                      # 915MHz freq high byte
    sx_write(0x07, 0xC0)                      # 915MHz freq mid byte
    sx_write(0x08, 0x00)                      # 915MHz freq low byte
    sx_write(REG_PA_CONFIG, 0x8F)            # PA_BOOST, max power
    sx_write(0x1D, 0x72)                      # BW 125kHz, CR 4/5, explicit header
    sx_write(0x1E, 0x74)                      # SF7, CRC on
    sx_write(0x26, 0x04)                      # low data rate optimize off
    sx_write(REG_FIFO_ADDR, sx_read(REG_TX_BASE))


def lora_send(payload):
    sx_write(REG_FIFO_ADDR, sx_read(REG_TX_BASE))   # reset FIFO ptr
    for b in payload:
        sx_write(REG_FIFO, b)
    sx_write(REG_PAYLOAD_LEN, len(payload))
    sx_write(REG_OP_MODE, MODE_LORA_TX)             # transmit
    deadline = time.ticks_ms() + 3000
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if sx_read(REG_IRQ_FLAGS) & 0x08:           # TxDone flag
            break
        time.sleep_ms(10)
    sx_write(REG_IRQ_FLAGS, 0xFF)                   # clear all IRQ flags
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)


# ── GPS ──────────────────────────────────────────────────────────────────
def read_gps(timeout_ms=8000):
    """Parse GPRMC for a valid fix. Returns (lat, lon) or (None, None)."""
    deadline = time.ticks_ms() + timeout_ms
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if gps.any():
            line = gps.readline()
            if line and b'GPRMC' in line:
                try:
                    parts = line.decode().split(',')
                    if parts[2] == 'A':             # A = active / valid fix
                        rlat = float(parts[3])
                        rlon = float(parts[5])
                        lat = int(rlat / 100) + (rlat % 100) / 60
                        lon = int(rlon / 100) + (rlon % 100) / 60
                        if parts[4] == 'S': lat = -lat
                        if parts[6] == 'W': lon = -lon
                        return lat, lon
                except (ValueError, IndexError):
                    pass
    return None, None


# ── Signed 32-bit coordinate encoding ────────────────────────────────────
# MicroPython's int.to_bytes()/int.from_bytes() take no 'signed' argument,
# so negative values (western longitudes, southern latitudes) are handled
# manually as two's complement. The finder's parse_ble_payload() and the
# RTC cache below must all agree on this encoding.
def i32_to_bytes(value):
    """Encode a signed int as 4 bytes big-endian two's complement."""
    return (value & 0xFFFFFFFF).to_bytes(4, 'big')


def i32_from_bytes(raw):
    """Decode 4 bytes big-endian two's complement as a signed int."""
    value = int.from_bytes(raw, 'big')
    return value - 0x100000000 if value & 0x80000000 else value


# ── Payload + bursts ─────────────────────────────────────────────────────
def make_payload(lat, lon, burst_index):
    lat_int = int(lat * 1_000_000)
    lon_int = int(lon * 1_000_000)
    data = bytearray(b'\xFF\xFF')                   # private company ID
    data += DEVICE_ID                               # b'TAG1'
    data += i32_to_bytes(lat_int)
    data += i32_to_bytes(lon_int)
    data += bytes([burst_index])
    return bytes(data)


def ble_advertise(payload):
    adv = bytes([len(payload) + 1, 0xFF]) + payload
    ble.gap_advertise(100_000, adv_data=adv)        # 100ms window
    time.sleep_ms(120)
    ble.gap_advertise(None)


# SOS morse weights: 3 short (dot) / 3 long (dash) / 3 short (dot)
SOS_WEIGHTS = [1, 1, 1, 3, 3, 3, 1, 1, 1]           # weight 1 = short, 3 = long


def fire_burst(lat, lon, n_packets):
    # One LoRa packet per burst (Meshtastic node handles mesh relay if bridged)
    lora_init()
    lora_send(make_payload(lat, lon, 0))

    # BLE advertisements in the SOS timing pattern
    weights = SOS_WEIGHTS[:n_packets]
    for i, weight in enumerate(weights):
        ble_advertise(make_payload(lat, lon, i))
        time.sleep_ms(300 * weight)                 # short or long gap


# ── RTC memory (state survives deep sleep) ───────────────────────────────
# Byte 0: cycle_index  (0/1 -- which sleep interval in current phase)
# Byte 1: gps_skip     (counts up, GPS read when == 0)
# Byte 2: phase        (0/1/2)
# Byte 3: charge_flag  (1 = was charging last wake)
# Bytes 4-5: cycle_count (16-bit big-endian -- total cycles ever)
# Bytes 6-13: cached last-known GPS fix (lat 4 bytes, lon 4 bytes)
def read_rtc():
    try:
        m = machine.RTC().memory()
        if len(m) < 6:
            raise ValueError
        cycle_idx   = m[0] % 2
        gps_skip    = m[1] % 3
        phase       = min(m[2], 2)
        charge_flag = m[3] & 1
        cycle_count = (m[4] << 8) | m[5]
        return cycle_idx, gps_skip, phase, charge_flag, cycle_count
    except (ValueError, IndexError):
        return 0, 0, 0, 0, 0


def write_rtc(cycle_idx, gps_skip, phase, charge_flag, cycle_count):
    cycle_count = min(cycle_count, 0xFFFF)
    # Preserve the GPS cache (bytes 6-13) when rewriting state
    old = bytearray(machine.RTC().memory())
    new = bytearray(14)
    if len(old) >= 14:
        new[6:14] = old[6:14]
    new[0] = cycle_idx % 2
    new[1] = gps_skip % 3
    new[2] = min(phase, 2)
    new[3] = charge_flag & 1
    new[4] = (cycle_count >> 8) & 0xFF
    new[5] = cycle_count & 0xFF
    machine.RTC().memory(bytes(new))


def save_gps_cache(lat, lon):
    lat_int = int(lat * 1_000_000)
    lon_int = int(lon * 1_000_000)
    mem = bytearray(machine.RTC().memory())
    if len(mem) < 14:
        mem = mem + bytes(14 - len(mem))
    mem[6:10] = i32_to_bytes(lat_int)
    mem[10:14] = i32_to_bytes(lon_int)
    machine.RTC().memory(bytes(mem))


def load_gps_cache():
    try:
        m = machine.RTC().memory()
        lat = i32_from_bytes(m[6:10]) / 1_000_000
        lon = i32_from_bytes(m[10:14]) / 1_000_000
        if lat == 0 and lon == 0:
            return None, None
        return lat, lon
    except (ValueError, IndexError):
        return None, None


# ── Main entry -- runs on every wake from deep sleep ─────────────────────
def run():
    cycle_idx, gps_skip, phase, charge_flag, cycle_count = read_rtc()
    is_charging = (CHRG_PIN.value() == 0)

    # Deploy-on-unplug: USB removed (was charging, now not) resets to Phase 0
    if charge_flag and not is_charging:
        cycle_idx = 0
        gps_skip = 0
        phase = 0
        cycle_count = 0

    # Charging mode: deep sleep, do not advance phase or counters.
    # Must be deepsleep -- waking from lightsleep resumes here and a
    # SystemExit would drop to the REPL and halt the tag; deep-sleep
    # wake restarts main.py so the charge pin is re-checked in 30s.
    if is_charging:
        write_rtc(cycle_idx, gps_skip, phase, 1, cycle_count)
        machine.deepsleep(30_000)

    # Advance phase if cumulative cycle thresholds crossed
    if phase == 0 and cycle_count >= PHASE_THRESHOLDS[0]:
        phase = 1
    elif phase == 1 and cycle_count >= PHASE_THRESHOLDS[1]:
        phase = 2

    # GPS: read on skip==0, otherwise use the cached fix
    if gps_skip == 0:
        lat, lon = read_gps(timeout_ms=8000)
        if lat is not None:
            save_gps_cache(lat, lon)
    else:
        lat, lon = load_gps_cache()

    # Fire the burst if we have a position at all
    if lat is not None:
        fire_burst(lat, lon, BURST_PACKETS[phase])

    # Advance counters and deep sleep
    next_cycle_idx = (cycle_idx + 1) % 2
    next_gps_skip = (gps_skip + 1) % GPS_EVERY_N[phase]
    next_cycle_count = min(cycle_count + 1, 0xFFFF)
    write_rtc(next_cycle_idx, next_gps_skip, phase, 0, next_cycle_count)

    sleep_ms = SLEEP_PHASES[phase][cycle_idx]
    machine.deepsleep(sleep_ms)   # < 0.2mA until the timer fires and main.py restarts


run()
