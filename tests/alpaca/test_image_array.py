"""``AlpacaDeviceClient``'s capture-transfer members, against a mocked server.

No emulator here: these exercise only the HTTP contract — the Accept header,
the content-type check, and the refusal to fall back to JSON — leaving the
binary decode itself to ``test_imagebytes.py``.
"""

from __future__ import annotations

import struct

import httpx
import numpy as np
import pytest

from ssc.client import AlpacaClient, AlpacaTransportError
from ssc.client.imagebytes import ImageArrayElementType

_HEADER_FORMAT = "<iiIIiiiiiii"
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


def _image_bytes_response(client_transaction_id: int) -> httpx.Response:
    array = np.arange(6, dtype="<u2").reshape(2, 3)
    header = struct.pack(
        _HEADER_FORMAT,
        1,  # metadata version
        0,  # error number
        client_transaction_id,
        1,  # server transaction id
        _HEADER_SIZE,  # data start
        ImageArrayElementType.UINT16,
        ImageArrayElementType.UINT16,
        2,  # rank
        2,  # dimension1
        3,  # dimension2
        0,  # dimension3
    )
    return httpx.Response(
        200,
        content=header + array.tobytes(),
        headers={"content-type": "application/imagebytes"},
    )


async def test_image_array_requests_imagebytes_and_decodes_it() -> None:
    seen: dict[str, object] = {}

    async def driver(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        seen["client_transaction_id"] = int(request.url.params["ClientTransactionID"])
        return _image_bytes_response(seen["client_transaction_id"])

    transport = httpx.MockTransport(driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        frame = await client.device("camera").image_array()

    assert seen["accept"] == "application/imagebytes"
    assert frame.array.shape == (2, 3)
    np.testing.assert_array_equal(frame.array, np.arange(6).reshape(2, 3))
    assert frame.client_transaction_id == seen["client_transaction_id"]


async def test_a_json_response_to_an_imagebytes_request_is_refused_not_reparsed() -> None:
    async def driver(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Value": [[1, 2]], "ErrorNumber": 0, "ErrorMessage": ""})

    transport = httpx.MockTransport(driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        with pytest.raises(AlpacaTransportError, match="does not fall back to the JSON array"):
            await client.device("camera").image_array()


async def test_start_exposure_and_image_ready_use_the_ordinary_json_path() -> None:
    """These are plain ASCOM members — no ImageBytes involved, no new
    transport logic; this only pins down the member names and parameters."""
    calls: list[tuple[str, dict[str, str]]] = []

    async def driver(request: httpx.Request) -> httpx.Response:
        params = (
            dict(request.url.params)
            if request.method == "GET"
            else dict(httpx.QueryParams(request.content.decode()))
        )
        calls.append((request.url.path, params))
        value: object = True if request.url.path.endswith("/imageready") else None
        return httpx.Response(
            200,
            json={
                "Value": value,
                "ClientTransactionID": int(params["ClientTransactionID"]),
                "ServerTransactionID": 1,
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    transport = httpx.MockTransport(driver)
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        client = AlpacaClient(http=http)
        camera = client.device("camera")
        await camera.start_exposure(1.0, light=True)
        ready = await camera.image_ready()

    assert ready is True
    put_path, put_params = calls[0]
    assert put_path == "/api/v1/camera/0/startexposure"
    assert put_params["Duration"] == "1.0"
    assert put_params["Light"] == "True"
    get_path, _ = calls[1]
    assert get_path == "/api/v1/camera/0/imageready"
