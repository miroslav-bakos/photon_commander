# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""Bounded authenticated read-only telemetry on the Seestar interop channel.

Only the battery allowlist leaves this module. Device-state replies contain
Wi-Fi credentials and must never be logged or returned wholesale.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


async def read_battery(host: str, pem: str, *, budget_s: float = 3.0) -> dict[str, object]:
    """Authenticate, read, close within one budget; never touch imaging state."""
    async with asyncio.timeout(budget_s):
        key = serialization.load_pem_private_key(pem.encode(), password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise ValueError("Seestar interop requires an RSA private key")
        reader, writer = await asyncio.open_connection(host, 4700, limit=262144)
        try:
            async def call(seq: int, method: str, params: dict[str, object]) -> object:
                writer.write((json.dumps({"id": seq, "method": method, "params": params}) + "\r\n").encode())
                await writer.drain()
                while True:
                    line = await reader.readline()
                    if not line:
                        raise ConnectionError("interop channel closed")
                    reply = json.loads(line)
                    if reply.get("id") != seq:
                        continue
                    if reply.get("error") or reply.get("code", 0) != 0:
                        raise ValueError("interop request rejected")
                    return reply.get("result")

            challenge_result = await call(1, "get_verify_str", {})
            if not isinstance(challenge_result, dict) or not isinstance(challenge_result.get("str"), str):
                raise ValueError("interop authentication challenge unavailable")
            challenge = challenge_result["str"]
            signature = key.sign(challenge.encode(), padding.PKCS1v15(), hashes.SHA1())
            if await call(2, "verify_client", {"data": challenge, "sign": base64.b64encode(signature).decode()}) != 0:
                raise ValueError("interop authentication refused")
            result = await call(3, "get_device_state", {})
            pi = result.get("pi_status") if isinstance(result, dict) else None
            if not isinstance(pi, dict):
                raise ValueError("battery telemetry unavailable")
            capacity = pi.get("battery_capacity")
            if isinstance(capacity, bool) or not isinstance(capacity, (int, float)) or not 0 <= capacity <= 100:
                raise ValueError("battery capacity unavailable")
            return {
                "percent": float(capacity),
                "charging_status": pi.get("charger_status") if isinstance(pi.get("charger_status"), str) else None,
                "external_power": pi.get("charge_online") if isinstance(pi.get("charge_online"), bool) else None,
                "measured_at": datetime.now(UTC).isoformat(),
                "note": None,
            }
        finally:
            writer.close()
            # Close locally without letting an unresponsive device extend the budget.
