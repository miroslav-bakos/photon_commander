# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM ``IFocuserV3`` over the Seestar's ZWO GPIO EAF.

``get_focuser_state`` reports ``max_step_range: 2600`` and a state string;
``get_focuser_position`` reports the absolute position (1209 when observed). Both
are read; neither is hardcoded.

Motion is absent: ``auto_focus`` is in the device's own operation list, but
neither ``start_auto_focus`` nor any absolute-move verb is verified on firmware
8.46 — ``stop_auto_focus`` was probed and returned 103 — so ``Move`` and ``Halt``
answer ``NotImplemented``.

**On 9.16+ every member below tries the device's own Alpaca server first**
(``prefer_native_getters``/``prefer_native_setters``, ``ssc/alpaca/devices/base.py``)
— ``focuser/0`` is the telephoto optic's focuser, ``focuser/1`` the wide optic's.
Measured 9.31, connected (``focuser/0``): ``position`` 1210,
``maxstep``/``maxincrement`` 2600, ``temperature`` 31.3, ``tempcomp``/
``tempcompavailable`` both false, ``stepsize`` not implemented. Unlike 8.46,
``Move`` is a real device verb here — SSC's own ``ssc.autofocus`` still has to
drive it and judge focus, since ASCOM's ``Focuser`` interface has no autofocus
routine of its own and the device offers none either (``docs/M0-FINDINGS.md``).

``get_focuser_state``/``get_focuser_position`` have no focuser-selection
parameter at all, so they can only ever be the telephoto's own reading:
``focuser/1`` (the wide optic) has no JSON-RPC route and answers
``NotImplemented`` there, working only once the native Alpaca surface is
active.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar, Final

from ssc.alpaca.devices.base import (
    AlpacaDevice,
    Getter,
    Setter,
    const,
    unimplemented,
    unimplemented_put,
)
from ssc.alpaca.errors import ValueNotSetError
from ssc.alpaca.wire import RequestParams
from ssc.seestar.models import FocuserState

#: ``get_focuser_state.state`` when the focuser is not moving.
FOCUSER_STATE_IDLE: Final = "idle"

#: Why every member of ``focuser/{n != 0}`` answers ``NotImplemented`` on the
#: Seestar JSON-RPC surface — see the module docstring.
NO_JSON_RPC_ROUTE_TO_SECOND_FOCUSER: Final = (
    "this driver's Seestar JSON-RPC mapping (firmware 8.46) has no method that "
    "addresses a second focuser; focuser/{n} exists only on the device's own "
    "native Alpaca surface (9.16+), and this session is not on it"
)


class FocuserDevice(AlpacaDevice):
    """The electronic focuser, read-only until a move verb is verified.

    ``device_number`` 0 is the telephoto optic's focuser, matching the 8.46
    JSON-RPC mapping below. ``device_number`` 1 is the wide optic's focuser —
    it has no JSON-RPC route at all, so every read answers ``NotImplemented``
    unless the native Alpaca surface is active.
    """

    device_type: ClassVar[str] = "focuser"
    interface_version: ClassVar[int] = 3
    default_name: ClassVar[str] = "Photon Commander Seestar Focuser"
    default_description: ClassVar[str] = "ZWO Seestar focuser via Photon Commander"

    async def _state(self) -> FocuserState:
        payload = await self.backend.call("get_focuser_state", cache=True)
        if not isinstance(payload, Mapping):
            raise ValueNotSetError("get_focuser_state did not return an object")
        return FocuserState.from_payload(payload)

    async def _max_step(self) -> int:
        state = await self._state()
        if state.max_step_range is None:
            raise ValueNotSetError("the device did not report max_step_range")
        return state.max_step_range

    def build_getters(self) -> dict[str, Getter]:
        getters: dict[str, Getter] = {
            "absolute": const(True),
            "ismoving": self._get_is_moving,
            "maxincrement": self._get_max_step,
            "maxstep": self._get_max_step,
            "position": self._get_position,
            "stepsize": unimplemented(
                "Focuser.StepSize",
                "the device does not report a physical step size in microns, and a "
                "guessed one would make every focus curve wrong",
            ),
            "tempcomp": const(False),
            "tempcompavailable": const(False),
            "temperature": unimplemented(
                "Focuser.Temperature", "the focuser reports no temperature probe"
            ),
        }
        if self.device_number != 0:
            # get_focuser_state/get_focuser_position have no focuser-selection
            # parameter — every legacy answer above would in fact be the
            # telephoto's own reading, mislabelled. prefer_native_getters
            # below still tries the device's own focuser/{n} first.
            reason = NO_JSON_RPC_ROUTE_TO_SECOND_FOCUSER.format(n=self.device_number)
            getters = {member: unimplemented(f"Focuser.{member}", reason) for member in getters}
        return self.prefer_native_getters(getters)

    async def _get_max_step(self, _: RequestParams) -> object:
        return await self._max_step()

    async def _get_is_moving(self, _: RequestParams) -> object:
        state = await self._state()
        if state.state is None:
            raise ValueNotSetError("the device did not report a focuser state")
        return state.state.casefold() != FOCUSER_STATE_IDLE

    async def _get_position(self, _: RequestParams) -> object:
        payload = await self.backend.call("get_focuser_position", cache=True)
        if isinstance(payload, bool) or not isinstance(payload, int):
            raise ValueNotSetError(
                f"get_focuser_position returned {payload!r}, not an absolute step count"
            )
        return payload

    def build_setters(self) -> dict[str, Setter]:
        return self.prefer_native_setters(
            {
                "halt": unimplemented_put(
                    "Focuser.Halt",
                    "stop_auto_focus was probed on firmware 8.46 and returned 103; no halt "
                    "verb is known",
                ),
                "move": unimplemented_put(
                    "Focuser.Move",
                    "no absolute-move verb is verified on firmware 8.46; the device's own "
                    "auto_focus operation is exposed as the ssc.start_auto_focus action and "
                    "is itself unverified",
                ),
                "tempcomp": unimplemented_put(
                    "Focuser.TempComp", "the focuser has no temperature compensation"
                ),
            }
        )


__all__ = ["FOCUSER_STATE_IDLE", "NO_JSON_RPC_ROUTE_TO_SECOND_FOCUSER", "FocuserDevice"]
