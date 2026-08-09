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
import urandom
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
# Pico SPI0 defaults: SCK=GP18, MOSI=GP19, MISO=GP16
spi = SPI(0, baudrate=5_000_000, polarity=0, phase=0,
          sck=Pin(18), mosi=Pin(19), miso=Pin(16))
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
REG_PKT_RSSI    = 0x1A
REG_PAYLOAD_LEN = 0x22
REG_IRQ_FLAGS_MASK = 0x11
MODE_LORA_SLEEP = 0x80
MODE_LORA_STDBY = 0x81
MODE_LORA_TX    = 0x83
MODE_LORA_RXCON = 0x85   # continuous receive

# IRQ flag bits
IRQ_TX_DONE     = 0x08
IRQ_PAYLOAD_CRC = 0x20
IRQ_RX_DONE     = 0x40


def sx_write(reg, val):
    """Write one register (MSB of address set = write)."""
    cs(0)
    spi.write(bytes([(reg & 0x7F) | 0x80, val & 0xFF]))
    cs(1)


def sx_read(reg):
    """Read one register via a full-duplex SPI transaction."""
    buf = bytearray([reg & 0x7F, 0x00])
    cs(0)
    spi.write_readinto(buf, buf)
    cs(1)
    return buf[1]


def lora_init():
    """Reset and configure the SX1276 for 915 MHz LoRa, SF7, BW 125 kHz."""
    rst(0)
    time.sleep_ms(10)
    rst(1)
    time.sleep_ms(10)

    # LongRangeMode can only be changed in Sleep
    sx_write(REG_OP_MODE, MODE_LORA_SLEEP)
    time.sleep_ms(10)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)

    # Frf = 915e6 / (32e6 / 2^19) = 0xE4C000
    sx_write(0x06, 0xE4)                      # freq MSB
    sx_write(0x07, 0xC0)                      # freq MID
    sx_write(0x08, 0x00)                      # freq LSB
    sx_write(REG_PA_CONFIG, 0x8F)             # PA_BOOST, max power
    sx_write(0x1D, 0x72)                      # BW 125kHz, CR 4/5, explicit header
    sx_write(0x1E, 0x74)                      # SF7, CRC on
    sx_write(0x26, 0x04)                      # LowDataRateOptimize off
    sx_write(REG_IRQ_FLAGS, 0xFF)             # clear sticky IRQs
    sx_write(REG_FIFO_ADDR, sx_read(REG_RX_BASE))


def lora_send(payload):
    """Transmit a LoRa packet. Payload max 255 bytes."""
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
        if sx_read(REG_IRQ_FLAGS) & IRQ_TX_DONE:
            break
        time.sleep_ms(5)

    sx_write(REG_IRQ_FLAGS, 0xFF)
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)


def lora_receive(timeout_ms=2000):
    """Listen for one LoRa packet. Returns payload bytes or None.

    CRC errors are discarded and listening continues until timeout so a
    single corrupted frame does not abort the whole RX window.
    """
    sx_write(REG_OP_MODE, MODE_LORA_STDBY)
    sx_write(REG_IRQ_FLAGS, 0xFF)
    sx_write(REG_FIFO_ADDR, sx_read(REG_RX_BASE))
    sx_write(REG_OP_MODE, MODE_LORA_RXCON)

    deadline = time.ticks_ms() + timeout_ms
    try:
        while time.ticks_diff(deadline, time.ticks_ms()) > 0:
            flags = sx_read(REG_IRQ_FLAGS)
            if flags & IRQ_RX_DONE:
                sx_write(REG_IRQ_FLAGS, 0xFF)
                if flags & IRQ_PAYLOAD_CRC:
                    # Bad CRC -- keep listening for a clean packet
                    continue
                n = sx_read(REG_RX_NB_BYTES)
                if n == 0 or n > 255:
                    continue
                sx_write(REG_FIFO_ADDR, sx_read(REG_RX_CURRENT))
                return bytes(sx_read(REG_FIFO) for _ in range(n))
            time.sleep_ms(5)
        return None
    finally:
        sx_write(REG_OP_MODE, MODE_LORA_STDBY)
        sx_write(REG_IRQ_FLAGS, 0xFF)


