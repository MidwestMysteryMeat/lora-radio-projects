"""
tracker_ui.py -- Finder compass UI (runs on the Pi 4)

Displays a compass + signal interface on the 7" touchscreen. Listens on
BLE (close range, ~100ft) and Meshtastic/LoRa (long range) simultaneously
via background threads, and always uses the most recently received tag
position regardless of which radio delivered it. Inside BLE range the
display switches to live BLE data for precise close-range direction finding.

NOTE -- radio-path status (see README "Radio-path status"): the BLE
close-range path is the working one. The Meshtastic/LoRa listener below
is NOT functional against the current tag firmware: the tag transmits
raw SX1276 frames, which the Meshtastic stack never decodes into the
position packets this thread subscribes to (and '!TAG1' is not a real
Meshtastic node id). The thread is kept as scaffolding for a future
Meshtastic-based tag. parse_lora_payload() is provided for a raw SX1262
receiver path if you add one later.

Deps:
    pip3 install pygame gpsd-py3 bleak meshtastic

Hardware:
    - Pi 4 + Waveshare SX1262 LoRa HAT (Meshtastic pre-flashed) on /dev/ttyS0
    - USB GPS dongle via gpsd (/dev/ttyUSB0)
    - USB BLE dongle (or onboard BLE)
    - Official 7" DSI touchscreen (800x480)
"""

import math
import time
import threading
import asyncio

import pygame

from bleak import BleakScanner

try:
    from gpsd import connect as gps_connect, get_current
    HAS_GPSD = True
except ImportError:
    HAS_GPSD = False
    print("[tracker_ui] gpsd-py3 not available -- finder GPS position disabled.")

try:
    import meshtastic
    import meshtastic.serial_interface
    HAS_MESHTASTIC = True
except ImportError:
    HAS_MESHTASTIC = False
    print("[tracker_ui] meshtastic library not available -- LoRa receive disabled.")

# ── Shared state (updated by background threads under _state_lock) ───────
_state_lock = threading.Lock()
state = {
    'tag_lat':    None,
    'tag_lon':    None,
    'tag_rssi':   None,        # None = not currently in BLE range
    'radio_mode': 'SEARCHING', # 'BLE' | 'LORA' | 'SEARCHING'
    'last_ble_seen':  0.0,     # epoch of most recent valid BLE frame
    'last_lora_seen': 0.0,     # epoch of most recent valid LoRa position
    'my_lat':     None,
    'my_lon':     None,
    'last_burst': 0.0,         # epoch of last received burst
    'next_burst': 0.0,         # predicted next burst epoch
    'burst_cycle': 0,          # 0 = expecting 1min, 1 = expecting 3min
}
BURST_PATTERN = [60, 180]      # must match the tag firmware phase-1 sleep
DEVICE_ID = b'TAG1'
MAX_BURST_INDEX = 8
BLE_RANGE_TIMEOUT_S = 8        # if no BLE packet in this long, drop out of BLE mode
LORA_STALE_S = 300             # after this, LoRa is considered stale
BURST_DEDUP_S = 8              # one SOS burst contains several advertisements
PRIVATE_COMPANY_ID = 0xFFFF


def _snapshot_state():
    """Copy state under the lock for the render thread."""
    with _state_lock:
        return dict(state)


def _is_recent(timestamp, now, timeout):
    """True when timestamp is nonzero and no older than timeout seconds."""
    return bool(timestamp) and 0 <= (now - timestamp) <= timeout


def radio_mode_for_timestamps(now, last_ble_seen, last_lora_seen):
    """Choose the freshest usable radio, preferring short-range BLE."""
    if _is_recent(last_ble_seen, now, BLE_RANGE_TIMEOUT_S):
        return 'BLE'
    if _is_recent(last_lora_seen, now, LORA_STALE_S):
        return 'LORA'
    return 'SEARCHING'


def should_count_burst(last_burst, now):
    """Deduplicate the multiple BLE/LoRa observations in one SOS burst."""
    return not _is_recent(last_burst, now, BURST_DEDUP_S)


