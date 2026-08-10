"""Host-runnable unit tests for encode/decode, NMEA, math, and parsers.

The firmware files import MicroPython-only modules (machine, bluetooth)
or heavy host deps (pygame, bleak) at module level, so this test extracts
the pure functions out of the source files with `ast` and executes them
under CPython. Run with:

    python tests/test_coord_roundtrip.py

Verifies that encoder, RTC cache, and decoder all agree on signed
two's-complement encoding, including negative (western/southern)
coordinates, plus GPS NMEA parsing and compass math.
"""

import ast
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Test points: Minneapolis (negative longitude), Sydney (negative latitude),
# Accra (near 0,0 — historically broken by truthiness checks), Tokyo.
POINTS = [
    (44.97, -93.26),
    (-33.86, 151.21),
    (5.60, 0.0),
    (35.68, 139.69),
]


def with_nmea_checksum(body):
    """Append the checksum for an NMEA body beginning with ``$``."""
    assert body.startswith('$') and '*' not in body
    checksum = 0
    for char in body[1:]:
        checksum ^= ord(char)
    return '{}*{:02X}'.format(body, checksum)


def load_functions(relpath, names, extra_globals=None):
    """Extract the named top-level functions from a source file and exec
    them in a fresh namespace, without importing the file's dependencies."""
    src = (ROOT / relpath).read_text(encoding='utf-8')
    tree = ast.parse(src)
    wanted = [
        n for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name in names
    ]
    missing = set(names) - {n.name for n in wanted}
    assert not missing, f'{relpath}: functions not found: {sorted(missing)}'
    module = ast.Module(body=wanted, type_ignores=[])
    ns = {'math': math}
    if extra_globals:
        ns.update(extra_globals)
    exec(compile(module, str(ROOT / relpath), 'exec'), ns)
    return ns


def check_close(decoded, original, label, tol=1e-6):
    # Encoding rounds to int(deg * 1e6); round-trip must be exact to
    # that resolution (< 1e-6 degrees, about 11cm).
    assert abs(decoded - original) < tol, (
        f'{label}: {original} -> {decoded} (error {decoded - original})'
    )


# ── Encoding ─────────────────────────────────────────────────────────────
def test_tag_helpers_roundtrip(tag):
    """Tag i32 helpers: exact signed round-trip (also covers the RTC
    cache, which stores coordinates via these same helpers)."""
    for value in (
        44_970_000, -93_260_000, -33_860_000, 151_210_000,
        0, -1, 2**31 - 1, -2**31,
    ):
        raw = tag['i32_to_bytes'](value)
        assert len(raw) == 4
        back = tag['i32_from_bytes'](raw)
        assert back == value, f'i32 round-trip failed: {value} -> {back}'


def test_ble_payload_roundtrip(tag, finder):
    """Tag make_payload -> finder parse_ble_payload."""
    for lat, lon in POINTS:
        payload = tag['make_payload'](lat, lon, 0)
        got_lat, got_lon = finder['parse_ble_payload'](payload)
        assert got_lat is not None, f'finder failed to parse {payload!r}'
        check_close(got_lat, lat, 'BLE lat')
        check_close(got_lon, lon, 'BLE lon')


def test_ble_payload_bleak_stripped(tag, finder):
    """bleak strips the 2-byte company id; payload should still parse."""
    for lat, lon in POINTS:
        full = tag['make_payload'](lat, lon, 0)
        # Manufacturer body after company_id 0xFFFF
        stripped = full[2:]
        assert stripped.startswith(b'TAG1')
        got_lat, got_lon = finder['parse_ble_payload'](stripped)
        assert got_lat is not None
        check_close(got_lat, lat, 'stripped BLE lat')
        check_close(got_lon, lon, 'stripped BLE lon')


def test_ble_payload_rejects_ambiguous_frames(tag, finder):
    full = tag['make_payload'](44.97, -93.26, 7)
    stripped = full[2:]
    parsed = finder['parse_tag_payload'](full)
    assert parsed is not None and parsed[2] == 7

    # The old marker search accepted TAG1 at any offset and ignored trailing
    # data. Only the two layouts emitted/exposed by the tag are valid.
    assert finder['parse_tag_payload'](b'junk' + stripped) is None
    assert finder['parse_tag_payload'](stripped + b'\x00') is None
    assert finder['parse_tag_payload'](b'\x34\x12' + stripped) is None
    assert finder['parse_ble_payload'](b'unrelated-TAG1-payload') == (None, None)
    assert finder['parse_tag_payload'](stripped[:-1] + b'\x09') is None

    for bad_index in (-1, 9, 256):
        try:
            tag['make_payload'](44.97, -93.26, bad_index)
            assert False, 'tag accepted burst index {}'.format(bad_index)
        except ValueError:
            pass


