# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Choosing a control surface by observation, never by firmware version.

The rule these tests exist to hold: SSC must not decide how to talk to a scope
from the number the firmware calls itself. The device this was built against
reports "9.31", which is not a public release — ZWO re-sent the same firmware
under a higher number to deliver an installer fix — so version ordering does not
track capability on this product line. What is observable is whether a healthy
Alpaca server answers, and that is what routing is allowed to look at.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping, Sequence

import httpx
import pytest

from ssc.seestar.native import NativeAlpacaSession
from ssc.seestar.routing import (
    ALPACA_DISCOVERY_PORT,
    CONTROL_SURFACE_ENV,
    ControlSurface,
    RoutedSession,
    SurfaceChoice,
    forced_surface,
    probe_control_surface,
)
from ssc.seestar.session import (
    CAPABILITY_PROBE_KEYS,
    ConnectionState,
    DeviceInfo,
    JsonRpcSeestarSession,
    SessionError,
    SessionStatus,
)
from tests.seestar.test_native_alpaca import DEVICES, FakeDeviceAlpaca

NATIVE = SurfaceChoice(
    surface=ControlSurface.NATIVE_ALPACA,
    alpaca_port=32323,
    evidence="UDP 32227 advertised AlpacaPort 32323; configureddevices listed 7 device(s)",
)
JSON_RPC = SurfaceChoice(
    surface=ControlSurface.JSON_RPC,
    alpaca_port=None,
    evidence="UDP 32227 did not answer; no Alpaca server on the standard port",
)


