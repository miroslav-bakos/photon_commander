# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Alpaca client ``ssc-core`` uses to drive the scope.

SSC's own UI does not take a shortcut past the driver (``CLAUDE.md``
invariant 2): everything above the seam goes through these calls, over the same
HTTP surface a third-party client would use.

The client is strict on its own side of the contract:

* It allocates a ``ClientTransactionID`` per request and **verifies the echo**.
  A mismatch raises rather than being ignored — a driver that misroutes
  transaction ids may be answering the wrong question.
* ``PUT`` bodies are form-encoded, as the spec requires.
* A non-zero ``ErrorNumber`` becomes :class:`AlpacaRemoteError`, carrying the
  number so a caller can tell "not implemented" from "not connected" without
  parsing text.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import TracebackType
from typing import Any, Final

import httpx

from ssc.client.errors import (
    AlpacaProtocolError,
    AlpacaRemoteError,
    AlpacaTransportError,
)
from ssc.client.imagebytes import ImageBytesFrame, decode_image_bytes
from ssc.client.models import DeviceAddress, DiscoveredDevice, ProbeResult

#: Where the three pre-connect vendor actions live. They answer identically
#: from any of the seven devices — the backend behind them is shared — but the
#: driver serves them on ``telescope/0``, so that is what the client targets.
_ADDRESS_DEVICE_TYPE: Final = "telescope"
_ADDRESS_DEVICE_NUMBER: Final = 0

#: Matches the driver's default. Never a device address — this is SSC's own port.
DEFAULT_BASE_URL: Final = "http://127.0.0.1:5555"

DEFAULT_REQUEST_TIMEOUT_S: Final = 15.0

#: Identifies SSC to the driver. Alpaca uses it only for logging.
DEFAULT_CLIENT_ID: Final = 1

#: The only ``ImageArray`` transfer this client asks for — 2.5x faster and
#: 2.5x smaller than the JSON array on the same frame (``docs/M0-FINDINGS.md``).
_IMAGE_BYTES_ACCEPT: Final = "application/imagebytes"


#: ASCOM's own spelling for every PUT parameter SSC sends, keyed by its
#: lower-cased form. The spec makes these names case-SENSITIVE, and the S30
#: Pro's native Alpaca server enforces that strictly: a `gain=300` body is
#: refused with `HTTP 400 - Bad Form Key Capitalization(s): Gain, received:
#: gain`, observed live on 2026-09-10 while setting camera gain.
#:
#: Callers already spell these correctly, but SSC's own Alpaca *server*
#: lower-cases every parameter it receives (`ssc.alpaca.wire.RequestParams`,
#: deliberately, so it accepts what lenient clients send) and a request that
#: crosses that seam on its way to the device arrives here already flattened.
#: Restoring the canonical spelling at the one point where SSC writes to a
#: device fixes it for every route, rather than in each layer that forwards.
_CANONICAL_PUT_PARAMS: Final[Mapping[str, str]] = {
    name.lower(): name
    for name in (
        # common
        "Connected", "Action", "Parameters", "Command", "Raw", "Value", "Id", "State", "Name",
        # camera
        "Gain", "Offset", "Duration", "Light", "BinX", "BinY", "NumX", "NumY",
        "StartX", "StartY", "CoolerOn", "SetCCDTemperature", "FastReadout",
        "ReadoutMode", "SubExposureDuration",
        # telescope
        "RightAscension", "Declination", "TargetRightAscension", "TargetDeclination",
        "Altitude", "Azimuth", "SiteLatitude", "SiteLongitude", "SiteElevation",
        "Tracking", "TrackingRate", "RightAscensionRate", "DeclinationRate",
        "Axis", "Rate", "Direction", "UTCDate", "DoesRefraction", "SideOfPier",
        "GuideRateRightAscension", "GuideRateDeclination",
        # focuser / filterwheel / rotator / switch / dome
        "Position", "TempComp", "Reverse", "Brightness", "Slaved", "ShutterState",
        "Temperature", "Speed", "SwitchValue", "Absolute",
    )
}


def _canonical_params(params: Mapping[str, object]) -> Mapping[str, object]:
    """``params`` with every ASCOM parameter name in its canonical spelling.

    A name SSC does not recognise is passed through untouched rather than
    guessed at: inventing a capitalisation would be worse than sending what
    the caller asked for and letting the device say no.
    """
    return {_CANONICAL_PUT_PARAMS.get(key.lower(), key): value for key, value in params.items()}


