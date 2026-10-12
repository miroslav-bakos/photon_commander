# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM ``IFilterWheelV2`` over the Seestar's LP filter wheel.

``get_wheel_setting.names`` is ``["dark", "IRCUT", "LP"]`` on firmware 8.46 and
becomes ``FilterWheel.Names`` directly.

**The wire is 1-based and ASCOM is 0-based.** ``get_wheel_position`` returned
``1`` for the first slot. The conversion happens in exactly one place, here, and
is tested in both directions rather than left as a comment.

**On 9.16+ this whole mapping is bypassed.** The device's own Alpaca server
(``ssc.seestar.native``) exposes ``filterwheel/0`` directly — already 0-based,
since it is a conformant ASCOM server rather than the raw wire — and every
member below tries that first (``prefer_native_getters``/``prefer_native_setters``
in ``ssc/alpaca/devices/base.py``). Measured 9.31: ``names`` is
``['Dark', 'IR', 'LP']``, ``focusoffsets`` is ``[0, 0, 0]``. Whatever the
device answers for a ``Position`` write — success or a real ASCOM error — is
what this driver reports; SSC invents nothing on this surface.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from ssc.alpaca.devices.base import AlpacaDevice, Getter, Setter, unimplemented_put
from ssc.alpaca.errors import ValueNotSetError
from ssc.alpaca.wire import RequestParams
from ssc.seestar.models import WheelSetting, WheelState

#: ASCOM reports ``-1`` from ``Position`` while the wheel is in motion.
POSITION_MOVING: Final = -1

#: ``get_wheel_state.state`` while the wheel is turning.
WHEEL_STATE_MOVING: Final = "moving"

#: The wire numbers slots from 1; ASCOM numbers them from 0.
WIRE_POSITION_ORIGIN: Final = 1


def wire_to_ascom(wire_position: int) -> int:
    """1-based device slot -> 0-based ASCOM position."""
    return wire_position - WIRE_POSITION_ORIGIN


def ascom_to_wire(ascom_position: int) -> int:
    """0-based ASCOM position -> 1-based device slot."""
    return ascom_position + WIRE_POSITION_ORIGIN


class FilterWheelDevice(AlpacaDevice):
    """The LP filter wheel."""

    device_type: ClassVar[str] = "filterwheel"
    interface_version: ClassVar[int] = 2
    default_name: ClassVar[str] = "Photon Commander Seestar Filter Wheel"
    default_description: ClassVar[str] = "ZWO Seestar LP filter wheel via Photon Commander"

    async def _names(self) -> tuple[str, ...]:
        payload = await self.backend.call("get_wheel_setting", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_wheel_setting did not return an object")
        setting = WheelSetting.from_payload(payload)
        if not setting.names:
            raise ValueNotSetError("the device did not report the filter wheel's slot names")
        return setting.names

    async def _is_moving(self) -> bool:
        payload = await self.backend.call("get_wheel_state", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_wheel_state did not return an object")
        state = WheelState.from_payload(payload)
        if state.state is None:
            raise ValueNotSetError("the device did not report a filter wheel state")
        return state.state.casefold() == WHEEL_STATE_MOVING

    def build_getters(self) -> dict[str, Getter]:
        return self.prefer_native_getters(
            {
                "focusoffsets": self._get_focus_offsets,
                "names": self._get_names,
                "position": self._get_position,
            }
        )

    async def _get_names(self, _: RequestParams) -> object:
        return list(await self._names())

    async def _get_focus_offsets(self, _: RequestParams) -> object:
        """Zero per slot — ASCOM's own encoding for a wheel with no offsets.

        ``get_wheel_setting.exp_sec`` sits next to ``names`` and is *not* a focus
        offset; it is a per-filter exposure hint. Mapping it here would put
        seconds into a member that means focuser steps.
        """
        return [0] * len(await self._names())

    async def _get_position(self, _: RequestParams) -> object:
        if await self._is_moving():
            return POSITION_MOVING
        payload = await self.backend.call("get_wheel_position", cache=True)
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise ValueNotSetError(f"get_wheel_position returned {payload!r}, not a slot number")
        position = wire_to_ascom(payload)
        slots = len(await self._names())
        if not 0 <= position < slots:
            raise ValueNotSetError(
                f"the device reported wire slot {payload} which is outside its own "
                f"{slots}-slot name list; SSC will not report a position it cannot explain"
            )
        return position

    def build_setters(self) -> dict[str, Setter]:
        return self.prefer_native_setters(
            {
                "position": unimplemented_put(
                    "FilterWheel.Position",
                    "no verb that moves the filter wheel is verified on firmware 8.46; the "
                    "wheel's position is readable but not commandable",
                )
            }
        )


__all__ = [
    "POSITION_MOVING",
    "WHEEL_STATE_MOVING",
    "WIRE_POSITION_ORIGIN",
    "FilterWheelDevice",
    "ascom_to_wire",
    "wire_to_ascom",
]