def note_burst_received(source='BLE', received_at=None):
    """Record burst timing and predict the next one.

    Tag phase-1 alternates 1 min sleep -> burst -> 3 min sleep -> burst.
    Prediction is approximate if the tag is still in phase 0 or 2.
    """
    now = time.time() if received_at is None else received_at
    with _state_lock:
        if source == 'BLE':
            state['radio_mode'] = 'BLE'
        elif not _is_recent(
                state['last_ble_seen'], now, BLE_RANGE_TIMEOUT_S):
            state['radio_mode'] = 'LORA'

        # The tag advertises several packets per SOS burst and the finder can
        # hear the same burst over both radios. Advance the alternating sleep
        # prediction only once for the whole burst.
        if not should_count_burst(state['last_burst'], now):
            return False
        state['last_burst'] = now
        cycle = state['burst_cycle']
        state['next_burst'] = now + BURST_PATTERN[cycle]
        state['burst_cycle'] = (cycle + 1) % 2
        return True


# ── Compass math ─────────────────────────────────────────────────────────
def bearing_to(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = (math.cos(lat1r) * math.sin(lat2r) -
         math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon))
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8  # Earth radius in miles
    d1 = math.radians(lat2 - lat1)
    d2 = math.radians(lon2 - lon1)
    a = (math.sin(d1 / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(d2 / 2) ** 2)
    a = min(1.0, max(0.0, a))
    return 2 * R * math.asin(math.sqrt(a))


def i32_from_bytes(raw):
    """Decode 4 bytes big-endian two's complement as a signed int."""
    if raw is None or len(raw) < 4:
        raise ValueError('need 4 bytes')
    value = int.from_bytes(raw[:4], 'big')
    return value - 0x100000000 if value & 0x80000000 else value


# ── Payload parsers ──────────────────────────────────────────────────────
def parse_tag_payload(raw: bytes):
    """Extract (lat, lon, burst_index) from one exact tag frame.

    Mirrors make_payload() in the tag firmware: the payload contains the
    TAG1 marker followed by lat(4) + lon(4) big-endian SIGNED
    (two's-complement) fixed-point, degrees * 1e6. Sign extension here
    must match the tag's i32_to_bytes() -- US longitudes are negative.

    ``raw`` may be either the full manufacturer body (including 0xFFFF
    company id prefix) or the post-company-id remainder from bleak. Exact
    lengths and marker positions prevent unrelated manufacturer data that
    contains the bytes ``TAG1`` from being interpreted as a location.
    """
    try:
        raw = bytes(raw)
        if len(raw) == 15 and raw[:2] == b'\xFF\xFF':
            raw = raw[2:]
        if (len(raw) != 13 or raw[:4] != DEVICE_ID or
                raw[12] > MAX_BURST_INDEX):
            return None
        lat = i32_from_bytes(raw[4:8]) / 1_000_000
        lon = i32_from_bytes(raw[8:12]) / 1_000_000
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return None
        return lat, lon, raw[12]
    except (ValueError, IndexError, TypeError):
        return None


def parse_ble_payload(raw: bytes):
    """Extract the backward-compatible ``(lat, lon)`` pair from a tag frame."""
    parsed = parse_tag_payload(raw)
    if parsed is None:
        return None, None
    return parsed[0], parsed[1]


def parse_lora_payload(raw: bytes):
    """Decode a raw SX1276 frame from the tag (same layout as BLE body).

    Use this if you add a raw LoRa receiver on the finder. The Meshtastic
    path cannot decode these frames.
    """
    return parse_ble_payload(raw)


# ── BLE scanner thread ───────────────────────────────────────────────────
async def ble_scan_loop():
    def on_detect(device, adv):
        raw = getattr(adv, 'manufacturer_data', None) or {}
        if not raw:
            return
        for company_id, payload in raw.items():
            if company_id != PRIVATE_COMPANY_ID:
                continue
            # bleak may hand back bytes or bytearray
            parsed = parse_tag_payload(payload)
            if parsed is None:
                continue
            lat, lon, _burst_index = parsed
            rssi = getattr(adv, 'rssi', None)
            now = time.time()
            with _state_lock:
                state['tag_lat'] = lat
                state['tag_lon'] = lon
                state['tag_rssi'] = rssi
                state['last_ble_seen'] = now
                state['radio_mode'] = 'BLE'
            note_burst_received(source='BLE', received_at=now)
            break

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1)
            # Recompute mode from independent source timestamps. In particular,
            # a LoRa update must not keep stale BLE RSSI alive, and a lone LoRa
            # fix must eventually age back to SEARCHING.
            now = time.time()
            with _state_lock:
                mode = radio_mode_for_timestamps(
                    now, state['last_ble_seen'], state['last_lora_seen'],
                )
                state['radio_mode'] = mode
                if mode != 'BLE':
                    state['tag_rssi'] = None
    finally:
        await scanner.stop()


