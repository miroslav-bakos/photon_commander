"""Interop authentication and strict credential-free battery projection."""
from __future__ import annotations

import asyncio
import base64
import json

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ssc.seestar.interop import read_battery


async def test_authenticated_battery_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    reader = asyncio.StreamReader()
    sent: list[dict] = []
    class Writer:
        closed = False
        def write(self, data: bytes) -> None:
            request = json.loads(data)
            sent.append(request)
            result = {1: {"str": "challenge"}, 2: 0, 3: {
                "ap": {"password": "never-expose-this"},
                "pi_status": {"battery_capacity": 73, "charger_status": "Charging", "charge_online": True},
            }}[request["id"]]
            reader.feed_data((json.dumps({"id": request["id"], "result": result, "code": 0}) + "\r\n").encode())
        async def drain(self) -> None:
            pass
        def close(self) -> None:
            self.closed = True
    writer = Writer()
    async def connect(host: str, port: int, **_: object):
        assert (host, port) == ("scope.test", 4700)
        return reader, writer
    monkeypatch.setattr(asyncio, "open_connection", connect)
    result = await read_battery("scope.test", pem)
    key.public_key().verify(base64.b64decode(sent[1]["params"]["sign"]), b"challenge",
                            padding.PKCS1v15(), hashes.SHA1())
    assert sent[2]["params"] == {}
    assert result["percent"] == 73
    assert result["external_power"] is True
    assert "never-expose-this" not in json.dumps(result)
    assert set(result) == {"percent", "charging_status", "external_power", "measured_at", "note"}
    assert writer.closed


async def test_stalled_channel_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption()).decode()
    async def stalled(*_: object, **__: object):
        await asyncio.Event().wait()
    monkeypatch.setattr(asyncio, "open_connection", stalled)
    with pytest.raises(TimeoutError):
        await read_battery("scope.test", pem, budget_s=0.01)
