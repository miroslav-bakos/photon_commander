# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``camera/1``, ``focuser/1`` and ``switch/0`` — the three devices that join
the original four (``server.py::build_devices``).

Against the emulator (Seestar JSON-RPC, firmware 8.46) none of the three has a
wire route at all — see the ``NO_JSON_RPC_ROUTE_*`` constants in
``ssc/alpaca/devices/camera.py``, ``focuser.py`` and ``switch.py`` — so every
member but the ASCOM common set and ``Connected`` must answer ``NotImplemented``
rather than relaying ``camera/0``'s own data under the wrong device number.
Their real behaviour is exercised over the native Alpaca surface in
``tests/alpaca/test_native_passthrough.py``.
"""

from __future__ import annotations

import httpx

from ssc.alpaca.errors import ErrorNumber
from tests.alpaca.conftest import api_get, api_put, error_number, value


async def _connect(http: httpx.AsyncClient, device: str, device_number: int = 0) -> None:
    response = await api_put(http, device, "connected", device_number=device_number, Connected=True)
    assert response.status_code == 200, response.text
    assert response.json()["ErrorNumber"] == 0, response.text


async def test_camera_1_and_focuser_1_are_reachable_but_refuse_the_8_46_mapping(
    http: httpx.AsyncClient,
) -> None:
    await _connect(http, "camera", device_number=1)
    await _connect(http, "focuser", device_number=1)

    for device, member in (
        ("camera", "gain"),
        ("camera", "cameraxsize"),
        ("camera", "sensortype"),
        ("focuser", "position"),
        ("focuser", "maxstep"),
    ):
        number = await error_number(http, device, member, device_number=1)
        assert number == ErrorNumber.NOT_IMPLEMENTED, (device, member)
        response = await api_get(http, device, member, device_number=1)
        assert "camera/1" in response.text or "focuser/1" in response.text, response.text


async def test_camera_0_is_unaffected_by_camera_1s_refusal(http: httpx.AsyncClient) -> None:
    """The gate is per-instance: connecting and reading camera/1 must not
    change what camera/0 (the telephoto, the 8.46 mapping's own device)
    answers."""
    await _connect(http, "camera", device_number=0)
    await _connect(http, "camera", device_number=1)

    assert await value(http, "camera", "cameraxsize", device_number=0) == 2160
    number = await error_number(http, "camera", "cameraxsize", device_number=1)
    assert number == ErrorNumber.NOT_IMPLEMENTED


async def test_switch_0_refuses_the_8_46_mapping_but_common_members_still_answer(
    http: httpx.AsyncClient,
) -> None:
    for member in ("connected", "description", "driverinfo", "supportedactions"):
        response = await api_get(http, "switch", member)
        assert response.status_code == 200
        assert response.json()["ErrorNumber"] == 0, (member, response.text)

    await _connect(http, "switch")
    for member in ("maxswitch", "getswitch", "getswitchname"):
        params = {"Id": 0} if member != "maxswitch" else {}
        number = await error_number(http, "switch", member, **params)
        assert number == ErrorNumber.NOT_IMPLEMENTED, member


async def test_switch_setters_refuse_the_8_46_mapping(http: httpx.AsyncClient) -> None:
    await _connect(http, "switch")
    response = await api_put(http, "switch", "setswitch", Id=0, State=True)
    assert response.json()["ErrorNumber"] == ErrorNumber.NOT_IMPLEMENTED


async def test_camera_1s_own_connected_is_independent_of_camera_0s(
    http: httpx.AsyncClient,
) -> None:
    await _connect(http, "camera", device_number=0)
    assert (await api_get(http, "camera", "connected", device_number=1)).json()["Value"] is False
    await _connect(http, "camera", device_number=1)
    assert (await api_get(http, "camera", "connected", device_number=1)).json()["Value"] is True
