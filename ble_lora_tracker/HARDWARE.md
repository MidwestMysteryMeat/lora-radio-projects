# Tri-Radio Tracker — Hardware

Reconstructed from the original build. Two devices: a small worn/carried
**Tag** and a handheld **Finder** with a screen.

## System overview

The Tag wakes on a deep-sleep cycle, reads GPS, and fires a 9-packet SOS
burst over LoRa (long range) and BLE (short range) simultaneously, then
deep-sleeps again. The Finder runs Meshtastic + a BLE scanner + a compass
UI at once — it tracks burst timing and shows a countdown to the next
expected burst. Inside ~100ft BLE range the display auto-switches to live
BLE data for close-range direction finding.

## Tag — parts list (~$53–62 per tag)

| Component | Model | Purpose | ~Cost |
|---|---|---|---|
| XIAO ESP32-C3 | Seeed Studio | Runs Meshtastic, owns the LoRa radio via SPI | $5.00 |
| XIAO ESP32-S3 | Seeed Studio | Runs MicroPython: BLE broadcast, GPS, power cycle | $7.00 |
| LoRa radio | RFM95W SX1276 915MHz | Long-range radio, SPI-controlled by the ESP32-C3 | $11.00 |
| 915MHz antenna | SMA stub 3dBi (or internal flex) | See antenna section below | $3.50 |
| GPS module | GY-NEO6MV2 | Reads own coordinates, UART to ESP32-S3 | $5.50 |
| LiPo battery | 3.7V 2500–5000mAh flat | ~4.5–7.4 days in SOS burst mode | $8–14 |
| LiPo charger | TP4056 USB-C board | Charges via USB-C, regulated 3.7V out | $2.00 |
| Power switch | SPDT slide 3-pin | Hard power on/off for both boards | $1.00 |
| Enclosure | Hammond 1591BSBK / 1599ESGYBK | ABS project box | $6–9 |
| Wiring / misc | 28AWG silicone, foam tape, M2 screws | Mounting + internal wiring | $4.50 |

The two microcontrollers share one enclosure and talk over a 3-wire UART
bridge soldered directly between the boards. The ESP32-C3 owns the LoRa
radio; the ESP32-S3 handles BLE, GPS, and the deep-sleep power cycle.

## Finder — parts list

| Component | Purpose |
|---|---|
| Raspberry Pi 4 | Runs the compass UI + all three radios |
| Waveshare SX1262 LoRa HAT | Meshtastic pre-flashed, talks to Pi over UART (`/dev/ttyS0`) |
| 7" DSI touchscreen (800x480) | Compass display |
| USB GPS dongle (e.g. GlobalSat BU-353-S4) | Finder's own position, read via gpsd (`/dev/ttyUSB0`) |
| USB BLE dongle (e.g. CSR8510) or onboard BLE | Close-range tag detection |
| SmartiPi Touch 2 case | Houses Pi + HAT + screen |

## Antenna notes (915MHz LoRa)

The BLE antenna is already internal (PCB trace on the ESP32-S3 module).
The LoRa antenna is the one with options:

- **External SMA stub** (default): baseline range, needs a hole in the enclosure
- **Internal flex antenna** (~$3–5, "915MHz flexible antenna uFL"): ~1–2dB loss (~15% range reduction), no enclosure hole, nothing to snag — recommended for a worn tag
- **DIY wire monopole** (free): cut a wire to exactly 82.2mm (quarter-wave at 915MHz: `300 / 915 / 4 * 1000 = 82.2mm`), solder to the RFM95W antenna pad, route along the inside wall away from anything metallic. Performance very close to the stub.

For a tag worn/carried by a person, the internal flex is the right call —
no external protrusion, and LoRa at 915MHz/SF7 still covers several miles
even with a mediocre antenna.

## Power / runtime

The ESP32-S3 draws under 0.2mA in deep sleep. Runtime depends on battery
and phase: roughly 4.5 days on a 2500mAh cell, ~7.4 days on 5000mAh, in
burst mode. The three-phase schedule (frequent bursts early, sparser
later) trades update frequency for battery life as time since activation
increases.

## Tag GPIO map (XIAO ESP32-S3)

| Pin | GPIO | Connected to | Function |
|---|---|---|---|
| D0 | GPIO1 | RFM95W DIO0 | LoRa TX-done interrupt |
| D1 | GPIO2 | RFM95W RST | LoRa hardware reset on init |
| D2 | GPIO3 | TP4056 CHRG (pull-up) | Charging detection (LOW = charging) |
| D3 | GPIO4 | RFM95W NSS/CS | SPI chip select |
| D6 | GPIO43 | GPS RX | GPS config commands (optional) |
| D7 | GPIO44 | GPS TX | NMEA sentences in |
| D8 | GPIO7 | RFM95W SCK | SPI clock |
| D9 | GPIO8 | RFM95W MISO | SPI data in |
| D10 | GPIO9 | RFM95W MOSI | SPI data out |
| BAT+ | — | TP4056 OUT+ via slide switch | Board power input |

## Charging and the deploy-on-unplug trigger

The TP4056's CHRG pin goes LOW while charging, HIGH when idle, wired to
GPIO3 with an internal pull-up — the only extra wire needed, no extra
components. The firmware uses it two ways:

- **While charging:** the tag light-sleeps in a 30-second polling loop
  (~1mA) and does NOT advance its phase or cycle counters — state is
  frozen so shelf time doesn't burn through the phase schedule.
- **On unplug (deploy trigger):** when USB is removed the CHRG pin
  transitions HIGH; the firmware detects that the previous wake was
  charging and this one isn't, and resets all counters to zero — a fresh
  Phase 0 start. No button, no app, no manual reset. Plug in to store,
  unplug to deploy.
