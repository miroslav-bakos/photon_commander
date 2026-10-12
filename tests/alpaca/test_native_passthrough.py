# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Telescope, FilterWheel, Focuser and Camera over the device's own native
Alpaca server.

The fake below reproduces the 9.31 facts in ``docs/M0-FINDINGS.md``: four
*separate* ASCOM device instances (``telescope/0``, ``focuser/0``,
``filterwheel/0``, ``camera/0``), each independently gated behind its own
``Connected`` — trap 1, "properties are refused with 1031 until the device is
Connected" — ``Park`` refusing below the horizon (trap 3, ``[1279]``, which is
ASCOM's own ``0x4FF`` in decimal), and ``ImageArray`` served only as the
ImageBytes binary transfer, never plain JSON. A test that let SSC pass without
exercising those traps would prove nothing about the live device.

These tests go through the real ASGI app (``create_app``), exactly as ConformU
and NINA would reach the driver — never call the device classes directly.
"""

from __future__ import annotations

import struct
from collections.abc import AsyncIterator
from typing import Final
from urllib.parse import parse_qs

import httpx
import numpy as np
import pytest

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.errors import ErrorNumber
from ssc.alpaca.server import ServerConfig, create_app
from ssc.client.alpaca import AlpacaClient
from ssc.client.errors import AlpacaRemoteError
from ssc.client.imagebytes import ImageArrayElementType
from ssc.seestar.native import NativeAlpacaSession
from tests.alpaca.conftest import api_get, api_put, error_number, put_error_number, value

_NOT_CONNECTED: Final = 0x407  # 1031, decimal in the docs
_BELOW_HORIZON: Final = 0x4FF  # 1279, decimal in the docs
_BAD_RATE: Final = 0x401  # 1025, decimal in the docs

#: Matches ``ssc/client/imagebytes.py``'s wire format — see that module and
#: ``tests/alpaca/test_image_array.py``, which builds the identical header.
_IMAGE_HEADER_FORMAT: Final = "<iiIIiiiiiii"
_IMAGE_HEADER_SIZE: Final = struct.calcsize(_IMAGE_HEADER_FORMAT)

DEVICES: Final[tuple[dict[str, object], ...]] = (
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telescope",
        "DeviceType": "Telescope",
        "DeviceNumber": 0,
        "UniqueID": "9a901430",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telephoto Focuser",
        "DeviceType": "Focuser",
        "DeviceNumber": 0,
        "UniqueID": "fc6d1ef8",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Wide Focuser",
        "DeviceType": "Focuser",
        "DeviceNumber": 1,
        "UniqueID": "fc6d1ef9",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Filter Wheel",
        "DeviceType": "FilterWheel",
        "DeviceNumber": 0,
        "UniqueID": "55abab3f",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telephoto Camera",
        "DeviceType": "Camera",
        "DeviceNumber": 0,
        "UniqueID": "3c8a7e10",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Wide Camera",
        "DeviceType": "Camera",
        "DeviceNumber": 1,
        "UniqueID": "3c8a7e11",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Switch",
        "DeviceType": "Switch",
        "DeviceNumber": 0,
        "UniqueID": "0d1e2f30",
    },
)

#: The mount's own ``can*`` flags, as ``NativeAlpacaSession`` reads them at
#: handshake — see ``_TELESCOPE_CAPABILITIES`` in ``ssc/seestar/native.py``.
TELESCOPE_CAPABILITIES: Final[dict[str, bool]] = {
    "canpark": True,
    "canunpark": True,
    "canfindhome": True,
    "canslew": True,
    "canslewasync": True,
    "canslewaltazasync": False,
    "cansettracking": True,
    "cansync": True,
    "cansetpark": False,
    "cansetpierside": False,
}


class FakeNativeSeestar:
    """The device's own Alpaca server, with the observed 9.31 quirks kept in.

    Three independently-``Connected`` devices, each refusing every other
    member with 1031 until its own ``Connected = True`` has been set and read
    back — exactly what ``SeestarBackend._ensure_native_connected`` exists for.
    """

    def __init__(self) -> None:
        self.connected: dict[str, bool] = {
            "telescope/0": False,
            "focuser/0": False,
            "focuser/1": False,
            "filterwheel/0": False,
            "camera/0": False,
            "camera/1": False,
            "switch/0": False,
        }
        self.telescope: dict[str, object] = {
            **TELESCOPE_CAPABILITIES,
            "alignmentmode": 1,
            "equatorialsystem": 1,
            "rightascension": 5.5,
            "declination": 42.0,
            "altitude": 45.0,
            "azimuth": 180.0,
            "atpark": True,
            "athome": True,
            "tracking": False,
            "slewing": False,
        }
        self.focuser: dict[str, object] = {
            "position": 1210,
            "maxstep": 2600,
            "maxincrement": 2600,
            "temperature": 31.3125,
            "tempcomp": False,
            "tempcompavailable": False,
            "absolute": True,
            "ismoving": False,
        }
        #: The wide optic's own focuser — deliberately different numbers from
        #: ``self.focuser`` so a test can tell the two apart, not just that
        #: both happen to answer the telephoto's own state.
        self.focuser_wide: dict[str, object] = {
            "position": 340,
            "maxstep": 1500,
            "maxincrement": 1500,
            "temperature": 29.5,
            "tempcomp": False,
            "tempcompavailable": False,
            "absolute": True,
            "ismoving": False,
        }
        self.filterwheel: dict[str, object] = {
            "names": ["Dark", "IR", "LP"],
            "position": 1,
            "focusoffsets": [0, 0, 0],
        }
        self.camera: dict[str, object] = {
            "canabortexposure": True,
            "imageready": False,
        }
        #: The wide camera — its own state, own frame, own ``ImageReady``
        #: clock, matching ``docs/M0-FINDINGS.md`` 9.31: both cameras expose
        #: independently and the two payloads differ.
        self.camera_wide: dict[str, object] = {
            "canabortexposure": True,
            "imageready": False,
        }
        #: One switch, ``Dew heater``, range 0-1 (``docs/M0-FINDINGS.md`` 9.31:
        #: "switch device is solely dew heater"). ``maxswitch`` answers ``0``
        #: while disconnected on the real device — a lying capability read
        #: this fake need not model, since ``AlpacaDevice._guard`` refuses the
        #: request before it would ever reach here (see ``switch.py``).
        self.switch: dict[str, object] = {
            "maxswitch": 1,
            "state": False,
            "value": 0.0,
        }
        #: A tiny stand-in for the real 2160 x 3840 frame — shape and dtype
        #: are what this test checks, not size.
        self.camera_frame: np.typing.NDArray[np.uint16] = np.arange(24, dtype="<u2").reshape(4, 6)
        #: The wide camera's own frame — different values, so a decoded
        #: payload can be told apart from the telephoto's.
        self.camera_frame_wide: np.typing.NDArray[np.uint16] = (
            np.arange(24, 48, dtype="<u2").reshape(4, 6)
        )
        self.read_while_disconnected: list[str] = []
        self.moveaxis_calls: list[tuple[str, str]] = []
        self.start_exposure_calls: list[tuple[str, str]] = []

    def client_factory(self, host: str, port: int) -> AlpacaClient:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url=f"http://{host}:{port}"
        )
        return AlpacaClient(http=http)

    # -- the server ------------------------------------------------------- #

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        transaction = self._transaction_id(request)

        if path == "/management/apiversions":
            return self._value([1], transaction)
        if path == "/management/v1/configureddevices":
            return self._value([dict(entry) for entry in DEVICES], transaction)

        parts = path.removeprefix("/api/v1/").split("/")
        if len(parts) != 3:
            return self._error(0x400, f"{path} is not implemented", transaction)
        device_type, number, member = parts
        key = f"{device_type}/{number}"
        if key not in self.connected:
            return self._error(0x400, f"{key} is not a configured device", transaction)

        if member == "connected":
            if request.method == "PUT":
                self.connected[key] = self._form(request).get("connected") == "True"
                return self._value(None, transaction)
            return self._value(self.connected[key], transaction)

        if not self.connected[key]:
            self.read_while_disconnected.append(f"{key}.{member}")
            return self._error(_NOT_CONNECTED, f"{member} requires a connected device", transaction)

        handler = getattr(self, f"_{device_type}", None)
        if handler is None:
            return self._error(0x400, f"{device_type} is not implemented", transaction)
        return handler(number, member, request, transaction)

    def _telescope(
        self, _number: str, member: str, request: httpx.Request, transaction: int
    ) -> httpx.Response:
        if request.method == "PUT":
            form = self._form(request)
            if member == "park":
                if float(self.telescope["altitude"]) < 0:
                    return self._error(
                        _BELOW_HORIZON,
                        "SET_SCOPE_SET_TRACK_STATE fail: fail to operate. The possible "
                        "reason is that the telescope was pointed below the horizon.",
                        transaction,
                    )
                self.telescope["atpark"] = True
                self.telescope["tracking"] = False
                return self._value(None, transaction)
            if member == "unpark":
                self.telescope["atpark"] = False
                return self._value(None, transaction)
            if member == "findhome":
                if float(self.telescope["altitude"]) < 0:
                    self.telescope["altitude"] = 0.0
                self.telescope["athome"] = True
                return self._value(None, transaction)
            if member == "moveaxis":
                axis, rate = form.get("axis", ""), form.get("rate", "")
                self.moveaxis_calls.append((axis, rate))
                if axis == "2":
                    return self._error(0x400, "MoveAxis is not implemented for Axis 2", transaction)
                if abs(float(rate or 0.0)) > 6.0:
                    return self._error(_BAD_RATE, "MoveAxis, Rate must be between -6 and 6 degrees", transaction)
                return self._value(None, transaction)
            if member == "tracking":
                self.telescope["tracking"] = form.get("tracking") == "True"
                return self._value(None, transaction)
            return self._error(0x400, f"Telescope.{member} is not implemented", transaction)
        if member not in self.telescope:
            return self._error(0x400, f"Telescope.{member} is not implemented", transaction)
        return self._value(self.telescope[member], transaction)

    def _focuser(
        self, number: str, member: str, request: httpx.Request, transaction: int
    ) -> httpx.Response:
        focuser = self.focuser if number == "0" else self.focuser_wide
        if request.method == "PUT":
            if member == "move":
                position = int(self._form(request)["position"])
                if not 0 <= position <= int(focuser["maxstep"]):
                    return self._error(0x401, "Position is out of range", transaction)
                focuser["position"] = position
                return self._value(None, transaction)
            return self._error(0x400, f"Focuser.{member} is not implemented", transaction)
        if member not in focuser:
            return self._error(0x400, f"Focuser.{member} is not implemented", transaction)
        return self._value(focuser[member], transaction)

    def _filterwheel(
        self, _number: str, member: str, request: httpx.Request, transaction: int
    ) -> httpx.Response:
        if request.method == "PUT":
            if member == "position":
                position = int(self._form(request)["position"])
                names = self.filterwheel["names"]
                assert isinstance(names, list)
                if not 0 <= position < len(names):
                    return self._error(0x401, "Position is out of range", transaction)
                self.filterwheel["position"] = position
                return self._value(None, transaction)
            return self._error(0x400, f"FilterWheel.{member} is not implemented", transaction)
        if member not in self.filterwheel:
            return self._error(0x400, f"FilterWheel.{member} is not implemented", transaction)
        return self._value(self.filterwheel[member], transaction)

    def _camera(
        self, number: str, member: str, request: httpx.Request, transaction: int
    ) -> httpx.Response:
        camera = self.camera if number == "0" else self.camera_wide
        frame = self.camera_frame if number == "0" else self.camera_frame_wide
        if request.method == "PUT":
            if member == "startexposure":
                form = self._form(request)
                self.start_exposure_calls.append((form.get("duration", ""), form.get("light", "")))
                camera["imageready"] = True
                return self._value(None, transaction)
            return self._error(0x400, f"Camera.{member} is not implemented", transaction)
        if member == "imagearray":
            accept = request.headers.get("accept", "")
            if "application/imagebytes" not in accept:
                # The real device's own default GET answers the nested-JSON
                # array here; the fake refuses instead, since this driver must
                # never actually reach for it (docs/M0-FINDINGS.md).
                return self._error(0x400, "Camera.ImageArray JSON is not implemented", transaction)
            return self._image_bytes(transaction, frame)
        if member not in camera:
            return self._error(0x400, f"Camera.{member} is not implemented", transaction)
        return self._value(camera[member], transaction)

    def _image_bytes(
        self, transaction: int, frame: np.typing.NDArray[np.uint16] | None = None
    ) -> httpx.Response:
        pixels = self.camera_frame if frame is None else frame
        header = struct.pack(
            _IMAGE_HEADER_FORMAT,
            1,  # metadata version
            0,  # error number
            transaction,
            transaction,  # server transaction id — this driver replaces it, never relays it
            _IMAGE_HEADER_SIZE,
            ImageArrayElementType.UINT16,
            ImageArrayElementType.UINT16,
            2,  # rank
            pixels.shape[0],
            pixels.shape[1],
            0,
        )
        return httpx.Response(
            200,
            content=header + pixels.tobytes(),
            headers={"content-type": "application/imagebytes"},
        )

    def _switch(
        self, _number: str, member: str, request: httpx.Request, transaction: int
    ) -> httpx.Response:
        if request.method == "PUT":
            form = self._form(request)
            if member == "setswitch":
                self.switch["state"] = form.get("state") == "True"
                self.switch["value"] = 1.0 if self.switch["state"] else 0.0
                return self._value(None, transaction)
            if member == "setswitchvalue":
                value = float(form["value"])
                if not 0.0 <= value <= 1.0:
                    return self._error(0x401, "Value is out of range", transaction)
                self.switch["value"] = value
                self.switch["state"] = value > 0.0
                return self._value(None, transaction)
            return self._error(0x400, f"Switch.{member} is not implemented", transaction)
        if member == "maxswitch":
            return self._value(self.switch["maxswitch"], transaction)
        if member == "canwrite":
            return self._value(True, transaction)
        if member == "getswitch":
            return self._value(self.switch["state"], transaction)
        if member == "getswitchvalue":
            return self._value(self.switch["value"], transaction)
        if member == "getswitchname":
            return self._value("Dew heater", transaction)
        if member == "getswitchdescription":
            return self._value("Dew heater", transaction)
        if member == "minswitchvalue":
            return self._value(0.0, transaction)
        if member == "maxswitchvalue":
            return self._value(1.0, transaction)
        if member == "switchstep":
            return self._value(1.0, transaction)
        return self._error(0x400, f"Switch.{member} is not implemented", transaction)

    @staticmethod
    def _form(request: httpx.Request) -> dict[str, str]:
        # Alpaca parameter names are case-insensitive on the wire (architecture policy);
        # the real device accepts any spelling, so the fake must too.
        return {
            key.lower(): values[0]
            for key, values in parse_qs(request.content.decode("utf-8")).items()
            if values
        }

    def _transaction_id(self, request: httpx.Request) -> int:
        raw = (
            self._form(request).get("clienttransactionid")
            if request.method == "PUT"
            else parse_qs(request.url.query.decode("utf-8")).get("ClientTransactionID", ["0"])[0]
        )
        return int(raw or 0)

    @staticmethod
    def _value(value: object, transaction: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Value": value,
                "ClientTransactionID": transaction,
                "ServerTransactionID": transaction,
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    @staticmethod
    def _error(number: int, message: str, transaction: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ClientTransactionID": transaction,
                "ServerTransactionID": transaction,
                "ErrorNumber": number,
                "ErrorMessage": message,
            },
        )


@pytest.fixture
def fake_device() -> FakeNativeSeestar:
    return FakeNativeSeestar()


@pytest.fixture
async def native_backend(fake_device: FakeNativeSeestar) -> AsyncIterator[SeestarBackend]:
    session = NativeAlpacaSession(
        "device.invalid", port=32323, client_factory=fake_device.client_factory
    )
    backend = SeestarBackend(session, cache_ttl_s=0.0)
    try:
        yield backend
    finally:
        await backend.aclose()


@pytest.fixture
async def native_http(native_backend: SeestarBackend) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(native_backend, config=ServerConfig(server_name="SSC Alpaca native test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as client:
        yield client


async def _connect(http: httpx.AsyncClient, device: str, *, device_number: int = 0) -> None:
    response = await api_put(
        http, device, "connected", device_number=device_number, Connected=True
    )
    assert response.status_code == 200, response.text
    assert response.json()["ErrorNumber"] == 0, response.text


# --------------------------------------------------------------------------- #
# Telescope
# --------------------------------------------------------------------------- #


async def test_telescope_capabilities_come_from_the_device_not_the_8_46_constants(
    native_http: httpx.AsyncClient,
) -> None:
    await _connect(native_http, "telescope")
    # The 8.46 mapping hardcodes every one of these false (no verb existed);
    # a device that actually offers them must not be shadowed by that constant.
    assert await value(native_http, "telescope", "canpark") is True
    assert await value(native_http, "telescope", "canunpark") is True
    assert await value(native_http, "telescope", "canfindhome") is True
    assert await value(native_http, "telescope", "cansync") is True
    assert await value(native_http, "telescope", "canslewaltazasync") is False


async def test_telescope_reads_coordinates_the_8_46_mapping_could_never_offer(
    native_http: httpx.AsyncClient,
) -> None:
    await _connect(native_http, "telescope")
    assert await value(native_http, "telescope", "rightascension") == 5.5
    assert await value(native_http, "telescope", "declination") == 42.0
    assert await value(native_http, "telescope", "altitude") == 45.0
    assert await value(native_http, "telescope", "azimuth") == 180.0


async def test_telescope_frame_is_reported_verbatim_not_translated(
    native_http: httpx.AsyncClient,
) -> None:
    """Trap 4: EquatorialSystem is topocentric-of-date, AlignmentMode reads
    polar on a physically alt-az mount. The driver must relay both, not
    reinterpret either."""
    await _connect(native_http, "telescope")
    assert await value(native_http, "telescope", "equatorialsystem") == 1
    assert await value(native_http, "telescope", "alignmentmode") == 1


async def test_properties_are_refused_until_connected_and_never_read_early(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    number = await error_number(native_http, "telescope", "rightascension")
    assert number == ErrorNumber.NOT_CONNECTED
    assert fake_device.read_while_disconnected == [], (
        "a property must never reach the device before Connected = True — trap 1"
    )


async def test_park_refuses_below_the_horizon_and_findhome_recovers_it(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "telescope")
    fake_device.telescope["altitude"] = -50.84

    number = await put_error_number(native_http, "telescope", "park")
    assert number == _BELOW_HORIZON
    response = await api_put(native_http, "telescope", "park")
    assert "below the horizon" in response.json()["ErrorMessage"]

    findhome = await api_put(native_http, "telescope", "findhome")
    assert findhome.json()["ErrorNumber"] == 0
    assert fake_device.telescope["altitude"] == 0.0

    park_again = await api_put(native_http, "telescope", "park")
    assert park_again.json()["ErrorNumber"] == 0
    assert fake_device.telescope["atpark"] is True


async def test_unpark_alone_moves_nothing_a_motion_command_is_what_raises_the_arm(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "telescope")
    before = fake_device.telescope["altitude"]

    unpark = await api_put(native_http, "telescope", "unpark")
    assert unpark.json()["ErrorNumber"] == 0
    assert await value(native_http, "telescope", "atpark") is False
    assert fake_device.telescope["altitude"] == before, "Unpark permits motion; it does not move anything itself"

    moveaxis = await api_put(native_http, "telescope", "moveaxis", Axis=1, Rate=2.0)
    assert moveaxis.json()["ErrorNumber"] == 0
    assert fake_device.moveaxis_calls[-1] == ("1", "2.0")


async def test_moveaxis_rate_cap_is_the_devices_own_not_reinvented_here(
    native_http: httpx.AsyncClient,
) -> None:
    await _connect(native_http, "telescope")
    number = await put_error_number(native_http, "telescope", "moveaxis", Axis=0, Rate=10.0)
    assert number == _BAD_RATE


# --------------------------------------------------------------------------- #
# FilterWheel
# --------------------------------------------------------------------------- #


async def test_filterwheel_is_read_and_moved_through_its_own_connected_device(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "filterwheel")
    assert await value(native_http, "filterwheel", "names") == ["Dark", "IR", "LP"]
    assert await value(native_http, "filterwheel", "focusoffsets") == [0, 0, 0]

    moved = await api_put(native_http, "filterwheel", "position", Position=2)
    assert moved.json()["ErrorNumber"] == 0
    assert await value(native_http, "filterwheel", "position") == 2

    back = await api_put(native_http, "filterwheel", "position", Position=1)
    assert back.json()["ErrorNumber"] == 0
    assert await value(native_http, "filterwheel", "position") == 1
    assert fake_device.read_while_disconnected == []


# --------------------------------------------------------------------------- #
# Focuser
# --------------------------------------------------------------------------- #


async def test_focuser_is_read_and_moved_through_its_own_connected_device(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "focuser")
    assert await value(native_http, "focuser", "position") == 1210
    assert await value(native_http, "focuser", "maxstep") == 2600
    assert await value(native_http, "focuser", "temperature") == pytest.approx(31.3125)

    moved = await api_put(native_http, "focuser", "move", Position=1220)
    assert moved.json()["ErrorNumber"] == 0
    assert await value(native_http, "focuser", "position") == 1220

    back = await api_put(native_http, "focuser", "move", Position=1210)
    assert back.json()["ErrorNumber"] == 0
    assert await value(native_http, "focuser", "position") == 1210
    assert fake_device.read_while_disconnected == []


async def test_connecting_the_telescope_does_not_connect_the_focuser_on_the_device(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    """``NativeAlpacaSession`` connects only ``telescope/0`` at handshake
    (``ssc/seestar/native.py``); ``focuser/0`` is a distinct ASCOM device
    instance on the device's own server and must get its own ``Connected =
    True`` only when something actually reads it."""
    await _connect(native_http, "telescope")
    assert fake_device.connected["telescope/0"] is True
    assert fake_device.connected["focuser/0"] is False

    await _connect(native_http, "focuser")
    assert await value(native_http, "focuser", "position") == 1210
    assert fake_device.connected["focuser/0"] is True


# --------------------------------------------------------------------------- #
# Camera
# --------------------------------------------------------------------------- #


async def test_camera_capability_flags_come_from_the_device_not_the_8_46_constants(
    native_http: httpx.AsyncClient,
) -> None:
    # The 8.46 mapping hardcodes canabortexposure false (it starts no ASCOM
    # exposure); a device that actually does start one must not be shadowed.
    await _connect(native_http, "camera")
    assert await value(native_http, "camera", "canabortexposure") is True


async def test_camera_start_exposure_image_ready_and_image_array_round_trip(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "camera")
    assert await value(native_http, "camera", "imageready") is False

    started = await api_put(native_http, "camera", "startexposure", Duration=0.5, Light=True)
    assert started.json()["ErrorNumber"] == 0
    assert fake_device.start_exposure_calls == [("0.5", "True")]
    assert await value(native_http, "camera", "imageready") is True

    # ImageArray only ever answers through ImageBytes; ssc/client/alpaca.py's
    # own image_array() is what asks for it and decodes it.
    client = AlpacaClient(http=native_http)
    frame = await client.device("camera").image_array()
    assert frame.array.shape == fake_device.camera_frame.shape
    assert frame.array.dtype == fake_device.camera_frame.dtype
    np.testing.assert_array_equal(frame.array, fake_device.camera_frame)


async def test_image_array_without_the_imagebytes_accept_header_is_notimplemented(
    native_http: httpx.AsyncClient,
) -> None:
    """This driver never falls back to the plain JSON array — on the device's
    surface, or its own (``docs/M0-FINDINGS.md``: 2.5x the size for the same
    frame)."""
    await _connect(native_http, "camera")
    number = await error_number(native_http, "camera", "imagearray")
    assert number == ErrorNumber.NOT_IMPLEMENTED


async def test_image_array_before_connected_is_notconnected_not_a_transport_error(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    """The binary path repeats the ordinary ``Connected`` guard rather than
    inheriting it (``CameraDevice.image_bytes``) — this pins that down."""
    client = AlpacaClient(http=native_http)
    with pytest.raises(AlpacaRemoteError) as caught:
        await client.device("camera").image_array()
    assert caught.value.is_not_connected
    assert fake_device.read_while_disconnected == []


async def test_connecting_the_telescope_does_not_connect_the_camera_on_the_device(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "telescope")
    assert fake_device.connected["telescope/0"] is True
    assert fake_device.connected["camera/0"] is False


# --------------------------------------------------------------------------- #
# The wide optics and the dew heater — the three devices that join the
# original four (``server.py::build_devices``); see ``docs/M0-FINDINGS.md`` 9.31.
# --------------------------------------------------------------------------- #


async def test_focuser_1_is_the_wide_optics_own_device_not_focuser_0_again(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "focuser", device_number=0)
    await _connect(native_http, "focuser", device_number=1)

    tele = await api_get(native_http, "focuser", "position", device_number=0)
    wide = await api_get(native_http, "focuser", "position", device_number=1)
    assert tele.json()["Value"] == 1210
    assert wide.json()["Value"] == 340
    assert wide.json()["Value"] != tele.json()["Value"]
    # Each sub-device gets its own Connected = True on the device's own
    # server, lazily, the first time one of its properties is actually read
    # (``SeestarBackend._ensure_native_connected``) — not at SSC's own
    # ``Connected = True``, which only acquires the shared session.
    assert fake_device.connected["focuser/0"] is True
    assert fake_device.connected["focuser/1"] is True

    moved = await api_put(native_http, "focuser", "move", device_number=1, Position=400)
    assert moved.json()["ErrorNumber"] == 0
    assert (await api_get(native_http, "focuser", "position", device_number=1)).json()["Value"] == 400
    # The telephoto focuser is untouched by moving the wide one.
    assert (await api_get(native_http, "focuser", "position", device_number=0)).json()["Value"] == 1210


async def test_camera_1_is_the_wide_cameras_own_device_and_frame(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    await _connect(native_http, "camera", device_number=1)

    started = await api_put(
        native_http, "camera", "startexposure", device_number=1, Duration=0.2, Light=True
    )
    assert started.json()["ErrorNumber"] == 0
    assert fake_device.connected["camera/1"] is True
    assert fake_device.connected["camera/0"] is False
    ready = await api_get(native_http, "camera", "imageready", device_number=1)
    assert ready.json()["Value"] is True

    client = AlpacaClient(http=native_http)
    frame = await client.device("camera", 1).image_array()
    assert frame.array.shape == fake_device.camera_frame_wide.shape
    np.testing.assert_array_equal(frame.array, fake_device.camera_frame_wide)
    # It is a genuinely different payload from the telephoto's own frame.
    assert not np.array_equal(frame.array, fake_device.camera_frame)


async def test_switch_0_reports_the_one_dew_heater_switch_once_connected(
    native_http: httpx.AsyncClient,
) -> None:
    await _connect(native_http, "switch")
    assert await value(native_http, "switch", "maxswitch") == 1
    assert await value(native_http, "switch", "getswitchname", Id=0) == "Dew heater"
    assert await value(native_http, "switch", "getswitch", Id=0) is False

    turned_on = await api_put(native_http, "switch", "setswitch", Id=0, State=True)
    assert turned_on.json()["ErrorNumber"] == 0
    assert await value(native_http, "switch", "getswitch", Id=0) is True
    assert await value(native_http, "switch", "getswitchvalue", Id=0) == 1.0

    assert await value(native_http, "switch", "minswitchvalue", Id=0) == 0.0
    assert await value(native_http, "switch", "maxswitchvalue", Id=0) == 1.0


async def test_switch_0_maxswitch_is_refused_before_connecting_not_the_lying_zero(
    native_http: httpx.AsyncClient, fake_device: FakeNativeSeestar
) -> None:
    """The real device answers ``MaxSwitch`` ``0`` while disconnected
    (``docs/M0-FINDINGS.md`` 9.31 — "the not-connected trap, third instance").
    This driver's own ``Connected`` guard must refuse before that lying read
    is ever reached."""
    number = await error_number(native_http, "switch", "maxswitch")
    assert number == _NOT_CONNECTED
    assert fake_device.read_while_disconnected == []
