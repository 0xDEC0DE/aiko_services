"""Game controller input for drone commands.

ControlReadJoystick aims to be generic enough to be adapted into a
joystick source for use by other Elements, but for now it is coupled
to the drone flight controls.

All axis/button mappings are defined as PipelineElement parameters
(see "Mapping" below), as a convenience this file may be run
standalone with `--inspect` to see raw axis/button/hat values for
building a Mapping for a new controller.

Controller disconnection is treated as a terminal event: it emits
one final safety frame (zeroed sticks, "land"), then stops the stream.
"""

import contextlib
import dataclasses
import itertools
import os
import struct

import cachetools
from asciimatics.utilities import BoxTool

# Oh pygame, you so silly...
os.environ["SDL_VIDEODRIVER"] = os.environ["SDL_AUDIODRIVER"] = "dummy"
os.environ["SDL_JOYSTICK_ALLOW_BACKGROUND_EVENTS"] = "1"
os.environ["PYGAME_HIDE_SUPPORT_PROMPT"] = "1"

import time
from contextlib import contextmanager
from typing import Iterator, Optional, Tuple

import click
import hid
import pygame
from asciimatics.exceptions import ResizeScreenError
from asciimatics.screen import Screen

import aiko_services as aiko

__all__ = ["ControlReadJoystick"]

_LOGGER = aiko.process.logger(__name__)

# --------------------------------------------------------------------------- #