class AlpacaClient:
    """A minimal, complete Alpaca client over one driver."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        client_id: int = DEFAULT_CLIENT_ID,
        http: httpx.AsyncClient | None = None,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
    ) -> None:
        self._client_id = client_id
        self._transaction_id = 0
        self._owns_http = http is None
        self._http = http or httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=request_timeout_s
        )

    async def __aenter__(self) -> AlpacaClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    # -- management ---------------------------------------------------------- #

    async def api_versions(self) -> list[int]:
        value = await self._request("GET", "/management/apiversions", {}, member="apiversions")
        if not isinstance(value, list):
            raise AlpacaTransportError("apiversions did not return a list")
        return [int(item) for item in value]

    async def description(self) -> Mapping[str, Any]:
        value = await self._request("GET", "/management/v1/description", {}, member="description")
        if not isinstance(value, Mapping):
            raise AlpacaTransportError("description did not return an object")
        return value

    async def configured_devices(self) -> list[Mapping[str, Any]]:
        value = await self._request(
            "GET", "/management/v1/configureddevices", {}, member="configureddevices"
        )
        if not isinstance(value, list):
            raise AlpacaTransportError("configureddevices did not return a list")
        return [item for item in value if isinstance(item, Mapping)]

    # -- devices ------------------------------------------------------------- #

    def device(self, device_type: str, device_number: int = 0) -> AlpacaDeviceClient:
        return AlpacaDeviceClient(self, device_type, device_number)

    async def get_property(
        self, device_type: str, device_number: int, member: str, **params: object
    ) -> object:
        return await self._request(
            "GET",
            f"/api/v1/{device_type.lower()}/{device_number}/{member.lower()}",
            params,
            member=f"{device_type}.{member}",
        )

    async def put_property(
        self, device_type: str, device_number: int, member: str, **params: object
    ) -> object:
        return await self._request(
            "PUT",
            f"/api/v1/{device_type.lower()}/{device_number}/{member.lower()}",
            params,
            member=f"{device_type}.{member}",
        )

    async def image_array(
        self, device_type: str, device_number: int, member: str = "imagearray"
    ) -> ImageBytesFrame:
        """Fetch ``ImageArray`` as ImageBytes and decode it.

        Requests the binary transfer explicitly — this client never falls
        back to the JSON array (``docs/M0-FINDINGS.md``: 2.5x slower, 2.5x
        larger for the same frame). A server that answers JSON anyway, or
        anything else, is reported plainly rather than silently re-parsed.
        """
        self._transaction_id += 1
        transaction_id = self._transaction_id
        path = f"/api/v1/{device_type.lower()}/{device_number}/{member.lower()}"
        params = {
            "ClientID": str(self._client_id),
            "ClientTransactionID": str(transaction_id),
        }
        full_member = f"{device_type}.{member}"
        try:
            response = await self._http.get(
                path, params=params, headers={"Accept": _IMAGE_BYTES_ACCEPT}
            )
        except httpx.HTTPError as exc:
            raise AlpacaTransportError(
                f"{full_member}: {_transport_detail(exc, self._http.timeout)}"
            ) from exc

        if response.status_code != 200:
            raise AlpacaTransportError(
                f"{full_member}: HTTP {response.status_code} — {response.text.strip()[:400]}"
            )
        content_type = response.headers.get("content-type", "")
        if _IMAGE_BYTES_ACCEPT not in content_type:
            raise AlpacaTransportError(
                f"{full_member}: requested {_IMAGE_BYTES_ACCEPT!r} but the server answered "
                f"{content_type or '(no content-type)'!r} — this client does not fall back to "
                "the JSON array"
            )
        return decode_image_bytes(
            response.content,
            member=full_member,
            expected_client_transaction_id=transaction_id,
        )

    async def action(
        self,
        device_type: str,
        device_number: int,
        name: str,
        parameters: Mapping[str, object] | None = None,
    ) -> object:
        """Invoke a vendor action and decode its result.

        ``Action`` returns a string by contract. SSC's own ``ssc.*`` actions
        always put JSON in it, so that is decoded here rather than in every
        caller. Not every action this client reaches is one of SSC's own,
        though: the device's own native camera vendor actions answer a *bare*
        string — measured live, ``GetFanSpeed`` -> ``'0'``, ``GetAdcSize`` ->
        ``'16'``, ``GetSN`` -> a UUID (``docs/M0-FINDINGS.md``) — none of which
        is valid JSON on its own (a bare UUID does not parse; a bare digit
        string parses as the *number*, not the string that was actually sent).
        A reply that is not valid JSON is therefore not an error: it is
        returned exactly as received, rather than rejected as malformed.
        """
        raw = await self.put_property(
            device_type,
            device_number,
            "action",
            Action=name,
            Parameters=json.dumps(parameters or {}, separators=(",", ":")),
        )
        if not isinstance(raw, str):
            raise AlpacaTransportError(
                f"action {name!r} returned {type(raw).__name__}, not a string"
            )
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    # -- device discovery and addressing -------------------------------------- #
    #
    # These three answer before Connected = True — device discovery and address
    # selection are not ASCOM concepts. Callable directly on the client: no
    # device needs to be looked up first, since there is nothing to connect to
    # yet (architecture policy invariant 11 — no address is ever a default).

    async def discover(
        self, *, timeout_s: float | None = None, subnets: Sequence[str] | None = None
    ) -> list[DiscoveredDevice]:
        """Scan the LAN for a Seestar. Delegates to ``ssc.discover``."""
        params: dict[str, object] = {}
        if timeout_s is not None:
            params["timeout_s"] = timeout_s
        if subnets is not None:
            params["subnets"] = list(subnets)
        payload = await self._address_action("ssc.discover", params)
        devices = payload.get("devices")
        if not isinstance(devices, list):
            raise AlpacaTransportError("ssc.discover did not return a devices list")
        return [_discovered_device(item) for item in devices]

    async def probe(self, host: str, port: int | None = None) -> ProbeResult:
        """Test one address and report what is actually there. Delegates to ``ssc.probe``."""
        params: dict[str, object] = {"host": host}
        if port is not None:
            params["port"] = port
        payload = await self._address_action("ssc.probe", params)
        return _probe_result(payload)

    async def set_device_address(self, host: str, port: int | None = None) -> DeviceAddress:
        """Set the target the driver will connect to on the next ``Connected = True``."""
        params: dict[str, object] = {"host": host}
        if port is not None:
            params["port"] = port
        payload = await self._address_action("ssc.set_device_address", params)
        return _device_address(payload)

    async def _address_action(self, name: str, params: Mapping[str, object]) -> Mapping[str, Any]:
        payload = await self.action(_ADDRESS_DEVICE_TYPE, _ADDRESS_DEVICE_NUMBER, name, params)
        if not isinstance(payload, Mapping):
            raise AlpacaTransportError(f"action {name!r} did not return an object")
        return payload

    # -- transport ----------------------------------------------------------- #

    async def _request(
        self, method: str, path: str, params: Mapping[str, object], *, member: str
    ) -> object:
        self._transaction_id += 1
        transaction_id = self._transaction_id
        payload = {
            **{key: _encode(value) for key, value in _canonical_params(params).items()},
            "ClientID": str(self._client_id),
            "ClientTransactionID": str(transaction_id),
        }
        try:
            if method == "GET":
                response = await self._http.get(path, params=payload)
            else:
                response = await self._http.put(path, data=payload)
        except httpx.HTTPError as exc:
            raise AlpacaTransportError(
                f"{member}: {_transport_detail(exc, self._http.timeout)}"
            ) from exc

        if response.status_code != 200:
            raise AlpacaTransportError(
                f"{member}: HTTP {response.status_code} — {response.text.strip()[:400]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise AlpacaTransportError(f"{member}: response was not JSON") from exc
        if not isinstance(body, Mapping):
            raise AlpacaTransportError(f"{member}: response was not a JSON object")

        echoed = body.get("ClientTransactionID")
        if echoed != transaction_id:
            raise AlpacaProtocolError(
                f"{member}: driver echoed ClientTransactionID {echoed!r}, expected {transaction_id}"
            )
        number = body.get("ErrorNumber", 0)
        if not isinstance(number, int) or isinstance(number, bool):
            raise AlpacaTransportError(f"{member}: ErrorNumber was {number!r}")
        if number != 0:
            message = body.get("ErrorMessage")
            if not isinstance(message, str) or not message.strip():
                raise AlpacaProtocolError(
                    f"{member}: ErrorNumber {number:#05x} arrived with an empty ErrorMessage"
                )
            raise AlpacaRemoteError(number, message, member=member)
        return body.get("Value")


class AlpacaDeviceClient:
    """One device, bound to a client. Sugar over ``get``/``put``/``action``."""

    def __init__(self, client: AlpacaClient, device_type: str, device_number: int) -> None:
        self._client = client
        self._device_type = device_type
        self._device_number = device_number

    @property
    def device_type(self) -> str:
        return self._device_type

    @property
    def device_number(self) -> int:
        return self._device_number

    async def get(self, member: str, **params: object) -> object:
        return await self._client.get_property(
            self._device_type, self._device_number, member, **params
        )

    async def put(self, member: str, **params: object) -> object:
        return await self._client.put_property(
            self._device_type, self._device_number, member, **params
        )

    async def action(self, name: str, parameters: Mapping[str, object] | None = None) -> object:
        return await self._client.action(self._device_type, self._device_number, name, parameters)

    async def connect(self) -> None:
        await self.put("connected", Connected=True)

    async def disconnect(self) -> None:
        await self.put("connected", Connected=False)

    async def is_connected(self) -> bool:
        return bool(await self.get("connected"))

    async def supported_actions(self) -> list[str]:
        value = await self.get("supportedactions")
        if not isinstance(value, list):
            raise AlpacaTransportError("SupportedActions did not return a list")
        return [str(item) for item in value]

    # -- one exposure, pulled as ImageBytes ----------------------------------
    #
    # The transfer layer only: start it, poll for it, decode it. No capture
    # loop, no FITS header, no retry policy — that belongs above this seam.

    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
        """``Camera.StartExposure`` — begins immediately; poll ``image_ready``."""
        await self.put("startexposure", Duration=duration_s, Light=light)

    async def image_ready(self) -> bool:
        """``Camera.ImageReady`` — true once ``image_array`` has a frame to return."""
        return bool(await self.get("imageready"))

    async def image_array(self) -> ImageBytesFrame:
        """``Camera.ImageArray``, pulled and decoded as ImageBytes."""
        return await self._client.image_array(self._device_type, self._device_number)

    # -- live state, for a Live tab that polls rather than one that pushes --- #
    #
    # Alpaca has no push channel; ssc-core's own scheduler decides the cadence
    # and calls these repeatedly. What belongs here is the read itself: a
    # typed decode of the JSON these vendor actions already return, once,
    # rather than every caller re-doing ``action(...)`` and hoping the shape
    # is right.

    async def status(self) -> Mapping[str, Any]:
        """Link state, device identity and event health. Delegates to ``ssc.status``."""
        payload = await self.action("ssc.status")
        if not isinstance(payload, Mapping):
            raise AlpacaTransportError("ssc.status did not return an object")
        return payload

    async def device_state(self, keys: Sequence[str] | None = None) -> Mapping[str, Any]:
        """Mount, camera, focuser and other device state in one round trip.

        Delegates to ``ssc.device_state``. Standard members (``RightAscension``,
        ``Slewing``, ``Position``, ...) cover the rest; this is for the bulk read
        a Live tab wants instead of one GET per field.
        """
        params: dict[str, object] = {} if keys is None else {"keys": list(keys)}
        payload = await self.action("ssc.device_state", params)
        if not isinstance(payload, Mapping):
            raise AlpacaTransportError("ssc.device_state did not return an object")
        return payload

    async def capabilities(self) -> Mapping[str, Any]:
        """What this driver will and will not send, and why. Delegates to ``ssc.capabilities``."""
        payload = await self.action("ssc.capabilities")
        if not isinstance(payload, Mapping):
            raise AlpacaTransportError("ssc.capabilities did not return an object")
        return payload


def _encode(value: object) -> str:
    """Alpaca is a text protocol; booleans go on the wire as True/False."""
    if isinstance(value, bool):
        return "True" if value else "False"
    return str(value)


def _transport_detail(exc: httpx.HTTPError, timeout: httpx.Timeout) -> str:
    """Describe an httpx failure in words. Never returns the empty string.

    ``str()`` on an httpx timeout **is** the empty string — a device that
    accepts a request and then never answers raises ``ReadTimeout('')`` — so
    formatting one straight into a message produces ``"telescope.connected: "``,
    which tells nobody anything. Observed live on an S30 Pro's own Alpaca
    server on 2026-09-10, whose mount subsystem was refusing reads at the time:
    the session logged an empty reason and dropped the link every 15 s.
    """
    text = str(exc).strip()
    if text:
        return text
    phases: Mapping[type[httpx.HTTPError], tuple[str, float | None]] = {
        httpx.ConnectTimeout: ("connect", timeout.connect),
        httpx.ReadTimeout: ("read", timeout.read),
        httpx.WriteTimeout: ("write", timeout.write),
        httpx.PoolTimeout: ("waiting for a free connection", timeout.pool),
    }
    phase = phases.get(type(exc))
    if phase is None:
        return f"{type(exc).__name__} (the client reported no detail)"
    name, budget = phase
    limit = "no limit" if budget is None else f"{budget:g}s"
    return f"{name} timed out after {limit} with no response ({type(exc).__name__})"


# --------------------------------------------------------------------------- #
# Decoding ssc.discover / ssc.probe / ssc.set_device_address. Every field is
# checked; a wrong shape is a transport error, never a silently substituted
# default (a driver bug here must not turn into a mis-pointed telescope).
# --------------------------------------------------------------------------- #


def _required_str(payload: Mapping[str, Any], key: str, *, action: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise AlpacaTransportError(f"{action}: {key!r} was {type(value).__name__}, not a string")
    return value


def _optional_str(payload: Mapping[str, Any], key: str, *, action: str) -> str | None:
    value = payload.get(key)
    if value is None or isinstance(value, str):
        return value
    raise AlpacaTransportError(
        f"{action}: {key!r} was {type(value).__name__}, not a string or null"
    )


def _required_int(payload: Mapping[str, Any], key: str, *, action: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise AlpacaTransportError(f"{action}: {key!r} was {type(value).__name__}, not an integer")
    return value


def _optional_int(payload: Mapping[str, Any], key: str, *, action: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise AlpacaTransportError(
            f"{action}: {key!r} was {type(value).__name__}, not an integer or null"
        )
    return value


def _required_bool(payload: Mapping[str, Any], key: str, *, action: str) -> bool:
    value = payload.get(key)
    if not isinstance(value, bool):
        raise AlpacaTransportError(f"{action}: {key!r} was {type(value).__name__}, not a boolean")
    return value


def _discovered_device(item: object) -> DiscoveredDevice:
    if not isinstance(item, Mapping):
        raise AlpacaTransportError("ssc.discover: a device entry was not an object")
    return DiscoveredDevice(
        host=_required_str(item, "host", action="ssc.discover"),
        port=_required_int(item, "port", action="ssc.discover"),
        firmware=_optional_str(item, "firmware", action="ssc.discover"),
        model=_optional_str(item, "model", action="ssc.discover"),
        serial=_optional_str(item, "serial", action="ssc.discover"),
        note=_optional_str(item, "note", action="ssc.discover"),
    )


def _probe_result(payload: Mapping[str, Any]) -> ProbeResult:
    return ProbeResult(
        reachable=_required_bool(payload, "reachable", action="ssc.probe"),
        handshake=_required_bool(payload, "handshake", action="ssc.probe"),
        port=_optional_int(payload, "port", action="ssc.probe"),
        firmware=_optional_str(payload, "firmware", action="ssc.probe"),
        model=_optional_str(payload, "model", action="ssc.probe"),
        serial=_optional_str(payload, "serial", action="ssc.probe"),
        verified=_required_bool(payload, "verified", action="ssc.probe"),
        reason=_optional_str(payload, "reason", action="ssc.probe"),
    )


def _device_address(payload: Mapping[str, Any]) -> DeviceAddress:
    return DeviceAddress(
        host=_required_str(payload, "host", action="ssc.set_device_address"),
        port=_required_int(payload, "port", action="ssc.set_device_address"),
    )


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_CLIENT_ID",
    "DEFAULT_REQUEST_TIMEOUT_S",
    "AlpacaClient",
    "AlpacaDeviceClient",
    "ImageBytesFrame",
]
