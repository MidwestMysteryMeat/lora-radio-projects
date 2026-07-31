"""
tracker_ui.py -- Finder compass UI (runs on the Pi 4)

Displays a compass + signal interface on the 7" touchscreen. Listens on
BLE (close range, ~100ft) and Meshtastic/LoRa (long range) simultaneously
via background threads, and always uses the most recently received tag
position regardless of which radio delivered it. Inside BLE range the
display switches to live BLE data for precise close-range direction finding.

Reconstructed from conversation history.

Deps:
    pip3 install pygame gpsd-py3 bleak meshtastic

Hardware:
    - Pi 4 + Waveshare SX1262 LoRa HAT (Meshtastic pre-flashed) on /dev/ttyS0
    - USB GPS dongle via gpsd (/dev/ttyUSB0)
    - USB BLE dongle (or onboard BLE)
    - Official 7" DSI touchscreen (800x480)
"""

import pygame
import math
import time
import threading
import asyncio

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

# ── Shared state (updated by background threads) ─────────────────────────
state = {
    'tag_lat':    None,
    'tag_lon':    None,
    'tag_rssi':   None,        # None = not currently in BLE range
    'radio_mode': 'SEARCHING', # 'BLE' | 'LORA' | 'SEARCHING'
    'lora_age':   999,         # seconds since last LoRa packet
    'my_lat':     None,
    'my_lon':     None,
    'last_burst': 0,           # epoch of last received burst
    'next_burst': 0,           # predicted next burst epoch
    'burst_cycle': 0,          # 0 = expecting 1min, 1 = expecting 3min
}
BURST_PATTERN = [60, 180]      # must match the tag firmware phase-1 sleep
DEVICE_ID = b'TAG1'
BLE_RANGE_TIMEOUT_S = 8        # if no BLE packet in this long, drop out of BLE mode


def note_burst_received():
    """Record burst timing and predict the next one (tag alternates
    1min sleep -> burst -> 3min sleep -> burst -> repeat)."""
    now = time.time()
    state['last_burst'] = now
    cycle = state['burst_cycle']
    state['next_burst'] = now + BURST_PATTERN[cycle]
    state['burst_cycle'] = (cycle + 1) % 2


