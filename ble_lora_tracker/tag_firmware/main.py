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

Runs on the ESP32-S3. The paired ESP32-C3 is intended to run Meshtastic;
this firmware also drives an RFM95W directly over SPI for a raw LoRa
beacon. Those two LoRa paths conflict if they share one radio -- pick one
(see README "Radio-path status"). The BLE path is self-contained and
works without the C3.

Reconstructed from conversation history -- verify pin assignments and
915MHz register values against your wiring.

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
spi = SPI(2, baudrate=5_000_000, polarity=0, phase=0,
          sck=Pin(7), mosi=Pin(9), miso=Pin(8))
cs = Pin(4, Pin.OUT, value=1)    # NSS / chip select
rst = Pin(2, Pin.OUT, value=1)   # RST
dio0 = Pin(1, Pin.IN)            # TX-done interrupt (optional poll aid)

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

IRQ_TX_DONE     = 0x08


def sx_write(reg, val):
    cs(0)
    spi.write(bytes([(reg & 0x7F) | 0x80, val & 0xFF]))
    cs(1)


def sx_read(reg):
    buf = bytearray([reg & 0x7F, 0x00])
    cs(0)
    spi.write_readinto(buf, buf)
    cs(1)
    return buf[1]


def lora_init():
    rst(0)
    time.sleep_ms(10)
    rst(1)
    time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_SLEEP)   # sleep (required to set LoRa bit)
    time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)
    # Frf = 915e6 / (32e6 / 2^19) = 0xE4C000
    sx_write(0x06, 0xE4)
    sx_write(0x07, 0xC0)
    sx_write(0x08, 0x00)
    sx_write(REG_PA_CONFIG, 0x8F)            # PA_BOOST, max power
    sx_write(0x1D, 0x72)                      # BW 125kHz, CR 4/5, explicit header
    sx_write(0x1E, 0x74)                      # SF7, CRC on
    sx_write(0x26, 0x04)                      # low data rate optimize off
    sx_write(REG_IRQ_FLAGS, 0xFF)
    sx_write(REG_FIFO_ADDR, sx_read(REG_TX_BASE))


def lora_send(payload):
    """Transmit one raw LoRa frame. No-op if payload empty or too long."""
    if not payload or len(payload) > 255:
        return
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)
    sx_write(REG_IRQ_FLAGS, 0xFF)
    sx_write(REG_FIFO_ADDR, sx_read(REG_TX_BASE))
    for b in payload:
        sx_write(REG_FIFO, b)
    sx_write(REG_PAYLOAD_LEN, len(payload))
    sx_write(REG_OP_MODE, MODE_LORA_TX)
    deadline = time.ticks_ms() + 3000
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        # Always require the TxDone IRQ bit. DIO0 alone is not enough —
        # an unwired/floating pin would exit TX early and cut the packet.
        if sx_read(REG_IRQ_FLAGS) & IRQ_TX_DONE:
            break
        time.sleep_ms(5)
    sx_write(REG_IRQ_FLAGS, 0xFF)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)


