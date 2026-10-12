# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ``ssc.*`` vendor action vocabulary.

ASCOM has no words for stacking, polar alignment, mosaics or annotation, so those
live behind the standard ``Action`` member. Rules this module enforces:

* Every action is namespaced ``ssc.`` — there is never any doubt whose
  vocabulary a client is speaking.
* Every action appears in ``SupportedActions``. A name that is not in that list
  answers ``ActionNotImplemented`` (``0x40C``); a name that *is* in it but whose
  Seestar verb is unverified answers ``NotImplemented`` (``0x400``). The two are
  different facts and get different numbers.
* Every action has a declared parameter schema, published through
  ``ssc.action_schemas``, and validation that **rejects unknown keys**. A typo in
  a client's payload is an error, never a silently ignored field.
* ``Action`` returns a string, so every result is JSON encoded into it.

The operation half of the vocabulary is derived from the device's own published
operation list (``get_app_state``), not from a wish list — see
``capabilities.OPERATIONS``.
"""

from __future__ import annotations

import ipaddress
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Final

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.capabilities import (
    COMMAND_VOCABULARY_COMPLETE,
    OBSERVED_FIRMWARE,
    OPERATIONS,
    VERBS,
    start_verb,
    stop_verb,
    unavailable_reason,
    verb_is_callable,
)
from ssc.alpaca.errors import (
    ActionNotImplementedError,
    AlpacaNotImplementedError,
    InvalidOperationError,
    InvalidValueError,
)
from ssc.seestar.discovery import DiscoveryError, is_rfc1918
from ssc.seestar.discovery import discover as seestar_discover
from ssc.seestar.discovery import probe as seestar_probe
from ssc.seestar.transport import CONTROL_PORT

#: Every vendor action carries this prefix, without exception.
ACTION_NAMESPACE: Final = "ssc."

#: Actions that must answer before ``Connected = True`` — device discovery and
#: address configuration are not ASCOM concepts and necessarily precede a link.
#: Consulted by ``devices.base.AlpacaDevice._guard``, which extends
#: ``PRE_CONNECT_MEMBERS`` to look inside ``Action`` for these three names only;
#: every other action still requires a connected device.
PRE_CONNECT_ACTIONS: Final[frozenset[str]] = frozenset(
    {"ssc.discover", "ssc.probe", "ssc.set_device_address"}
)

#: The view mode driven against real hardware. Other modes are believed to exist
#: (scenery, sun, moon) but none has been sent, so none is offered.
VERIFIED_VIEW_MODES: Final[tuple[str, ...]] = ("star",)

Args = Mapping[str, object]
Handler = Callable[[SeestarBackend, Args], Awaitable[object]]


# --------------------------------------------------------------------------- #
# Parameter schemas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Param:
    """One declared action parameter."""

    name: str
    kind: str  # "string" | "number" | "integer" | "boolean" | "array"
    description: str
    required: bool = True
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None

    def json_schema(self) -> dict[str, object]:
        schema: dict[str, object] = {"type": self.kind, "description": self.description}
        if self.minimum is not None:
            schema["minimum"] = self.minimum
        if self.maximum is not None:
            schema["maximum"] = self.maximum
        if self.choices is not None:
            schema["enum"] = list(self.choices)
        if self.kind == "array":
            schema["items"] = {"type": "string"}
        return schema

    def coerce(self, action: str, value: object) -> object:
        if self.kind == "boolean":
            if not isinstance(value, bool):
                raise InvalidValueError(f"{action}: '{self.name}' must be a boolean")
            return value
        if self.kind == "string":
            if not isinstance(value, str):
                raise InvalidValueError(f"{action}: '{self.name}' must be a string")
            if self.choices is not None and value not in self.choices:
                raise InvalidValueError(
                    f"{action}: '{self.name}' must be one of {list(self.choices)}, got {value!r}"
                )
            return value
        if self.kind == "array":
            if isinstance(value, str) or not isinstance(value, Sequence):
                raise InvalidValueError(f"{action}: '{self.name}' must be an array of strings")
            if any(not isinstance(item, str) for item in value):
                raise InvalidValueError(f"{action}: '{self.name}' must contain only strings")
            return list(value)
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise InvalidValueError(f"{action}: '{self.name}' must be a number")
        if self.kind == "integer" and not isinstance(value, int):
            raise InvalidValueError(f"{action}: '{self.name}' must be an integer")
        number = float(value)
        if self.minimum is not None and number < self.minimum:
            raise InvalidValueError(
                f"{action}: '{self.name}' must be >= {self.minimum}, got {number}"
            )
        if self.maximum is not None and number > self.maximum:
            raise InvalidValueError(
                f"{action}: '{self.name}' must be <= {self.maximum}, got {number}"
            )
        return value


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """One vendor action: its name, its schema, its verb and its handler."""

    name: str
    summary: str
    handler: Handler
    params: tuple[Param, ...] = ()
    #: The Seestar verb this action needs, if any. ``None`` means it only reads.
    verb: str | None = None

    def json_schema(self) -> dict[str, object]:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": self.name,
            "description": self.summary,
            "type": "object",
            "properties": {param.name: param.json_schema() for param in self.params},
            "required": [param.name for param in self.params if param.required],
            "additionalProperties": False,
        }

    def parse(self, raw: str | None) -> dict[str, object]:
        """Validate the ``Parameters`` payload. Unknown keys are an error."""
        text = (raw or "").strip()
        if not text:
            payload: object = {}
        else:
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise InvalidValueError(
                    f"{self.name}: Parameters must be a JSON object ({exc})"
                ) from exc
        if not isinstance(payload, Mapping):
            raise InvalidValueError(f"{self.name}: Parameters must be a JSON object")

        declared = {param.name: param for param in self.params}
        unknown = sorted(set(payload) - set(declared))
        if unknown:
            raise InvalidValueError(
                f"{self.name}: unknown parameter(s) {unknown}; "
                f"this action accepts {sorted(declared) or 'no parameters'}"
            )
        args: dict[str, object] = {}
        for name, param in declared.items():
            if name not in payload:
                if param.required:
                    raise InvalidValueError(f"{self.name}: required parameter '{name}' is missing")
                continue
            args[name] = param.coerce(self.name, payload[name])
        return args


# --------------------------------------------------------------------------- #
# JSON encoding of results
# --------------------------------------------------------------------------- #


def jsonable(value: object) -> object:
    """Recursively render a result JSON-safe. UTC datetimes become ISO strings."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [jsonable(item) for item in value]
    return str(value)


