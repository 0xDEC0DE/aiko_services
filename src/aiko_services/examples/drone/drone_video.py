# drone_video.py
#
# XR872 raw-UDP MJPEG DataSource
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# NetopSun XR872 WiFi drone video transport (not RTSP / HTTP-MJPEG / RTP):
# a 7-byte "start" handshake causes the device to spray JPEG frames as
# fast as it can, fragmented across UDP datagrams with a 4-byte header,
# until a corresponding "stop" handshake is sent. Reverse-engineered
# from the vendor Android app.
#
# There is no "scheme://host:port" grammar that fits this device cleanly
# so this DataSource takes plain parameters instead of a "data_sources"
# URL.  cf., VideoReadWebcam
#
# Usage
# ~~~~~
# aiko_pipeline create drone_pipeline.json -s 1 -ll debug          \
#     -p ImageReadDroneXR872.device_ip 192.168.28.1                \
#     -p ImageReadDroneXR872.video_port 7070                       \
#     -p ImageReadDroneXR872.rxtx_port 7080
#
#  Handshake:
#    Send 7 bytes  CC 5A 01 82 02 36 B7  to start the video stream.
#    Send 7 bytes  CC 5A 01 82 02 37 B4  to stop it.
#
#  Video packets (received on the local "video port", default 7070):
#    4-byte header + payload, max UDP payload size 1472 bytes total:
#      byte[0] = frame ID        (increments per JPEG frame, wraps at 256)
#      byte[1] = last-packet flag (1 == final packet of this frame)
#      byte[2] = packet sequence number within the frame (1-based, wraps at 256)
#      byte[3] = unused/reserved in the original code
#      byte[4:] = JPEG payload chunk
#
#     A packet is only accepted if its total length is exactly 1472 bytes,
#     OR it is the last packet of a frame (which can be shorter).  This assumes
#     that the network MTU will always be the Ethernet default value, but since
#     the drone provides the network we're using, it's a relatively safe bet.
#
#    Frames are reassembled by concatenating payload chunks in sequence
#    order; sequence number 1 starts a new frame. Any gap in the sequence
#    (other than a resync on seq==1) causes the rest of that frame to be
#    dropped silently, matching the original Java behavior.
#
#    A completed frame is only delivered to the callback if it round-trips
#    as a valid JPEG: starts with FF D8 (SOI) and ends with FF D9 (EOI).

import queue
import socket
import threading
from typing import Tuple

import aiko_services as aiko
from aiko_services.elements.media import bytes_to_image

__all__ = ["ImageReadDroneXR872"]

_LOGGER = aiko.process.logger(__name__)

# --------------------------------------------------------------------------- #
# Direct port of XR872VideoFrameDataExtractor.onVideoData() -- reassembles
# the fragmented UDP stream into complete, validated JPEG frames.
#
# Runs entirely inside the receive thread. Reassembly state (buffer
# position, current frame id, last sequence number) is NOT thread-safe


class _XR872FrameExtractor:
    MAX_FRAME_SIZE = 300_000  # matches frameBufferSize in the original
    HEADER_LEN = 4
    FULL_PACKET_SIZE = 1472  # only enforced for non-final packets
    IMAGE_START = b"\xff\xd8"
    IMAGE_END = b"\xff\xd9"

    def __init__(self, on_frame):
        self._on_frame = on_frame  # callback: on_frame(bytes) -> None
        self._buf = bytearray(self.MAX_FRAME_SIZE)
        self._pos = -1
        self._current_frame_id = 0
        self._last_packet_num = 0

    def feed(self, packet: bytes):
        n = len(packet)
        if n < self.HEADER_LEN:
            return

        is_last = packet[1] == 1
        if n != self.FULL_PACKET_SIZE and not is_last:
            return

        frame_id = packet[0]
        seq_num = packet[2]

        if seq_num == 1:
            self._pos = 0
            self._current_frame_id = frame_id
            self._last_packet_num = 1
        else:
            # gap -- silently drop the rest of this frame, matching
            # the original device-side behaviour
            if (self._last_packet_num + 1) % 256 != seq_num:
                return
        self._last_packet_num = seq_num

        if self._current_frame_id != frame_id:
            return  # stray packet from a different frame

        payload_len = n - self.HEADER_LEN
        if payload_len < 0:
            return

        if self._pos + payload_len >= len(self._buf):
            self._pos = 0  # frame too large for the buffer -- reset
            return

        self._buf[self._pos : self._pos + payload_len] = packet[self.HEADER_LEN : n]
        self._pos += payload_len

        if not is_last or self._pos < 2:
            return

        frame = bytes(self._buf[: self._pos])
        if frame.startswith(self.IMAGE_START) and frame.endswith(self.IMAGE_END):
            self._on_frame(frame)


