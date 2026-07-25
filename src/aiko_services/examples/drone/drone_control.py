# XR872 flight-control DataTarget
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# NetopSun XR872 WiFi drone flight-control transport: plain UDP, one
# port (default 7080), no handshake. The wire packet is a live, raw
# stick reading (roll/pitch/yaw/throttle) plus discrete action flags
# (takeoff/land/flip/...), sent continuously about every 70ms -- the
# drone auto-lands and stops listening for control input if packets
# stop arriving for roughly a second.
#
# IMPORTANT: "roll/pitch/yaw/throttle" are NOT a setpoint the device
# pursues -- they are a momentary joystick reading relayed as-is, every
# send, with no ramping or persistence semantics of its own. There is
# no telemetry of any kind (not even battery, in this implementation),
# so this element can never know the drone's actual attitude or
# position -- only what it last told the drone to do. "control_state"
# below surfaces that uncertainty rather than assuming it away.
#
# parameter: "device_ip"           drone's WiFi AP address,
#                                   default: 192.168.28.1
# parameter: "rxtx_port"           drone's flight-control port,
#                                   default: 7080
# parameter: "send_interval"       heartbeat period in seconds,
#                                   default: 0.07 (matches the device)
# parameter: "control_lease_time"  seconds of failed sends before
#                                   "control_state" flips to
#                                   "presumed_lost", default: 0.9
#                                   (comfortably under the device's
#                                   ~1s auto-land timeout)
#
# Frame data (via process_frame()):
#   control_input:     {"roll", "pitch", "yaw", "throttle"}, each -1..1
#   requested_action:   one of "takeoff", "land", "flip", "calibrate",
#                        "emergency_stop", "headless_on", "headless_off",
#                        "light_on", "light_off" -- or absent
#
# TODO(nic): Add response parsing (battery response packet).  Unsure how that
#  information would be useful/actionable, though.

"""
The drone control protocol was lifted from a decompiled Android app
(package com.netopsun.zerox_air; classes com.netopsun.xr872devices.*,
com.netopsun.rxtxprotocol.simple_drone_protocol.SimpleDroneProtocol,
com.netopsun.drone.activitys.ControlActivity).

HOW THE REAL APP TALKS TO THIS HARDWARE
-----------------------------------------
com.netopsun.drone.DevicesUtil.initDevices() picks which backend
to use based on what IP your phone's WiFi client gets from the
drone's AP. A gateway of 192.168.28.1 selects XR872Devices, which
is the simplest of the several backends in this app:

  - Flight control: plain UDP, both directions, port 7080. No auth,
    no encryption (XR872RxTxCommunicator.send() is a bare
    DatagramSocket.send()
  - Video:  plain UDP, both directions, on port 7070. No auth, no encryption
    sending a known "start" handshake packet will cause it to spew
    JPEG frames in return until a corresponding "stop" handshake
    packet is sent.  The caller is responsible for reassembling the
    frames into JPEG images.
  - There is NO separate "take photo" / "start record" command sent
    to the drone for this module — in ControlActivity, both of those
    are done by grabbing frames from the local video stream.  The
    only drone-facing signal is two bits in the control packet
    (openDroneTakePhotoLight / openDroneRecordLight) which just
    flash a light on the drone to notify humans that they are being
    recorded; they don't trigger anything drone-side.  Included
    below as flags for completeness.

PACKET FORMAT (SimpleDroneProtocol, model flag "HTS_UFO")
-----------------------------------------------------------
8 bytes, sent continuously (app re-sends every 70ms):

    byte 0        : 0x66 (fixed)
    byte 1        : roll     -> int(clamp(roll,  -1, 1) * 127 + 127)
    byte 2        : pitch    -> int(clamp(pitch, -1, 1) * 127 + 127)
    byte 3        : throttle -> int(clamp(throttle, -1, 1) * 127 + 127)
    byte 4        : yaw      -> int(clamp(yaw, -1, 1) * 127 + 127)
    byte 5        : flag bits (see _flags_byte below)
    byte 6        : XOR checksum of bytes[1:6] (inclusive of byte 5)
    byte 7        : 0x99 (fixed)

Flag bits (bytes[5], from SimpleDroneProtocol.fill_HTS_UFO_flag):
    0x01 takeoff
    0x02 landing
    0x04 emergency stop
    0x08 turn over / flip 360
    0x10 headless mode
    0x20 fly-back ("return to me" style feature, exact semantics unconfirmed)
    0x40 open light
    0x80 calibration

You must keep sending packets continuously; the drone will auto-land/cut
motors if control packets stop arriving for ~1 second, and the app's
own reconnect/heartbeat logic (isReconnectIfNoReceive) assumes a
steady stream.
"""