# ── Compass math ─────────────────────────────────────────────────────────
def bearing_to(lat1, lon1, lat2, lon2):
    dlon = math.radians(lon2 - lon1)
    lat1r, lat2r = math.radians(lat1), math.radians(lat2)
    x = math.sin(dlon) * math.cos(lat2r)
    y = math.cos(lat1r) * math.sin(lat2r) - math.sin(lat1r) * math.cos(lat2r) * math.cos(dlon)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def haversine_miles(lat1, lon1, lat2, lon2):
    R = 3958.8  # Earth radius in miles
    d1 = math.radians(lat2 - lat1)
    d2 = math.radians(lon2 - lon1)
    a = math.sin(d1 / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(d2 / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ── BLE scanner thread ───────────────────────────────────────────────────
def parse_ble_payload(raw: bytes):
    """Extract (lat, lon) from a tag's manufacturer-data payload.
    Mirrors make_ble_payload() in the tag firmware: the payload contains
    the TAG1 marker followed by lat(4) + lon(4) big-endian fixed-point."""
    try:
        if b'TAG1' not in raw:
            return None, None
        idx = raw.index(b'TAG1') + 4
        lat = int.from_bytes(raw[idx:idx + 4], 'big') / 1_000_000
        lon = int.from_bytes(raw[idx + 4:idx + 8], 'big') / 1_000_000
        return lat, lon
    except Exception:
        return None, None


async def ble_scan_loop():
    def on_detect(device, adv):
        raw = adv.manufacturer_data
        if not raw:
            return
        for company_id, payload in raw.items():
            lat, lon = parse_ble_payload(payload)
            if lat is not None:
                state['tag_lat'] = lat
                state['tag_lon'] = lon
                state['tag_rssi'] = adv.rssi
                state['radio_mode'] = 'BLE'
                note_burst_received()

    scanner = BleakScanner(detection_callback=on_detect)
    await scanner.start()
    try:
        while True:
            await asyncio.sleep(1)
            # Drop out of BLE mode if we haven't heard a BLE packet recently
            if state['tag_rssi'] is not None and (time.time() - state['last_burst']) > BLE_RANGE_TIMEOUT_S:
                state['tag_rssi'] = None
                if state['radio_mode'] == 'BLE':
                    state['radio_mode'] = 'LORA' if state['lora_age'] < 300 else 'SEARCHING'
    finally:
        await scanner.stop()


# ── Meshtastic / LoRa thread ─────────────────────────────────────────────
def start_meshtastic():
    if not HAS_MESHTASTIC:
        return
    from pubsub import pub

    def on_receive(packet, interface):
        try:
            pos = packet['decoded']['position']
            if packet.get('fromId', '') == '!TAG1' or 'TAG1' in str(packet):
                state['tag_lat'] = pos['latitude']
                state['tag_lon'] = pos['longitude']
                state['lora_age'] = 0
                state['last_burst'] = time.time()
                if state['radio_mode'] != 'BLE':
                    state['radio_mode'] = 'LORA'
                note_burst_received()
        except Exception:
            pass

    try:
        meshtastic.serial_interface.SerialInterface('/dev/ttyS0')
        pub.subscribe(on_receive, 'meshtastic.receive.position')
        while True:
            time.sleep(1)
            state['lora_age'] += 1
    except Exception as e:
        print(f"[tracker_ui] Meshtastic thread error: {e}")


# ── Pygame compass display ───────────────────────────────────────────────
def draw_display(screen, bearing, distance_mi, mode, rssi, next_burst_secs):
    W, H = screen.get_size()
    screen.fill((15, 23, 42))   # dark navy

    # Radio mode badge
    badge_colors = {'BLE': (37, 99, 235), 'LORA': (13, 148, 136), 'SEARCHING': (107, 114, 128)}
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
        tip = (cx + int((r - 18) * math.cos(rad)), cy + int((r - 18) * math.sin(rad)))
        tail = (cx - int(55 * math.cos(rad)), cy - int(55 * math.sin(rad)))
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
        s = font_cd2.render(f'Next burst in {int(next_burst_secs)}s', True, (148, 163, 184))
        screen.blit(s, (20, H - 40))

    pygame.display.flip()


# ── Main loop ────────────────────────────────────────────────────────────
def main():
    pygame.init()
    screen = pygame.display.set_mode((800, 480))
    pygame.display.set_caption("Tri-Radio Tracker")

    if HAS_GPSD:
        try:
            gps_connect()
        except Exception as e:
            print(f"[tracker_ui] gpsd connect failed: {e}")

    threading.Thread(target=start_meshtastic, daemon=True).start()
    threading.Thread(target=lambda: asyncio.run(ble_scan_loop()), daemon=True).start()

    clock = pygame.time.Clock()
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return
            if event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                return

        if HAS_GPSD:
            try:
                fix = get_current()
                state['my_lat'] = fix.lat
                state['my_lon'] = fix.lon
            except Exception:
                pass

        bearing = distance = None
        if all([state['my_lat'], state['my_lon'], state['tag_lat'], state['tag_lon']]):
            bearing = bearing_to(state['my_lat'], state['my_lon'], state['tag_lat'], state['tag_lon'])
            distance = haversine_miles(state['my_lat'], state['my_lon'], state['tag_lat'], state['tag_lon'])

        next_burst_secs = max(0, state['next_burst'] - time.time())
        draw_display(screen, bearing, distance, state['radio_mode'], state['tag_rssi'], next_burst_secs)
        clock.tick(10)   # 10fps is plenty for a compass


if __name__ == '__main__':
    main()