def encode(value: object) -> str:
    """Encode an action result for ``Action``'s string return."""
    return json.dumps(jsonable(value), separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Shared operations (also used by the standard ASCOM members)
# --------------------------------------------------------------------------- #


async def start_auto_goto(
    backend: SeestarBackend,
    *,
    ra_hours: float,
    dec_degrees: float,
    target_name: str,
    is_j2000: bool,
) -> object:
    """Slew. RA in hours and dec in degrees, matching both ASCOM and the wire.

    The units coincide, which is exactly why the conversion is written out: a
    silent pass-through is indistinguishable from an untested assumption.
    """
    if not 0.0 <= ra_hours < 24.0:
        raise InvalidValueError(f"right ascension must be in [0, 24) hours, got {ra_hours}")
    if not -90.0 <= dec_degrees <= 90.0:
        raise InvalidValueError(f"declination must be in [-90, 90] degrees, got {dec_degrees}")
    await _require_arm_raised(backend)
    return await backend.call(
        "start_auto_goto",
        {
            "target_name": target_name,
            "ra": float(ra_hours),
            "dec": float(dec_degrees),
            "is_j2000": bool(is_j2000),
        },
    )


async def stop_auto_goto(backend: SeestarBackend) -> object:
    return await backend.call("stop_auto_goto")


async def _require_arm_raised(backend: SeestarBackend) -> None:
    """Refuse a slew from a stowed arm, and say what the user has to do.

    ``start_auto_goto`` answers 210 for two different reasons — no star view, or
    a stowed arm — and only one of them is fixable from here. Firmware 8.46 has
    no verb that raises the arm: every park/unpark/open-arm candidate answers
    103, and it was raised from the phone app during the live test. So SSC checks
    ``mount.close`` first and reports the actionable half itself, instead of
    passing back a bare "not supported" that reads as a driver bug.

    An unknown ``close`` is not treated as stowed: the call goes through and the
    device gets the final word.
    """
    mount = (await backend.device_state()).mount
    if mount is not None and mount.close is True:
        raise InvalidOperationError(
            "the Seestar's arm is stowed (mount.close is true) and firmware 8.46 "
            "exposes no verb that raises it — every park, unpark and open-arm "
            "candidate answers 103. Raise the arm on the device itself, then slew"
        )


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #


async def _status(backend: SeestarBackend, _: Args) -> object:
    status = backend.status
    return {
        "connected": backend.is_connected,
        "state": str(status.state),
        "reason": status.reason,
        "endpoint": status.endpoint,
        "control_connected": status.control_connected,
        "events_connected": status.events_connected,
        "events_dropped": status.events_dropped,
        "connect_attempts": status.connect_attempts,
        "changed_at": status.changed_at.isoformat(),
        "device": jsonable(backend.device_info),
        "holders": sorted(backend.holders),
        "battery": backend.battery_status(),
    }


async def _capabilities(_: SeestarBackend, __: Args) -> object:
    """The honesty surface: what SSC will and will not send, and why.

    ``CLAUDE.md`` invariant 7 — an unverified capability is surfaced, never
    papered over. The UI reads this to say so out loud.
    """
    return {
        "observed_firmware": OBSERVED_FIRMWARE,
        "command_vocabulary_complete": COMMAND_VOCABULARY_COMPLETE,
        "operations": list(OPERATIONS),
        "verbs": {
            name: {
                "status": str(entry.status),
                "callable": entry.is_callable,
                "params": list(entry.params),
                "note": entry.note,
            }
            for name, entry in sorted(VERBS.items())
        },
    }


async def _action_schemas(_: SeestarBackend, __: Args) -> object:
    return {name: spec.json_schema() for name, spec in sorted(REGISTRY.items())}


async def _device_state(backend: SeestarBackend, args: Args) -> object:
    keys = args.get("keys")
    if isinstance(keys, list):
        return await backend.device_state(tuple(str(key) for key in keys))
    return await backend.device_state()


def _reader(method: str) -> Handler:
    async def handler(backend: SeestarBackend, _: Args) -> object:
        return await backend.call(method, cache=True)

    return handler


async def _solve_result(backend: SeestarBackend, args: Args) -> object:
    method = "get_last_solve_result" if args.get("last") is True else "get_solve_result"
    return await backend.call(method, cache=True)


async def _start_view(backend: SeestarBackend, args: Args) -> object:
    mode = args["mode"]
    return await backend.call("iscope_start_view", {"mode": mode})


async def _stop_view(backend: SeestarBackend, _: Args) -> object:
    return await backend.call("iscope_stop_view")


async def _start_auto_goto_action(backend: SeestarBackend, args: Args) -> object:
    return await start_auto_goto(
        backend,
        ra_hours=float(args["ra_hours"]),  # type: ignore[arg-type]
        dec_degrees=float(args["dec_degrees"]),  # type: ignore[arg-type]
        target_name=str(args.get("target_name", "SSC target")),
        is_j2000=bool(args.get("is_j2000", True)),
    )


def _bare_verb(method: str) -> Handler:
    async def handler(backend: SeestarBackend, _: Args) -> object:
        return await backend.call(method)

    return handler


# --------------------------------------------------------------------------- #
# Pre-connect actions: discovery, probing, and picking the target address.
#
# None of these touch ``backend.call`` or the verb gate — they run before any
# session exists, which is the entire reason they are pre-connect in the first
# place. ``ssc.seestar.discovery`` is the only thing they delegate to.
# --------------------------------------------------------------------------- #


def _require_ipv4_network(text: str) -> ipaddress.IPv4Network:
    try:
        return ipaddress.IPv4Network(text, strict=False)
    except ValueError as exc:
        raise InvalidValueError(f"ssc.discover: {text!r} is not an IPv4 network") from exc


async def _discover(_: SeestarBackend, args: Args) -> object:
    kwargs: dict[str, object] = {}
    timeout_s = args.get("timeout_s")
    if timeout_s is not None:
        kwargs["timeout"] = float(timeout_s)  # type: ignore[arg-type]
    subnets = args.get("subnets")
    if subnets is not None:
        assert isinstance(subnets, list)  # Param already enforced this
        kwargs["subnets"] = [_require_ipv4_network(str(text)) for text in subnets]
    try:
        found = await seestar_discover(**kwargs)  # type: ignore[arg-type]
    except DiscoveryError as exc:
        raise InvalidValueError(f"ssc.discover: {exc}") from exc
    return {
        "devices": [
            {
                "host": device.host,
                "port": device.control_port,
                "firmware": device.info.firmware,
                "model": device.info.model,
                "serial": device.info.serial,
                # What was observed about this device, in the same spirit as the
                # session's routing evidence: a scan that reports "firmware
                # unknown" with no explanation reads like a fault.
                "note": device.info.note,
            }
            for device in found
        ]
    }


async def _probe(_: SeestarBackend, args: Args) -> object:
    host = str(args["host"])
    port = int(args.get("port", CONTROL_PORT))  # type: ignore[call-overload]
    info = await seestar_probe(host, control_port=port)
    # ``DeviceInfo.reachable`` is true only once the protocol handshake
    # (test_connection) has answered — see ssc.seestar.session.identify(). A
    # host that accepted the TCP connection but never answered it is reported
    # by a note starting "connected but", which is the one other case the
    # underlying probe distinguishes; anything else collapses "reachable" and
    # "handshake" together rather than inventing a distinction the evidence
    # does not support.
    handshake = info.reachable
    reachable = handshake or (info.note is not None and info.note.startswith("connected but"))
    return {
        "reachable": reachable,
        "handshake": handshake,
        # The port that answered, which is not always the port asked about: a
        # device serving the native Alpaca surface is identified on its Alpaca
        # port. The caller remembers this one, so "Test connection" and
        # "Connect" agree about the address.
        "port": info.control_port,
        "firmware": info.firmware,
        "model": info.model,
        "serial": info.serial,
        "verified": info.firmware_is_verified,
        "reason": info.note,
    }


def _validate_target_host(host: str) -> str:
    """Loopback or RFC1918 only — a public host is never a valid target."""
    text = host.strip()
    try:
        address = ipaddress.ip_address(text)
    except ValueError as exc:
        raise InvalidValueError(f"ssc.set_device_address: {host!r} is not an IP address") from exc
    if address.is_loopback:
        return text
    if isinstance(address, ipaddress.IPv4Address) and is_rfc1918(
        ipaddress.IPv4Network(f"{address}/32")
    ):
        return text
    raise InvalidValueError(
        f"ssc.set_device_address: {host!r} is not a private (RFC1918) or loopback "
        "address; SSC will not target a publicly routable host (CLAUDE.md invariant 11)"
    )


async def _set_device_address(backend: SeestarBackend, args: Args) -> object:
    host = _validate_target_host(str(args["host"]))
    port = int(args.get("port", CONTROL_PORT))  # type: ignore[call-overload]
    await backend.set_target_address(host, port)
    return {"host": host, "port": port}


# --------------------------------------------------------------------------- #
# The registry
# --------------------------------------------------------------------------- #

_READ_ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec("ssc.status", "Connection state, device identity and event health.", _status),
    ActionSpec(
        "ssc.capabilities",
        "Every known Seestar command verb with the evidence behind it, and whether "
        "SSC will send it.",
        _capabilities,
    ),
    ActionSpec(
        "ssc.action_schemas",
        "The JSON parameter schema for every ssc.* action.",
        _action_schemas,
    ),
    ActionSpec(
        "ssc.device_state",
        "Typed device state. Credential-bearing keys ('ap', 'setting') are never read.",
        _device_state,
        params=(
            Param(
                "keys",
                "array",
                "get_device_state sections to read; omit for SSC's default set.",
                required=False,
            ),
        ),
    ),
    ActionSpec(
        "ssc.app_state",
        "The device's own operation list with each operation's is_working flag.",
        _reader("get_app_state"),
    ),
    ActionSpec(
        "ssc.controls",
        "Raw get_controls: the authoritative gain and exposure ranges.",
        _reader("get_controls"),
    ),
    ActionSpec(
        "ssc.solve_result",
        "The current or last plate-solve result. Answers ValueNotSet when there is none.",
        _solve_result,
        params=(
            Param(
                "last",
                "boolean",
                "Read the last stored solve instead of the current one.",
                required=False,
            ),
        ),
    ),
    ActionSpec("ssc.disk_volume", "Device storage totals in MB.", _reader("get_disk_volume")),
    ActionSpec("ssc.albums", "The device's album tree.", _reader("get_albums")),
    ActionSpec(
        "ssc.station_state",
        "Wi-Fi client state. Carries no password field and none is read.",
        _reader("pi_station_state"),
    ),
    ActionSpec(
        "ssc.device_time",
        "The device's own clock and IANA timezone name.",
        _reader("pi_get_time"),
    ),
    ActionSpec("ssc.view_state", "Current view pipeline state.", _reader("get_view_state")),
    ActionSpec("ssc.stack_info", "Stacked frame dimensions.", _reader("get_stack_info")),
)

