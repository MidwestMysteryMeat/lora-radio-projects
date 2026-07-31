"""
main.py -- Two-device LoRa GPS locator (Raspberry Pi Pico, MicroPython)

Same firmware runs on both devices. Each one:
  1. reads its own GPS coordinates
  2. broadcasts them over LoRa (direct SX1276/RFM95W register writes)
  3. listens for the other device's broadcast
  4. shows the other device's distance + bearing on the OLED

No server, no WiFi, no phone, no infrastructure -- fully self-contained.
Range is roughly 2-5km open field, 500m-2km in forest.

Reconstructed from conversation history. This is the bare-metal LoRa
version (no Meshtastic dependency) -- it drives the radio directly over
SPI. Verify pin assignments and the 915MHz register values against your
specific module before relying on it.

Hardware per device (~$35-40):
  - Raspberry Pi Pico 2 WH
  - GY-NEO6MV2 GPS (UART)
  - RA-02 / SX1276 / RFM95W 915MHz LoRa module (SPI)
  - SSD1306 OLED (I2C) for the standalone distance/arrow display
  - 3.7V LiPo + TP4056 charger

Antenna: the stock stub works; a wire cut to 82.2mm (quarter-wave at
915MHz) soldered to the antenna pad noticeably improves range.
"""

import time
import math
from machine import UART, SPI, Pin, I2C

import ssd1306   # OLED driver -- copy ssd1306.py to device root

# ── This device's ID -- set differently on each of the two devices ───────
MY_ID = b'DEV1'      # set the other unit to b'DEV2'
OTHER_ID = b'DEV2'   # set the other unit to b'DEV1'

# ── GPS on UART1 (TX=GP4, RX=GP5) ────────────────────────────────────────
gps = UART(1, baudrate=9600, tx=Pin(4), rx=Pin(5))

# ── OLED on I2C0 (SDA=GP0, SCL=GP1) ──────────────────────────────────────
i2c = I2C(0, sda=Pin(0), scl=Pin(1), freq=400000)
oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)

# ── SX1276 / RFM95W on SPI0 ──────────────────────────────────────────────
spi = SPI(0, baudrate=5_000_000, sck=Pin(18), mosi=Pin(19), miso=Pin(16))
cs = Pin(17, Pin.OUT, value=1)
rst = Pin(20, Pin.OUT, value=1)

# ── SX1276 registers ─────────────────────────────────────────────────────
REG_FIFO        = 0x00
REG_OP_MODE     = 0x01
REG_PA_CONFIG   = 0x09
REG_FIFO_ADDR   = 0x0D
REG_TX_BASE     = 0x0E
REG_RX_BASE     = 0x0F
REG_RX_CURRENT  = 0x10
REG_IRQ_FLAGS   = 0x12
REG_RX_NB_BYTES = 0x13
REG_PAYLOAD_LEN = 0x22
MODE_LORA_SLEEP = 0x80
MODE_LORA_STDBY = 0x81
MODE_LORA_TX    = 0x83
MODE_LORA_RXCON = 0x85   # continuous receive


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
    sx_write(REG_OP_MODE, MODE_LORA_SLEEP)   # sleep mode (required to set LoRa bit)
    time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)   # LoRa standby
    sx_write(0x06, 0xE4)                      # 915MHz freq high byte
    sx_write(0x07, 0xC0)                      # 915MHz freq mid byte
    sx_write(0x08, 0x00)                      # 915MHz freq low byte
    sx_write(REG_PA_CONFIG, 0x8F)            # PA_BOOST, max power
    sx_write(0x1D, 0x72)                      # BW 125kHz, CR 4/5, explicit header
    sx_write(0x1E, 0x74)                      # SF7, CRC on
    sx_write(0x26, 0x04)                      # low data rate optimize off
    sx_write(REG_FIFO_ADDR, sx_read(REG_RX_BASE))


