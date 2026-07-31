# LoRa Radio Projects

> ## ⚠️ READ BEFORE BUILDING — location/tracking tech, consent & law apply
>
> These builds read GPS and broadcast **position** over LoRa/BLE, and can locate
> people, vehicles, or gear.
>
> - **Consent only.** Track only your own property, or a person who **knows and
>   has agreed** to carry a tag. Covert/non-consensual tracking of a person is
>   illegal in many places and harmful regardless. You alone are responsible for
>   lawful use.
> - **Radio law.** 915 MHz frequency, transmit power, and duty cycle are
>   region-regulated (868 MHz in the EU, etc.). Operate within your local rules.
> - **No warranty.** Provided as-is; the authors accept no liability for any
>   damage, interference, legal consequence, or harm from use or misuse.
> - **Reconstructed code — verify before trusting.** Register values, pins,
>   timing, and Meshtastic packet formats must be checked against your hardware.
>
> Full terms: [`DISCLAIMER.md`](DISCLAIMER.md) and [`LICENSE`](LICENSE) (AGPL-3.0).

A family of off-grid **LoRa (915 MHz) location** builds that grew from one idea —
*"know where the other person/thing is, with no phone, cell, server, or
internet."* They progress from a bare-metal two-device locator up to a dual-radio
SOS tracker.

## The three builds

| Project | What it is | Complexity | Radios |
|---|---|---|---|
| [`pico_lora_locator/`](pico_lora_locator/) | Two identical Pi Pico devices that each show the other's distance + bearing on an OLED | Simplest — bare-metal SX1276 over SPI, no Meshtastic | LoRa |
| [`meshtastic_hiker_compass/`](meshtastic_hiker_compass/) | 3-device hiking party: handhelds point a compass needle at each other/home; a solar roof node relays to extend range | Mid — rides on Meshtastic for the mesh; custom compass sketch | LoRa mesh (Meshtastic) |
| [`ble_lora_tracker/`](ble_lora_tracker/) | A worn **Tag** bursts GPS over BLE + LoRa on a phased deep-sleep schedule; a Pi 4 **Finder** shows a compass + distance and switches to BLE RSSI for close-range homing | Most involved — dual-ESP32 tag + Pi Finder UI | BLE + LoRa + GPS |

Rough range for all: open field several km/miles; forest much less (down to
~0.25 mi in dense wet forest). The single biggest cheap upgrade is a proper
915 MHz antenna — a wire cut to **82.2 mm** (quarter-wave) beats many stub
antennas.

## Costs at a glance

| Build | Units | Approx cost | Notes |
|---|---|---|---|
| `pico_lora_locator` | 2 devices | **~$70–80** | ~$35–40 each; cheapest entry point |
| `meshtastic_hiker_compass` | 3 devices (2 handhelds + roof relay) | **~$231** | T-Echo handhelds + solar roof node; see its `HARDWARE.md` |
| `ble_lora_tracker` — Tag | 1 tag | **~$53–62** | Dual XIAO ESP32 + RFM95W + GPS + LiPo |
| `ble_lora_tracker` — Finder | 1 finder | **~$200–225** | Pi 4 + SX1262 HAT + 7" screen + USB GPS + BLE + case |

Per-project bills of materials are in each project's `README.md` / `HARDWARE.md`.

## What's still needed to actually build these

All three run on paper but are **reconstructed prototypes**. Concrete gaps to
close before a working field build:

**`pico_lora_locator`**
- Copy `ssd1306.py` (micropython-lib) to each device — not included.
- The `lora_receive()` RX path (RxDone/CRC handling) is reconstructed — **bench-test it** with two units before trusting.
- No collision handling: both units TX on the same frequency, so simultaneous sends can collide. Add random jitter / listen-before-talk if you see dropouts.
- No power management — it runs the radio continuously. Add deep-sleep between cycles for real battery life.
- Set `MY_ID`/`OTHER_ID` opposite on the two units.

**`meshtastic_hiker_compass`**
- Flash **Meshtastic** to all 3 devices and set a shared channel + PSK (steps in its `HARDWARE.md`).
- Copy drivers `ssd1306.py` and `qmc5883l.py` to each handheld.
- `parse_position_line()` is a **simplified stub** — Meshtastic's serial output format varies by build (JSON vs protobuf-text). Verify against your output or use the Meshtastic Python API.
- **Magnetometer calibration** (hard/soft-iron) is required for an accurate heading needle — not yet implemented.
- Set real `MY_NODE_ID` / `OTHER_NODE_ID` / `HOME_NODE_ID` (read from the Meshtastic app) and the roof node's coordinates (shipped as `0.0/0.0` placeholder).
- Solar roof node: size panel/battery + weatherproof enclosure (see `HARDWARE.md`).

**`ble_lora_tracker`**
- **Resolve the LoRa path.** The docs describe the ESP32-C3 owning LoRa via Meshtastic *and* the tag firmware driving an RFM95W directly over SPI — pick one. The included `tag_firmware/main.py` does the direct-SPI path; if you use the Meshtastic-bridge path instead, you also need the C3's Meshtastic config + the UART bridge glue (not included).
- Verify tag **pin assignments and 915 MHz register values** against your wiring.
- Finder: flash Meshtastic to the SX1262 HAT, configure `gpsd` (`/dev/ttyUSB0`), and confirm the BLE dongle enumerates.
- Confirm the BLE manufacturer-data format matches between `tag_firmware` (`make_payload`) and `finder` (`parse_ble_payload`).
- Field-test the 3-phase burst schedule against real battery life and tune `PHASE_THRESHOLDS`.

## Setup

Each project has its own setup steps and hardware notes:

- [`pico_lora_locator/README.md`](pico_lora_locator/README.md) — flash MicroPython, copy drivers, set IDs.
- [`meshtastic_hiker_compass/README.md`](meshtastic_hiker_compass/README.md) + [`HARDWARE.md`](meshtastic_hiker_compass/HARDWARE.md) — Meshtastic flashing, channel/PSK, drivers.
- [`ble_lora_tracker/README.md`](ble_lora_tracker/README.md) + [`HARDWARE.md`](ble_lora_tracker/HARDWARE.md) — tag flashing, Finder deps (`pygame gpsd-py3 bleak meshtastic`), `gpsd` config, systemd unit.

## Kept splittable

Each subdirectory is self-contained. To promote one into its own repo:

```bash
git subtree split --prefix=ble_lora_tracker -b tracker-only
# or just copy the folder out
```

## License

Licensed under the **GNU Affero General Public License v3.0** (AGPL-3.0) — chosen
deliberately because this is location/tracking technology: improvements,
including anything run as a networked service, must stay open (AGPL's network-use
clause). See [`LICENSE`](LICENSE) and the tracking/consent terms in
[`DISCLAIMER.md`](DISCLAIMER.md).