# ── GPS ──────────────────────────────────────────────────────────────────
def parse_rmc_sentence(line):
    """Parse a GPRMC/GNRMC (any talker) NMEA sentence into (lat, lon).

    Accepts bytes or str. Returns (None, None) on invalid/no-fix.
    Pure function so host unit tests can exercise it without UART hardware.
    """
    try:
        if isinstance(line, bytes):
            text = line.decode('ascii', 'ignore')
        else:
            text = str(line)
        text = text.strip()
        # Drop checksum tail if present
        if '*' in text:
            text = text.split('*', 1)[0]
        # Find $xxRMC (GP/GN/GL/GA and other talkers all end in RMC)
        start = text.find('$')
        if start < 0:
            return None, None
        text = text[start:]
        parts = text.split(',')
        if len(parts) < 7 or 'RMC' not in parts[0]:
            return None, None
        if parts[2] != 'A':                     # A = valid fix
            return None, None
        rlat = float(parts[3])
        rlon = float(parts[5])
        # NMEA: DDMM.MMMM / DDDMM.MMMM
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
    # Drain stale buffer so we prefer a fresh fix
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


# ── Payload ──────────────────────────────────────────────────────────────
# MicroPython's int.to_bytes()/int.from_bytes() take no 'signed' argument,
# so negative values (western longitudes, southern latitudes) are handled
# manually as two's complement. Both devices run this same file, so the
# encoding and decoding stay in agreement.
def i32_to_bytes(value):
    """Encode a signed int as 4 bytes big-endian two's complement."""
    return (value & 0xFFFFFFFF).to_bytes(4, 'big')


def i32_from_bytes(raw):
    """Decode 4 bytes big-endian two's complement as a signed int."""
    if raw is None or len(raw) < 4:
        raise ValueError('need 4 bytes')
    value = int.from_bytes(raw[:4], 'big')
    return value - 0x100000000 if value & 0x80000000 else value


def make_payload(device_id, lat, lon):
    """Build a 12-byte location packet: id(4) + lat_i32 + lon_i32."""
    if not isinstance(device_id, (bytes, bytearray)) or len(device_id) != 4:
        raise ValueError('device_id must be 4 bytes')
    lat_int = int(round(lat * 1_000_000))
    lon_int = int(round(lon * 1_000_000))
    data = bytearray(device_id)
    data += i32_to_bytes(lat_int)
    data += i32_to_bytes(lon_int)
    return bytes(data)


def parse_payload(data):
    """Returns (device_id, lat, lon) or None."""
    if data is None or len(data) < 12:
        return None
    device_id = bytes(data[0:4])
    try:
        lat = i32_from_bytes(data[4:8]) / 1_000_000
        lon = i32_from_bytes(data[8:12]) / 1_000_000
    except ValueError:
        return None
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    return device_id, lat, lon


# ── Math ─────────────────────────────────────────────────────────────────
def haversine_miles(lat1, lon1, lat2, lon2):
    """Great-circle distance in miles."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    # Clamp for floating-point edge cases at antipodes
    a = min(1.0, max(0.0, a))
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_to(lat1, lon1, lat2, lon2):
    """Initial true-north bearing in degrees from point 1 to point 2."""
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r) -
         math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


# ── OLED ─────────────────────────────────────────────────────────────────
def _draw_circle(cx, cy, r, color=1):
    """Draw a circle using FrameBuffer.ellipse (standard MicroPython)."""
    # micropython-lib ssd1306 subclasses FrameBuffer, which has ellipse()
    # (not circle) since MicroPython 1.20.
    oled.ellipse(cx, cy, r, r, color)


def draw_arrow(bearing, distance_mi):
    oled.fill(0)
    cx, cy, r = 32, 32, 28
    _draw_circle(cx, cy, r, 1)
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
        oled.text("{:.1f}mi".format(distance_mi), 68, 24, 1)
        if bearing is not None:
            oled.text("{:d}deg".format(int(bearing)), 68, 40, 1)
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
            # Random TX jitter so two units that wake together are less
            # likely to collide on the same channel.
            time.sleep_ms(urandom.getrandbits(8) * 4)  # 0–1020 ms
            try:
                lora_send(make_payload(MY_ID, my_lat, my_lon))
            except Exception:
                lora_init()  # recover radio if SPI glitched

        # Listen for the other device (window covers some of their jitter)
        packet = lora_receive(timeout_ms=2500)
        parsed = parse_payload(packet)
        if parsed is not None:
            device_id, lat, lon = parsed
            if device_id == OTHER_ID:
                other_lat, other_lon = lat, lon

        bearing = distance = None
        # Use `is not None` so 0.0 (equator / prime meridian) is valid
        if (my_lat is not None and my_lon is not None and
                other_lat is not None and other_lon is not None):
            bearing = bearing_to(my_lat, my_lon, other_lat, other_lon)
            distance = haversine_miles(my_lat, my_lon, other_lat, other_lon)

        draw_arrow(bearing, distance)
        time.sleep(2)


main()