def lora_send(payload):
    lora_init()
    sx_write(REG_FIFO_ADDR, sx_read(REG_TX_BASE))   # reset FIFO ptr to TX base
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


def lora_receive(timeout_ms=2000):
    """Listen for one LoRa packet. Returns the payload bytes or None."""
    sx_write(REG_FIFO_ADDR, sx_read(REG_RX_BASE))
    sx_write(REG_OP_MODE, MODE_LORA_RXCON)          # continuous RX
    deadline = time.ticks_ms() + timeout_ms
    try:
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            flags = sx_read(REG_IRQ_FLAGS)
            if flags & 0x40:                        # RxDone flag
                sx_write(REG_IRQ_FLAGS, 0xFF)
                if flags & 0x20:                    # CRC error -- discard
                    return None
                n = sx_read(REG_RX_NB_BYTES)
                sx_write(REG_FIFO_ADDR, sx_read(REG_RX_CURRENT))
                data = bytes(sx_read(REG_FIFO) for _ in range(n))
                return data
            time.sleep_ms(10)
        return None
    finally:
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


# ── Payload ──────────────────────────────────────────────────────────────
def make_payload(device_id, lat, lon):
    lat_int = int(lat * 1_000_000)
    lon_int = int(lon * 1_000_000)
    data = bytearray(device_id)                     # 4-byte device id
    data += lat_int.to_bytes(4, 'big', True)
    data += lon_int.to_bytes(4, 'big', True)
    return bytes(data)


def parse_payload(data):
    """Returns (device_id, lat, lon) or None."""
    if data is None or len(data) < 12:
        return None
    device_id = data[0:4]
    lat = int.from_bytes(data[4:8], 'big', True) / 1_000_000
    lon = int.from_bytes(data[8:12], 'big', True) / 1_000_000
    return device_id, lat, lon


# ── Math ─────────────────────────────────────────────────────────────────
def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_to(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ── OLED ─────────────────────────────────────────────────────────────────
def draw_arrow(bearing, distance_mi):
    oled.fill(0)
    cx, cy, r = 32, 32, 28
    oled.circle(cx, cy, r, 1)
    oled.pixel(cx, cy - r + 3, 1)   # north tick

    if bearing is not None:
        angle = math.radians(bearing - 90)
        tip_x = int(cx + (r - 4) * math.cos(angle))
        tip_y = int(cy + (r - 4) * math.sin(angle))
        tail_x = int(cx - (r - 12) * math.cos(angle))
        tail_y = int(cy - (r - 12) * math.sin(angle))
        oled.line(tail_x, tail_y, tip_x, tip_y, 1)
        oled.fill_rect(tip_x - 2, tip_y - 2, 4, 4, 1)

    oled.text("OTHER", 68, 8, 1)
    if distance_mi is not None:
        oled.text(f"{distance_mi:.1f}mi", 68, 24, 1)
        if bearing is not None:
            oled.text(f"{int(bearing)}deg", 68, 40, 1)
    else:
        oled.text("no fix", 68, 24, 1)
    oled.show()


# ── Main loop ────────────────────────────────────────────────────────────
def main():
    lora_init()
    other_lat = other_lon = None

    while True:
        my_lat, my_lon = read_gps(timeout_ms=8000)

        if my_lat is not None:
            lora_send(make_payload(MY_ID, my_lat, my_lon))

        # Listen for the other device
        packet = lora_receive(timeout_ms=2000)
        parsed = parse_payload(packet)
        if parsed:
            device_id, lat, lon = parsed
            if device_id == OTHER_ID:
                other_lat, other_lon = lat, lon

        bearing = distance = None
        if None not in (my_lat, my_lon, other_lat, other_lon):
            bearing = bearing_to(my_lat, my_lon, other_lat, other_lon)
            distance = haversine_miles(my_lat, my_lon, other_lat, other_lon)

        draw_arrow(bearing, distance)
        time.sleep(2)


main()