class StubSession:
    """A session that only has to be picked, started and read."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.started = False
        self.closed = False
        self._listeners: list[Callable[[SessionStatus], None]] = []
        self._status = SessionStatus(state=ConnectionState.DISCONNECTED, endpoint=f"{host}:{port}")

    @property
    def status(self) -> SessionStatus:
        return self._status

    @property
    def state(self) -> ConnectionState:
        return self._status.state

    @property
    def device(self) -> DeviceInfo | None:
        return None

    def subscribe_state(self, listener: Callable[[SessionStatus], None]) -> Callable[[], None]:
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def subscribe_events(self, listener: Callable[[object], None]) -> Callable[[], None]:
        return lambda: None

    async def start(self) -> None:
        self.started = True
        self._publish(
            SessionStatus(
                state=ConnectionState.DEGRADED,
                endpoint=f"{self.host}:{self.port}",
                reason="a reason of the delegate's own",
                control_connected=True,
            )
        )

    async def aclose(self) -> None:
        self.closed = True

    async def wait_for_state(
        self,
        states: Collection[ConnectionState],
        *,
        timeout: float = 5.0,  # noqa: ASYNC109 - mirrors the session protocol
    ) -> SessionStatus:
        del states, timeout
        return self._status

    async def call(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
        raise_on_error: bool = True,
        allow_unverified: bool = False,
    ) -> object:
        raise AssertionError("not exercised")

    async def get_device_state(
        self,
        keys: Sequence[str] = CAPABILITY_PROBE_KEYS,
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - mirrors the session protocol
    ) -> object:
        raise AssertionError("not exercised")

    def _publish(self, status: SessionStatus) -> None:
        self._status = status
        for listener in list(self._listeners):
            listener(status)


def _probe(choice: SurfaceChoice) -> Callable[[str], object]:
    async def probe(host: str) -> SurfaceChoice:
        del host
        return choice

    return probe


def _routed(
    choice: SurfaceChoice, control_port: int = 4701
) -> tuple[RoutedSession, dict[str, StubSession]]:
    built: dict[str, StubSession] = {}

    def native(host: str, port: int) -> StubSession:
        built["native"] = StubSession(host, port)
        return built["native"]

    def jsonrpc(host: str, port: int) -> StubSession:
        built["json-rpc"] = StubSession(host, port)
        return built["json-rpc"]

    session = RoutedSession(
        "10.0.0.9",
        control_port,
        probe=_probe(choice),  # type: ignore[arg-type]
        native_factory=native,  # type: ignore[arg-type]
        jsonrpc_factory=jsonrpc,  # type: ignore[arg-type]
    )
    return session, built


async def test_a_healthy_alpaca_server_wins_and_the_port_comes_from_the_probe() -> None:
    session, built = _routed(NATIVE)
    await session.start()

    assert "json-rpc" not in built
    assert built["native"].port == 32323
    assert built["native"].started is True
    assert session.choice == NATIVE


async def test_no_alpaca_server_means_the_json_rpc_control_port() -> None:
    session, built = _routed(JSON_RPC)
    await session.start()

    assert "native" not in built
    assert built["json-rpc"].port == 4701


async def test_the_json_rpc_fallback_is_never_aimed_at_the_alpaca_port() -> None:
    """A remembered address carries the port that last worked. On a device
    driven over its own Alpaca server that is :32323, so a transient probe
    failure used to try a second port. Current Seestar firmware can accept TCP
    on :4701 without answering commands, so that fallback hid the actual fix:
    re-enable the device's Alpaca Protocol toggle. Observed in the field on
    2026-09-14."""
    session, built = _routed(JSON_RPC, control_port=32323)
    with pytest.raises(SessionError, match="Check device service availability") as caught:
        await session.start()

    assert "json-rpc" not in built
    assert "did not try legacy JSON-RPC" in str(caught.value)
    assert ":4701" in str(caught.value)
    assert session.choice is not None
    assert session.status.state is ConnectionState.DISCONNECTED
    assert session.status.endpoint == "10.0.0.9:32323"
    assert session.status.reason == str(caught.value)


async def test_a_real_json_rpc_control_port_is_left_alone() -> None:
    session, built = _routed(JSON_RPC, control_port=4801)
    await session.start()

    assert built["json-rpc"].port == 4801
    assert session.choice is not None
    assert "is an Alpaca port" not in session.choice.evidence


async def test_the_evidence_is_published_not_just_logged() -> None:
    session, _ = _routed(NATIVE)
    seen: list[SessionStatus] = []
    session.subscribe_state(seen.append)
    await session.start()

    # Both the delegate's own reason and what SSC observed to choose it, so a
    # user on an unrecognisable build number can see why SSC picked this path.
    assert session.status.reason is not None
    assert "a reason of the delegate's own" in session.status.reason
    assert "control surface: native-alpaca" in session.status.reason
    assert NATIVE.evidence in session.status.reason
    assert seen and seen[-1].reason == session.status.reason


async def test_before_start_there_is_no_surface_and_no_pretending() -> None:
    session, _ = _routed(NATIVE)

    assert session.choice is None
    assert session.status.state is ConnectionState.DISCONNECTED
    assert session.device is None
    with pytest.raises(SessionError):
        await session.call("test_connection")


async def test_a_forced_surface_skips_the_probe_entirely() -> None:
    async def explode(host: str) -> SurfaceChoice:
        raise AssertionError("the probe must not run when a surface is forced")

    built: dict[str, StubSession] = {}

    def native(host: str, port: int) -> StubSession:
        built["native"] = StubSession(host, port)
        return built["native"]

    session = RoutedSession(
        "10.0.0.9",
        4701,
        surface=ControlSurface.NATIVE_ALPACA,
        probe=explode,  # type: ignore[arg-type]
        native_factory=native,  # type: ignore[arg-type]
    )
    await session.start()

    assert built["native"].started is True
    assert session.choice is not None
    assert CONTROL_SURFACE_ENV in session.choice.evidence


def test_an_unrecognised_forced_surface_fails_loudly() -> None:
    assert forced_surface({}) is ControlSurface.AUTO
    assert forced_surface({CONTROL_SURFACE_ENV: "native-alpaca"}) is ControlSurface.NATIVE_ALPACA
    assert forced_surface({CONTROL_SURFACE_ENV: " JSON-RPC "}) is ControlSurface.JSON_RPC
    with pytest.raises(SessionError) as caught:
        forced_surface({CONTROL_SURFACE_ENV: "carrier-pigeon"})
    assert "carrier-pigeon" in str(caught.value)


def _silent(host: str, timeout: float) -> object:
    """No Alpaca discovery answer — the "try the standard port" branch."""

    async def discover() -> int | None:
        return None

    del host, timeout
    return discover()


def _advertises(port: int) -> Callable[[str, float], object]:
    def discover(host: str, timeout: float) -> object:
        del host, timeout

        async def answer() -> int | None:
            return port

        return answer()

    return discover


async def test_the_probe_reports_native_alpaca_when_a_server_answers() -> None:
    device = FakeDeviceAlpaca()
    choice = await probe_control_surface(
        "10.0.0.9",
        client_factory=device.client_factory,
        discover_port=_silent,  # type: ignore[arg-type]
    )

    assert choice.surface is ControlSurface.NATIVE_ALPACA
    assert choice.alpaca_port == 32323
    assert f"listed {len(DEVICES)} device(s)" in choice.evidence
    assert "did not answer" in choice.evidence


async def test_the_advertised_port_is_used_rather_than_the_standard_one() -> None:
    device = FakeDeviceAlpaca()
    choice = await probe_control_surface(
        "10.0.0.9",
        client_factory=device.client_factory,
        discover_port=_advertises(11111),  # type: ignore[arg-type]
    )

    assert choice.alpaca_port == 11111
    assert f"UDP {ALPACA_DISCOVERY_PORT} advertised AlpacaPort 11111" in choice.evidence


async def test_the_probe_falls_back_when_nothing_answers_there() -> None:
    device = FakeDeviceAlpaca()
    device.offline = True
    choice = await probe_control_surface(
        "10.0.0.9",
        client_factory=device.client_factory,
        discover_port=_silent,  # type: ignore[arg-type]
    )

    assert choice.surface is ControlSurface.JSON_RPC
    assert choice.alpaca_port is None
    assert "no Alpaca server answered there" in choice.evidence


async def test_the_probe_refuses_a_server_that_does_not_speak_alpaca_v1() -> None:
    device = FakeDeviceAlpaca(api_versions=(2,))
    choice = await probe_control_surface(
        "10.0.0.9",
        client_factory=device.client_factory,
        discover_port=_silent,  # type: ignore[arg-type]
    )

    assert choice.surface is ControlSurface.JSON_RPC
    assert "apiversions [2]" in choice.evidence


async def test_a_slow_server_does_not_hold_up_a_connect() -> None:
    async def crawl(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    def factory(host: str, port: int) -> object:
        from ssc.client.alpaca import AlpacaClient

        return AlpacaClient(
            http=httpx.AsyncClient(
                transport=httpx.MockTransport(crawl), base_url=f"http://{host}:{port}"
            )
        )

    choice = await probe_control_surface(
        "10.0.0.9",
        client_factory=factory,  # type: ignore[arg-type]
        timeout=0.05,
        discover_port=_silent,  # type: ignore[arg-type]
    )

    assert choice.surface is ControlSurface.JSON_RPC
    assert "TimeoutError" in choice.evidence


def test_the_two_implementations_are_what_routing_can_choose() -> None:
    # A guard against the fork quietly growing a third arm somewhere else: these
    # are the only two sessions in SSC, and both satisfy the same protocol.
    for implementation in (JsonRpcSeestarSession, NativeAlpacaSession):
        for member in ("start", "aclose", "wait_for_state", "call", "get_device_state"):
            assert callable(getattr(implementation, member))
