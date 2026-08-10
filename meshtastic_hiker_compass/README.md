> ⚠️ **Prototype — recovered code, not fully finished.**
> Reconstructed from conversation history. Register values, pin assignments, and
> Meshtastic packet formats are from the original build and **must be verified
> against your actual hardware before flashing.** Treat as a working starting point.

# Meshtastic Hiking-Party Compass

Three-device off-grid system for a hiking party. Two handhelds each show
a live compass needle pointing at the other hiker (or the home node),
with distance. A solar-powered roof node relays over Meshtastic to extend
range. Everyone carries a device and sees each other — no phone, no cell,
no server.

> **Reconstruction note:** rebuilt from the original inline code in
> conversation history. The compass math, magnetometer heading, OLED
> needle drawing, and haversine are the original code. The Meshtastic
> serial position parser (`parse_position_line`) is the piece most worth
> checking against your actual Meshtastic build — its serial output
> format varies by version/config (JSON vs protobuf-text), and the
> original parser was explicitly a simplified stub. Swap in `ujson.loads`
> or the Meshtastic Python API on the receive side if the raw parse
> doesn't match your output.

## Files

- `main.py` — handheld MicroPython sketch: reads Meshtastic position over
  serial, magnetometer heading, draws the needle; toggle switch picks
  target (other hiker vs home node)
- `HARDWARE.md` — full BOM (~$231), wiring for handhelds + solar roof
  node, Meshtastic flashing + channel/PSK config, driver install steps

## How it works

- Meshtastic firmware on the three mesh nodes does the networking, GPS
  sharing, and routing.
- A **separate display controller plus JSON bridge is required** to run the
  included MicroPython sketch. Flashing MicroPython onto a T-Echo replaces
  Meshtastic; the two firmwares cannot run concurrently on that MCU.
- The display sketch reads bridged positions, works out
  bearing to the selected target, subtracts the device's own compass
  heading so the needle points where to actually *walk*, and renders it on
  the OLED
- The roof node (ROUTER role) relays packets so two hikers out of direct
  range of each other still stay connected through it

## Known limitation — serial position parser

`parse_position_line()` in `main.py` expects Meshtastic to emit **JSON
lines** over the serial port. Stock Meshtastic firmware does not do that:
its serial/USB port speaks the **framed protobuf client API** (the same
binary protocol the phone app and Python CLI use), so `ujson.loads` on
raw serial lines will never match and no positions will ever be parsed.
To make this path real you need one of:

- a companion bridge (e.g. a host or second MCU running the Meshtastic
  Python API) that decodes packets and forwards them as JSON lines to
  the handheld's UART, or
- a protobuf-frame parser on the handheld itself (decode the 0x94 0xC3
  framed `FromRadio` messages), or
- a Meshtastic build/module configured to emit text/JSON output on a
  spare serial port.

If `main.py` is flashed to a separate compatible controller without the missing
bridge, it only shows "Waiting for GPS fix..." because no position updates
arrive.

The setup is therefore an architecture prototype, not a flashable three-device
build. See [`PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md) for the
decision and acceptance gates.

## Range reality

- Open field: 3-6 miles; moderate forest: ~0.9-1.5 miles; dense wet
  forest: as low as 0.25 miles
- The roof node / mesh relay is exactly how you fix the two-device forest
  range problem — a third relay node at a high point keeps hikers
  connected even when they can't reach each other directly

This is the mesh-networked middle version of a three-project family:
simpler bare-metal LoRa in `../pico_lora_locator/`, and a dual-ESP32
SOS-burst locator in `../ble_lora_tracker/`.
