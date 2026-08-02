"""Host-runnable round-trip test for the signed lat/lon wire encoding.

The firmware files import MicroPython-only modules (machine, bluetooth)
or heavy host deps (pygame, bleak) at module level, so this test extracts
the encode/decode functions straight out of the source files with `ast`
and executes them under CPython. Run with:

    python tests/test_coord_roundtrip.py

Verifies that encoder, RTC cache, and decoder all agree on signed
two's-complement encoding, including negative (western/southern)
coordinates.
"""

import ast
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Test points: Minneapolis (negative longitude), Sydney (negative latitude)
POINTS = [(44.97, -93.26), (-33.86, 151.21)]


def load_functions(relpath, names, extra_globals=None):
    """Extract the named top-level functions from a source file and exec
    them in a fresh namespace, without importing the file's dependencies."""
    src = (ROOT / relpath).read_text(encoding="utf-8")
    tree = ast.parse(src)
    wanted = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in names]
    missing = set(names) - {n.name for n in wanted}
    assert not missing, f"{relpath}: functions not found: {sorted(missing)}"
    module = ast.Module(body=wanted, type_ignores=[])
    ns = dict(extra_globals or {})
    exec(compile(module, str(ROOT / relpath), "exec"), ns)
    return ns


def check_close(decoded, original, label):
    # Encoding truncates to int(deg * 1e6); round-trip must be exact to
    # that resolution (< 1e-6 degrees, about 11cm).
    assert abs(decoded - original) < 1e-6, (
        f"{label}: {original} -> {decoded} (error {decoded - original})")


def test_tag_helpers_roundtrip(tag):
    """Tag i32 helpers: exact signed round-trip (also covers the RTC
    cache, which stores coordinates via these same helpers)."""
    for value in (44_970_000, -93_260_000, -33_860_000, 151_210_000,
                  0, -1, 2**31 - 1, -2**31):
        raw = tag["i32_to_bytes"](value)
        assert len(raw) == 4
        back = tag["i32_from_bytes"](raw)
        assert back == value, f"i32 round-trip failed: {value} -> {back}"


def test_ble_payload_roundtrip(tag, finder):
    """Tag make_payload -> finder parse_ble_payload."""
    for lat, lon in POINTS:
        payload = tag["make_payload"](lat, lon, 0)
        got_lat, got_lon = finder["parse_ble_payload"](payload)
        assert got_lat is not None, f"finder failed to parse {payload!r}"
        check_close(got_lat, lat, "BLE lat")
        check_close(got_lon, lon, "BLE lon")


def test_pico_payload_roundtrip(pico):
    """Pico make_payload -> pico parse_payload (both locator ends)."""
    for lat, lon in POINTS:
        payload = pico["make_payload"](b"DEV1", lat, lon)
        parsed = pico["parse_payload"](payload)
        assert parsed is not None, f"pico failed to parse {payload!r}"
        device_id, got_lat, got_lon = parsed
        assert device_id == b"DEV1"
        check_close(got_lat, lat, "pico lat")
        check_close(got_lon, lon, "pico lon")


def main():
    tag = load_functions(
        "ble_lora_tracker/tag_firmware/main.py",
        ["i32_to_bytes", "i32_from_bytes", "make_payload"],
        extra_globals={"DEVICE_ID": b"TAG1"})
    finder = load_functions(
        "ble_lora_tracker/finder/tracker_ui.py",
        ["parse_ble_payload"])
    pico = load_functions(
        "pico_lora_locator/main.py",
        ["i32_to_bytes", "i32_from_bytes", "make_payload", "parse_payload"])

    test_tag_helpers_roundtrip(tag)
    test_ble_payload_roundtrip(tag, finder)
    test_pico_payload_roundtrip(pico)
    print("OK: all encode->decode round-trips exact for", POINTS)


if __name__ == "__main__":
    main()