def test_pico_payload_roundtrip(pico):
    """Pico make_payload -> pico parse_payload (both locator ends)."""
    for lat, lon in POINTS:
        payload = pico['make_payload'](b'DEV1', lat, lon)
        parsed = pico['parse_payload'](payload)
        assert parsed is not None, f'pico failed to parse {payload!r}'
        device_id, got_lat, got_lon = parsed
        assert device_id == b'DEV1'
        check_close(got_lat, lat, 'pico lat')
        check_close(got_lon, lon, 'pico lon')


def test_pico_rejects_short_payload(pico):
    assert pico['parse_payload'](None) is None
    assert pico['parse_payload'](b'') is None
    assert pico['parse_payload'](b'DEV1xxxx') is None  # only 8 bytes
    assert pico['parse_payload'](b'DEV1' + bytes(9)) is None  # trailing data


def test_encoders_reject_invalid_coordinates(tag, pico):
    invalid = [
        (90.000001, 0), (-90.000001, 0),
        (0, 180.000001), (0, -180.000001),
        (float('nan'), 0),
    ]
    for lat, lon in invalid:
        try:
            tag['make_payload'](lat, lon, 0)
            assert False, 'tag accepted invalid coordinates {!r}'.format((lat, lon))
        except ValueError:
            pass
        try:
            pico['make_payload'](b'DEV1', lat, lon)
            assert False, 'pico accepted invalid coordinates {!r}'.format((lat, lon))
        except ValueError:
            pass


def test_lora_payload_alias(tag, finder):
    """parse_lora_payload must match parse_ble_payload for tag frames."""
    for lat, lon in POINTS:
        payload = tag['make_payload'](lat, lon, 3)
        a = finder['parse_ble_payload'](payload)
        b = finder['parse_lora_payload'](payload)
        assert a == b


# ── NMEA RMC ─────────────────────────────────────────────────────────────
def test_parse_rmc_minneapolis(pico):
    # 44°58.2' N, 93°15.6' W  -> ~44.97, -93.26
    sentence = with_nmea_checksum(
        '$GPRMC,123519,A,4458.2000,N,09315.6000,W,'
        '022.4,084.4,230394,003.1,W'
    )
    lat, lon = pico['parse_rmc_sentence'](sentence)
    assert lat is not None
    check_close(lat, 44.97, 'RMC lat', tol=1e-3)
    check_close(lon, -93.26, 'RMC lon', tol=1e-3)


def test_parse_rmc_gnrmc_and_void(pico):
    # Multi-GNSS talker, valid
    ok = (
        '$GNRMC,123519.00,A,3351.6000,S,15112.6000,E,'
        '0.0,0.0,010124,,,A'
    )
    lat, lon = pico['parse_rmc_sentence'](ok)
    assert lat is not None and lat < 0 and lon > 0

    # Void fix must be rejected
    void = '$GPRMC,123519,V,4458.2000,N,09315.6000,W,0,0,230394,,,A'
    lat, lon = pico['parse_rmc_sentence'](void)
    assert lat is None and lon is None


def test_parse_rmc_tag_matches_pico(tag, pico):
    sentence = with_nmea_checksum(
        '$GPRMC,000000,A,0000.0000,N,0000.0000,E,'
        '0,0,010124,,,A'
    )
    assert tag['parse_rmc_sentence'](sentence) == pico['parse_rmc_sentence'](sentence)


def test_parse_rmc_rejects_corruption_and_bad_fields(tag, pico):
    body = '$GPRMC,123519,A,4458.2000,N,09315.6000,W,0,0,230394,,,A'
    valid = with_nmea_checksum(body)
    corrupt = valid.replace('4458.2000', '4458.3000')
    bad_minutes = '$GPRMC,123519,A,4460.0000,N,09315.6000,W,0,0,230394,,,A'
    bad_hemisphere = '$GPRMC,123519,A,4458.2000,X,09315.6000,W,0,0,230394,,,A'
    bad_type = '$GPRMCPLUS,123519,A,4458.2000,N,09315.6000,W'
    for parser in (tag['parse_rmc_sentence'], pico['parse_rmc_sentence']):
        assert parser(valid)[0] is not None
        assert parser(corrupt) == (None, None)
        assert parser(bad_minutes) == (None, None)
        assert parser(bad_hemisphere) == (None, None)
        assert parser(bad_type) == (None, None)


# ── Math ─────────────────────────────────────────────────────────────────
def test_haversine_known_distance(pico):
    # Minneapolis -> roughly Chicago ≈ 355 miles (ballpark)
    d = pico['haversine_miles'](44.97, -93.26, 41.88, -87.63)
    assert 330 < d < 380, f'unexpected distance {d}'


