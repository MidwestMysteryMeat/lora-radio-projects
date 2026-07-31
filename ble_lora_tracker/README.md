> ⚠️ **Prototype — recovered code, not fully finished.**
> Reconstructed from conversation history. Register values, pin assignments, and
> Meshtastic packet formats are from the original build and **must be verified
> against your actual hardware before flashing.** Treat as a working starting point.

# Tri-Radio Tracker (BLE + LoRa + GPS)

A two-part locator system: a small **Tag** that broadcasts its GPS
position over both LoRa (long range) and BLE (close range), and a
handheld **Finder** (Pi 4 + touchscreen) that shows a compass pointing
toward the tag and switches to precise BLE direction-finding once you're
within ~100ft.

> **Reconstruction note:** this replaces an earlier placeholder version
> that was a generic single-board sketch. This is rebuilt from the actual
> original build found in conversation history — the full ESP32-S3
> MicroPython tag firmware (charge handling, GPS caching, deploy-on-unplug,
> the real 6-byte RTC state layout), and the Pi Finder compass UI using
> Meshtastic's real `pub.subscribe('meshtastic.receive.position')` API.
> Code is reconstructed from inline snippets, so verify pin assignments
> against your actual wiring before relying on it. The tag firmware's
> phase-threshold cycle counts are from the original but worth tuning to
> real-world battery behavior.

## Structure

```
ble_lora_tracker/
├── HARDWARE.md               # full BOM (Tag ~$53-62, Finder), antenna notes, power
├── tag_firmware/
│   └── main.py               # ESP32-S3 MicroPython: GPS + BLE + SOS burst + deep sleep
└── finder/
    ├── tracker_ui.py         # Pi 4 pygame compass UI, tri-radio listener
    └── tracker.service       # systemd unit to auto-start the UI on boot
```

## How it works

- **Tag** (dual XIAO ESP32: C3 runs Meshtastic/LoRa, S3 runs the firmware
  here): wakes from deep sleep, reads GPS, fires a 9-packet SOS burst
  (3 short / 3 long / 3 short) over BLE and LoRa at once, then sleeps.
  A three-phase schedule bursts frequently right after activation and
  sparser later to stretch battery life. Phase/cycle state survives deep
  sleep in RTC memory.
- **Finder** (Pi 4 + Waveshare SX1262 HAT + 7" screen + USB GPS/BLE):
  listens on LoRa and BLE simultaneously, always uses the freshest tag
  position, and renders a compass arrow + distance. Predicts the next
  burst and shows a countdown. Inside BLE range it flips to live BLE
  RSSI for close-range homing.

## Setup

Tag: flash MicroPython to the XIAO ESP32-S3, copy `tag_firmware/main.py`
to the board root. The ESP32-C3 runs Meshtastic separately and connects
over the UART bridge.

Finder:
```bash
pip3 install pygame gpsd-py3 bleak meshtastic
sudo apt install gpsd gpsd-clients -y
# set DEVICES="/dev/ttyUSB0" and GPSD_OPTIONS="-n" in /etc/default/gpsd
sudo systemctl restart gpsd
python3 finder/tracker_ui.py
```

See `HARDWARE.md` for the full parts list, antenna options, and wiring.

## Important use note

This design is capable of tracking a person's location. Whatever it's
used for, the person being tracked should know about and consent to it —
tracking someone without their knowledge is illegal in many places and
harmful regardless of legality. Build it for things you have the right to
locate: your own gear, your own vehicle, or a person who has agreed to
carry it.
