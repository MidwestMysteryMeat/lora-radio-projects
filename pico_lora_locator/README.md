> ⚠️ **Prototype — recovered code, not fully finished.**
> Reconstructed from conversation history. Register values, pin assignments, and
> Meshtastic packet formats are from the original build and **must be verified
> against your actual hardware before flashing.** Treat as a working starting point.

# Pico LoRa GPS Locator (two devices)

Two identical devices that each know where the other is. Each reads its
own GPS, broadcasts it over LoRa, listens for the other, and shows a
distance + direction arrow on a small OLED. No server, no WiFi, no phone.

> **Reconstruction note:** rebuilt from the original inline code in
> conversation history. This is the earliest and most self-contained of
> the three LoRa builds — bare-metal SX1276 register access over SPI,
> no Meshtastic dependency. The register values (915MHz, SF7, BW 125kHz,
> CR 4/5) are from the original; verify against your specific module.
> The `lora_receive()` RX path is reconstructed to match the original's
> `lora_send()` TX style — test the RxDone/CRC handling on real hardware.

## Hardware per device (~$35-40, ~$70-80 the pair)

| Part | ~Cost |
|---|---|
| Raspberry Pi Pico 2 WH | $9 |
| GY-NEO6MV2 GPS | $5.50 |
| RA-02 / SX1276 / RFM95W 915MHz LoRa module | $5-8 |
| SSD1306 OLED (128x64) | $4 |
| 3.7V LiPo + TP4056 charger | $10 |
| Breadboard + jumpers | $7 |

## Range

- Open field: 2-5km
- Forest: 500m-2km depending on terrain
- Biggest cheap upgrade: replace the stub antenna with a wire cut to
  exactly 82.2mm (quarter-wave at 915MHz), soldered to the antenna pad

## Setup

1. Flash MicroPython to both Picos
2. Copy `ssd1306.py` (from micropython-lib) to each device root
3. Copy `main.py` to each device — but set `MY_ID`/`OTHER_ID` opposite
   on the two units (DEV1 sees DEV2 and vice versa)

## How it works

```
loop:
  read my GPS
  broadcast (my id, lat, lon) over LoRa
  listen ~2s for the other device's packet
  if heard: compute bearing + haversine distance
  draw arrow + distance on OLED
  sleep
```

This was the starting point of a larger family of builds — it later grew
into a Meshtastic hiking-party compass (`../meshtastic_hiker_compass/`)
and a dual-ESP32 SOS-burst tracker (`../ble_lora_tracker/`).
