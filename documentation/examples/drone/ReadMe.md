---
title: NetopSun XR872 drone example index
description: Index of the NetopSun XR872 WiFi drone example concept
  documents — the camera DataSource and the flight-control DataTarget —
  and the current gap between them
type: index
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone
related: [pipeline, pipeline_element, data_source_target, stream, xgo_robot]
version: "0.8"
last_updated: 2026-08-27
---

# NetopSun XR872 drone example index

One concept document per Python module in
`src/aiko_services/examples/drone/`. These modules talk to a NetopSun
XR872 WiFi toy drone (vendor app package `com.netopsun.zerox_air`) as
Aiko Services [PipelineElements](../../concepts/pipeline_element.md).
The source `src/aiko_services/examples/drone/ReadMe.md` introduces the
hardware.

Navigation: [concepts guide](../../concepts/ReadMe.md) ·
[xgo_robot example](../xgo_robot/ReadMe.md)

## Module documents

| Document | Summary |
|----------|---------|
| [drone_video](drone_video.md) | `ImageReadDroneXR872` — camera `DataSource`. Handshake + fragmented-MJPEG reassembly over raw UDP. Wired into a working, committed Pipeline |
| [drone_control](drone_control.md) | `FlightWriteDroneXR872` — flight-control `DataTarget`. Continuous stick/action heartbeat over raw UDP, adapted from an already-validated plain Python reference implementation. **Not wired into any Pipeline** |

## Example PipelineDefinitions

| PipelineDefinition | Module document(s) | Purpose |
|--------------------|--------------------|---------|
| `drone_pipeline.json` | [drone_video](drone_video.md) | Camera → convert → YOLO detect → overlay → display → metrics. Video-only; does not include flight control |

There is no committed PipelineDefinition that includes
`FlightWriteDroneXR872`. See [drone_control](drone_control.md) §
"Current limitations and roadmap" for what is missing to add one.

## Current state, in one sentence

Aiko can watch this drone today; it cannot fly it yet — the
flight-control element is implemented and its protocol is known-working,
but nothing in the repository wires it into a Pipeline or feeds it the
`control_input` / `requested_action` frame data it expects.

## Related documentation

- [Pipeline](../../concepts/pipeline.md) — Pipeline/PipelineElement graph
  model; current limitations on multiple independently-clocked sources
  in one graph
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  the base classes both drone elements extend
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle both elements rely on
- [xgo_robot example](../xgo_robot/ReadMe.md) — the closest analog in
  this repository: another physical-robot example with the same
  input-source gap (keyboard → command) left as an open `To Do`
