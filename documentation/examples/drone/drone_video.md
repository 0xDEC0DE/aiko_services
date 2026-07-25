---
title: Drone camera video (drone_video.py)
description: ImageReadDroneXR872 — a DataSource PipelineElement that
  reassembles the NetopSun XR872's fragmented raw-UDP MJPEG stream into
  images. Ported directly from a decompiled Android app; wired into a
  working, committed Pipeline
type: concept
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone/drone_video.py
  - src/aiko_services/examples/drone/drone_pipeline.json
related: [pipeline, pipeline_element, data_source_target, stream,
  drone_control, image_io, yolo]
version: "0.8"
last_updated: 2026-08-27
---

# Drone camera video (drone_video.py)

## Overview

**`ImageReadDroneXR872`** is the camera
[DataSource](../../concepts/data_source_target.md)
[PipelineElement](../../concepts/pipeline_element.md) for the NetopSun
XR872 WiFi drone. It sends a fixed 7-byte handshake over UDP to start
the video stream, then receives JPEG frames fragmented across UDP
datagrams (a 4-byte per-packet header: frame id, last-packet flag,
sequence number, one reserved byte) and reassembles them.

Like [drone_control](drone_control.md), this module's protocol
handling is a direct port of a decompiled Android app
(`XR872VideoFrameDataExtractor.onVideoData()`), and has been
separately validated against a known-working reference implementation.

**Why to use it**: this is the one part of the drone example that
already runs today, end to end:

```bash
cd src/aiko_services/examples/drone
aiko_pipeline create drone_pipeline.json -s 1 -ll debug
```

## For application developers

### Command-line usage

```bash
aiko_pipeline create drone_pipeline.json -s 1 -ll debug \
  -p ImageReadDroneXR872.device_ip 192.168.28.1
```

`drone_pipeline.json` wires
`ImageReadDroneXR872 → ImageConvert → YoloDetector → ImageOverlay →
VideoShow → Metrics` — camera in, object detection overlay, live
display. It carries `_create_stream_` / `_destroy_stream_exit_`
parameters, so `-s 1` both creates the Stream and exits the process
when it is destroyed, the same convention used across the other example
pipelines.

### Public API

| Class | Kind | Inputs → Outputs | Parameters |
|-------|------|-------------------|------------|
| `ImageReadDroneXR872` | DataSource | `records: [bytes]` → `images: [image]` | `device_ip` (default `192.168.28.1`), `video_port` (default `7070`), `rxtx_port` (default `7080`), `data_batch_size` (default `1`) |

Service protocol: `image_read_drone_xr872:0`.

Live shared state:

| Share item | Meaning |
|------------|---------|
| `frame_count` | Running count of successfully decoded frames |
| `device_ip`, `video_port`, `rxtx_port` | Resolved connection parameters |

**Stream lifecycle behavior:**

- `start_stream()` binds a UDP socket on `video_port`, sends the 7-byte
  start handshake to `(device_ip, rxtx_port)`, starts a daemon receive
  thread, and starts `create_frames(stream, self.frame_generator)`.
- The receive thread feeds raw datagrams into an internal
  `_XR872FrameExtractor`, which reassembles complete, validated JPEGs
  (checked for `FFD8`/`FFD9` start/end markers) and pushes them onto a
  queue.
- `frame_generator()` drains up to `data_batch_size` queued records per
  call; returns `StreamEvent.NO_FRAME` when the queue is empty.
- `process_frame()` decodes each queued record to an image via
  `bytes_to_image()`, logging and dropping any that fail to decode
  rather than failing the whole Stream.
- `stop_stream()` joins the receive thread, sends the 7-byte stop
  handshake, and closes the socket.

## For framework developers (internals)

### Design

```
   ImageReadDroneXR872                    _XR872FrameExtractor
   ┌──────────────────────┐              ┌───────────────────────┐
   │ start_stream():       │   UDP:7070   │ feed(packet):         │
   │  send start handshake │─────────────►│  reassemble by seq_num│
   │  spawn recv thread ───┼──────────────┼─►  on_frame(jpeg) ────┼──► queue
   │ frame_generator():    │              │  drop frame on gap    │
   │  drain queue          │              │  validate SOI/EOI     │
   │ process_frame():      │              └───────────────────────┘
   │  bytes_to_image()     │
   └──────────────────────┘
```

- **Reassembly state is not thread-safe by design** — the module
  docstring is explicit that `_XR872FrameExtractor` runs entirely
  inside the single receive thread; nothing else touches its buffer,
  position or sequence counters.
- **Any sequence gap silently drops the rest of that frame**,
  matching the decompiled Java behavior rather than attempting
  recovery or logging (no visibility into how often this triggers in
  practice).
- **Packet-size assumption.** A packet is only accepted if it is
  exactly 1472 bytes, or is the frame's final packet (which may be
  shorter). This assumes the local network MTU is the Ethernet
  default — reasonable since the drone provides the network, but not
  verified against alternate configurations.

### Implementation notes

- `MAX_FRAME_SIZE = 300_000` matches the original app's
  `frameBufferSize`; a frame exceeding it resets the reassembly buffer
  and drops the in-progress frame rather than growing it.
- There is no "take photo" / "start recording" signal to the drone in
  this protocol — per the module docstring, the vendor app's
  photo/record buttons only flash a light on the drone (two flag bits
  carried in the *flight-control* packet, see
  [drone_control](drone_control.md)) and otherwise just grab frames
  from this same local video stream. Any photo/record feature built on
  top of this element would be a client-side concern, not a
  drone-side command.
- `stream.variables["timestamps"]` is hard-coded to a 25 fps clock in
  `process_frame()` — an assumed, not measured, frame rate.

### CRC card

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `ImageReadDroneXR872` | Send start/stop handshake; run the UDP receive thread; hand off to the frame extractor; decode queued JPEGs to images; report `frame_count` | [DataSource](../../concepts/data_source_target.md) (base), [PipelineElement](../../concepts/pipeline_element.md) (`create_frames()`), `_XR872FrameExtractor` (packet reassembly), `bytes_to_image()` ([image_io](../../elements/media/image_io.md)) |

## Current limitations and roadmap

- No hardware-capture-based validation of the reassembly logic is
  recorded anywhere in the repository — it is a direct port of
  decompiled logic, not an independently-verified implementation (contrast
  with [drone_control](drone_control.md)).
- No documentation existed for this module before this document.
- Otherwise, this is the working half of the drone example: it has a
  committed, runnable Pipeline and needs no wiring changes to keep
  functioning as-is.

## Related concepts

- [drone_control](drone_control.md) — the flight-control element this
  video path is meant to run alongside
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  base class contract
- [Pipeline](../../concepts/pipeline.md) — graph model
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle, `create_frames()` pacing
- [image_io](../../elements/media/image_io.md) — `bytes_to_image()`,
  `ImageConvert`, `ImageOverlay` used downstream in `drone_pipeline.json`
- [yolo](../yolo/yolo.md) — `YoloDetector`, used downstream in
  `drone_pipeline.json`
