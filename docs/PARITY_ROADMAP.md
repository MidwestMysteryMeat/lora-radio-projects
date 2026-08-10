# Capability parity roadmap

## Purpose and scope

The three projects intentionally use different radios, user interfaces, and
power profiles. "Parity" therefore means a shared, testable release contract
for location correctness, stale-data handling, configuration, diagnostics,
privacy, and hardware evidence. It does not mean forcing every build to have
BLE, Meshtastic, a relay, or identical hardware.

Status legend: **Ready** means the repository implementation and host checks
cover the capability; **Partial** means only part of the end-to-end path exists;
**Blocked** means an architecture or hardware decision is required; **N/A** is
an intentional product difference. No status in this document is evidence of
hardware validation. See [`../PRODUCTION_READINESS.md`](../PRODUCTION_READINESS.md)
for the release gates that still apply to every project.

## Current capability matrix

| Capability | Pico locator | Hiker compass | BLE/LoRa tracker | Parity target |
|---|---|---|---|---|
| Coordinate encoding and bounds checks | Ready | Ready for bridge JSON | Ready for BLE/raw frames and RTC cache | One set of golden coordinate vectors passes for every implemented transport. |
| Corrupt or malformed GPS rejection | Ready: RMC checksum and field checks | Partial: positions depend on the external bridge/Meshtastic path | Ready: RMC checksum and field checks | Each GPS ingress rejects invalid fixes, corrupt checksums, invalid minutes, hemispheres, and ranges. |
| Long-range position transport | Partial: raw SX1276 path is implemented but not bench-tested | Blocked: display-side JSON bridge/native client is not selected | Blocked: tag sends raw LoRa while Finder expects Meshtastic | Every advertised long-range path exchanges 100 bidirectional packets on the documented hardware and rejects malformed frames. |
| Distance and bearing | Ready in host checks | Ready in host checks | Ready in host checks | Shared edge-case vectors cover zero coordinates, cardinal bearings, and representative distances. |
| Stale/no-fix behavior | Partial: display retains the last received fix without an explicit age indicator | Partial: waiting state exists; bridge freshness contract is undefined | Ready in host logic with independent BLE/LoRa freshness | Every UI visibly distinguishes no fix, stale fix, and current fix using documented timeouts. |
| Local user display | Partial: OLED logic exists; device validation remains | Blocked end-to-end by the display-controller architecture | Partial: Pi UI exists; touchscreen/hardware validation remains | A recorded hardware test verifies boot, no-fix, active, stale, and recovery screens. |
| Close-range homing | N/A | N/A | Partial: BLE RSSI mode is implemented but not field-calibrated | Tracker-only acceptance: measured RSSI behavior and clear fallback when BLE expires. |
| Mesh relay | N/A | Partial: Meshtastic relay design exists, display integration does not | N/A | Hiker-only acceptance: two handhelds exchange positions through the roof relay with the direct path unavailable. |
| Power management and retained state | Blocked: no measured sleep strategy | Partial: Meshtastic node behavior is external; display controller is undecided | Partial: phased deep sleep and versioned RTC state are host-tested | Measured active/sleep current, brownout recovery, retained-state corruption recovery, and target-runtime soak results are attached to a release. |
| Configuration/provisioning | Partial: IDs and radio values are source constants | Blocked: real node IDs, roof coordinates, bridge, and calibration are not provisioned | Blocked: radio owner/architecture and real node ID are unresolved | Per-device configuration is documented, validated at startup, and does not require source edits for deployment values. |
| Authenticated/replay-resistant positions | Blocked | Partial: Meshtastic channel protection is available, but the bridge and application threat model are undefined | Blocked for BLE and raw LoRa broadcasts | Publish a threat model and use an authenticated, replay-resistant envelope wherever spoofing or location confidentiality matters. |
| Automated host checks | Ready | Ready for parser/math helpers | Ready | CI runs the same syntax and logic suite on every supported host Python version. |
| Hardware-in-the-loop evidence | Blocked | Blocked | Blocked | A dated, reproducible hardware acceptance record satisfies the production-readiness checklist. |

