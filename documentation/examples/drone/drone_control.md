---
title: Drone flight control (drone_control.py)
description: FlightWriteDroneXR872 — a DataTarget PipelineElement that
  sends the NetopSun XR872's continuous stick/action heartbeat over raw
  UDP. Protocol adapted from an already-validated plain Python reference
  implementation. Implemented but not yet wired into any Pipeline
type: concept
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone/drone_control.py
related: [pipeline, pipeline_element, data_source_target, stream, share,
  lease, drone_video]
version: "0.8"
last_updated: 2026-08-27
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

**Current gap**: nothing in this repository produces that frame data,
and no committed PipelineDefinition includes this element at all — see
"Current limitations and roadmap" below.

## For application developers

### Command-line usage

None committed. `FlightWriteDroneXR872` has never been referenced from
a PipelineDefinition, so there is no `aiko_pipeline create ...` example
to run yet. Once a Pipeline includes it (see roadmap), its own
parameters resolve the usual way, for example:

```bash
aiko_pipeline create some_pipeline.json -s 1 -ll debug \
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
  explicitly reversed).

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
  lease and socket.

## For framework developers (internals)

### Design

```
        FlightWriteDroneXR872
        ┌───────────────────────────────────────────────┐
 self:  │ _roll, _pitch, _yaw, _throttle, _flags (locked)│
        │        ▲                          │            │
process_frame()   │                    _send_heartbeat()  │
(sets values)      │                    (timer, 70ms) ────┼──► UDP:7080
        │        ▼                          │            │
 share: │ control_input, requested_action, control_state  │
        │        ▲                                        │
        │        └── Lease (control_lease_time) ──────────┘
        │             expiry → "presumed_lost"
        └───────────────────────────────────────────────┘
```

- **Two independent clocks, deliberately.** The 70ms wire heartbeat
  (device requirement) and the lease-based "presume control lost" check
  (`control_lease_time`, comfortably under the device's ~1s auto-land
  timeout) are separate timers for separate purposes — see the module
  docstring.
- **No telemetry.** The drone reports nothing back on this channel (not
  even battery). `control_state` is therefore a statement about
  *delivery*, not about the drone's actual state, and the code is
  explicit that this uncertainty should be surfaced, not assumed away.
- **Pulsed vs. toggle actions** are two small lookup tables
  (`PULSED_ACTIONS`, `TOGGLE_ACTIONS`) rather than special-cased
  branches, mirroring the reference app's `RxTxProtocol.setTakeOff` /
  `setLanding` behavior.
- **`_pulse_flag()`** self-clears via a one-shot timer pattern
  (add a handler, remove itself the first time it fires) because Aiko's
  `event` module does not yet have a native "fire once after N seconds"
  timer — noted in the code as a known gap tracked in
  [Event](../../concepts/event.md).

### Implementation notes

- Packet layout: `0x66` header, roll/pitch/throttle/yaw each mapped
  `-1..1 → 0..255`, a flag byte, an XOR checksum of bytes 1–5, `0x99`
  footer. Sent via a bare `socket.sendto()` — no handshake, no
  encryption, matching the reference implementation this was adapted
  from.
- A failed send is logged and does **not** extend the control lease —
  so a real network outage correctly drives `control_state` toward
  `presumed_lost` rather than being masked by the timer continuing to
  fire.
- `TODO(nic)` in source: battery/telemetry response-packet parsing is
  unimplemented; the author notes uncertainty about whether it would
  even be actionable given the rest of this channel has no telemetry
  loop today.

### CRC card

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `FlightWriteDroneXR872` | Maintain current stick/flag state from `process_frame()`; send the UDP heartbeat on its own timer; track delivery health via `Lease`; expose `control_input` / `requested_action` / `control_state` as shared state | [DataTarget](../../concepts/data_source_target.md) (base), [PipelineElement](../../concepts/pipeline_element.md), [Lease](../../concepts/lease.md), [Share](../../concepts/share.md) (`ECProducer`), [Event](../../concepts/event.md) (timer handlers) |

## Current limitations and roadmap

This element is functionally complete and its protocol is
known-working, but it is **disconnected from the rest of the
framework**:

1. **No PipelineDefinition references it.** `drone_pipeline.json`
   only contains the video branch (see [drone_video](drone_video.md)).
   There is no committed graph that includes
   `FlightWriteDroneXR872` at all.
2. **No source of `control_input` / `requested_action` exists in this
   repository.** No keyboard, joystick or gamepad PipelineElement
   produces this frame-data shape anywhere in the framework. The
   closest precedent, `xgo_robot/robot_control.py`, lists the same gap
   ("Pitch, roll, yaw: keyboard arrows and display value") as an
   un-implemented `To Do`, not a working example to copy.
3. **Combining with the video Pipeline is an open design question**,
   not just a wiring exercise: video frames arrive at the camera's own
   pace, while flight control needs its strict, independent 70ms/1s
   timing. This element already sidesteps that by driving its own
   timer rather than depending on Stream frame cadence, which suggests
   it can run as a node in *any* graph path independent of upstream
   frame rate — but that has not been exercised, since it has never
   been placed in a graph at all (see next point).
4. **Untested as a graph node.** `start_stream()` / `stop_stream()`
   should be invoked correctly by `PipelineImpl.create_stream()` for
   any local element in a graph path, including this one — but since no
   PipelineDefinition has ever included this element, that has not been
   exercised end-to-end.
5. **No documentation existed for this module before this document** —
   unlike every other example package, which has both a source-level
   `ReadMe.md` and an OKF concept document.

## Related concepts

- [drone_video](drone_video.md) — the camera DataSource this element
  is meant to operate alongside
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  base class contract
- [Pipeline](../../concepts/pipeline.md) — graph model; current
  limitation on multiple independently-clocked sources per graph
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle
- [Lease](../../concepts/lease.md) — the delivery-health failsafe this
  element builds on
- [Share (Eventual Consistency)](../../concepts/share.md) — live
  `control_state` reporting
- [Event](../../concepts/event.md) — timer handlers; the missing
  one-shot timer noted above