# ── GPS ──────────────────────────────────────────────────────────────────
def parse_rmc_sentence(line):
    """Parse a GPRMC/GNRMC NMEA sentence into (lat, lon) or (None, None)."""
    try:
        if isinstance(line, bytes):
            text = line.decode('ascii', 'ignore')
        else:
            text = str(line)
        text = text.strip()
        if '*' in text:
            text = text.split('*', 1)[0]
        start = text.find('$')
        if start < 0:
            return None, None
        text = text[start:]
        parts = text.split(',')
        if len(parts) < 7 or 'RMC' not in parts[0]:
            return None, None
        if parts[2] != 'A':
            return None, None
        rlat = float(parts[3])
        rlon = float(parts[5])
        lat = int(rlat // 100) + (rlat % 100) / 60.0
        lon = int(rlon // 100) + (rlon % 100) / 60.0
        if parts[4] == 'S':
            lat = -lat
        if parts[6] == 'W':
            lon = -lon
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None, None
        return lat, lon
    except (ValueError, IndexError, TypeError):
        return None, None


def read_gps(timeout_ms=8000):
    """Parse GPRMC/GNRMC for a valid fix. Returns (lat, lon) or (None, None)."""
    while gps.any():
        try:
            gps.readline()
        except Exception:
            break

    deadline = time.ticks_ms() + timeout_ms
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if gps.any():
            line = gps.readline()
            if line and b'RMC' in line:
                lat, lon = parse_rmc_sentence(line)
                if lat is not None:
                    return lat, lon
        else:
            time.sleep_ms(20)
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
    if raw is None or len(raw) < 4:
        raise ValueError('need 4 bytes')
    value = int.from_bytes(raw[:4], 'big')
    return value - 0x100000000 if value & 0x80000000 else value


# ── Payload + bursts ─────────────────────────────────────────────────────
def make_payload(lat, lon, burst_index):
    """Build BLE manufacturer-data body: company_id(2) + TAG1 + lat + lon + idx.

    company_id 0xFFFF is placed in the manufacturer-data body so the full
    BLE AD structure is [len, 0xFF, 0xFF, 0xFF, 'T','A','G','1', ...].
    bleak exposes company_id=0xFFFF and the remaining bytes starting at TAG1.
    """
    lat_int = int(round(lat * 1_000_000))
    lon_int = int(round(lon * 1_000_000))
    data = bytearray(b'\xFF\xFF')                   # private company ID
    data += DEVICE_ID                               # b'TAG1'
    data += i32_to_bytes(lat_int)
    data += i32_to_bytes(lon_int)
    data += bytes([burst_index & 0xFF])
    return bytes(data)


def ble_advertise(payload):
    """Advertise manufacturer-specific data for a short window."""
    if not payload or len(payload) > 29:
        return  # BLE adv max 31 bytes including len+type
    adv = bytes([len(payload) + 1, 0xFF]) + payload
    try:
        ble.gap_advertise(100_000, adv_data=adv)    # 100ms interval (us)
        time.sleep_ms(120)
    finally:
        try:
            ble.gap_advertise(None)
        except Exception:
            pass


# SOS morse weights: 3 short (dot) / 3 long (dash) / 3 short (dot)
SOS_WEIGHTS = [1, 1, 1, 3, 3, 3, 1, 1, 1]


def fire_burst(lat, lon, n_packets):
    """One LoRa frame + n_packets BLE ads in SOS timing."""
    n_packets = max(1, min(n_packets, len(SOS_WEIGHTS)))
    try:
        lora_init()
        lora_send(make_payload(lat, lon, 0))
    except Exception:
        pass  # BLE still useful if LoRa SPI fails

    weights = SOS_WEIGHTS[:n_packets]
    for i, weight in enumerate(weights):
        ble_advertise(make_payload(lat, lon, i))
        time.sleep_ms(300 * weight)


# ── RTC memory (state survives deep sleep) ───────────────────────────────
# Byte 0: cycle_index  (0/1 -- which sleep interval in current phase)
# Byte 1: gps_skip     (counts up, GPS read when == 0)
# Byte 2: phase        (0/1/2)
# Byte 3: charge_flag  (1 = was charging last wake)
# Bytes 4-5: cycle_count (16-bit big-endian -- total cycles ever)
# Bytes 6-13: cached last-known GPS fix (lat 4 bytes, lon 4 bytes)
# Byte 14: magic (0xA5) -- detects first boot / wiped RTC
RTC_MAGIC = 0xA5
RTC_LEN = 15


def read_rtc():
    try:
        m = machine.RTC().memory()
        if len(m) < 6 or (len(m) >= RTC_LEN and m[14] != RTC_MAGIC):
            raise ValueError('uninitialized RTC')
        cycle_idx   = m[0] % 2
        phase       = min(m[2], 2)
        gps_period  = GPS_EVERY_N[phase]
        gps_skip    = m[1] % gps_period
        charge_flag = m[3] & 1
        cycle_count = (m[4] << 8) | m[5]
        return cycle_idx, gps_skip, phase, charge_flag, cycle_count
    except (ValueError, IndexError, TypeError):
        return 0, 0, 0, 0, 0


def write_rtc(cycle_idx, gps_skip, phase, charge_flag, cycle_count):
    cycle_count = min(int(cycle_count), 0xFFFF)
    phase = min(int(phase), 2)
    gps_period = GPS_EVERY_N[phase]
    # Preserve the GPS cache (bytes 6-13) when rewriting state
    old = bytearray(machine.RTC().memory())
    new = bytearray(RTC_LEN)
    if len(old) >= 14:
        new[6:14] = old[6:14]
    new[0] = int(cycle_idx) % 2
    new[1] = int(gps_skip) % gps_period
    new[2] = phase
    new[3] = int(charge_flag) & 1
    new[4] = (cycle_count >> 8) & 0xFF
    new[5] = cycle_count & 0xFF
    new[14] = RTC_MAGIC
    machine.RTC().memory(bytes(new))


def save_gps_cache(lat, lon):
    lat_int = int(round(lat * 1_000_000))
    lon_int = int(round(lon * 1_000_000))
    mem = bytearray(machine.RTC().memory())
    if len(mem) < RTC_LEN:
        mem = mem + bytes(RTC_LEN - len(mem))
        mem[14] = RTC_MAGIC
    mem[6:10] = i32_to_bytes(lat_int)
    mem[10:14] = i32_to_bytes(lon_int)
    machine.RTC().memory(bytes(mem))


def load_gps_cache():
    """Return cached (lat, lon) or (None, None).

    Uses a valid-bit approach via the magic byte + non-default check.
    (0, 0) in the Gulf of Guinea is treated as "no cache" — acceptable
    for this hiking/asset-tracker use case.
    """
    try:
        m = machine.RTC().memory()
        if len(m) < 14:
            return None, None
        lat = i32_from_bytes(m[6:10]) / 1_000_000
        lon = i32_from_bytes(m[10:14]) / 1_000_000
        if lat == 0.0 and lon == 0.0:
            return None, None
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None, None
        return lat, lon
    except (ValueError, IndexError, TypeError):
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

    # GPS: read on skip==0; always fall back to cache if the live read fails
    lat = lon = None
    if gps_skip == 0:
        lat, lon = read_gps(timeout_ms=8000)
        if lat is not None:
            save_gps_cache(lat, lon)
        else:
            lat, lon = load_gps_cache()
    else:
        lat, lon = load_gps_cache()

    # Fire the burst if we have any position (live or cached)
    if lat is not None and lon is not None:
        fire_burst(lat, lon, BURST_PACKETS[phase])

    # Advance counters and deep sleep
    next_cycle_idx = (cycle_idx + 1) % 2
    next_gps_skip = (gps_skip + 1) % GPS_EVERY_N[phase]
    next_cycle_count = min(cycle_count + 1, 0xFFFF)
    write_rtc(next_cycle_idx, next_gps_skip, phase, 0, next_cycle_count)

    sleep_ms = SLEEP_PHASES[phase][cycle_idx]
    machine.deepsleep(sleep_ms)   # < 0.2mA until the timer fires and main.py restarts


run()
