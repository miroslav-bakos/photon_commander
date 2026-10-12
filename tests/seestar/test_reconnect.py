"""The reconnect path, driven by the device's own reset-then-refuse fault.

This is the test the findings demand: an ``ECONNRESET`` means the control server
restarted, the port then refuses connections for several seconds, and a naive
retry loop would hold the device down. It has to be exercised offline, in CI.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable

from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.seestar.session import (
    BackoffPolicy,
    ConnectionState,
    JsonRpcSeestarSession,
    SessionStatus,
)

BACKOFF = BackoffPolicy(initial_s=0.5, factor=2.0, cap_s=4.0)
QUIET_EVENTS = {"event_stream_silence_s": 0.01, "close_event_stream_after_push": False}


def test_backoff_is_jittered_capped_and_never_collapses_to_zero() -> None:
    rng = random.Random(7)
    delays = [BACKOFF.delay(attempt, rng) for attempt in range(1, 12) for _ in range(20)]

    assert min(delays) >= BACKOFF.initial_s / 2, "a zero delay would hot-loop the device"
    assert max(delays) <= BACKOFF.cap_s
    assert len(set(delays)) > 1, "identical delays mean no jitter, so clients synchronise"
    assert BACKOFF.delay(6, rng) > BACKOFF.delay(1, rng)


async def test_a_control_reset_reconnects_through_published_states(
    recording_sleeper: tuple[list[float], Callable[[float], Awaitable[None]]],
) -> None:
    delays, sleeper = recording_sleeper
    emulator = SeestarEmulator(
        EmulatorConfig(reset_refusal_s=0.08, **QUIET_EVENTS), event_script=()
    )
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BACKOFF,
        rng=random.Random(3),
        sleeper=sleeper,
        call_timeout=2.0,
    )
    history: list[SessionStatus] = []
    session.subscribe_state(history.append)
    await session.start()
    try:
        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)
        connections_before = emulator.control_connection_count

        # The observed fault: the control server restarts under us.
        emulator.trigger_control_reset("params-less get_device_state")

        await session.wait_for_state({ConnectionState.DISCONNECTED}, timeout=5.0)
        loss = next(
            status
            for status in history
            if status.state is ConnectionState.DISCONNECTED and status.reason
        )
        assert "ECONNRESET" in (loss.reason or "")
        assert "control server restarted" in (loss.reason or "")

        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

        assert emulator.control_connection_count > connections_before
        assert emulator.control_reset_count == 1
        assert session.device is not None
        assert session.status.reason is None
    finally:
        await session.aclose()
        await emulator.aclose()

    # Every wait during the refusal window respected the backoff floor: the
    # driver never hot-loops a device that is trying to come back up.
    assert delays, "the reconnect path must back off, not spin"
    assert min(delays) >= BACKOFF.initial_s / 2


async def test_the_refusal_window_is_survived_not_fought(
    recording_sleeper: tuple[list[float], Callable[[float], Awaitable[None]]],
) -> None:
    delays, sleeper = recording_sleeper
    # A long refusal relative to the sleeper's real wait forces several refused
    # attempts, which is exactly the situation that would pin the device down.
    emulator = SeestarEmulator(
        EmulatorConfig(reset_refusal_s=0.15, **QUIET_EVENTS), event_script=()
    )
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BACKOFF,
        rng=random.Random(11),
        sleeper=sleeper,
        call_timeout=2.0,
    )
    reasons: list[str] = []
    session.subscribe_state(lambda status: reasons.append(status.reason or ""))
    await session.start()
    try:
        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)
        emulator.trigger_control_reset()
        await session.wait_for_state({ConnectionState.DISCONNECTED}, timeout=5.0)
        await session.wait_for_state({ConnectionState.READY}, timeout=10.0)
    finally:
        await session.aclose()
        await emulator.aclose()

    assert any("refusing" in reason for reason in reasons), (
        "the refusal window must be reported, not silently retried"
    )
    assert min(delays) >= BACKOFF.initial_s / 2


async def test_an_in_flight_call_fails_when_the_server_resets_under_it() -> None:
    emulator = SeestarEmulator(
        EmulatorConfig(reply_delay_s=0.2, reset_refusal_s=0.01, **QUIET_EVENTS),
        event_script=(),
    )
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(5),
        call_timeout=5.0,
    )
    await session.start()
    try:
        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

        pending = asyncio.create_task(session.call("get_setting"))
        await asyncio.sleep(0.02)
        emulator.trigger_control_reset()

        try:
            result = await asyncio.wait_for(pending, timeout=5.0)
        except Exception as exc:
            assert type(exc).__name__ in {"RpcConnectionLostError", "NotConnectedError"}
        else:  # pragma: no cover - the point of the test is that this cannot happen
            raise AssertionError(f"a reset call must not resolve, got {result!r}")
    finally:
        await session.aclose()
        await emulator.aclose()


async def test_losing_the_event_stream_is_reported_separately_from_control() -> None:
    """The device pushes an event then closes: routine, not a degradation."""
    emulator = SeestarEmulator(
        EmulatorConfig(event_stream_silence_s=0.005, close_event_stream_after_push=True)
    )
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(2),
        call_timeout=2.0,
        event_reattach_delay_s=0.005,
    )
    events: list[str] = []
    session.subscribe_events(lambda event: events.append(type(event).__name__))
    await session.start()
    try:
        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)
        await asyncio.sleep(0.15)

        assert emulator.event_connection_count > 1, "the stream must be re-attached"
        assert "PiStatusEvent" in events
        assert "EventStreamClosed" in events
        # Control is untouched by the event stream's normal churn.
        assert session.state is ConnectionState.READY
    finally:
        await session.aclose()
        await emulator.aclose()