# NOTE(nic): I still find it bizarre that this is not the default behaviour for dataclasses
@dataclasses.dataclass(frozen=True)
class ConvertsTypes:
    def __post_init__(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if value is None:
                super().__setattr__(field.name, field.default)
            elif not isinstance(value, field.type):
                super().__setattr__(field.name, field.type(value))


@dataclasses.dataclass(frozen=True)
class Mapping(ConvertsTypes):
    """Mapping: which physical axis/button drives which logical control.

    Defaults taken from an Xbox Series X controller.
    """

    roll_axis: int = 2  # Right stick left/right
    pitch_axis: int = 3  # Right stick up/down
    yaw_axis: int = 0  # Left stick left/right
    throttle_axis: int = 1  # Left stick up/down

    takeoff_button: int = 11  # D-Pad Up
    land_button: int = 12  # D-Pad Down
    emergency_button: int = 1  # B Button
    flip_button: int = 10  # R Button

    invert_pitch: bool = True
    invert_yaw: bool = False
    invert_throttle: bool = True

    def validate(self, num_axes: int, num_buttons: int) -> None:
        """Check resolved parameter values against the attached controller."""
        axes = dict(
            roll_axis=self.roll_axis,
            pitch_axis=self.pitch_axis,
            yaw_axis=self.yaw_axis,
            throttle_axis=self.throttle_axis
        )
        buttons = dict(
            takeoff_button=self.takeoff_button,
            land_button=self.land_button,
            emergency_button=self.emergency_button
        )

        self._validate_group(axes, kind="axis", kinds="axes", count=num_axes)
        self._validate_group(buttons, kind="button", kinds="buttons", count=num_buttons)

    def _validate_group(self, assignments: dict, *, kind: str, kinds: str, count: int) -> None:
        by_index = {}
        for name, index in assignments.items():
            if not 0 <= index < count:
                raise ValueError(f"{name}={index} is out of range for a controller with {count} {kinds}")
            by_index.setdefault(index, []).append(name)
        for index, names in by_index.items():
            if len(names) > 1:
                raise ValueError(f"{kind} {index} is assigned to multiple controls: {', '.join(sorted(names))}")


@dataclasses.dataclass(frozen=True)
class Config(ConvertsTypes):
    deadzone: float = 0.08
    expo: float = 0.35
    max_roll: float = 1.0
    max_pitch: float = 1.0
    max_yaw: float = 1.0
    max_throttle: float = 1.0


@contextmanager
def controller_session(index: int = 0) -> Iterator[pygame.joystick.Joystick]:
    pygame.init()
    pygame.joystick.init()
    joystick = None
    try:
        if pygame.joystick.get_count() <= index:
            raise ValueError(
                f"Controller index {index} is unavailable; found {pygame.joystick.get_count()} controller(s)"
            )
        joystick = pygame.joystick.Joystick(index)
        joystick.init()
        yield joystick
    finally:
        if joystick is not None:
            joystick.quit()
        pygame.joystick.quit()
        pygame.quit()


@cachetools.cached(cache=cachetools.TTLCache(maxsize=1, ttl=1.0))
def _enumerate_hid():
    return hid.enumerate()


def enumerate_hid():
    s = set()
    for x in _enumerate_hid():
        s.add((x["vendor_id"], x["product_id"], x["release_number"]))
    return tuple(s)


# --------------------------------------------------------------------------- #
# ControlReadJoystick is a DataSource for a local game controller
#
# parameter: "controller_index"   which pygame joystick to open, default: 0
# parameter: "roll_axis", "pitch_axis", "yaw_axis", "throttle_axis"
# parameter: "takeoff_button", "land_button", "emergency_button", "flip_button"
# parameter: "invert_pitch", "invert_yaw", "invert_throttle"
# parameter: "deadzone", "expo"
# parameter: "max_roll", "max_pitch", "max_yaw", "max_throttle"
#
# Output (via process_frame()):
#   control_input:     {"roll", "pitch", "yaw", "throttle"}, each -1..1
#   requested_action:   "takeoff" | "land" | "emergency_stop" | "flip" | None


class ControlReadJoystick(aiko.DataSource):  # PipelineElement
    DEFAULT_CONTROLLER_INDEX = 0

    def __init__(self, context: aiko.ContextPipelineElement):
        context.set_protocol("control_read_joystick:0")
        context.call_init(self, "PipelineElement", context)

        self.share["connected"] = False
        self.share["control_input"] = dict(roll=0.0, pitch=0.0, yaw=0.0, throttle=0.0)
        self.share["requested_action"] = None

        self._controller_stack: Optional[contextlib.ExitStack] = None
        self.joystick: Optional[pygame.joystick.Joystick] = None

        self.controller_index, _ = self.get_parameter("controller_index", self.DEFAULT_CONTROLLER_INDEX)

        self.mapping = Mapping(
            roll_axis=self.get_parameter("roll_axis")[0],
            pitch_axis=self.get_parameter("pitch_axis")[0],
            yaw_axis=self.get_parameter("yaw_axis")[0],
            throttle_axis=self.get_parameter("throttle_axis")[0],
            takeoff_button=self.get_parameter("takeoff_button")[0],
            land_button=self.get_parameter("land_button")[0],
            emergency_button=self.get_parameter("emergency_button")[0],
            flip_button=self.get_parameter("flip_button")[0],
            invert_pitch=self.get_parameter("invert_pitch")[0],
            invert_yaw=self.get_parameter("invert_yaw")[0],
            invert_throttle=self.get_parameter("invert_throttle")[0],
        )
        self.config = Config(
            deadzone=self.get_parameter("deadzone")[0],
            expo=self.get_parameter("expo")[0],
            max_roll=self.get_parameter("max_roll")[0],
            max_pitch=self.get_parameter("max_pitch")[0],
            max_yaw=self.get_parameter("max_yaw")[0],
            max_throttle=self.get_parameter("max_throttle")[0],
        )

    @classmethod
    def parse_guid(cls, guid):
        _, _, vendor_id, _, product_id, _, release_number, _ = struct.unpack("<8H", bytes.fromhex(guid))
        return vendor_id, product_id, release_number

    def shape_axis(self, value: float) -> float:
        """Apply dead-zone, renormalization, expo, and final clamping."""
        value = max(-1.0, min(1.0, float(value)))
        magnitude = abs(value)
        if magnitude <= self.config.deadzone:
            return 0.0

        # Preserve full-scale response after removing the center dead zone.
        magnitude = (magnitude - self.config.deadzone) / (1.0 - self.config.deadzone)
        # expo=0 is linear; higher values soften the center.
        magnitude = (1.0 - self.config.expo) * magnitude + self.config.expo * magnitude**3
        shaped = magnitude if value >= 0.0 else -magnitude
        return max(-1.0, min(1.0, shaped))

    def axis(self, index: int, *, invert: bool) -> float:
        if not 0 <= index < self.joystick.get_numaxes():
            raise ValueError(f"Axis {index} is unavailable; controller has {self.joystick.get_numaxes()} axes")

        value = self.shape_axis(self.joystick.get_axis(index))
        return -value if invert else value

    def button_down(self, index: int) -> bool:
        return 0 <= index < self.joystick.get_numbuttons() and bool(self.joystick.get_button(index))

    def make_command(self) -> dict:
        """Return the normalized shape expected by FlightWriteDroneXR872."""

        # This treats the throttle axis as a centered axis. If your controller
        # exposes a trigger in [0, 1], remap it with trigger_to_axis().
        values = dict(
            roll=self.axis(self.mapping.roll_axis, invert=False),
            pitch=self.axis(self.mapping.pitch_axis, invert=self.mapping.invert_pitch),
            yaw=self.axis(self.mapping.yaw_axis, invert=self.mapping.invert_yaw),
            throttle=self.axis(self.mapping.throttle_axis, invert=self.mapping.invert_throttle),
        )
        values["roll"] *= self.config.max_roll
        values["pitch"] *= self.config.max_pitch
        values["yaw"] *= self.config.max_yaw
        values["throttle"] *= self.config.max_throttle

        requested_action = None
        if self.button_down(self.mapping.emergency_button):
            requested_action = "emergency_stop"
        elif self.button_down(self.mapping.land_button):
            requested_action = "land"
        elif self.button_down(self.mapping.takeoff_button):
            requested_action = "takeoff"
        elif self.button_down(self.mapping.flip_button):
            requested_action = "flip"

        return dict(control_input=values, requested_action=requested_action)

    def start_stream(self, stream, stream_id):
        stack = contextlib.ExitStack()
        try:
            joystick = stack.enter_context(controller_session(index=self.controller_index))
            self.mapping.validate(joystick.get_numaxes(), joystick.get_numbuttons())
            self.joystick = joystick
            self.guid_data = self.parse_guid(joystick.get_guid())
            self._controller_stack = stack
            self.stopping = False
            self.share["connected"] = True
            self.create_frames(stream, self.frame_generator, rate=50.0)
            return aiko.StreamEvent.OKAY, {}
        except Exception as e:
            stack.close()
            return aiko.StreamEvent.ERROR, {"diagnostic": str(e)}

    def stop_stream(self, stream, stream_id):
        self.share["connected"] = False
        if self._controller_stack is not None:
            self._controller_stack.close()
            self._controller_stack = None
        self.joystick = None
        return aiko.StreamEvent.OKAY, {}

    # NOTE(nic): there's a wacky little Heisenbug where joystick removal
    #  events don't always get caught, and it'll keep transmitting the last
    #  values, forever. As a backstop, enumerate the HID table periodically
    #  and flag a removal if the device disappears.
    def _controller_removed(self) -> bool:
        if self.guid_data not in enumerate_hid():
            return True
        for event in pygame.event.get():
            if event.type == pygame.JOYDEVICEREMOVED:
                return True
        return False

    def frame_generator(self, stream, frame_id):
        if self.stopping:
            return aiko.StreamEvent.STOP, {"diagnostic": "controller disconnected"}

        if self._controller_removed():
            self.logger.warning(f"{self.my_id()}: controller disconnected; sending land and stopping this Stream")
            self.share["connected"] = False
            self.stopping = True
            # Treat removal as an immediate safety event: still deliver one
            # more frame (land, zeroed sticks) before the next call stops.
            return aiko.StreamEvent.OKAY, {
                "control_input": {"roll": 0.0, "pitch": 0.0, "yaw": 0.0, "throttle": 0.0},
                "requested_action": "land",
            }

        # Axis state is sampled continuously via the Pipeline's own frame
        # rate (see create_frames()), so a quiet centered stick remains valid
        return aiko.StreamEvent.OKAY, self.make_command()

    def process_frame(self, stream, control_input, requested_action) -> Tuple[aiko.StreamEvent, dict]:
        if any(x for x in control_input.values()):
            self.logger.info(f"{self.my_id()}: control input = {control_input}")
        if requested_action:
            self.logger.info(f"{self.my_id()}: sending command = {requested_action}")
        self.share["control_input"] = control_input
        self.share["requested_action"] = requested_action
        return aiko.StreamEvent.OKAY, {"control_input": control_input, "requested_action": requested_action}


# --------------------------------------------------------------------------- #


def inspect_controller(joystick: pygame.joystick.Joystick, guid_data) -> None:  # noqa: C901
    """Display raw axis/button/hat data for debugging and building a mapping.

    The values printed here are exactly what "roll_axis", "takeoff_button",
    etc. (see Mapping) refer to -- read them off and pass them to
    "aiko_pipeline create ... -p ControlReadJoystick.<field> <value>".
    """
    _HAT_ARROWS = {
        (0, 1): ("\u2191", "^"),
        (-1, -1): ("\u2199", "/"),
        (0, 0): ("\u2022", "o"),
        (-1, 1): ("\u2196", "\\"),
        (1, 1): ("\u2197", "/"),
        (1, -1): ("\u2198", "\\"),
        (-1, 0): ("\u2190", "<"),
        (1, 0): ("\u2192", ">"),
        (0, -1): ("\u2193", "v"),
    }

    def _draw_box(screen, box, x, y, w, h, title=None, colour=Screen.COLOUR_WHITE):
        if w < 2 or h < 2:
            return

        screen.print_at(box.box_top(w), x, y, colour=colour)
        for row in range(1, h - 1):
            screen.print_at(box.v, x, y + row, colour=colour)
            screen.print_at(box.v, x + w - 1, y + row, colour=colour)
        screen.print_at(box.box_bottom(w), x, y + h - 1, colour=colour)
        if title:
            screen.print_at(f" {title} ", x + 2, y, colour=Screen.COLOUR_CYAN, attr=Screen.A_BOLD)

    def _draw_axis_bar(screen, box, x, y, width, value):
        track = list("\u2500" * width)
        track[width // 2] = box.cross
        screen.print_at("".join(track), x, y, colour=Screen.COLOUR_WHITE)

        magnitude = abs(value)
        colour = (
            Screen.COLOUR_WHITE if magnitude < 0.08 else Screen.COLOUR_YELLOW if magnitude < 0.6 else Screen.COLOUR_RED
        )
        pos = max(0, min(width - 1, int(round((value + 1.0) / 2.0 * (width - 1)))))
        screen.print_at("\u2588", x + pos, y, colour=colour, attr=Screen.A_BOLD)

    def _draw_axes_panel(screen, box, x, y, w, h, axes):
        _draw_box(screen, box, x, y, w, h, title="Axes")
        if not axes:
            screen.print_at("(none)", x + 2, y + 2, colour=Screen.COLOUR_WHITE)
            return

        bar_width = max(4, w - 13)
        row = y + 2
        for i, value in enumerate(axes):
            if row >= y + h - 1:
                break
            screen.print_at(f"{i:>2}", x + 2, row, colour=Screen.COLOUR_WHITE, attr=Screen.A_BOLD)
            _draw_axis_bar(screen, box, x + 5, row, bar_width, value)
            screen.print_at(f"{value:+.2f}", x + 6 + bar_width, row, colour=Screen.COLOUR_WHITE)
            row += 1

    def _draw_buttons_panel(screen, box, x, y, w, h, buttons):
        _draw_box(screen, box, x, y, w, h, title="Buttons")
        if not buttons:
            screen.print_at("(none)", x + 2, y + 2, colour=Screen.COLOUR_WHITE)
            return

        cell_w = 4
        cols = max(1, (w - 4) // cell_w)
        row, col = y + 2, 0
        for i, pressed in enumerate(buttons):
            if row >= y + h - 1:
                break
            cx = x + 2 + col * cell_w
            text = f"{i:>2}"
            if pressed:
                screen.print_at(text, cx, row, colour=Screen.COLOUR_BLACK, bg=Screen.COLOUR_GREEN, attr=Screen.A_BOLD)
            else:
                screen.print_at(text, cx, row, colour=Screen.COLOUR_WHITE)
            col += 1
            if col >= cols:
                col, row = 0, row + 1

    def _draw_hats_panel(screen, box, x, y, w, h, hats):
        _draw_box(screen, box, x, y, w, h, title="Hats")
        if not hats:
            screen.print_at("(none)", x + 2, y + 2, colour=Screen.COLOUR_WHITE)
            return

        row = y + 2
        for i, (hx, hy) in enumerate(hats):
            if row >= y + h - 1:
                break
            active = (hx, hy) != (0, 0)
            colour = Screen.COLOUR_CYAN if active else Screen.COLOUR_WHITE
            attr = Screen.A_BOLD if active else Screen.A_NORMAL
            arrow = _HAT_ARROWS.get((hx, hy), ("?", "?"))[int(not screen.unicode_aware)]
            screen.print_at(f"{i:>2}  {arrow}  ({hx:>2},{hy:>2})", x + 2, row, colour=colour, attr=attr)
            row += 1

    def _draw_hid_panel(screen, box, x, y, w, h, guid_data):
        _draw_box(screen, box, x, y, w, h, title="HID Table")
        column = x + 2
        line = itertools.count(y + 2)
        max = screen.height - 2
        s = set()
        for hid in _enumerate_hid():
            for key in ('path', 'usage_page', 'usage', 'interface_number', 'bus_type'):
                hid.pop(key, None)
            s.add(tuple(hid.items()))

        for hid in s:
            i = next(line)
            if i >= max:
                return
            output = dict(hid)

            if (output["vendor_id"], output["product_id"], output["release_number"]) == guid_data:
                screen.print_at(output, column, i, attr=Screen.A_BOLD, colour=Screen.COLOUR_WHITE)
            else:
                screen.print_at(output, column, i, colour=Screen.COLOUR_WHITE)

    def _inspect(screen: Screen) -> None:
        box = BoxTool(screen.unicode_aware)

        while True:
            pygame.event.pump()

            if screen.has_resized():
                raise ResizeScreenError("Screen resized")

            ev = screen.get_key()
            if ev in (ord("q"), ord("Q")):
                return

            width, height = screen.width, screen.height
            screen.clear_buffer(Screen.COLOUR_WHITE, Screen.A_NORMAL, Screen.COLOUR_BLACK)

            header_h = 4
            _draw_box(screen, box, 0, 0, width, header_h, title="Controller")
            screen.print_at(joystick.get_name(), 2, 1, colour=Screen.COLOUR_WHITE, attr=Screen.A_BOLD)
            screen.print_at(f"GUID: {joystick.get_guid()}", 2, 2, colour=Screen.COLOUR_WHITE)
            vendor_id, product_id, release_number = guid_data
            info = (
                f"Vendor: 0x{vendor_id:04x} ({vendor_id}) "
                f"Product: 0x{product_id:04x} ({product_id}) "
                f"Release: 0x{release_number:04x} ({release_number})"
            )
            screen.print_at(info, max(2, width - len(info) - 2), 1, colour=Screen.COLOUR_WHITE)

            body_y = header_h
            body_h = (height - header_h) // 2
            col_w = width // 3

            axes = [round(joystick.get_axis(i), 3) for i in range(joystick.get_numaxes())]
            buttons = [joystick.get_button(i) for i in range(joystick.get_numbuttons())]
            hats = [joystick.get_hat(i) for i in range(joystick.get_numhats())]

            _draw_axes_panel(screen, box, 0, body_y, col_w, body_h, axes)
            _draw_buttons_panel(screen, box, col_w, body_y, col_w, body_h, buttons)
            _draw_hats_panel(screen, box, col_w * 2, body_y, width - col_w * 2, body_h, hats)
            _draw_hid_panel(screen, box, 0, (height - body_h), width, body_h - 1, guid_data)

            footer = "Move controls / press buttons to see live values.  q to quit."
            screen.print_at(footer.center(width), 0, height - 1, colour=Screen.COLOUR_BLACK, bg=Screen.COLOUR_WHITE)

            screen.refresh()
            time.sleep(0.05)

    while True:
        try:
            Screen.wrapper(_inspect)
            break
        except ResizeScreenError:
            continue
        except KeyboardInterrupt:
            break


@click.command()
@click.option("--inspect", is_flag=True, required=True, help="Print raw inputs for building a controller mapping.")
@click.argument("index", type=int, required=False, default=ControlReadJoystick.DEFAULT_CONTROLLER_INDEX)
def main(inspect: bool, index: int) -> None:
    """Read a controller and print its raw axis/button state for mapping.

    Use the printed axis/button indices as PipelineElement parameters, e.g.

    \b
        aiko_pipeline create some_pipeline.json -s 1  \\
            -p ControlReadJoystick.roll_axis 2  \\
            -p ControlReadJoystick.takeoff_button 11
    """
    try:
        with controller_session(index=index) as joystick:
            inspect_controller(joystick, ControlReadJoystick.parse_guid(joystick.get_guid()))
    except ValueError as e:
        click.echo(e, err=True)
        click.get_current_context().exit(1)


if __name__ == "__main__":
    main()
