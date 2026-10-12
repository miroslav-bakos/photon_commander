"""Shared fixtures for the protocol tests.

Everything runs against the in-process emulator or a scripted transport. No test
in this directory opens a socket — ``pytest-socket`` is on, and the offline path
is the point.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.seestar.transport import TransportClosedError


class ScriptedTransport:
    """A transport whose peer says exactly what a test tells it to say."""

    def __init__(self, name: str = "scripted") -> None:
        self.sent: list[str] = []
        self._name = name
        self._inbox: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._connected = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def send_line(self, line: str) -> None:
        if not self._connected:
            raise TransportClosedError(f"{self._name}: not connected")
        self.sent.append(line)

    async def receive_line(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self) -> None:
        self._connected = False
        self._inbox.put_nowait(TransportClosedError(f"{self._name}: closed"))

    # -- test-side controls -------------------------------------------------- #

    def feed(self, line: str) -> None:
        self._inbox.put_nowait(line)

    def feed_response(
        self,
        request_id: int,
        method: str,
        *,
        result: object = None,
        code: int = 0,
        error: str | None = None,
        timestamp: str = "969.942410482",
    ) -> None:
        envelope: dict[str, object] = {
            "jsonrpc": "2.0",
            "Timestamp": timestamp,
            "method": method,
            "id": request_id,
        }
        if error is None:
            envelope["result"] = result
        else:
            envelope["error"] = error
        envelope["code"] = code
        self.feed(json.dumps(envelope))

    def fail(self, exc: BaseException) -> None:
        self._inbox.put_nowait(exc)


@pytest.fixture
def emulator_config() -> EmulatorConfig:
    """Observed behaviours, observed timings collapsed so CI is not slow."""
    return EmulatorConfig(event_stream_silence_s=0.01, reset_refusal_s=0.05)


@pytest.fixture
async def emulator(emulator_config: EmulatorConfig) -> AsyncIterator[SeestarEmulator]:
    device = SeestarEmulator(emulator_config)
    try:
        yield device
    finally:
        await device.aclose()


@pytest.fixture
def recording_sleeper() -> tuple[list[float], Callable[[float], Awaitable[None]]]:
    """A sleeper that records the delays asked for and sleeps a tiny fraction.

    Lets a test assert the backoff policy without waiting for it.
    """
    delays: list[float] = []

    async def sleeper(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(min(delay, 0.01))

    return delays, sleeper
