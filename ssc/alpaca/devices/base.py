# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Common ASCOM device behaviour: dispatch, ``Connected``, and honest gaps.

Two rules shape every device class in this package:

* **Every member of the interface appears in the dispatch table**, even the ones
  this driver cannot do. A member that is missing from the table is a *malformed
  request* (HTTP 400); a member that exists but cannot be performed is
  ``NotImplemented`` (``0x400`` on HTTP 200). Collapsing the two would tell a
  client the interface is wrong when the truth is the capability is unproven.
* **Every ``NotImplemented`` carries a reason.** ``architecture policy`` invariant 7: an
  unavailable capability is surfaced, never papered over.

``Connected`` is real state. The seven devices share one Seestar session through
:class:`~ssc.alpaca.backend.SeestarBackend`, refcounted, so connecting the camera
and the telescope opens one connection and closes it once.

**Two control surfaces, one dispatch table.** On a device whose control surface
is Seestar JSON-RPC (8.46), ``build_getters``/``build_setters`` are the whole
mapping. On a device that has withdrawn JSON-RPC and ships its own ASCOM Alpaca
server instead (9.16+, ``ssc.seestar.native``), the *same standard member* is
usually available straight off that server — more authoritative than anything
this driver could compute, and ``CLAUDE.md`` invariant 2 says to prefer it.
:meth:`AlpacaDevice.prefer_native` wraps a device's whole getter or setter dict
so every member tries the device's own Alpaca server first (via
:meth:`~ssc.alpaca.backend.SeestarBackend.native_get`/``native_put``) and falls
back to the dict's own mapping only when that surface is not the one in use —
so the 8.46 mapping written out in each device module keeps working unchanged,
and a 9.16+ device gets a direct pass-through with no per-member duplication.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import ClassVar, Final

from ssc.alpaca import actions
from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.errors import (
    AlpacaNotImplementedError,
    BadRequestError,
    NotConnectedError,
)
from ssc.alpaca.wire import RequestParams

#: Reported through ``DriverVersion`` on every device.
DRIVER_VERSION: Final = "1.0"

#: Reported through ``/management/v1/description``.
MANUFACTURER: Final = "Photon Commander"

Getter = Callable[[RequestParams], Awaitable[object]]
#: A ``PUT`` handler returns ``None`` for a plain method response, or a value for
#: the few members (``Action``, ``CommandString``, ``CommandBool``) that return one.
Setter = Callable[[RequestParams], Awaitable[object]]

#: Carried by every Alpaca request but never part of a device's own vocabulary
#: — stripped before a request's parameters are forwarded to the device's own
#: Alpaca server, which allocates its own pair.
_UNFORWARDED_PARAMS: Final[frozenset[str]] = frozenset({"clientid", "clienttransactionid"})

#: ASCOM's own canonical capitalisation for every parameter name a standard
#: ``Telescope``/``FilterWheel``/``Focuser`` member ever carries.
#:
#: This should not matter — Alpaca parameter *names* are case-insensitive by
#: spec (``ssc/alpaca/wire.py``) — but the Seestar's own native Alpaca server
#: does not honour that. Measured live, 2026-09-10: forwarding a lower-cased
#: ``axis=1`` to its ``MoveAxis`` refuses with HTTP 400, ``"Bad Form Key
#: Capitalization(s): Axis, received: axis"``. ``RequestParams`` lower-cases
#: every incoming parameter name to do its own case-insensitive matching (by
#: spec, correctly), so that casing has to be reconstructed before a value is
#: sent back out to a server that — unlike this driver — enforces it.
_CANONICAL_PARAM_NAMES: Final[Mapping[str, str]] = {
    "axis": "Axis",
    "rate": "Rate",
    "direction": "Direction",
    "duration": "Duration",
    "rightascension": "RightAscension",
    "declination": "Declination",
    "azimuth": "Azimuth",
    "altitude": "Altitude",
    "tracking": "Tracking",
    "trackingrate": "TrackingRate",
    "rightascensionrate": "RightAscensionRate",
    "declinationrate": "DeclinationRate",
    "doesrefraction": "DoesRefraction",
    "siteelevation": "SiteElevation",
    "sitelatitude": "SiteLatitude",
    "sitelongitude": "SiteLongitude",
    "slewsettletime": "SlewSettleTime",
    "targetrightascension": "TargetRightAscension",
    "targetdeclination": "TargetDeclination",
    "utcdate": "UTCDate",
    "sideofpier": "SideOfPier",
    "guideratedeclination": "GuideRateDeclination",
    "guideraterightascension": "GuideRateRightAscension",
    "position": "Position",
    "tempcomp": "TempComp",
    "light": "Light",
    "id": "Id",
    "name": "Name",
    "state": "State",
    "value": "Value",
}