def test_bearing_due_north(pico):
    # From equator toward a point due north
    b = pico['bearing_to'](0.0, 0.0, 1.0, 0.0)
    assert abs(b - 0.0) < 1.0, f'expected ~0 got {b}'


def test_bearing_due_east(pico):
    b = pico['bearing_to'](0.0, 0.0, 0.0, 1.0)
    assert abs(b - 90.0) < 1.0, f'expected ~90 got {b}'


def test_zero_coords_are_valid_for_distance(finder):
    """Regression: `all([lat, lon, ...])` treated 0.0 as missing."""
    d = finder['haversine_miles'](0.0, 0.0, 0.0, 1.0)
    assert d > 60  # ~69 miles per degree longitude at equator


def test_finder_radio_freshness_and_burst_dedup(finder):
    mode = finder['radio_mode_for_timestamps']
    assert mode(100.0, 99.0, 100.0) == 'BLE'
    assert mode(100.0, 0.0, 99.0) == 'LORA'
    assert mode(1000.0, 900.0, 600.0) == 'SEARCHING'

    count = finder['should_count_burst']
    assert count(0.0, 100.0)
    assert not count(100.0, 107.0)  # another packet/radio in same SOS burst
    assert count(100.0, 109.0)
    assert count(100.0, 99.0)       # wall-clock correction must not lock it out


class FakeRTC:
    def __init__(self):
        self.data = b''

    def memory(self, value=None):
        if value is not None:
            self.data = bytes(value)
        return self.data


class FakeMachine:
    def __init__(self):
        self.rtc = FakeRTC()

    def RTC(self):
        return self.rtc


def test_rtc_cache_validity_and_migration(tag_rtc, fake_machine):
    assert tag_rtc['load_gps_cache']() == (None, None)

    # A current state without a saved fix must not turn zeroed bytes into (0, 0).
    tag_rtc['write_rtc'](0, 0, 0, 0, 0)
    assert tag_rtc['load_gps_cache']() == (None, None)

    # Once explicitly saved, (0, 0) is a valid coordinate and survives state writes.
    tag_rtc['save_gps_cache'](0.0, 0.0)
    assert tag_rtc['load_gps_cache']() == (0.0, 0.0)
    tag_rtc['write_rtc'](1, 2, 1, 1, 42)
    assert tag_rtc['load_gps_cache']() == (0.0, 0.0)
    assert tag_rtc['read_rtc']() == (1, 2, 1, 1, 42)

    # Corrupt/partial current layouts are reset instead of trusted.
    fake_machine.rtc.data = bytes(10)
    assert tag_rtc['read_rtc']() == (0, 0, 0, 0, 0)
    assert tag_rtc['load_gps_cache']() == (None, None)

    # Preserve compatibility with the original six-byte state until rewritten.
    fake_machine.rtc.data = bytes([1, 2, 1, 1, 0x12, 0x34])
    assert tag_rtc['read_rtc']() == (1, 2, 1, 1, 0x1234)
    tag_rtc['save_gps_cache'](44.97, -93.26)
    assert tag_rtc['read_rtc']() == (1, 2, 1, 1, 0x1234)
    lat, lon = tag_rtc['load_gps_cache']()
    check_close(lat, 44.97, 'RTC lat')
    check_close(lon, -93.26, 'RTC lon')


# ── Meshtastic JSON parser ───────────────────────────────────────────────
def test_meshtastic_parse_scaled_and_decimal(mesh):
    # Scaled integer form (1e-7 degrees)
    line = (
        '{"from":"!aabbccdd","decoded":{"position":'
        '{"latitudeI":449700000,"longitudeI":-932600000}}}'
    )
    parsed = mesh['parse_position_line'](line)
    assert parsed is not None
    node, lat, lon = parsed
    assert 'aabbccdd' in node
    check_close(lat, 44.97, 'mesh lat', tol=1e-5)
    check_close(lon, -93.26, 'mesh lon', tol=1e-5)

    # Decimal degrees, including equator (0.0 must not be dropped)
    line2 = '{"fromId":"!11223344","latitude":0.0,"longitude":10.5}'
    parsed2 = mesh['parse_position_line'](line2)
    assert parsed2 is not None
    _, lat2, lon2 = parsed2
    assert lat2 == 0.0
    check_close(lon2, 10.5, 'mesh lon2')


def test_meshtastic_rejects_garbage(mesh):
    assert mesh['parse_position_line'](b'') is None
    assert mesh['parse_position_line'](b'not json at all') is None
    assert mesh['parse_position_line'](b'{"from":"!abc"}') is None