_VIEW_ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec(
        "ssc.start_view",
        "Enter a view mode. Star view is a precondition for a slew: start_auto_goto "
        "answers code 210 without it.",
        _start_view,
        params=(
            Param(
                "mode",
                "string",
                "View mode. Only 'star' has been driven against hardware.",
                choices=VERIFIED_VIEW_MODES,
            ),
        ),
        verb="iscope_start_view",
    ),
    ActionSpec("ssc.stop_view", "Leave view mode.", _stop_view, verb="iscope_stop_view"),
    ActionSpec(
        "ssc.stop_create_dark",
        "Stop dark-frame creation. A verified verb that belongs to no operation in "
        "get_app_state's list.",
        _bare_verb("stop_create_dark"),
        verb="stop_create_dark",
    ),
)

#: Runs before any device is connected — see ``PRE_CONNECT_ACTIONS``.
_PRE_CONNECT_ACTIONS: Final[tuple[ActionSpec, ...]] = (
    ActionSpec(
        "ssc.discover",
        "Scan this host's own RFC 1918 subnets for a Seestar. Bounded, "
        "cancellable, RFC 1918 only — see ssc.seestar.discovery.discover.",
        _discover,
        params=(
            Param(
                "timeout_s",
                "number",
                "Overall scan deadline in seconds. Omit for the discovery module's default.",
                required=False,
                minimum=0.0,
            ),
            Param(
                "subnets",
                "array",
                "CIDR ranges to scan instead of this host's own, e.g. '192.168.1.0/24'. "
                "Still refused unless RFC 1918.",
                required=False,
            ),
        ),
    ),
    ActionSpec(
        "ssc.probe",
        "Test one address and report what is actually there — reachable, "
        "handshake, firmware, or the real reason it failed.",
        _probe,
        params=(
            Param("host", "string", "The address to test."),
            Param(
                "port",
                "integer",
                "The control port. Defaults to the Seestar control port (4701).",
                required=False,
                minimum=1,
                maximum=65535,
            ),
        ),
    ),
    ActionSpec(
        "ssc.set_device_address",
        "Set the host and port the backend will connect to on the next "
        "Connected = True. Rejects a publicly routable host.",
        _set_device_address,
        params=(
            Param("host", "string", "The address to target. Loopback or RFC 1918 only."),
            Param(
                "port",
                "integer",
                "The control port. Defaults to the Seestar control port (4701).",
                required=False,
                minimum=1,
                maximum=65535,
            ),
        ),
    ),
)

