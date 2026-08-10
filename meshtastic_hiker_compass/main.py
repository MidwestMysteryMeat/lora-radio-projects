"""
main.py -- Prototype handheld compass display (MicroPython)

This cannot run alongside stock Meshtastic on the same T-Echo: flashing
MicroPython replaces the Meshtastic firmware. The sketch is retained as
the display-side prototype for an architecture with a separate
MicroPython controller and a JSON serial bridge, or as logic to port into
a native Meshtastic module. It reads bridged position data, reads the
magnetometer for the device's own heading, computes bearing to the selected
target, and draws a compass needle on the OLED.

A physical toggle switch picks the target: the other handheld, or the
home/roof node.

Reconstructed from conversation history.

Requires these drivers on the device root (see README):
  - ssd1306.py   (OLED)
  - qmc5883l.py  (magnetometer)

IMPORTANT -- position feed: stock Meshtastic speaks framed protobuf on
serial, not JSON lines. parse_position_line() expects JSON (from a
bridge, or a build that emits text). Until that feed is wired, the
display stays on "Waiting for GPS fix...". See README.
"""

from machine import I2C, Pin, UART
import math
import time
import ssd1306
import qmc5883l

# ── Hardware setup ───────────────────────────────────────────────────────
# Reconstructed T-Echo pin concept. Do not use this on a Meshtastic-flashed
# T-Echo; select pins for the separate display controller (see HARDWARE.md).
i2c = I2C(0, sda=Pin(21), scl=Pin(22), freq=400000)
oled = ssd1306.SSD1306_I2C(128, 64, i2c, addr=0x3C)      # OLED at 0x3C
compass_chip = qmc5883l.QMC5883L(i2c)                     # magnetometer at 0x0D

toggle = Pin(5, Pin.IN, Pin.PULL_UP)   # HIGH = other handheld, LOW = home node
uart = UART(1, baudrate=115200)         # Meshtastic serial position feed

# ── Node IDs (set after first boot, read from the Meshtastic app) ────────
MY_NODE_ID    = "!aabbccdd"
OTHER_NODE_ID = "!11223344"
HOME_NODE_ID  = "!99887766"

# ── Position store (None = no fix yet; 0.0 is a valid coordinate) ───────
positions = {
    MY_NODE_ID:    {"lat": None, "lon": None},
    OTHER_NODE_ID: {"lat": None, "lon": None},
    HOME_NODE_ID:  {"lat": None, "lon": None},  # set roof coords or wait for mesh
}


# ── Math ─────────────────────────────────────────────────────────────────
def haversine(lat1, lon1, lat2, lon2):
    """Distance in miles between two GPS coordinates."""
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlon / 2) ** 2)
    a = min(1.0, max(0.0, a))
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
    """Read magnetometer and return heading in degrees (0 = North).

    Note: without hard/soft-iron calibration the heading will have
    several degrees of error. Field-calibrate before trusting the needle.
    """
    x, y, z = compass_chip.read()
    heading = math.degrees(math.atan2(y, x))
    return (heading + 360) % 360


# ── OLED drawing ─────────────────────────────────────────────────────────
def _draw_circle(cx, cy, r, color=1):
    """FrameBuffer.ellipse is the portable circle primitive."""
    oled.ellipse(cx, cy, r, r, color)


def draw_compass(relative_bearing, distance, target_label):
    """
    Draw compass needle on OLED.
    relative_bearing: degrees clockwise from up (0 = straight ahead), i.e.
    the target bearing already adjusted for the device's own heading so the
    needle points where to actually walk.
    """
    oled.fill(0)

    cx, cy, r = 32, 32, 28
    _draw_circle(cx, cy, r, 1)
    oled.pixel(cx, cy - r + 3, 1)      # north tick

    angle = math.radians(relative_bearing - 90)
    tip_x = int(cx + (r - 4) * math.cos(angle))
    tip_y = int(cy + (r - 4) * math.sin(angle))
    tail_x = int(cx - (r - 12) * math.cos(angle))
    tail_y = int(cy - (r - 12) * math.sin(angle))

    oled.line(tail_x, tail_y, tip_x, tip_y, 1)
    oled.fill_rect(tip_x - 2, tip_y - 2, 4, 4, 1)

    oled.text(target_label[:8], 68, 8, 1)
    oled.text("{:.1f}mi".format(distance), 68, 24, 1)
    oled.text("{:d}deg".format(int(relative_bearing)), 68, 40, 1)

    oled.show()


