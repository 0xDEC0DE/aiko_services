---
title: Drone flight control (drone_control.py)
description: FlightWriteDroneXR872 — a DataTarget PipelineElement that
  sends the NetopSun XR872's continuous stick/action heartbeat over raw
  UDP. Protocol adapted from an already-validated plain Python reference
  implementation. Now wired into drone_pipeline.json, driven by
  ControlReadJoystick
type: concept
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone/drone_control.py
related: [pipeline, pipeline_element, data_source_target, stream, share,
  lease, drone_video, controller_input]
version: "0.9"
last_updated: 2026-09-11
---

# Drone flight control (drone_control.py)

## Overview

**`FlightWriteDroneXR872`** is the flight-control
[DataTarget](../../concepts/data_source_target.md)
[PipelineElement](../../concepts/pipeline_element.md) for the NetopSun
XR872 WiFi drone. It sends an 8-byte UDP packet — roll, pitch, yaw,
throttle plus discrete action flags — to the drone continuously, about
every 70ms, matching the device's own heartbeat expectation. If packets
stop arriving for roughly a second, the drone auto-lands and stops
listening for control input.

The wire protocol was adapted onto Aiko from a decompiled Android
app, the packet format and timing here are known-working against
real hardware.

**Why to use it**: drive the drone from Pipeline frame data —
`control_input` for continuous stick values, `requested_action` for
one-shot or toggle commands:

```python
element.process_frame(stream,
    control_input={"roll": 0.2, "pitch": 0.0, "yaw": 0.0, "throttle": 0.5},
    requested_action="takeoff")
```

`drone_pipeline.json` wires this element up to
[`ControlReadJoystick`](controller_input.md), which produces exactly
this frame-data shape from a local game controller. See "Current
limitations and roadmap" below for what still is not covered.

## For application developers

### Command-line usage

`FlightWriteDroneXR872` is the tail of its own Graph Path,
`(ControlReadJoystick FlightWriteDroneXR872)`, in `drone_pipeline.json`.
Start an MQTT bus and the registrar, create the Pipeline, then create
that Graph Path's Stream in a separate window:

```bash
hatch run aiko_pipeline create --log_level _all --log_mqtt all \
  -r src/aiko_services/examples/drone/drone_pipeline.json

hatch run aiko_pipeline update p_drone -s 2 -gp ControlReadJoystick
```

`FlightWriteDroneXR872`'s own parameters resolve the usual way, for
example to point at a drone on a non-default address:

```bash
hatch run aiko_pipeline update p_drone -s 2 -gp ControlReadJoystick \
  -p FlightWriteDroneXR872.device_ip 192.168.28.1
```

### Public API

| Class | Kind | Inputs → Outputs | Parameters |
|-------|------|-------------------|------------|
| `FlightWriteDroneXR872` | DataTarget | `control_input`, `requested_action` → none | `device_ip` (default `192.168.28.1`), `rxtx_port` (default `7080`), `send_interval` (default `0.07` seconds), `control_lease_time` (default `0.9` seconds) |

Service protocol: `flight_write_drone_xr872:0`.

`process_frame(stream, control_input=None, requested_action=None)`:

- `control_input` is `{"roll", "pitch", "yaw", "throttle"}`, each
  `-1.0..1.0`. It is a **momentary stick reading relayed as-is**, not a
  setpoint the drone pursues — there is no ramping or persistence
  applied on top of it.