#: Actions whose parameter shape was observed on the wire. Everything else in the
#: operation family declares no parameters, because none has been seen.
_TYPED_OPERATION_ACTIONS: Final[Mapping[str, ActionSpec]] = MappingProxyType(
    {
        "ssc.start_auto_goto": ActionSpec(
            "ssc.start_auto_goto",
            "Slew to J2000 coordinates. RA in hours, dec in degrees, as on the wire.",
            _start_auto_goto_action,
            params=(
                Param("ra_hours", "number", "Right ascension in hours.", minimum=0.0, maximum=24.0),
                Param(
                    "dec_degrees", "number", "Declination in degrees.", minimum=-90.0, maximum=90.0
                ),
                Param("target_name", "string", "Label shown on the device.", required=False),
                Param(
                    "is_j2000",
                    "boolean",
                    "True when the coordinates are J2000 (the default).",
                    required=False,
                ),
            ),
            verb="start_auto_goto",
        )
    }
)


def _build_registry() -> Mapping[str, ActionSpec]:
    registry: dict[str, ActionSpec] = {}
    for spec in _READ_ACTIONS + _VIEW_ACTIONS + _PRE_CONNECT_ACTIONS:
        registry[spec.name] = spec
    for operation in OPERATIONS:
        for prefix, method in (("start", start_verb(operation)), ("stop", stop_verb(operation))):
            name = f"ssc.{prefix}_{operation}"
            typed = _TYPED_OPERATION_ACTIONS.get(name)
            if typed is not None:
                registry[name] = typed
                continue
            entry = VERBS[method]
            registry[name] = ActionSpec(
                name,
                f"{prefix.capitalize()} the device's {operation!r} operation "
                f"(verb {method!r}, {entry.status.value} on firmware {entry.firmware}).",
                _bare_verb(method),
                verb=method,
            )
    return MappingProxyType(registry)