# --------------------------------------------------------------------------- #
# ImageReadDroneXR872 is a DataSource for the XR872 raw-UDP MJPEG transport
#
# parameter: "device_ip"    drone's WiFi AP address, default: 192.168.28.1
# parameter: "video_port"   local port to bind and receive frames on
# parameter: "rxtx_port"    drone's port for the start/stop handshake


class ImageReadDroneXR872(aiko.DataSource):  # PipelineElement
    DEFAULT_DEVICE_IP = "192.168.28.1"
    DEFAULT_VIDEO_PORT = 7070
    DEFAULT_RXTX_PORT = 7080
    RECV_BUFFER_SIZE = 14720  # matches the reference client
    SOCKET_TIMEOUT = 1.0  # lets the receive thread notice termination

    HANDSHAKE_START = b"\xcc\x5a\x01\x82\x02\x36\xb7"
    HANDSHAKE_STOP = b"\xcc\x5a\x01\x82\x02\x37\xb4"

    def __init__(self, context: aiko.ContextPipelineElement):
        context.set_protocol("image_read_drone_xr872:0")
        context.call_init(self, "PipelineElement", context)

        self._sock = None
        self._recv_thread = None
        self._terminate = False
        self._queue = queue.Queue()
        self._extractor = _XR872FrameExtractor(self._queue.put)

        self.share["frame_count"] = 0

    def start_stream(self, stream, stream_id):
        device_ip, _ = self.get_parameter("device_ip", self.DEFAULT_DEVICE_IP)
        video_port, _ = self.get_parameter("video_port", self.DEFAULT_VIDEO_PORT)
        rxtx_port, _ = self.get_parameter("rxtx_port", self.DEFAULT_RXTX_PORT)
        video_port = int(video_port)
        rxtx_port = int(rxtx_port)

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(("0.0.0.0", video_port))
            self._sock.settimeout(self.SOCKET_TIMEOUT)
        except OSError as os_error:
            diagnostic = f"Couldn't bind video port {video_port}: {os_error}"
            return aiko.StreamEvent.ERROR, {"diagnostic": diagnostic}

        self.share["device_ip"] = device_ip
        self.share["video_port"] = video_port
        self.share["rxtx_port"] = rxtx_port

        self._sock.sendto(self.HANDSHAKE_START, (device_ip, rxtx_port))
        self.logger.info(f"{self.my_id()}: sent start handshake to {device_ip}:{rxtx_port}")

        self._terminate = False
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

        self.create_frames(stream, self.frame_generator)
        return aiko.StreamEvent.OKAY, {}

    def stop_stream(self, stream, stream_id):
        self._terminate = True
        if self._recv_thread:
            self._recv_thread.join(timeout=2.0)
            self._recv_thread = None
        if self._sock:
            try:
                device_ip = self.share.get("device_ip", self.DEFAULT_DEVICE_IP)
                rxtx_port = self.share.get("rxtx_port", self.DEFAULT_RXTX_PORT)
                self._sock.sendto(self.HANDSHAKE_STOP, (device_ip, rxtx_port))
                self.logger.info(f"{self.my_id()}: sent stop handshake")
            except OSError:
                pass
            self._sock.close()
            self._sock = None
        return aiko.StreamEvent.OKAY, {}

    def _recv_loop(self):
        while not self._terminate:
            try:
                data, _addr = self._sock.recvfrom(self.RECV_BUFFER_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            if data:
                self._extractor.feed(data)  # may enqueue a completed frame

    def frame_generator(self, stream, frame_id):
        data_batch_size, _ = self.get_parameter("data_batch_size", default=1)
        data_batch_size = int(data_batch_size)

        records = []
        while data_batch_size > 0 and self._queue.qsize():
            data_batch_size -= 1
            records.append(self._queue.get())

        if not records:
            return aiko.StreamEvent.NO_FRAME, {}
        return aiko.StreamEvent.OKAY, {"records": records}

    def process_frame(self, stream, records) -> Tuple[aiko.StreamEvent, dict]:
        stream.variables["timestamps"] = [stream.frame_id * (1 / 25)]
        images = []
        for record in records:
            try:
                images.append(bytes_to_image(record))
            except Exception as exception:
                # SOI/EOI already passed in the extractor -- a decode
                # failure here is unusual; log and drop rather than
                # failing the whole Stream over one bad frame
                self.logger.warning(f"{self.my_id()}: bad JPEG frame " f"({len(record)} bytes): {exception}")
                continue
        self.share["frame_count"] = self.share.get("frame_count", 0) + len(images)
        return aiko.StreamEvent.OKAY, {"images": images}


# --------------------------------------------------------------------------- #
