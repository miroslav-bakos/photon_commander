# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Alpaca HTTP surface: ``/management/*`` and ``/api/v1/*``.

Third-party clients attach to this port, so "our UI works" is not the bar —
ConformU is. The details that decide conformance all live here:

* Every response echoes the client's ``ClientTransactionID`` and carries a
  strictly increasing ``ServerTransactionID``.
* Parameter names are case-insensitive; ``PUT`` bodies are
  ``application/x-www-form-urlencoded``.
* Device errors are **HTTP 200** with ``ErrorNumber`` and a non-empty
  ``ErrorMessage``. **HTTP 400** means the request itself was malformed — an
  unknown device, an unknown member, a missing or untypeable parameter.
  **HTTP 500** means this driver faulted, and nothing else.
* ``/management/apiversions`` is served at the spec's location *and* under
  ``/management/v1/`` so both readings of the path work.

Binding is loopback by default (``DESIGN.md`` §9). LAN exposure is an explicit
opt-in and a non-RFC1918 address is refused outright rather than warned about.
"""

from __future__ import annotations

import ipaddress
import logging
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final
from urllib.parse import parse_qsl

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.devices import (
    MANUFACTURER,
    AlpacaDevice,
    CameraDevice,
    FilterWheelDevice,
    FocuserDevice,
    SwitchDevice,
    TelescopeDevice,
)
from ssc.alpaca.devices.base import DRIVER_VERSION
from ssc.alpaca.errors import AlpacaError, BadRequestError, UnspecifiedError
from ssc.alpaca.imagebytes import encode_image_bytes_error, encode_image_bytes_frame
from ssc.alpaca.wire import (
    ALPACA_API_VERSION,
    RequestParams,
    TransactionCounter,
    error_response,
    method_response,
    value_response,
)
from ssc.client.imagebytes import ImageBytesFrame

_log = logging.getLogger(__name__)

#: The ASCOM Alpaca convention for a device server.
DEFAULT_ALPACA_PORT: Final = 5555

#: Loopback is the default and needs no opt-in.
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "::1", "localhost"})

#: Binds every interface, including any public one. Only reachable behind an
#: explicit LAN opt-in, and it says so out loud when it happens.
WILDCARD_HOSTS: Final[frozenset[str]] = frozenset({"0.0.0.0", "::"})

#: Stable namespace for the ``UniqueID`` in ``/management/v1/configureddevices``.
#: A client that pairs with a device must see the same id after a restart.
_UNIQUE_ID_NAMESPACE: Final = uuid.UUID("6f0a1b52-2f2a-5f8f-9a5d-4d1f3a0c9b71")

#: The one ``ImageArray`` transfer this driver ever serves (``CameraDevice``,
#: ``ssc/alpaca/imagebytes.py``) — never the plain JSON array.
_IMAGE_BYTES_MEDIA_TYPE: Final = "application/imagebytes"


class BindRefusedError(RuntimeError):
    """The requested bind address is not one this driver will listen on."""


def resolve_bind_host(host: str | None, *, allow_lan: bool = False) -> str:
    """Decide what to bind, or refuse loudly.

    Loopback always. A private (RFC1918 / link-local / unique-local) address, or
    the any-interface wildcard, only with ``allow_lan``. A routable public
    address never — ``DESIGN.md`` §9 puts telescope control on the LAN and
    nowhere else.
    """
    wanted = (host or "127.0.0.1").strip()
    if wanted in LOOPBACK_HOSTS:
        return wanted
    if not allow_lan:
        raise BindRefusedError(
            f"refusing to bind {wanted!r}: the Alpaca driver is loopback-only unless LAN "
            "exposure is explicitly enabled (DESIGN.md §9)"
        )
    if wanted in WILDCARD_HOSTS:
        _log.warning(
            "binding %r exposes telescope control on every interface on this host, "
            "not only the LAN; prefer an explicit private address",
            wanted,
        )
        return wanted
    try:
        address = ipaddress.ip_address(wanted)
    except ValueError as exc:
        raise BindRefusedError(
            f"refusing to bind {wanted!r}: it is not an IP address, and SSC will not "
            "resolve a name to decide whether a bind is safe"
        ) from exc
    if address.is_loopback or address.is_private or address.is_link_local:
        return wanted
    raise BindRefusedError(
        f"refusing to bind {wanted!r}: it is a publicly routable address. SSC is a LAN "
        "tool; exposing mount control to the internet is out of scope by design"
    )


@dataclass(frozen=True, slots=True)
class ServerConfig:
    """How this Alpaca server presents itself and where it listens."""

    host: str = "127.0.0.1"
    port: int = DEFAULT_ALPACA_PORT
    allow_lan: bool = False
    server_name: str = "Photon Commander Alpaca — ZWO Seestar"
    location: str = "Local network"
    #: Extra manufacturer text for ``/management/v1/description``.
    manufacturer_version: str = DRIVER_VERSION

    def bind_host(self) -> str:
        return resolve_bind_host(self.host, allow_lan=self.allow_lan)


@dataclass(frozen=True, slots=True)
class DeviceRegistry:
    """Every device this server serves, addressable as ASCOM addresses them."""

    devices: tuple[AlpacaDevice, ...] = field(default_factory=tuple)

    def lookup(self, device_type: str, device_number: int) -> AlpacaDevice | None:
        wanted = device_type.casefold()
        for device in self.devices:
            if device.device_type == wanted and device.device_number == device_number:
                return device
        return None

    def configured(self) -> list[dict[str, object]]:
        return [
            {
                "DeviceName": device.name,
                "DeviceType": device.device_type.capitalize(),
                "DeviceNumber": device.device_number,
                "UniqueID": str(uuid.uuid5(_UNIQUE_ID_NAMESPACE, device.unique_id)).upper(),
            }
            for device in self.devices
        ]


def build_devices(backend: SeestarBackend) -> tuple[AlpacaDevice, ...]:
    """The seven interfaces a Seestar presents, all over one session.

    ``camera/1`` (wide), ``focuser/1`` (wide) and ``switch/0`` (dew heater)
    join the original four. They have no route on the Seestar JSON-RPC
    surface (firmware 8.46) — every member of theirs answers ``NotImplemented``
    there — and are served for real only once the device's own native Alpaca
    server (9.16+) is the active control surface (see the ``NO_JSON_RPC_ROUTE_*``
    constants in each device module, and ``docs/M0-FINDINGS.md`` 9.31).
    """
    return (
        TelescopeDevice(backend),
        CameraDevice(backend, device_number=0, name="Photon Commander Seestar Camera (Telephoto)"),
        CameraDevice(backend, device_number=1, name="Photon Commander Seestar Camera (Wide)"),
        FocuserDevice(backend, device_number=0, name="Photon Commander Seestar Focuser (Telephoto)"),
        FocuserDevice(backend, device_number=1, name="Photon Commander Seestar Focuser (Wide)"),
        FilterWheelDevice(backend),
        SwitchDevice(backend),
    )


async def _put_params(request: Request) -> RequestParams:
    """Parse a ``PUT`` body as ``application/x-www-form-urlencoded``.

    Parsed directly rather than through Starlette's form handling so the driver
    carries no multipart dependency and so a query-string fallback stays
    possible: the spec puts PUT parameters in the body, but several real clients
    also append them to the URL.
    """
    body = (await request.body()).decode("utf-8", "replace")
    pairs: list[tuple[str, str]] = list(parse_qsl(body, keep_blank_values=True))
    pairs.extend(request.query_params.multi_items())
    return RequestParams(pairs)


def create_app(
    backend: SeestarBackend,
    *,
    config: ServerConfig | None = None,
    devices: Iterable[AlpacaDevice] | None = None,
) -> FastAPI:
    """Build the ASGI app. Nothing here binds a socket."""
    settings = config or ServerConfig()
    registry = DeviceRegistry(tuple(devices) if devices is not None else build_devices(backend))
    counter = TransactionCounter()

    app = FastAPI(
        title=settings.server_name,
        version=DRIVER_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.backend = backend
    app.state.registry = registry
    app.state.config = settings
    app.state.transactions = counter

    def envelope(params: RequestParams, value: object, *, has_value: bool) -> Response:
        server_tid = counter.allocate()
        if has_value:
            body = value_response(
                value,
                client_transaction_id=params.client_transaction_id,
                server_transaction_id=server_tid,
            )
        else:
            body = method_response(
                client_transaction_id=params.client_transaction_id,
                server_transaction_id=server_tid,
            )
        return JSONResponse(body)

    def failure(params: RequestParams, error: AlpacaError) -> Response:
        return JSONResponse(
            error_response(
                error,
                client_transaction_id=params.client_transaction_id,
                server_transaction_id=counter.allocate(),
            )
        )

    def image_bytes_response(params: RequestParams, frame: ImageBytesFrame) -> Response:
        payload = encode_image_bytes_frame(
            frame,
            client_transaction_id=params.client_transaction_id,
            server_transaction_id=counter.allocate(),
        )
        return Response(content=payload, media_type=_IMAGE_BYTES_MEDIA_TYPE)

    def image_bytes_failure(params: RequestParams, error: AlpacaError) -> Response:
        payload = encode_image_bytes_error(
            int(error.number),
            error.message,
            client_transaction_id=params.client_transaction_id,
            server_transaction_id=counter.allocate(),
        )
        return Response(content=payload, media_type=_IMAGE_BYTES_MEDIA_TYPE)

    # -- management -------------------------------------------------------- #

    def management(request: Request, value: object) -> Response:
        params = RequestParams(request.query_params.multi_items())
        return envelope(params, value, has_value=True)

    async def api_versions(request: Request) -> Response:
        return management(request, [ALPACA_API_VERSION])

    async def description(request: Request) -> Response:
        return management(
            request,
            {
                "ServerName": settings.server_name,
                "Manufacturer": MANUFACTURER,
                "ManufacturerVersion": settings.manufacturer_version,
                "Location": settings.location,
            },
        )

    async def configured_devices(request: Request) -> Response:
        return management(request, registry.configured())

    # The spec puts apiversions outside /v1 because it is what tells a client
    # which /v1 to use. Both spellings are served: refusing one would be a
    # conformance trap for no benefit.
    app.add_api_route("/management/apiversions", api_versions, methods=["GET"])
    app.add_api_route("/management/v1/apiversions", api_versions, methods=["GET"])
    app.add_api_route("/management/v1/description", description, methods=["GET"])
    app.add_api_route("/management/v1/configureddevices", configured_devices, methods=["GET"])

    # -- devices ------------------------------------------------------------ #

    def resolve(device_type: str, device_number: str) -> AlpacaDevice:
        try:
            number = int(device_number)
        except ValueError as exc:
            raise BadRequestError(f"device number {device_number!r} is not an integer") from exc
        if number < 0:
            raise BadRequestError(f"device number {number} is negative")
        device = registry.lookup(device_type, number)
        if device is None:
            raise BadRequestError(
                f"this server does not serve {device_type}/{number}; see "
                "/management/v1/configureddevices"
            )
        return device

    async def handle(
        request: Request, device_type: str, device_number: str, member: str, *, write: bool
    ) -> Response:
        params = (
            await _put_params(request)
            if write
            else RequestParams(request.query_params.multi_items())
        )
        # ``ImageArray`` is the one member with two response shapes on this
        # server: a client that asked for ``Accept: application/imagebytes``
        # gets the binary transfer (``CameraDevice.image_bytes`` — never the
        # ordinary JSON ``get()`` dispatch, which answers ``NotImplemented`` for
        # this member instead), everyone else gets the normal envelope below.
        wants_image_bytes = (
            not write
            and member.lower() == "imagearray"
            and _IMAGE_BYTES_MEDIA_TYPE in request.headers.get("accept", "").lower()
        )
        try:
            device = resolve(device_type, device_number)
            if wants_image_bytes:
                if not isinstance(device, CameraDevice):
                    raise BadRequestError(
                        f"{device_type}.ImageArray does not offer an ImageBytes transfer"
                    )
                return image_bytes_response(params, await device.image_bytes(params))
            if write:
                result = await device.put(member, params)
                return envelope(params, result, has_value=result is not None)
            return envelope(params, await device.get(member, params), has_value=True)
        except BadRequestError as exc:
            return PlainTextResponse(exc.message, status_code=400)
        except AlpacaError as exc:
            if wants_image_bytes:
                return image_bytes_failure(params, exc)
            return failure(params, exc)
        except Exception:  # the boundary: nothing escapes as a stack trace
            _log.exception(
                "unhandled fault serving %s %s/%s/%s",
                request.method,
                device_type,
                device_number,
                member,
            )
            return PlainTextResponse(
                "the SSC Alpaca driver faulted while serving this request; see the "
                "server log for the traceback",
                status_code=500,
            )

    async def device_get(
        request: Request, device_type: str, device_number: str, member: str
    ) -> Response:
        return await handle(request, device_type, device_number, member, write=False)

    async def device_put(
        request: Request, device_type: str, device_number: str, member: str
    ) -> Response:
        return await handle(request, device_type, device_number, member, write=True)

    app.add_api_route("/api/v1/{device_type}/{device_number}/{member}", device_get, methods=["GET"])
    app.add_api_route("/api/v1/{device_type}/{device_number}/{member}", device_put, methods=["PUT"])
    return app


def unspecified(message: str) -> AlpacaError:
    """Helper for callers that need to raise a generic device fault."""
    return UnspecifiedError(message)


def describe_devices(registry: DeviceRegistry) -> Sequence[Mapping[str, object]]:
    return registry.configured()


__all__ = [
    "DEFAULT_ALPACA_PORT",
    "LOOPBACK_HOSTS",
    "WILDCARD_HOSTS",
    "BindRefusedError",
    "DeviceRegistry",
    "ServerConfig",
    "build_devices",
    "create_app",
    "resolve_bind_host",
]