#: Name -> spec for every ``ssc.*`` action.
REGISTRY: Final[Mapping[str, ActionSpec]] = _build_registry()

#: Exactly what ``SupportedActions`` returns, on every device.
SUPPORTED_ACTIONS: Final[tuple[str, ...]] = tuple(sorted(REGISTRY))


async def invoke(backend: SeestarBackend, name: str, parameters: str | None) -> str:
    """Run one vendor action and return its JSON-encoded result.

    Order matters. An unknown name is ``ActionNotImplemented``; a known name
    whose verb is unverified is ``NotImplemented`` **before** its parameters are
    validated, so the client is told the real reason rather than a schema
    complaint about a call that could never have run.
    """
    spec = REGISTRY.get(name.strip())
    if spec is None:
        raise ActionNotImplementedError(
            f"{name!r} is not an SSC action; SupportedActions lists the {len(REGISTRY)} that exist"
        )
    if spec.verb is not None and not verb_is_callable(spec.verb):
        raise AlpacaNotImplementedError(f"{spec.name}: {unavailable_reason(spec.verb)}")
    args = spec.parse(parameters)
    return encode(await spec.handler(backend, args))


__all__ = [
    "ACTION_NAMESPACE",
    "PRE_CONNECT_ACTIONS",
    "REGISTRY",
    "SUPPORTED_ACTIONS",
    "VERIFIED_VIEW_MODES",
    "ActionSpec",
    "Param",
    "encode",
    "invoke",
    "jsonable",
    "start_auto_goto",
    "stop_auto_goto",
]