## Priority roadmap

### P0 — resolve architecture contradictions

1. **Hiker compass:** choose either a separate display MCU fed by a specified
   JSON bridge, a native Meshtastic module, or a handheld client that implements
   the framed protobuf API. Document the controller, wiring, framing, and
   ownership of GPS/display data.
2. **BLE/LoRa tracker:** choose raw LoRa end to end or Meshtastic end to end;
   assign the RFM95W to exactly one MCU; remove the unused competing design.
3. **All builds:** select supported regulatory regions and decide whether the
   product requires location confidentiality and spoof/replay resistance.

Acceptance criteria: each decision is recorded in the project documentation;
one wiring diagram and one versioned wire contract exist per active transport;
no README describes two incompatible firmwares or radio owners as concurrent.

### P1 — establish the common location contract

1. Define a versioned envelope containing source ID, coordinates, fix validity,
   monotonic sequence/counter, and integrity/authentication fields appropriate
   to the selected transport.
2. Apply common coordinate, age, duplicate, and malformed-frame semantics at
   every ingress. Make current/stale/no-fix states explicit in every UI.
3. Move deployment values—device IDs, target IDs, region/frequency, timeouts,
   calibration, and roof coordinates—out of ad-hoc source edits and validate
   them at startup.
4. Add shared golden vectors plus transport-specific compatibility tests.

Acceptance criteria: at least 100 generated round trips and a checked-in set of
cross-project golden vectors pass; corrupt, duplicate, replayed, out-of-range,
and stale samples have deterministic results; invalid configuration fails safe
with an actionable diagnostic.

### P2 — close hardware reliability gaps

| Project | Required milestone | Acceptance evidence |
|---|---|---|
| Pico locator | Verify SX1276 registers, CRC/RxDone behavior, collision mitigation, supply, and sleep strategy on two units. | 100 bidirectional packets, injected bad CRC rejection, packet-loss/range table, and measured active/idle current. |
| Hiker compass | Implement the P0 display architecture, persistent hard/soft-iron calibration, and real node/roof provisioning. | Relay-only field exchange, calibrated heading error results, bridge disconnect/recovery test, and 24-hour soak. |
| BLE/LoRa tracker | Implement the selected LoRa architecture, reliable power-present sensing, and complete tag/Finder provisioning. | BLE and LoRa compatibility record, charge/unplug/brownout state table, measured burst timing/current, and target-runtime soak. |

Acceptance criteria: every test record names exact board revisions, firmware
versions, antennas, region settings, instruments, and commands; failures and
retries remain in the record rather than being summarized away.

### P3 — security, privacy, and field qualification

1. Document consent, data exposure, key provisioning/rotation, lost-device
   handling, replay behavior, and safe failure modes in a threat model.
2. Measure airtime, duty cycle, transmit power, antenna configuration, and range
   for each supported region and intended terrain.
3. Run cold/warm boot, GPS loss/reacquisition, radio timeout, malformed input,
   brownout, stale-display, and recovery scenarios followed by the required soak
   tests.

Acceptance criteria: the threat model is reviewed; regional configurations are
traceable to measured settings; the complete hardware acceptance record in the
production-readiness document is attached to a tagged release.

### P4 — release parity

1. Version firmware, wire contracts, configuration schemas, BOMs, and wiring
   diagrams together; publish upgrade/rollback notes.
2. Add CI checks for configuration examples, documentation links, golden
   vectors, and any buildable firmware artifacts.
3. Maintain one release dashboard mapping every common gate and project-specific
   gate to evidence, owner, and last verification date.

Acceptance criteria: a release candidate has no undocumented deployment source
edits, produces reproducible artifacts, links every gate to evidence, and keeps
the **prototype** label until all required P0–P3 criteria for that project pass.

## Deliberate non-parity

- BLE RSSI homing and SOS burst scheduling belong only to the tracker.
- Meshtastic routing and the roof relay belong only to the hiker system.
- Bare-metal, dependency-light LoRa is the Pico locator's defining constraint.

These differences should remain explicit. Shared correctness and release
evidence—not identical features—are the parity goal.
