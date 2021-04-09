# Copyright (c) 2021 UAVCAN Consortium
# This software is distributed under the terms of the MIT License.
# Author: Pavel Kirienko <pavel@uavcan.org>

from __future__ import annotations
from typing import Iterable, Tuple, Callable, Dict
from collections import defaultdict
import dataclasses
from . import Controller, Sample, ControllerError, ControllerNotFoundError
import yakut
import mido  # type: ignore


_CH_MAX = 127


@dataclasses.dataclass()
class ButtonState:
    down: bool = False
    count: int = 0

    def update(self, down: bool) -> None:
        self.down = down
        self.count += down


class MIDIController(Controller):
    """
    Interface for standard MIDI HID controllers/faders.
    """

    def __init__(self, name: str) -> None:
        try:
            # noinspection PyUnresolvedReferences
            self._port: mido.ports.BaseInput = mido.open_input(name)
        except OSError as ex:
            raise ControllerNotFoundError(f"Cannot open MIDI port {name!r}") from ex
        except Exception as ex:
            raise ControllerError(str(ex)) from ex

        self._description = self._port.name or "?"

        self._analog: Dict[int, float] = {}
        self._buttons: Dict[int, ButtonState] = defaultdict(ButtonState)

    @property
    def description(self) -> str:
        return self._description

    def sample(self) -> Sample:
        if self._port.closed:
            raise ControllerNotFoundError("MIDI port is closed")
        return Sample(
            analog_axes=self._analog.copy(),
            push_buttons={axis: state.down for axis, state in self._buttons.items()},
            toggle_switches={axis: state.count % 2 == 0 for axis, state in self._buttons.items()},
        )

    def close(self) -> None:
        self._port.close()

    def _handle_control_change(self, channel: int, control: int, value: int) -> None:
        # Accept all channels.
        _ = channel
        # From the MIDI 1.0 Spec:
        #   All controller number assignments are designated by agreement between the MMA and JMSC. The numbers listed
        #   in Table III are specified for standard musical instrument applications. However, many non-musical devices
        #   which implement MIDI, such as lighting controllers, may use designated controller numbers at their
        #   discretion.
        # Here, we treat all controls equally regardless of their fixed functions prescribed by the MIDI spec.
        axis = control
        # Automatically differentiate between buttons and sliders by evaluating the value: if only min or max
        # are seen, assume this is a button, otherwise, assume it is a slider. I am not yet sure how sensible
        # this is considering the edge case: when we first receive a slider data and it happens to be either
        # min or max, it will be initially categorized as a button, and any following update will cause this
        # logic to reclassify it as a slider. This may be confusing for users so maybe we should instead
        # treat every channel as both slider AND button?
        if axis in self._analog or value not in (0, _CH_MAX):
            self._analog[axis] = value / _CH_MAX
            self._buttons.pop(axis, None)
        else:
            self._buttons[axis].update(value > (_CH_MAX // 2))

    # noinspection PyUnresolvedReferences
    def _callback(self, msg: mido.Message) -> None:
        try:
            _logger.debug("%s: MIDI message: %s", self, msg)
            if msg.type == "control_change":
                self._handle_control_change(msg.channel, msg.control, msg.value)
        except Exception as ex:  # pragma: no cover
            _logger.exception("%s: MIDI event handler failure: %s", self, ex)

    @staticmethod
    def list_controllers() -> Iterable[Tuple[str, Callable[[], Controller]]]:
        # noinspection PyUnresolvedReferences
        for name in mido.get_input_names():
            yield name, lambda: MIDIController(name)


_logger = yakut.get_logger(__name__)