- `requested_action` is one of: `takeoff`, `land`, `flip`, `calibrate`,
  `emergency_stop` (pulsed — set then auto-cleared after 0.3 seconds,
  matching the reference app's flag-pulse timing), or `headless_on` /
  `headless_off` / `light_on` / `light_off` (toggles — held until
  explicitly reversed). `ControlReadJoystick` currently only ever
  requests `flip`, `takeoff`, `land` or `emergency_stop` — the calibrate/
  headless/light actions are reachable through `process_frame()` but
  have no controller button mapped to them yet.

Live shared state:

| Share item | Meaning |
|------------|---------|
| `control_input` | Last-applied stick values (honest mirror of what was sent, not the drone's actual attitude) |
| `requested_action` | Last-requested discrete action |
| `control_state` | `not_connected` \| `commanded` \| `presumed_lost` — reflects **this element's delivery health**, not the drone's telemetry (there is none) |
| `device_ip`, `rxtx_port` | Resolved connection parameters |

**Stream lifecycle behavior:**

- `start_stream()` opens a UDP socket, starts a repeating timer
  (`send_interval` seconds) that calls `_send_heartbeat()`, and starts a
  [Lease](../../concepts/lease.md) (`control_lease_time` seconds,
  `automatic_extend=False`) whose expiry flips `control_state` to
  `presumed_lost`. Every successful send extends the lease and, if
  currently `presumed_lost`, flips it back to `commanded`.
- `process_frame()` updates the element's internal stick/flag state
  under a lock; the next heartbeat tick picks it up. It does not send a
  packet itself — sending is entirely timer-driven, decoupled from
  Pipeline Frame delivery.
- `stop_stream()` fires a `land` flag once, then tears down the timer,
  lease and socket. `ControlReadJoystick` also sends its own `land`
  frame on controller disconnect — see [controller_input](controller_input.md)
  — so a dropped controller and an explicit Stream teardown both end in
  a landing command reaching this element.

## For framework developers (internals)

### Design
```text
                         FlightWriteDroneXR872
        +-------------------------------------------------------+
self:   | _roll, _pitch, _yaw, _throttle, _flags (locked)       |
        |                                                       |
        |   +------------------+       +---------------------+  |
        |   | process_frame()  |       | _send_heartbeat()   |  |
        |   | sets values      |       | timer, 70ms         |--+--> UDP:7080
        |   +--------+---------+       +----------+----------+  |
        |            |                            |             |
        |            v                            |             |
        |   +------------------+                  |             |
share:  |   | control_input    |<-----------------+             |
        |   | requested_action |                                |
        |   | control_state    |                                |
        |   +--------+---------+                                |
        |            ^                                          |
        |            |                                          |
        |   Lease (control_lease_time)                          |
        |   expiry -> "presumed_lost"                           |
        +-------------------------------------------------------+
```

- **Two independent clocks, deliberately.** The 70ms wire heartbeat
  (device requirement) and the lease-based "presume control lost" check
  (`control_lease_time`, comfortably under the device's ~1s auto-land
  timeout) are separate timers for separate purposes — see the module
  docstring. This is also why the element sits comfortably on its own
  Graph Path: it never depends on the cadence of frames arriving from
  `ControlReadJoystick` (nominally 50Hz, see
  [controller_input](controller_input.md)) matching its own 70ms send
  interval.
- **No telemetry.** The drone reports nothing back on this channel (not
  even battery). `control_state` is therefore a statement about
  *delivery*, not about the drone's actual state, and the code is
  explicit that this uncertainty should be surfaced, not assumed away.
- **Pulsed vs. toggle actions** are two small lookup tables
  (`PULSED_ACTIONS`, `TOGGLE_ACTIONS`) rather than special-cased
  branches, mirroring the reference app's `RxTxProtocol.setTakeOff` /
  `setLanding` behavior.
- **`_pulse_flag()`** self-clears through a one-shot timer pattern
  (add a handler, remove itself the first time it fires) because Aiko's
  `event` module does not yet have a native "fire once after N seconds"
  timer — noted in the code as a known gap tracked in
  [Event](../../concepts/event.md).

### Implementation notes

- Packet layout: `0x66` header, roll/pitch/throttle/yaw each mapped
  `-1..1 → 0..255`, a flag byte, an XOR checksum of bytes 1–5, `0x99`
  footer. Sent through a bare `socket.sendto()` — no handshake, no
  encryption, matching the reference implementation this was adapted
  from.
- A failed send is logged and does **not** extend the control lease —
  so a real network outage correctly drives `control_state` toward
  `presumed_lost` rather than being masked by the timer continuing to
  fire.
- Battery/telemetry response-packet parsing is unimplemented
  (`TODO(nic)` in source).

### CRC card

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `FlightWriteDroneXR872` | Maintain current stick/flag state from `process_frame()`; send the UDP heartbeat on its own timer; track delivery health through `Lease`; expose `control_input` / `requested_action` / `control_state` as shared state | [DataTarget](../../concepts/data_source_target.md) (base), [PipelineElement](../../concepts/pipeline_element.md), [Lease](../../concepts/lease.md), [Share](../../concepts/share.md) (`ECProducer`), [Event](../../concepts/event.md) (timer handlers) |

## Current limitations and roadmap

The element is now wired end to end, but a few gaps remain:

1. **Only four requested_action values are reachable from the
   controller.** `calibrate`, `headless_on/off` and `light_on/off`
   are implemented here and callable directly through `process_frame()`,
   but `ControlReadJoystick`'s `Mapping` has no button fields for
   them yet — see [controller_input](controller_input.md).
2. **The two Graph Paths are independently selected, not combined.**
   `drone_pipeline.json` still declares video (`ImageReadDroneXR872`)
   and flight control (`ControlReadJoystick`) as two separate
   Graph-Path heads, each started with its own `-gp` and `-s` Stream id
   — not a single merged graph. That sidesteps the differing frame
   cadences cleanly (each Graph Path keeps its own pace), but means
   there is no single Stream whose lifecycle covers "the drone
   session" as a whole; each side is created and torn down separately,
   and tearing down one path while the other keeps running is
   untested.

## Related concepts

- [drone_video](drone_video.md) — the camera DataSource this element
  is meant to operate alongside
- [controller_input](controller_input.md) — `ControlReadJoystick`, the
  DataSource now feeding this element's `control_input` /
  `requested_action`
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  base class contract
- [Pipeline](../../concepts/pipeline.md) — graph model; multiple
  independently-headed Graph Paths in one PipelineDefinition
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle
- [Lease](../../concepts/lease.md) — the delivery-health failsafe this
  element builds on
- [Share (Eventual Consistency)](../../concepts/share.md) — live
  `control_state` reporting
- [Event](../../concepts/event.md) — timer handlers; the missing
  one-shot timer noted above
