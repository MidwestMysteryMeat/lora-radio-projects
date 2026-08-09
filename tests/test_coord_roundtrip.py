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
    sentence = (
        b'$GPRMC,123519,A,4458.2000,N,09315.6000,W,'
        b'022.4,084.4,230394,003.1,W*6A\r\n'
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
    sentence = (
        b'$GPRMC,000000,A,0000.0000,N,0000.0000,E,'
        b'0,0,010124,,,A*00'
    )
    assert tag['parse_rmc_sentence'](sentence) == pico['parse_rmc_sentence'](sentence)


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
            'parse_rmc_sentence',
        ],
        extra_globals={'DEVICE_ID': b'TAG1'},
    )
    finder = load_functions(
        'ble_lora_tracker/finder/tracker_ui.py',
        [
            'parse_ble_payload', 'parse_lora_payload',
            'haversine_miles', 'bearing_to', 'i32_from_bytes',
        ],
    )
    pico = load_functions(
        'pico_lora_locator/main.py',
        [
            'i32_to_bytes', 'i32_from_bytes', 'make_payload', 'parse_payload',
            'parse_rmc_sentence', 'haversine_miles', 'bearing_to',
        ],
    )
    mesh = load_functions(
        'meshtastic_hiker_compass/main.py',
        [
            'parse_position_line', '_normalize_node_id', '_node_matches',
            '_first_present', 'haversine', 'bearing_to',
        ],
    )

    tests = [
        ('tag i32 round-trip', lambda: test_tag_helpers_roundtrip(tag)),
        ('BLE payload round-trip', lambda: test_ble_payload_roundtrip(tag, finder)),
        ('BLE bleak-stripped', lambda: test_ble_payload_bleak_stripped(tag, finder)),
        ('pico payload round-trip', lambda: test_pico_payload_roundtrip(pico)),
        ('pico short payload', lambda: test_pico_rejects_short_payload(pico)),
        ('lora payload alias', lambda: test_lora_payload_alias(tag, finder)),
        ('RMC Minneapolis', lambda: test_parse_rmc_minneapolis(pico)),
        ('RMC GNRMC/void', lambda: test_parse_rmc_gnrmc_and_void(pico)),
        ('RMC tag==pico', lambda: test_parse_rmc_tag_matches_pico(tag, pico)),
        ('haversine known', lambda: test_haversine_known_distance(pico)),
        ('bearing north', lambda: test_bearing_due_north(pico)),
        ('bearing east', lambda: test_bearing_due_east(pico)),
        ('zero coords distance', lambda: test_zero_coords_are_valid_for_distance(finder)),
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