# ── Meshtastic / LoRa thread (scaffolding — see radio-path status) ───────
def start_meshtastic():
    if not HAS_MESHTASTIC:
        return
    try:
        from pubsub import pub
    except ImportError:
        print("[tracker_ui] pubsub not available -- LoRa receive disabled.")
        return

    def on_receive(packet, interface):  # noqa: ARG001
        try:
            decoded = packet.get('decoded') or {}
            pos = decoded.get('position')
            if not isinstance(pos, dict):
                return
            from_id = str(packet.get('fromId', '') or '')
            # Real Meshtastic node ids look like '!aabbccdd'. '!TAG1' will
            # never match a live node — keep the check for a future tag
            # that actually publishes position through Meshtastic.
            if from_id != '!TAG1' and 'TAG1' not in str(packet):
                # Still accept if caller configured a real node filter later
                return
            lat = pos.get('latitude')
            lon = pos.get('longitude')
            if lat is None or lon is None:
                # Some builds use scaled integers
                lat_i = pos.get('latitudeI')
                lon_i = pos.get('longitudeI')
                if lat_i is None or lon_i is None:
                    return
                lat = lat_i / 1e7
                lon = lon_i / 1e7
            lat = float(lat)
            lon = float(lon)
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
                return
            with _state_lock:
                state['tag_lat'] = lat
                state['tag_lon'] = lon
                state['last_lora_seen'] = time.time()
            note_burst_received(source='LORA')
        except Exception:
            pass

    try:
        # Keep a reference so the interface is not GC'd
        iface = meshtastic.serial_interface.SerialInterface('/dev/ttyS0')
        pub.subscribe(on_receive, 'meshtastic.receive.position')
        while True:
            time.sleep(1)
        # silence unused warning in some linters
        _ = iface
    except Exception as e:
        print(f"[tracker_ui] Meshtastic thread error: {e}")