def test_node_id_normalize(mesh):
    assert mesh['_normalize_node_id']('!AABBCCDD') == '!aabbccdd'
    assert mesh['_node_matches']('!aabbccdd', '!AABBCCDD')
    assert mesh['_node_matches']('!aabbccdd', 'aabbccdd')


# ── Runner ───────────────────────────────────────────────────────────────
def main():
    tag = load_functions(
        'ble_lora_tracker/tag_firmware/main.py',
        [
            'i32_to_bytes', 'i32_from_bytes', 'make_payload',
            'parse_rmc_sentence', '_nmea_checksum_ok',
        ],
        extra_globals={'DEVICE_ID': b'TAG1', 'MAX_BURST_INDEX': 8},
    )
    finder = load_functions(
        'ble_lora_tracker/finder/tracker_ui.py',
        [
            'parse_tag_payload', 'parse_ble_payload', 'parse_lora_payload',
            'haversine_miles', 'bearing_to', 'i32_from_bytes',
            '_is_recent', 'radio_mode_for_timestamps', 'should_count_burst',
        ],
        extra_globals={
            'DEVICE_ID': b'TAG1',
            'MAX_BURST_INDEX': 8,
            'BLE_RANGE_TIMEOUT_S': 8,
            'LORA_STALE_S': 300,
            'BURST_DEDUP_S': 8,
        },
    )
    pico = load_functions(
        'pico_lora_locator/main.py',
        [
            'i32_to_bytes', 'i32_from_bytes', 'make_payload', 'parse_payload',
            'parse_rmc_sentence', '_nmea_checksum_ok',
            'haversine_miles', 'bearing_to',
        ],
    )
    mesh = load_functions(
        'meshtastic_hiker_compass/main.py',
        [
            'parse_position_line', '_normalize_node_id', '_node_matches',
            '_first_present', 'haversine', 'bearing_to',
        ],
    )

    fake_machine = FakeMachine()
    tag_rtc = load_functions(
        'ble_lora_tracker/tag_firmware/main.py',
        [
            'i32_to_bytes', 'i32_from_bytes', 'read_rtc', 'write_rtc',
            'save_gps_cache', 'load_gps_cache',
        ],
        extra_globals={
            'machine': fake_machine,
            'GPS_EVERY_N': [3, 3, 3],
            'RTC_MAGIC': 0xA5,
            'RTC_LEN': 15,
            'RTC_FLAG_CHARGING': 0x01,
            'RTC_FLAG_GPS_VALID': 0x02,
        },
    )

    tests = [
        ('tag i32 round-trip', lambda: test_tag_helpers_roundtrip(tag)),
        ('BLE payload round-trip', lambda: test_ble_payload_roundtrip(tag, finder)),
        ('BLE bleak-stripped', lambda: test_ble_payload_bleak_stripped(tag, finder)),
        ('BLE frame validation', lambda: test_ble_payload_rejects_ambiguous_frames(tag, finder)),
        ('pico payload round-trip', lambda: test_pico_payload_roundtrip(pico)),
        ('pico short payload', lambda: test_pico_rejects_short_payload(pico)),
        ('encoder coordinate validation', lambda: test_encoders_reject_invalid_coordinates(tag, pico)),
        ('lora payload alias', lambda: test_lora_payload_alias(tag, finder)),
        ('RMC Minneapolis', lambda: test_parse_rmc_minneapolis(pico)),
        ('RMC GNRMC/void', lambda: test_parse_rmc_gnrmc_and_void(pico)),
        ('RMC tag==pico', lambda: test_parse_rmc_tag_matches_pico(tag, pico)),
        ('RMC validation', lambda: test_parse_rmc_rejects_corruption_and_bad_fields(tag, pico)),
        ('haversine known', lambda: test_haversine_known_distance(pico)),
        ('bearing north', lambda: test_bearing_due_north(pico)),
        ('bearing east', lambda: test_bearing_due_east(pico)),
        ('zero coords distance', lambda: test_zero_coords_are_valid_for_distance(finder)),
        ('finder freshness/dedup', lambda: test_finder_radio_freshness_and_burst_dedup(finder)),
        ('RTC cache/migration', lambda: test_rtc_cache_validity_and_migration(tag_rtc, fake_machine)),
        ('meshtastic parse', lambda: test_meshtastic_parse_scaled_and_decimal(mesh)),
        ('meshtastic garbage', lambda: test_meshtastic_rejects_garbage(mesh)),
        ('node id normalize', lambda: test_node_id_normalize(mesh)),
    ]

    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f'  OK  {name}')
        except Exception as e:
            failed += 1
            print(f'FAIL  {name}: {e}')

    if failed:
        print(f'\n{failed}/{len(tests)} tests failed')
        sys.exit(1)
    print(f'\nOK: all {len(tests)} tests passed for points {POINTS}')


if __name__ == '__main__':
    main()
