---
title: Game controller input (controller_input.py)
description: ControlReadJoystick — a DataSource PipelineElement that
  reads a local game controller (pygame) and emits the control_input /
  requested_action frame data FlightWriteDroneXR872 expects. Also a
  standalone --inspect CLI for building controller mappings
type: concept
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone/controller_input.py
related: [pipeline, pipeline_element, data_source_target, stream,
  drone_control]
version: "0.9"
last_updated: 2026-09-11
---

# Game controller input (controller_input.py)

## Overview

**`ControlReadJoystick`** is a game-controller
[DataSource](../../concepts/data_source_target.md)
[PipelineElement](../../concepts/pipeline_element.md). It reads a local
`pygame` joystick, applies dead-zone/expo shaping and axis
inversion/scaling, and emits `control_input` /
`requested_action` — exactly the frame-data shape
[`FlightWriteDroneXR872`](drone_control.md) expects. Although it is
currently only used to fly the XR872 drone, the module docstring notes
intended to be generic enough to feed other flight- or
motion-control elements later.

The module doubles as a small standalone tool: run it directly with
`--inspect` to see a live terminal view of a controller's raw axes,
buttons and hats, for building a new `Mapping` without editing code.

**Why to use it**: it allows users to fly the drone using their own
controllers, with controls customized to their own ergonomic preferences.

## For application developers

### Command-line usage

As a Pipeline element, `ControlReadJoystick` has no CLI of its own —
`aiko_pipeline` exercises it, and its axis/button mapping is set with
`-p`:

```bash
hatch run aiko_pipeline update p_drone -s 2 -gp ControlReadJoystick \
  -p ControlReadJoystick.roll_axis 2 \
  -p ControlReadJoystick.takeoff_button 11
```

Every `Mapping` and `Config` field defaults to `null` in
`drone_pipeline.json`, meaning "use the Xbox Series X defaults baked
into the `Mapping` dataclass." Only override the parameters your
controller actually needs remapped.

As a standalone script, the same file also provides an inspector for
figuring out those axis/button indices on a controller pygame has not
been mapped for yet:

```bash
hatch run python src/aiko_services/examples/drone/controller_input.py --inspect
```

This opens a full-screen terminal UI (through `asciimatics`) showing live
axis bars, button states, hat positions and the raw HID vendor/product
table for the first detected controller. Press `q` to quit. Read the
indices for whichever stick/button you want off this screen, then pass
them as `-p ControlReadJoystick.<field> <value>` per the example above.

### Public API

| Class | Kind | Inputs → Outputs | Parameters |
|-------|------|-------------------|------------|
| `ControlReadJoystick` | DataSource | none → `control_input`, `requested_action` | `controller_index` (default `0`); `roll_axis`, `pitch_axis`, `yaw_axis`, `throttle_axis` (default `2`, `3`, `0`, `1`); `takeoff_button`, `land_button`, `emergency_button` (default `11`, `12`, `1`); `invert_pitch`, `invert_yaw` (default `False`), `invert_throttle` (default `True`); `deadzone` (default `0.08`), `expo` (default `0.35`); `max_roll`, `max_pitch`, `max_yaw`, `max_throttle` (default `1.0` each) |

Service protocol: `control_read_joystick:0`.

Output shape, delivered every frame at the Stream's `rate` (see
below):

- `control_input`: `{"roll", "pitch", "yaw", "throttle"}`, each
  `-1.0..1.0` after dead-zone removal, expo shaping, inversion and the
  per-axis `max_*` scale.
- `requested_action`: one of `"flip"`, `"takeoff"`, `"land"`,
  `"emergency_stop"`, or `None`. Checked in that priority order
  (emergency beats land beats takeoff beats flip) when more than one mapped
  button is held. `FlightWriteDroneXR872` supports more actions
  (`calibrate`, `headless_on/off`, `light_on/off` — see
  [drone_control](drone_control.md)) that are not yet implemented.

Live shared state:

| Share item | Meaning |
|------------|---------|
| `connected` | Whether a controller session is currently open |
| `control_input` | Last emitted stick values, mirrored from `process_frame()` |
| `requested_action` | Last emitted discrete action |

**Stream lifecycle behavior:**

- `start_stream()` opens a `pygame` joystick session for
  `controller_index` through `controller_session()`, validates the
  configured `Mapping` against the controller's actual axis/button
  counts (raises if an index is out of range or if two logical
  controls share one physical index), and starts
  `create_frames(stream, self.frame_generator, rate=50.0)` — a fixed
  50Hz sample rate, independent of `FlightWriteDroneXR872`'s 70ms send
  interval.
- `frame_generator()` samples the controller each tick through
  `make_command()`. It also polls for disconnection every tick (see
  "Implementation notes"); on the tick disconnection is detected, it
  emits one final safety frame — zeroed sticks, `requested_action:
  "land"` — then returns `StreamEvent.STOP` on the *next* call, ending
  the Stream.
