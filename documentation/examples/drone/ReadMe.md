---
title: NetopSun XR872 drone example index
description: Index of the NetopSun XR872 WiFi drone example concept
  documents — the camera DataSource, the flight-control DataTarget, and
  the joystick DataSource that drives it, wired into a single
  two-Graph-Path Pipeline
type: index
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone
related: [pipeline, pipeline_element, data_source_target, stream, xgo_robot]
version: "0.9"
last_updated: 2026-09-11
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
| [drone_video](drone_video.md) | `ImageReadDroneXR872` — camera `DataSource`. Handshake + fragmented-MJPEG reassembly over raw UDP |
| [drone_control](drone_control.md) | `FlightWriteDroneXR872` — flight-control `DataTarget`. Continuous stick/action heartbeat over raw UDP, adapted from an already-validated plain Python reference implementation |
| [controller_input](controller_input.md) | `ControlReadJoystick` — local game-controller `DataSource`. Reads a `pygame` joystick, shapes the axes, and emits the `control_input` / `requested_action` frame data `FlightWriteDroneXR872` expects. Also a standalone `--inspect` CLI for building new controller mappings |

## Example PipelineDefinitions

| PipelineDefinition | Module document(s) | Purpose |
|--------------------|--------------------|---------|
| `drone_pipeline.json` | [drone_video](drone_video.md), [controller_input](controller_input.md), [drone_control](drone_control.md) | Two independent Graph Paths in one PipelineDefinition: camera → display (`ImageReadDroneXR872 → VideoShow`), and joystick → flight control (`ControlReadJoystick → FlightWriteDroneXR872`) |

## Related documentation

- [Pipeline](../../concepts/pipeline.md) — Pipeline/PipelineElement
  graph model; multiple independently-headed sub-graphs (Graph Paths)
  in one PipelineDefinition, selected with `-gp`
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  the base classes all three drone elements extend
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle all three elements rely on
- [xgo_robot example](../xgo_robot/ReadMe.md) — the closest analogue in
  this repository: another physical-robot example