def _forwardable_params(params: RequestParams) -> dict[str, str]:
    """This request's own parameters, minus the two Alpaca adds to every call,
    restored to ASCOM's own canonical capitalisation (:data:`_CANONICAL_PARAM_NAMES`)."""
    return {
        _CANONICAL_PARAM_NAMES.get(name, name): value
        for name, value in params.as_mapping().items()
        if name not in _UNFORWARDED_PARAMS
    }

#: Members that must answer while the device is disconnected. Everything else
#: raises ``NotConnected`` rather than queueing.
PRE_CONNECT_MEMBERS: Final[frozenset[str]] = frozenset(
    {
        "connected",
        "description",
        "driverinfo",
        "driverversion",
        "interfaceversion",
        "name",
        "supportedactions",
    }
)


def const(value: object) -> Getter:
    """A member whose value is a fixed property of this driver or device."""

    async def _get(_: RequestParams) -> object:
        return value

    return _get


def unimplemented(member: str, reason: str) -> Getter:
    """A member of the interface this driver cannot perform, and why."""

    async def _get(_: RequestParams) -> object:
        raise AlpacaNotImplementedError(f"{member} is not available: {reason}")

    return _get


def unimplemented_put(member: str, reason: str) -> Setter:
    async def _put(_: RequestParams) -> object:
        raise AlpacaNotImplementedError(f"{member} is not available: {reason}")

    return _put