# ── Pygame compass display ───────────────────────────────────────────────
def draw_display(screen, bearing, distance_mi, mode, rssi, next_burst_secs):
    W, H = screen.get_size()
    screen.fill((15, 23, 42))   # dark navy

    # Radio mode badge
    badge_colors = {
        'BLE': (37, 99, 235),
        'LORA': (13, 148, 136),
        'SEARCHING': (107, 114, 128),
    }
    color = badge_colors.get(mode, (107, 114, 128))
    pygame.draw.rect(screen, color, (20, 20, 160, 44), border_radius=8)
    font_sm = pygame.font.SysFont('Arial', 22, bold=True)
    screen.blit(font_sm.render(mode, True, (255, 255, 255)), (30, 30))

    # Compass circle
    cx, cy, r = W // 2, H // 2, 170
    pygame.draw.circle(screen, (30, 41, 59), (cx, cy), r)
    pygame.draw.circle(screen, (71, 85, 105), (cx, cy), r, 3)

    # Cardinal labels
    font_cd = pygame.font.SysFont('Arial', 24, bold=True)
    for deg, lbl in [(0, 'N'), (90, 'E'), (180, 'S'), (270, 'W')]:
        rad = math.radians(deg - 90)
        lx = cx + int((r + 26) * math.cos(rad))
        ly = cy + int((r + 26) * math.sin(rad))
        s = font_cd.render(lbl, True, (148, 163, 184))
        screen.blit(s, (lx - s.get_width() // 2, ly - s.get_height() // 2))

    # Bearing arrow
    if bearing is not None:
        rad = math.radians(bearing - 90)
        tip = (cx + int((r - 18) * math.cos(rad)),
               cy + int((r - 18) * math.sin(rad)))
        tail = (cx - int(55 * math.cos(rad)),
                cy - int(55 * math.sin(rad)))
        pygame.draw.line(screen, (59, 130, 246), tail, tip, 7)
        pygame.draw.circle(screen, (59, 130, 246), tip, 11)
        pygame.draw.circle(screen, (15, 23, 42), tip, 5)

    # Distance readout (feet under a mile, else miles)
    font_dist = pygame.font.SysFont('Arial', 38, bold=True)
    if distance_mi is not None:
        if distance_mi < 0.19:  # under ~1000 ft, show feet
            label = f'{int(distance_mi * 5280)} ft'
        else:
            label = f'{distance_mi:.2f} mi'
        s = font_dist.render(label, True, (226, 232, 240))
        screen.blit(s, (cx - s.get_width() // 2, cy + r + 40))
    else:
        s = font_dist.render('-- no fix --', True, (107, 114, 128))
        screen.blit(s, (cx - s.get_width() // 2, cy + r + 40))

    # RSSI (BLE close-range signal strength)
    if rssi is not None:
        font_r = pygame.font.SysFont('Arial', 20, bold=True)
        s = font_r.render(f'RSSI {rssi} dBm', True, (147, 197, 253))
        screen.blit(s, (W - s.get_width() - 20, 30))

    # Next-burst countdown
    font_cd2 = pygame.font.SysFont('Arial', 20)
    if next_burst_secs > 0:
        s = font_cd2.render(
            f'Next burst in {int(next_burst_secs)}s', True, (148, 163, 184)
        )
        screen.blit(s, (20, H - 40))

    pygame.display.flip()


# ── Main loop ────────────────────────────────────────────────────────────
def main():
    pygame.init()
    screen = pygame.display.set_mode((800, 480))
    pygame.display.set_caption('Tri-Radio Tracker')

    if HAS_GPSD:
        try:
            gps_connect()
        except Exception as e:
            print(f'[tracker_ui] gpsd connect failed: {e}')

    threading.Thread(target=start_meshtastic, daemon=True, name='meshtastic').start()
    threading.Thread(
        target=lambda: asyncio.run(ble_scan_loop()),
        daemon=True,
        name='ble-scan',
    ).start()

    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                pygame.quit()
                return
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                pygame.quit()
                return

        if HAS_GPSD:
            try:
                fix = get_current()
                # Prefer mode >= 2 (2D/3D fix) when the attribute exists
                mode = getattr(fix, 'mode', 3)
                lat = getattr(fix, 'lat', None)
                lon = getattr(fix, 'lon', None)
                if mode >= 2 and lat is not None and lon is not None:
                    with _state_lock:
                        state['my_lat'] = float(lat)
                        state['my_lon'] = float(lon)
            except Exception:
                pass

        snap = _snapshot_state()
        bearing = distance = None
        # `is not None` so equator / prime meridian (0.0) stay valid
        if (snap['my_lat'] is not None and snap['my_lon'] is not None and
                snap['tag_lat'] is not None and snap['tag_lon'] is not None):
            bearing = bearing_to(
                snap['my_lat'], snap['my_lon'],
                snap['tag_lat'], snap['tag_lon'],
            )
            distance = haversine_miles(
                snap['my_lat'], snap['my_lon'],
                snap['tag_lat'], snap['tag_lon'],
            )

        next_burst_secs = max(0.0, snap['next_burst'] - time.time())
        draw_display(
            screen, bearing, distance,
            snap['radio_mode'], snap['tag_rssi'], next_burst_secs,
        )
        clock.tick(10)   # 10fps is plenty for a compass


if __name__ == '__main__':
    main()