def draw_waiting():
    oled.fill(0)
    oled.text("Waiting for", 12, 20, 1)
    oled.text("GPS fix...", 20, 34, 1)
    oled.show()


# ── Meshtastic position parser ───────────────────────────────────────────
def _first_present(mapping, *keys):
    """Return the first value whose key exists and is not None.

    Important: do NOT use `a or b` — latitudeI can legitimately be 0
    (equator / prime meridian) and must not fall through.
    """
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return None


def _normalize_node_id(node):
    """Canonicalize Meshtastic node ids for comparison (with leading '!')."""
    if node is None:
        return None
    s = str(node).strip()
    if s.startswith('!'):
        return s.lower()
    # Numeric fromId is often a signed or unsigned 32-bit node num
    try:
        n = int(s)
        if n < 0:
            n = n & 0xFFFFFFFF
        return '!{:08x}'.format(n)
    except (ValueError, TypeError):
        pass
    return ('!' + s) if s else None


def parse_position_line(line):
    """
    Parse position updates from a JSON line feed.

    Meshtastic's native serial port speaks framed protobuf, not JSON.
    This parser is for a bridge (or custom build) that emits JSON lines
    with either scaled-integer fields (latitudeI/longitudeI, 1e-7 deg)
    or plain decimal-degree fields.
    """
    try:
        text = line.decode('utf-8', 'ignore') if isinstance(line, bytes) else line
        if not text or not text.strip():
            return None
        text = text.strip()
        # Cheap filter before loading JSON
        if 'lat' not in text.lower() and 'from' not in text.lower():
            return None

        try:
            import ujson as json
        except ImportError:
            import json

        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return None
        if not isinstance(obj, dict):
            return None

        node = _first_present(obj, 'fromId', 'from', 'sender')
        pos = obj
        decoded = obj.get('decoded')
        if isinstance(decoded, dict):
            inner = decoded.get('position')
            if isinstance(inner, dict):
                pos = inner
            else:
                pos = decoded

        lat = _first_present(pos, 'latitudeI', 'latitude', 'lat')
        lon = _first_present(pos, 'longitudeI', 'longitude', 'lon', 'lng')
        if node is None or lat is None or lon is None:
            return None

        try:
            lat = float(lat)
            lon = float(lon)
        except (TypeError, ValueError):
            return None

        # Scaled integers (latitudeI/longitudeI) are 1e-7 degrees
        if abs(lat) > 1000:
            lat = lat / 1e7
        if abs(lon) > 1000:
            lon = lon / 1e7

        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        return _normalize_node_id(node), lat, lon
    except Exception:
        return None


def _node_matches(known_id, received_id):
    """True if a received node id refers to the same node as known_id."""
    if received_id is None:
        return False
    a = _normalize_node_id(known_id)
    b = _normalize_node_id(received_id)
    if a is None or b is None:
        return False
    if a == b:
        return True
    # Also match without the '!' or on hex suffix
    return a.lstrip('!') == b.lstrip('!')


# ── Main loop ────────────────────────────────────────────────────────────
def main():
    while True:
        # Drain any position updates from the serial bridge
        while uart.any():
            line = uart.readline()
            if not line:
                break
            parsed = parse_position_line(line)
            if not parsed:
                continue
            node, lat, lon = parsed
            for key in positions:
                if _node_matches(key, node):
                    positions[key]['lat'] = lat
                    positions[key]['lon'] = lon
                    break

        # Which target is the toggle selecting?
        if toggle.value():   # HIGH = other handheld
            target_id, target_label = OTHER_NODE_ID, 'HIKER'
        else:                # LOW = home node
            target_id, target_label = HOME_NODE_ID, 'HOME'

        me = positions[MY_NODE_ID]
        target = positions[target_id]

        # `is not None` so coordinates of 0.0 remain valid
        if (me['lat'] is not None and me['lon'] is not None and
                target['lat'] is not None and target['lon'] is not None):
            true_bearing = bearing_to(me['lat'], me['lon'],
                                      target['lat'], target['lon'])
            distance = haversine(me['lat'], me['lon'],
                                 target['lat'], target['lon'])
            try:
                heading = get_device_heading()
            except Exception:
                heading = 0.0  # fall back to true-north needle
            relative = (true_bearing - heading + 360) % 360
            draw_compass(relative, distance, target_label)
        else:
            draw_waiting()

        time.sleep(0.5)


main()