import socket
import threading
from typing import Tuple

import aiko_services as aiko
from aiko_services.main import event
from aiko_services.main.lease import Lease

__all__ = ["FlightWriteDroneXR872"]

_LOGGER = aiko.process.logger(__name__)


def _axis_byte(value: float) -> int:
    value = max(-1.0, min(1.0, float(value)))
    return max(0, min(255, int(round(value * 127 + 127))))


def _xor_checksum(data: bytes) -> int:
    checksum = data[0]
    for byte in data[1:]:
        checksum ^= byte
    return checksum


# --------------------------------------------------------------------------- #


class FlightWriteDroneXR872(aiko.DataTarget):  # PipelineElement
    DEFAULT_DEVICE_IP = "192.168.28.1"
    DEFAULT_RXTX_PORT = 7080
    DEFAULT_SEND_INTERVAL = 0.07  # 70ms, matches the device's expectation
    DEFAULT_CONTROL_LEASE_TIME = 0.9  # under the device's ~1s auto-land

    PACKET_HEADER = 0x66
    PACKET_FOOTER = 0x99

    FLAG_TAKEOFF = 0x01
    FLAG_LANDING = 0x02
    FLAG_EMERGENCY_STOP = 0x04
    FLAG_TURN_OVER_360 = 0x08
    FLAG_HEADLESS = 0x10
    FLAG_FLYBACK = 0x20
    FLAG_OPEN_LIGHT = 0x40
    FLAG_CALIBRATION = 0x80

    PULSE_DURATION = 0.3  # matches the reference app's flag-pulse timing

    # One-shot actions that latch briefly then self-clear, vs toggles that
    # stay however they were last set
    PULSED_ACTIONS = {
        "takeoff": FLAG_TAKEOFF,
        "land": FLAG_LANDING,
        "flip": FLAG_TURN_OVER_360,
        "calibrate": FLAG_CALIBRATION,
        "emergency_stop": FLAG_EMERGENCY_STOP,
    }
    TOGGLE_ACTIONS = {
        "headless_on": (FLAG_HEADLESS, True),
        "headless_off": (FLAG_HEADLESS, False),
        "light_on": (FLAG_OPEN_LIGHT, True),
        "light_off": (FLAG_OPEN_LIGHT, False),
    }

    def __init__(self, context: aiko.ContextPipelineElement):
        context.set_protocol("flight_write_drone_xr872:0")
        context.call_init(self, "PipelineElement", context)

        self._sock = None
        self._device_ip = None
        self._rxtx_port = None
        self._send_interval = self.DEFAULT_SEND_INTERVAL
        self._send_timer_handler = None
        self._control_lease = None
        self._lock = threading.Lock()

        self._roll = 0.0
        self._pitch = 0.0
        self._yaw = 0.0
        self._throttle = 0.0
        self._flags = 0

        # Share is the honest, live surface of what this element is doing --
        # "control_input"/"requested_action" are explicitly a relayed stick
        # reading and discrete requests, NOT a setpoint; "control_state"
        # reflects OUR delivery health, not the drone's, since there is no
        # telemetry to know the drone's actual state
        self.share["control_input"] = {
            "roll": 0.0,
            "pitch": 0.0,
            "yaw": 0.0,
            "throttle": 0.0,
        }
        self.share["requested_action"] = None
        self.share["control_state"] = "not_connected"

    def start_stream(self, stream, stream_id):
        device_ip, _ = self.get_parameter("device_ip", self.DEFAULT_DEVICE_IP)
        rxtx_port, _ = self.get_parameter("rxtx_port", self.DEFAULT_RXTX_PORT)
        send_interval, _ = self.get_parameter("send_interval", self.DEFAULT_SEND_INTERVAL)
        control_lease_time, _ = self.get_parameter("control_lease_time", self.DEFAULT_CONTROL_LEASE_TIME)

        self._device_ip = device_ip
        self._rxtx_port = int(rxtx_port)
        self._send_interval = float(send_interval)

        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        except OSError as os_error:
            diagnostic = f"Couldn't create flight-control socket: {os_error}"
            return aiko.StreamEvent.ERROR, {"diagnostic": diagnostic}

        self.share["device_ip"] = device_ip
        self.share["rxtx_port"] = self._rxtx_port

        # The 70ms wire heartbeat and the "presume control lost" claim are
        # deliberately two different clocks -- see module docstring.
        # Not using "immediate=True": event.md documents that flag as
        # currently not firing immediately, so relying on it would be
        # assuming a broken behaviour works -- first packet goes out after
        # one send_interval instead, which is immaterial at this timescale
        self._send_timer_handler = self._send_heartbeat
        event.add_timer_handler(self._send_timer_handler, self._send_interval)

        self._control_lease = Lease(
            float(control_lease_time),
            self.my_id(all=True),
            lease_expired_handler=self._control_lease_expired,
            automatic_extend=False,
        )
        self.share["control_state"] = "commanded"

        return aiko.StreamEvent.OKAY, {}

    def stop_stream(self, stream, stream_id):
        # Fire a "land" command on teardown; if it doesn't arrive, the drone
        # should notice the disconnect and land anyway about ~1sec later.
        self._set_flag(self.FLAG_LANDING, True)
        self._send_heartbeat()

        if self._send_timer_handler:
            event.remove_timer_handler(self._send_timer_handler)
            self._send_timer_handler = None
        if self._control_lease:
            self._control_lease.terminate()
            self._control_lease = None
        if self._sock:
            self._sock.close()
            self._sock = None
        self.share["control_state"] = "not_connected"
        return aiko.StreamEvent.OKAY, {}

    def process_frame(self, steam, control_input=None, requested_action=None) -> Tuple[aiko.StreamEvent, dict]:
        if control_input:
            with self._lock:
                self._roll = control_input.get("roll", self._roll)
                self._pitch = control_input.get("pitch", self._pitch)
                self._yaw = control_input.get("yaw", self._yaw)
                self._throttle = control_input.get("throttle", self._throttle)
            self.share["control_input"] = {
                "roll": self._roll,
                "pitch": self._pitch,
                "yaw": self._yaw,
                "throttle": self._throttle,
            }

        if requested_action:
            self.share["requested_action"] = requested_action
            if requested_action in self.PULSED_ACTIONS:
                self._pulse_flag(self.PULSED_ACTIONS[requested_action])
            elif requested_action in self.TOGGLE_ACTIONS:
                flag, on = self.TOGGLE_ACTIONS[requested_action]
                self._set_flag(flag, on)
            else:
                self.logger.warning(f"{self.my_id()}: unrecognised requested_action: " f"{requested_action}")

        return aiko.StreamEvent.OKAY, {}

    # -------------------------------------------------------------- flags

    def _set_flag(self, flag, on):
        with self._lock:
            if on:
                self._flags |= flag
            else:
                self._flags &= ~flag

    def _pulse_flag(self, flag):
        """Set a flag, then clear it after PULSE_DURATION -- matches the
        reference app's RxTxProtocol.setTakeOff/setLanding behaviour.

        Ported onto Aiko's own Event timer.  There's no "count=N"
        auto-expiring timer yet (event.md lists it as a known gap),
        so this follows the documented one-shot idiom: a periodic
        handler that removes itself the first time it fires.
        """
        self._set_flag(flag, True)

        def _clear():
            self._set_flag(flag, False)
            event.remove_timer_handler(_clear)

        event.add_timer_handler(_clear, self.PULSE_DURATION)

    # ---------------------------------------------------------- heartbeat

    def _send_heartbeat(self):
        with self._lock:
            roll, pitch, throttle, yaw, flags = (
                self._roll,
                self._pitch,
                self._throttle,
                self._yaw,
                self._flags,
            )

        packet = bytearray(8)
        packet[0] = self.PACKET_HEADER
        packet[1] = _axis_byte(roll)
        packet[2] = _axis_byte(pitch)
        packet[3] = _axis_byte(throttle)
        packet[4] = _axis_byte(yaw)
        packet[5] = flags
        packet[6] = _xor_checksum(bytes(packet[1:6]))
        packet[7] = self.PACKET_FOOTER

        try:
            self._sock.sendto(bytes(packet), (self._device_ip, self._rxtx_port))
        except OSError as os_error:
            self.logger.warning(f"{self.my_id()}: heartbeat send failed: {os_error}")
            return  # don't extend the control lease on a failed send

        if self._control_lease:
            self._control_lease.extend()
        if self.share.get("control_state") == "presumed_lost":
            self.share["control_state"] = "commanded"
            self.logger.info(f"{self.my_id()}: heartbeat delivery resumed")

    def _control_lease_expired(self, lease_uuid):
        self.share["control_state"] = "presumed_lost"
        self.logger.warning(
            f"{self.my_id()}: heartbeat delivery stalled -- drone should "
            f"be presumed to have auto-landed and stopped listening"
        )


# --------------------------------------------------------------------------- #
