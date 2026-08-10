# Production-readiness gates

## Current decision

**Prototype only. Do not represent any of these builds as production-ready or
use them as a safety-of-life locator.** The host-verifiable protocol, GPS, state,
and geometry paths have automated coverage, but the repository still has
hardware and architecture blockers that software-only tests cannot close.

Run the host checks with:

```bash
python -B tests/test_coord_roundtrip.py
python -B -m compileall -q pico_lora_locator meshtastic_hiker_compass ble_lora_tracker tests
```

The same commands run on pull requests through GitHub Actions.

## What is covered automatically

- signed coordinate encoding/decoding in all three wire/cache paths;
- exact BLE/raw-LoRa tag frame validation, including Bleak's stripped company ID;
- coordinate range checks and valid zero-degree coordinates;
- RMC parsing for multiple talkers, checksum failures, invalid fixes, malformed
  minutes, and invalid hemisphere fields;
- great-circle distance and initial-bearing edge cases;
- Meshtastic JSON-bridge position parsing and node-ID normalization;
- RTC state migration, explicit cache validity, corruption fallback, and cache
  preservation across state writes;
- finder radio freshness and multi-packet burst de-duplication; and
- CPython syntax compilation of every Python source file.

These tests exercise pure functions extracted from the firmware. They do not
emulate GPIO, sleep current, UART framing, SPI timing, RF behavior, or a real
Meshtastic node.

## Blocking decisions

| Build | Blocker | Required decision/evidence |
|---|---|---|
| Pico locator | Reconstructed SX1276 pin/register and RX behavior | Verify both board revisions, exchange packets bidirectionally, inject a bad-CRC packet, and record packet-loss/range results. |
| Pico locator | No measured power design | Select a regulated battery supply, measure active/idle current, and add a sleep strategy if the measured runtime requires it. |
| Hiker compass | Stock Meshtastic serial is framed protobuf, while this sketch consumes JSON lines | Provide and test a specific JSON bridge/custom firmware, or replace the sketch with a client that implements the framed API. A stock T-Echo cannot simultaneously run this MicroPython sketch and Meshtastic firmware. |
| Hiker compass | Uncalibrated/untested magnetometer and placeholder node IDs | Store per-device hard/soft-iron calibration and verify headings; provision real node IDs and roof coordinates. |
| BLE/LoRa tracker | Tag sends raw SX1276 frames while the Finder subscribes to Meshtastic positions | Choose one end-to-end LoRa architecture. Either implement the documented C3 Meshtastic/UART bridge with a real node ID, or add and test a compatible raw-LoRa receiver to the Finder. |
| BLE/LoRa tracker | One RFM95W is assigned to both the C3 in the hardware design and the S3 in firmware | Assign exactly one radio owner and update both wiring and firmware before assembly. |
| BLE/LoRa tracker | TP4056 `CHRG` HIGH means charge-complete/idle as well as unplugged | Add a separate USB/power-present signal if deploy-on-unplug must be reliable; bench-test charge-in-progress, charge-complete, unplug, and brownout transitions. |
| All | Region-specific frequency, power, airtime, and antenna rules | Select a region configuration and document measured airtime/duty cycle and the applicable operator limits before transmission. |
| All | Broadcast positions are unauthenticated and unencrypted | Add an authenticated, replay-resistant protocol and key-provisioning plan wherever position confidentiality or spoof resistance is required. |

## Hardware acceptance record

Before removing the prototype label, attach a dated test record to a release:

1. Exact board/radio/GPS revisions, wiring photos, firmware versions, configured
   frequency/power, antenna type, and calibrated test equipment.
2. Cold boot, warm boot, RTC corruption, GPS loss/reacquisition, radio timeout,
   brownout, charge-complete, and unplug behavior.
3. At least 100 bidirectional packets per supported path with packet-loss and
   malformed/CRC-rejection results.
4. Range tests in the intended terrain and body/enclosure orientation, with no
   unqualified range promise derived from the estimates in the READMEs.
5. Measured worst-case active current, deep-sleep current, battery capacity,
   temperature, runtime, and low-voltage behavior.
6. A 24-hour soak test for handhelds and a full target-runtime soak for the tag,
   including logs of resets, stale fixes, and missed bursts.
7. A privacy/threat-model review and explicit, informed consent from every person
   who may carry a transmitting device.

Passing CI means the host logic is internally consistent. It is one release gate,
not evidence that the assembled radio system is ready for field reliance.