class AlpacaDevice:
    """One ASCOM device interface over the shared Seestar session."""

    device_type: ClassVar[str] = ""
    interface_version: ClassVar[int] = 0
    default_name: ClassVar[str] = ""
    default_description: ClassVar[str] = ""

    def __init__(
        self,
        backend: SeestarBackend,
        *,
        device_number: int = 0,
        name: str | None = None,
        unique_id: str | None = None,
    ) -> None:
        if not self.device_type:
            raise ValueError("an AlpacaDevice subclass must declare device_type")
        self._backend = backend
        self._device_number = device_number
        self._name = name or self.default_name
        self._unique_id = unique_id or f"ssc-seestar-{self.device_type}-{device_number}"
        self._connected = False
        self._getters: dict[str, Getter] = {}
        self._setters: dict[str, Setter] = {}
        self._install_common()
        self._getters.update(self.build_getters())
        self._setters.update(self.build_setters())

    # -- identity ------------------------------------------------------------ #

    @property
    def backend(self) -> SeestarBackend:
        return self._backend

    @property
    def device_number(self) -> int:
        return self._device_number

    @property
    def name(self) -> str:
        return self._name

    @property
    def unique_id(self) -> str:
        return self._unique_id

    @property
    def is_connected(self) -> bool:
        """ASCOM ``Connected``: this device wants a link *and* there is one.

        Deliberately not a client-set latch: a client's own ``Connected = True``
        (``self._connected``) is one half, the shared session's live reachability
        (:attr:`~ssc.alpaca.backend.SeestarBackend.is_connected`) is the other,
        and both must hold. A driver that answered ``Connected`` from the latch
        alone would keep reporting ``true`` through an outage the moment a
        client had ever asked to connect — plausible but wrong, exactly the
        fabricated value CLAUDE.md invariant 7 rules out.

        This is also the *only* place that truth is computed: :meth:`get`'s
        ``_guard`` and :meth:`_get_connected` both read this property, never
        ``self._connected`` directly, so ``Connected`` and every other member's
        ``NotConnected`` refusal can never disagree about whether the session is
        up. Field report, 2026-09-10: the owner's driver appeared to report
        ``Connected = True`` while every other telescope member refused with
        ``NotConnected`` for roughly half an hour. Investigated here: this
        property and the guard already share one computation and a regression
        test reproducing the exact shape (``tests/alpaca/test_connected_honesty.py``,
        a control-channel reset while ``Connected`` stays latched) already
        passes — confirmed by breaking it deliberately (having ``_get_connected``
        return ``self._connected`` alone reproduces the field symptom exactly,
        and the test catches it). The field disagreement is not reproducible at
        this layer; the likely explanation is genuine flapping of the
        underlying link, compounded by the health-poll detection latency in
        ``ssc/seestar/native.py`` (out of this package's ownership) — not a
        client-set-latch bug here.
        """
        return self._connected and self._backend.is_connected

    @property
    def holder_key(self) -> str:
        return f"{self.device_type}/{self._device_number}"

    def driver_info(self) -> str:
        return (
            f"{MANUFACTURER} — native ZWO Seestar driver. ASCOM {self.device_type} "
            f"interface version {self.interface_version}. Capabilities are gated on "
            "verbs verified against the device's own firmware; unverified ones "
            "answer NotImplemented rather than a fabricated value."
        )

    # -- subclass hooks ------------------------------------------------------ #

    def build_getters(self) -> dict[str, Getter]:
        return {}

    def build_setters(self) -> dict[str, Setter]:
        return {}

    # -- native Alpaca pass-through ------------------------------------------ #

    def prefer_native_getters(self, legacy: Mapping[str, Getter]) -> dict[str, Getter]:
        """Wrap a whole ``build_getters`` dict to prefer the device's own answer.

        Each member tries :meth:`~ssc.alpaca.backend.SeestarBackend.native_get`
        first; ``legacy``'s own handler runs only when
        :attr:`~ssc.alpaca.backend.SeestarBackend.native_surface_active` is
        false, so this changes nothing on an 8.46 device.
        """
        return {member: self._native_first_get(member, handler) for member, handler in legacy.items()}

    def prefer_native_setters(self, legacy: Mapping[str, Setter]) -> dict[str, Setter]:
        """The ``PUT`` counterpart of :meth:`prefer_native_getters`."""
        return {member: self._native_first_put(member, handler) for member, handler in legacy.items()}

    def _native_first_get(self, member: str, legacy: Getter) -> Getter:
        async def _get(params: RequestParams) -> object:
            if not self._backend.native_surface_active:
                return await legacy(params)
            return await self._backend.native_get(
                self.device_type, member, self._device_number, **_forwardable_params(params)
            )

        return _get

    def _native_first_put(self, member: str, legacy: Setter) -> Setter:
        async def _put(params: RequestParams) -> object:
            if not self._backend.native_surface_active:
                return await legacy(params)
            return await self._backend.native_put(
                self.device_type, member, self._device_number, **_forwardable_params(params)
            )

        return _put

    # -- dispatch ------------------------------------------------------------ #

    async def get(self, member: str, params: RequestParams) -> object:
        key = member.lower()
        handler = self._getters.get(key)
        if handler is None:
            if key in self._setters:
                raise BadRequestError(f"{self.device_type}.{member} is write-only; use PUT")
            raise BadRequestError(f"unknown {self.device_type} member '{member}'")
        self._guard(key, params)
        return await handler(params)

    async def put(self, member: str, params: RequestParams) -> object:
        key = member.lower()
        handler = self._setters.get(key)
        if handler is None:
            if key in self._getters:
                raise BadRequestError(f"{self.device_type}.{member} is read-only; use GET")
            raise BadRequestError(f"unknown {self.device_type} member '{member}'")
        self._guard(key, params)
        return await handler(params)

    def _guard(self, member: str, params: RequestParams) -> None:
        if member in PRE_CONNECT_MEMBERS or self.is_connected:
            return
        if (
            member == "action"
            and (params.raw("Action") or "").strip() in actions.PRE_CONNECT_ACTIONS
        ):
            # Discovery, probing and address selection are not ASCOM concepts
            # and necessarily precede a link — see actions.PRE_CONNECT_ACTIONS.
            # Every other action still requires Connected = True; unrecognised
            # or unsupported names fall through to actions.invoke, unaffected.
            return
        raise NotConnectedError(
            f"{self.device_type}.{member} requires Connected = True "
            f"(this device is {'linked but the session is down' if self._connected else 'not connected'})"
        )

    # -- common members ------------------------------------------------------ #

    def _install_common(self) -> None:
        self._getters.update(
            {
                "connected": self._get_connected,
                "description": const(self.default_description),
                "driverinfo": const(self.driver_info()),
                "driverversion": const(DRIVER_VERSION),
                "interfaceversion": const(self.interface_version),
                "name": const(self._name),
                "supportedactions": const(list(actions.SUPPORTED_ACTIONS)),
            }
        )
        self._setters.update(
            {
                "connected": self._put_connected,
                "action": self._put_action,
                "commandblind": self._command_unavailable("CommandBlind"),
                "commandbool": self._command_unavailable("CommandBool"),
                "commandstring": self._command_unavailable("CommandString"),
            }
        )

    async def _get_connected(self, _: RequestParams) -> object:
        return self.is_connected

    async def _put_connected(self, params: RequestParams) -> object:
        wanted = params.as_bool("Connected")
        if wanted:
            # Fails loudly rather than latching a flag that would contradict
            # every other member: if the shared session cannot be established
            # (unreachable host, auth locked, the ready-budget timeout in
            # ``SeestarBackend.acquire``), this raises and ``self._connected``
            # is never set — this PUT answers a real ASCOM error (0x407) on
            # HTTP 200 instead of a silent 200 that ``Connected`` would then
            # contradict on the very next read.
            await self._backend.acquire(self.holder_key)
            self._connected = True
        else:
            self._connected = False
            await self._backend.release(self.holder_key)
        return None

    async def _put_action(self, params: RequestParams) -> object:
        name = params.require("Action")
        return await actions.invoke(self._backend, name, params.raw("Parameters"))

    def _command_unavailable(self, member: str) -> Setter:
        return unimplemented_put(
            member,
            "the Seestar protocol has no free-form command channel; use the ssc.* "
            "vendor actions, which are typed and validated",
        )


__all__ = [
    "DRIVER_VERSION",
    "MANUFACTURER",
    "PRE_CONNECT_MEMBERS",
    "AlpacaDevice",
    "Getter",
    "Setter",
    "const",
    "unimplemented",
    "unimplemented_put",
]
