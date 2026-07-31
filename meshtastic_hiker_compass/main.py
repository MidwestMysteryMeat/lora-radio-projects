"""
main.py -- Handheld compass display (runs on the T-Echo, MicroPython)

Runs alongside Meshtastic firmware on the T-Echo. Meshtastic handles all
LoRa mesh networking, GPS position broadcasting, and routing; this sketch
just reads position data Meshtastic exposes over its local serial
interface, reads the on-board magnetometer for the device's own heading,
computes bearing to the selected target, and draws a compass needle on
the OLED.

A physical toggle switch picks the target: the other handheld, or the
home/roof node.

Reconstructed from conversation history.

Requires these drivers on the device root (see README):
  - ssd1306.py   (OLED)
  - qmc5883l.py  (magnetometer)
"""

from machine import I2C, Pin, UART
import math
import time
import ssd1306
import qmc5883l

# ── Hardware setup ───────────────────────────────────────────────────────
i2c = I2C(0, sda=Pin(21), scl=Pin(22), freq=400000)
oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)      # OLED at 0x3C
compass_chip = qmc5883l.QMC5883L(i2c)                     # magnetometer at 0x0D

toggle = Pin(5, Pin.IN, Pin.PULL_UP)   # HIGH = other handheld, LOW = home node
uart = UART(1, baudrate=115200)         # Meshtastic serial position feed

# ── Node IDs (set after first boot, read from the Meshtastic app) ────────
MY_NODE_ID    = "!aabbccdd"
OTHER_NODE_ID = "!11223344"
HOME_NODE_ID  = "!99887766"

# ── Position store ───────────────────────────────────────────────────────
positions = {
    MY_NODE_ID:    {"lat": 0.0, "lon": 0.0},
    OTHER_NODE_ID: {"lat": 0.0, "lon": 0.0},
    HOME_NODE_ID:  {"lat": 0.0, "lon": 0.0},   # set to your roof/home node coords
}


# ── Math ─────────────────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    """Distance in miles between two GPS coordinates."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def bearing_to(lat1, lon1, lat2, lon2):
    """True north bearing in degrees from point 1 to point 2."""
    dlon = math.radians(lon2 - lon1)
    lat1r = math.radians(lat1)
    lat2r = math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r) -
         math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def get_device_heading():
    """Read magnetometer and return heading in degrees (0 = North)."""
    x, y, z = compass_chip.read()
    heading = math.degrees(math.atan2(y, x))
    return (heading + 360) % 360


# ── OLED drawing ─────────────────────────────────────────────────────────
def draw_compass(relative_bearing, distance, target_label):
    """
    Draw compass needle on OLED.
    relative_bearing: degrees clockwise from up (0 = straight ahead), i.e.
    the target bearing already adjusted for the device's own heading so the
    needle points where to actually walk.
    """
    oled.fill(0)

    cx, cy, r = 32, 32, 28
    oled.circle(cx, cy, r, 1)          # outer ring
    oled.pixel(cx, cy - r + 3, 1)      # north tick

    angle = math.radians(relative_bearing - 90)
    tip_x = int(cx + (r - 4) * math.cos(angle))
    tip_y = int(cy + (r - 4) * math.sin(angle))
    tail_x = int(cx - (r - 12) * math.cos(angle))
    tail_y = int(cy - (r - 12) * math.sin(angle))

    oled.line(tail_x, tail_y, tip_x, tip_y, 1)      # needle
    oled.fill_rect(tip_x - 2, tip_y - 2, 4, 4, 1)   # arrowhead dot

    # Text panel (right side)
    oled.text(target_label, 68, 8, 1)
    oled.text(f"{distance:.1f}mi", 68, 24, 1)
    oled.text(f"{int(relative_bearing)}deg", 68, 40, 1)

    oled.show()


# ── Meshtastic position parser ───────────────────────────────────────────
def parse_position_line(line):
    """
    Parse position updates from Meshtastic serial output. Meshtastic emits
    position packets; extract node id, lat, lon.

    Meshtastic represents coordinates as scaled integers (latitudeI /
    longitudeI, in 1e-7 degrees) in most serial/JSON output. This handles
    both the scaled-integer fields and plain decimal-degree fields, since
    the exact format varies by Meshtastic build/config.
    """
    try:
        text = line.decode() if isinstance(line, bytes) else line
        if "from" not in text:
            return None
        import ujson
        try:
            obj = ujson.loads(text)
        except (ValueError, TypeError):
            return None

        node = obj.get("from") or obj.get("fromId")
        # Position may be nested under 'decoded'->'position' or flat
        pos = obj
        if "decoded" in obj and isinstance(obj["decoded"], dict):
            pos = obj["decoded"].get("position", obj["decoded"])

        lat = pos.get("latitudeI") or pos.get("latitude")
        lon = pos.get("longitudeI") or pos.get("longitude")
        if node is None or lat is None or lon is None:
            return None

        # Scaled integers (latitudeI/longitudeI) are 1e-7 degrees
        if abs(lat) > 1000:
            lat = lat / 1e7
        if abs(lon) > 1000:
            lon = lon / 1e7
        return str(node), lat, lon
    except Exception:
        return None


# ── Main loop ────────────────────────────────────────────────────────────
def main():
    while True:
        # Drain any position updates from Meshtastic
        if uart.any():
            line = uart.readline()
            parsed = parse_position_line(line)
            if parsed:
                node, lat, lon = parsed
                # Match against known node ids (Meshtastic ids may come in
                # different formats; store under whichever key matches)
                for key in positions:
                    if key.lstrip("!") in node or node in key:
                        positions[key]["lat"] = lat
                        positions[key]["lon"] = lon

        # Which target is the toggle selecting?
        if toggle.value():   # HIGH = other handheld
            target_id, target_label = OTHER_NODE_ID, "HIKER"
        else:                # LOW = home node
            target_id, target_label = HOME_NODE_ID, "HOME"

        me = positions[MY_NODE_ID]
        target = positions[target_id]

        if me["lat"] and me["lon"] and target["lat"] and target["lon"]:
            true_bearing = bearing_to(me["lat"], me["lon"], target["lat"], target["lon"])
            distance = haversine(me["lat"], me["lon"], target["lat"], target["lon"])
            # Subtract the device's own heading so the needle points to where
            # to actually walk, not just true-north bearing
            heading = get_device_heading()
            relative = (true_bearing - heading + 360) % 360
            draw_compass(relative, distance, target_label)
        else:
            oled.fill(0)
            oled.text("Waiting for", 12, 20, 1)
            oled.text("GPS fix...", 20, 34, 1)
            oled.show()

        time.sleep(0.5)


main()
