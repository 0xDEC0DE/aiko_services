---
title: Drone camera video (drone_video.py)
description: ImageReadDroneXR872 — a DataSource PipelineElement that
  reassembles the NetopSun XR872's fragmented raw-UDP MJPEG stream into
  images. One of two Graph Paths in drone_pipeline.json
type: concept
audience: [developers, end-users]
status: work-in-progress
ste: adapted
source:
  - src/aiko_services/examples/drone/drone_video.py
  - src/aiko_services/examples/drone/drone_pipeline.json
related: [pipeline, pipeline_element, data_source_target, stream,
  drone_control, controller_input, image_io, yolo]
version: "0.9"
last_updated: 2026-09-11
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

**Why to use it**: this is the video half of the drone example, on its
own Graph Path in `drone_pipeline.json`:

```bash
cd /path/to/repo
hatch run aiko_pipeline create --log_level _all --log_mqtt all \
  -r src/aiko_services/examples/drone/drone_pipeline.json
```

Then, in another window, create this Graph Path's Stream:

```bash
hatch run aiko_pipeline update p_drone -s 1 -gp ImageReadDroneXR872
```

## For application developers

### Command-line usage

```bash
hatch run aiko_pipeline update p_drone -s 1 -gp ImageReadDroneXR872 \
  -p ImageReadDroneXR872.device_ip 192.168.28.1
```

`drone_pipeline.json` wires `ImageReadDroneXR872 → VideoShow` on this
Graph Path.

The flight-control side of this same PipelineDefinition is a separate
Graph Path, started independently:

```bash
hatch run aiko_pipeline update p_drone -s 2 -gp ControlReadJoystick
```

See [drone_control](drone_control.md) and
[controller_input](controller_input.md) for that path.

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
  `_XR872FrameExtractor`, which reassembles them into JPEGs
  (checked for `FFD8`/`FFD9` start/end markers) and pushes them onto a
  queue.
- `frame_generator()` drains up to `data_batch_size` queued records per
  call; returns `StreamEvent.NO_FRAME` when the queue is empty.
- `process_frame()` decodes each queued record to an image through
  `bytes_to_image()`, logging and dropping any that fail to decode
  rather than failing the whole Stream.
- `stop_stream()` joins the receive thread, sends the 7-byte stop
  handshake, and closes the socket.

## For framework developers (internals)

### Design

```
   ImageReadDroneXR872                    _XR872FrameExtractor
   ┌───────────────────────┐              ┌───────────────────────┐
   │ start_stream():       │   UDP:7070   │ feed(packet):         │
   │  send start handshake │─────────────►│  reassemble by seq_num│
   │  spawn recv thread ───┼──────────────┼─►  on_frame(jpeg) ────┼──► queue
   │ frame_generator():    │              │  drop frame on gap    │
   │  drain queue          │              │  validate SOI/EOI     │
   │ process_frame():      │              └───────────────────────┘
   │  bytes_to_image()     │
   └───────────────────────┘
```

- **Reassembly state is not thread-safe by design** — the module
  docstring is explicit that `_XR872FrameExtractor` runs entirely
  inside the single receive thread; nothing else touches its buffer,
  position or sequence counters.
- **Any sequence gap silently drops the rest of that frame**,
  there is little point to recovery, logging, etc., since it is assumed
  there is always another frame to consume.
- **Packet-size assumption.** A packet is only accepted if it is
  exactly 1472 bytes, or is the frame's final packet (which may be
  shorter). This assumes the local network MTU is the Ethernet
  default — reasonable since the drone provides the network, but not
  guaranteed to be true if the controller is on the other end of a
  suitably complex network.

### Implementation notes

- `MAX_FRAME_SIZE = 300_000`: a frame exceeding it resets the
   reassembly buffer and drops the in-progress frame rather than growing it.
- `stream.variables["timestamps"]` is hard-coded to a 25 fps clock in
  `process_frame()` — an assumed, not measured, frame rate.

### CRC card

| Class | Responsibilities | Collaborators |
|-------|------------------|---------------|
| `ImageReadDroneXR872` | Send start/stop handshake; run the UDP receive thread; hand off to the frame extractor; decode queued JPEGs to images; report `frame_count` | [DataSource](../../concepts/data_source_target.md) (base), [PipelineElement](../../concepts/pipeline_element.md) (`create_frames()`), `_XR872FrameExtractor` (packet reassembly), `bytes_to_image()` ([image_io](../../elements/media/image_io.md)) |

## Related concepts

- [drone_control](drone_control.md) — the flight-control element that
  now runs alongside this one, on its own Graph Path in the same
  PipelineDefinition
- [controller_input](controller_input.md) — the joystick DataSource
  driving that flight-control Graph Path
- [DataSource / DataTarget](../../concepts/data_source_target.md) —
  base class contract
- [Pipeline](../../concepts/pipeline.md) — graph model; multiple
  independently-headed Graph Paths in one PipelineDefinition
- [Stream](../../concepts/stream.md) — `start_stream()` / `stop_stream()`
  lifecycle, `create_frames()` pacing