- `process_frame()` logs non-trivial input and mirrors
  `control_input` / `requested_action` into `share`, then passes them
  straight through as frame output.
- `stop_stream()` closes the controller session through the held
  `ExitStack` and clears `connected`.

## For framework developers (internals)

### Design

```
        ControlReadJoystick
        ┌────────────────────────────────────────────────────┐
        │ pygame.joystick ──► shape_axis() ──► make_command() │
        │        │                                  │          │
        │        │  (50Hz, create_frames rate=)      ▼          │
frame_generator()─┼──────────────────────────► control_input,   │
        │        │                             requested_action │
        │        │                                  │          │
        │  _controller_removed() poll              process_frame()
        │   (HID enumerate + JOYDEVICEREMOVED)        │          │
        │        └── on removal: one safety frame     ▼          │
        │             (zeroed sticks, "land"),  share: control_input,
        │             then StreamEvent.STOP     requested_action,│
        │                                         connected      │
        └────────────────────────────────────────────────────┘
```

- **Two disconnect-detection paths, combined.** `pygame`'s
  `JOYDEVICEREMOVED` event is the primary signal, but the module
  docstring/comment notes a "Heisenbug" where that event does not
  always fire; `_controller_removed()` backstops it by re-enumerating
  the OS HID table each tick and checking whether the controller's
  `(vendor_id, product_id, release_number)` GUID is still present.
  `_enumerate_hid()` is cached/rate-limited at 1 second intervals so
  this backstop does not hammer the HID subsystem at 50Hz.
- **Disconnection is terminal, not transient.** There is no
  reconnect-and-resume logic — once `_controller_removed()` trips, the
  element sends exactly one more (safety) frame and then stops its own
  Stream. A fresh Stream must be created to resume.
- **Axis shaping order matters.** `shape_axis()` removes the dead zone
  first, *then* renormalizes the remaining range back to `-1..1`
  before applying expo — so `deadzone` does not silently shrink the
  usable stick travel the way clamping-without-renormalizing would.

### Implementation notes

- `SDL_VIDEODRIVER` / `SDL_AUDIODRIVER` are forced to `"dummy"` at
  import time so `pygame` can read joystick state headlessly, without
  a display or audio device — needed since this runs inside a Pipeline
  process, not a game loop.
- `parse_guid()` decodes `pygame`'s 32-character joystick GUID as 8
  little-endian `uint16`s to recover `(vendor_id, product_id,
  release_number)` — the same triple `_controller_removed()` looks for
  in the `hid.enumerate()` table, since `pygame` and `hid` do not share
  an identifier space directly.
- The throttle axis is treated as centered (`-1..1`), matching typical
  dual-stick layouts; the module docstring notes that a trigger
  exposed as `0..1` would need remapping before reaching
  `make_command()` — no such remap is implemented here.

### CRC card

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `ControlReadJoystick` | Open/validate/close a `pygame` joystick session; sample and shape axes on a fixed-rate timer; detect controller removal through two independent signals and end the Stream safely; expose `control_input`/`requested_action`/`connected` as shared state | [DataSource](../../concepts/data_source_target.md) (base), [PipelineElement](../../concepts/pipeline_element.md) (`create_frames()`), `Mapping`/`Config` (dataclasses), `pygame.joystick`, `hid` (removal backstop) |
| `Mapping` | Hold and validate which physical axis/button drives which logical control | `ConvertsTypes` |
| `Config` | Hold dead-zone/expo/scale tuning | `ConvertsTypes` |

## Current limitations and roadmap

1. **Only four actions are mapped.** `flip`, `takeoff_button`, `land_button`
   and `emergency_button` are the only buttons this element reads;
   `calibrate`, `headless_on/off` and `light_on/off` — all
   supported by `FlightWriteDroneXR872` — have no `Mapping` field and
   are unreachable from a controller today.
2. **No reconnect-and-resume.** A controller unplugged mid-flight ends
   the Stream permanently (after the one safety frame); resuming
   requires creating a new Stream for this Graph Path, which is not
   automated.
3. **Single controller only**, selected once at `start_stream()` through
   `controller_index`; there is no hot-swap between multiple attached
   controllers within one Stream.

## Related concepts

- [drone_control](drone_control.md) — `FlightWriteDroneXR872`, the
  DataTarget this element's output is wired to
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  base class contract
- [Pipeline](../../concepts/pipeline.md) — graph model; this element's
  Graph Path runs independently of the video Graph Path in the same
  PipelineDefinition
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle, `create_frames()` fixed-rate pacing
- [Share (Eventual Consistency)](../../concepts/share.md) — live
  `connected` / `control_input` / `requested_action` reporting
