# ⚠️ READ THIS BEFORE BUILDING OR OPERATING ANY PART OF THIS PROJECT

These are **location / tracking** radio builds. They read GPS and broadcast
position over LoRa and/or BLE, and can be used to locate people, vehicles, or
gear.

## Consent & lawful use only

- Attach a tracker only to **your own** property, or to a person who **knows about
  and has agreed** to carry it.
- **Covert or non-consensual tracking of a person is illegal in many
  jurisdictions** and is harmful regardless of legality. Do not do it.
- You are solely responsible for ensuring your use is lawful where you are.

## Radio regulations

- 915 MHz is a region-specific ISM band (868 MHz in the EU, other allocations
  elsewhere). **Frequency, transmit power, and duty cycle are regulated.**
- Set the radio's frequency/power to match your local regulations before
  transmitting. Operating out of band or over the power/duty-cycle limit can be
  illegal and can interfere with other users.

## No warranty

Provided **as-is, without warranty of any kind**, express or implied. The authors
and copyright holders are **not liable** for any claim, damages, interference,
data loss, legal consequence, or other harm arising from use or misuse.

## Reconstructed code — verify before trusting

This is reconstructed from conversation history. Register values, pin
assignments, timing, and Meshtastic packet formats **must be verified against
your actual hardware** before you rely on any of it (see each project's README
for the specific unverified pieces).

**If you are not certain your use is legal and consensual, do not build or operate
this.**
