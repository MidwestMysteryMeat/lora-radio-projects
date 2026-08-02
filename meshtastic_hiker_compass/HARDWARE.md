# Meshtastic Hiking-Party Compass — Hardware & Setup

Reconstructed from conversation history. A 3-device Meshtastic system for
a hiking party: two handhelds that point at each other (or home) with a
live compass needle, plus a solar roof node that relays to extend range.

## System overview

Meshtastic firmware (flashed, not written) handles LoRa mesh networking,
GPS position sharing, and routing. A custom MicroPython sketch runs on
each handheld reading position from Meshtastic over serial, reading a
magnetometer for the device's own heading, and drawing a compass needle
pointing at the selected target. The roof node runs Meshtastic in ROUTER
role to relay packets and extend the mesh.

Full system ~$231.

## Handheld × 2

| Part | Model | ~Cost |
|---|---|---|
| LoRa/GPS board | LILYGO T-Echo (LoRa + GPS + e-ink integrated) | ~$50 |
| OLED display | SSD1306 128x64 I2C (for the animated needle; e-ink is too slow) | ~$4 |
| Magnetometer | QMC5883L I2C | ~$3 |
| Toggle switch | SPDT (target select: other hiker / home) | ~$1 |
| Battery | 3.7V LiPo + TP4056 | ~$10 |

## Home roof node × 1 (~$83)

| Part | Model | ~Cost |
|---|---|---|
| LoRa board | Heltec WiFi LoRa 32 V3 | ~$18 |
| High-gain antenna | 915MHz 5dBi fiberglass outdoor | ~$15 |
| Solar panel | 5V 6W | ~$12 |
| Solar charge controller | CN3791 MPPT board | ~$8 |
| LiPo battery | 3.7V 6000mAh | ~$14 |
| Weatherproof enclosure | IP67 ABS project box | ~$10 |
| Antenna cable | SMA male-female 1m | ~$6 |

## Wiring — handheld

Both the magnetometer and OLED share the same I2C bus (different addresses,
no conflict):

```
T-Echo I2C SDA  -> QMC5883L SDA + OLED SDA
T-Echo I2C SCL  -> QMC5883L SCL + OLED SCL
T-Echo 3.3V     -> QMC5883L VCC + OLED VCC
T-Echo GND      -> QMC5883L GND + OLED GND

Toggle switch:
  Common      -> T-Echo GPIO (GP5)
  Position A  -> 3.3V  (target = other handheld)
  Position B  -> GND   (target = home node)

I2C addresses: QMC5883L = 0x0D, SSD1306 = 0x3C (no conflict)
```

## Wiring — roof node

```
Heltec LoRa 32:
  SMA antenna port -> coax -> outdoor 5dBi antenna (mounted high)
  5V pin           -> CN3791 OUT+
  GND              -> CN3791 OUT-

CN3791 MPPT:
  SOLAR+/SOLAR-    -> 6W solar panel (face south)
  BAT+/BAT-        -> 6000mAh LiPo

Antenna vertical and as high as possible.
```

## Flashing Meshtastic (all 3 devices, before any custom code)

The T-Echo is an **nRF52840**, not an ESP32 — `esptool` does not apply to
it. It ships with a UF2 bootloader and flashes by drag-and-drop:

```bash
# T-Echo handhelds (both): nRF52840, UF2 bootloader.
# 1. Connect USB and double-press the reset button -- the board mounts
#    as a USB mass-storage drive (TECHOBOOT).
# 2. Download the T-Echo UF2 from Meshtastic's firmware releases
#    (firmware-t-echo-<version>.uf2).
# 3. Copy/drag the .uf2 onto the drive -- it flashes and reboots itself.
#
# Alternative (no drive appears / headless): serial DFU with the
# matching DFU .zip package:
#   pip install adafruit-nrfutil
#   adafruit-nrfutil dfu serial --package firmware-t-echo-<version>.zip \
#       -p /dev/ttyACM0 -b 115200

# Heltec WiFi LoRa 32 V3 roof node (ESP32-S3) -- this one IS esptool:
pip install meshtastic esptool --break-system-packages
esptool.py --chip esp32s3 write_flash 0x0 meshtastic-firmware-heltec-v3.bin
# (or use the Meshtastic Web Flasher, which picks the right offsets for you)
```

Then configure each device in the Meshtastic phone app:

```
Region:   US
Role:     CLIENT (handhelds) / ROUTER (roof node)
Channel:  private channel name + PSK key (so only your 3 devices talk)
Position: GPS enabled, broadcast every 30 seconds
```

ROUTER role on the roof node means it prioritises relaying packets and
never sleeps.

## Install the MicroPython drivers (each handheld)

Copy to the device root via Thonny's file browser:

- `ssd1306.py` — from micropython-lib (`drivers/display/ssd1306.py`)
- `qmc5883l.py` — search GitHub for "qmc5883l micropython"
- (LoRa is handled by Meshtastic on this build, so no sx1276 driver needed
  here — that's only for the bare-metal `../pico_lora_locator/` version)

Verify with the REPL: `import ssd1306; import qmc5883l` — no ImportError
means they're installed. Then copy `main.py` and set the three node IDs
(read them from the Meshtastic app after first boot).
