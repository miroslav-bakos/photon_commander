"""Handshake, capability negotiation, the method gate and credential hygiene."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator

import pytest

from ssc.seestar.emulator import PLACEHOLDER_SECRET, EmulatorConfig, SeestarEmulator
from ssc.seestar.session import (
    CAPABILITY_PROBE_KEYS,
    COMMAND_VOCABULARY_KNOWN,
    VERIFIED_COMMAND_METHODS,
    VERIFIED_METHODS,
    VERIFIED_READ_METHODS,
    AuthenticationRequiredError,
    BackoffPolicy,
    ConnectionState,
    HazardousCallError,
    JsonRpcSeestarSession,
    MethodNotAvailableError,
    NotConnectedError,
    SeestarSession,
    SessionError,
    UnverifiedMethodError,
    identify,
)

QUIET = {
    "event_stream_silence_s": 0.01,
    "close_event_stream_after_push": False,
    "reset_refusal_s": 0.05,
}


def _session(emulator: SeestarEmulator) -> SeestarSession:
    return JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(1),
        call_timeout=2.0,
        event_reattach_delay_s=0.01,
    )


async def _wait_for_reason(live: JsonRpcSeestarSession, needle: str, *, budget: float = 5.0) -> str:
    """Poll until a published reason mentions `needle`. Reasons are the contract."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    while loop.time() < deadline:
        reason = live.status.reason
        if reason and needle in reason:
            return reason
        await asyncio.sleep(0.01)
    raise AssertionError(
        f"no published reason mentioned {needle!r}; last was {live.status.reason!r}"
    )


@pytest.fixture
async def session() -> AsyncIterator[SeestarSession]:
    emulator = SeestarEmulator(EmulatorConfig(**QUIET), event_script=())
    live = _session(emulator)
    await live.start()
    try:
        yield live
    finally:
        await live.aclose()
        await emulator.aclose()


# --------------------------------------------------------------------------- #
# The state machine
# --------------------------------------------------------------------------- #


async def test_a_verified_device_reaches_ready_through_every_transition(
    session: JsonRpcSeestarSession,
) -> None:
    seen: list[ConnectionState] = []
    session.subscribe_state(lambda status: seen.append(status.state))

    status = await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    assert status.reason is None
    assert status.control_connected is True
    assert ConnectionState.READY in seen
    # CONNECTING/HANDSHAKING happen before a subscriber can attach, so assert the
    # published status rather than the full history here.
    assert session.state is ConnectionState.READY


async def test_the_device_identity_is_recorded_from_its_own_report(
    session: JsonRpcSeestarSession,
) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    device = session.device
    assert device is not None
    assert device.firmware == "8.46"
    assert device.model == "Seestar S30 Pro"
    assert device.is_verified is True
    assert device.capabilities["can_wide_cam_af"] is True
    assert device.firmware_is_verified is True


async def test_an_unverified_firmware_is_degraded_with_a_reason_not_a_crash() -> None:
    emulator = SeestarEmulator(EmulatorConfig(firmware="9.99", **QUIET), event_script=())
    live = _session(emulator)
    await live.start()
    try:
        status = await live.wait_for_state({ConnectionState.DEGRADED}, timeout=5.0)

        assert status.control_connected is True  # usable, but honest about it
        assert status.reason is not None
        assert "9.99" in status.reason
        assert live.device is not None
        assert live.device.firmware == "9.99"
    finally:
        await live.aclose()
        await emulator.aclose()


async def test_a_locked_device_fails_loudly_rather_than_guessing_a_handshake() -> None:
    emulator = SeestarEmulator(EmulatorConfig(is_verified=False, **QUIET), event_script=())
    live = _session(emulator)
    await live.start()
    try:
        reason = await _wait_for_reason(live, "authentication required")

        assert "pi_is_verified=false" in reason
        assert live.state is ConnectionState.DISCONNECTED
        assert live.device is None
    finally:
        await live.aclose()
        await emulator.aclose()


async def test_a_stalled_handshake_publishes_what_it_is_waiting_for() -> None:
    """A handshake that hangs must still say what it hangs on (invariant 7).

    Firmware 9.31's control port accepts TCP and then answers nothing at all, so
    the handshake stalls on its very first call. Because the RPC budget and the
    caller's readiness budget are both ``DEFAULT_TIMEOUT_S``, the RPC timeout
    loses that race by milliseconds and never publishes anything — the only
    thing the user was told was "no reason published". The emulator stands in
    for that here with a reply the device has simply not sent yet.
    """
    emulator = SeestarEmulator(EmulatorConfig(reply_delay_s=30.0, **QUIET), event_script=())
    live = _session(emulator)
    seen: list[tuple[ConnectionState, str | None]] = []
    live.subscribe_state(lambda status: seen.append((status.state, status.reason)))
    await live.start()
    try:
        await _wait_for_reason(live, "test_connection")

        assert (
            ConnectionState.HANDSHAKING,
            "waiting for a reply to test_connection",
        ) in seen, seen
        assert not any(
            state is ConnectionState.HANDSHAKING and reason is None for state, reason in seen
        ), f"a reasonless HANDSHAKING was published: {seen}"
    finally:
        await live.aclose()
        await emulator.aclose()


def test_authentication_required_is_its_own_error_type() -> None:
    assert issubclass(AuthenticationRequiredError, SessionError)


# --------------------------------------------------------------------------- #
# The method gate: never probe an unknown method against a live device
# --------------------------------------------------------------------------- #


async def test_an_unverified_method_is_refused_locally(session: SeestarSession) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    with pytest.raises(UnverifiedMethodError):
        await session.call("scope_open_arm")


async def test_a_method_probed_and_found_absent_is_named_as_such(
    session: JsonRpcSeestarSession,
) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    with pytest.raises(MethodNotAvailableError) as caught:
        await session.call("scope_speed_move")
    assert "103" in str(caught.value)


async def test_the_call_that_crashes_the_control_server_is_blocked(
    session: JsonRpcSeestarSession,
) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    with pytest.raises(HazardousCallError):
        await session.call("get_device_state")
    with pytest.raises(HazardousCallError):
        await session.call("get_device_state", {"keys": []})
    with pytest.raises(HazardousCallError):
        await session.get_device_state([])

    assert session.state is ConnectionState.READY  # nothing was sent


async def test_the_command_vocabulary_is_mapped_but_still_has_no_scope_namespace() -> None:
    """M0.5/M0.6 mapped a command vocabulary. It is real but it is not complete."""
    assert COMMAND_VOCABULARY_KNOWN is True
    # The single most useful negative result on 8.46: there is no scope_* family
    # at all. Community ports written for older firmware fail wholesale.
    assert not any(name.startswith("scope_") for name in VERIFIED_METHODS)
    assert all(name.startswith(("get_", "pi_", "test_")) for name in VERIFIED_READ_METHODS)
    # Verbs are enumerated from observation, never derived from the convention:
    # stop_merid_flip and stop_pa follow the same pattern and do not exist.
    assert "start_auto_goto" in VERIFIED_COMMAND_METHODS
    assert "stop_merid_flip" not in VERIFIED_METHODS
    assert not VERIFIED_READ_METHODS & VERIFIED_COMMAND_METHODS


async def test_calls_before_the_handshake_completes_are_refused() -> None:
    emulator = SeestarEmulator(EmulatorConfig(**QUIET), event_script=())
    live = _session(emulator)
    try:
        with pytest.raises(NotConnectedError):
            await live.call("test_connection")
    finally:
        await emulator.aclose()


# --------------------------------------------------------------------------- #
# Credential hygiene
# --------------------------------------------------------------------------- #


async def test_the_capability_probe_never_asks_for_a_credential_key(
    session: JsonRpcSeestarSession,
) -> None:
    assert "ap" not in CAPABILITY_PROBE_KEYS
    assert "setting" not in CAPABILITY_PROBE_KEYS

    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)
    state = await session.get_device_state()
    assert state.device is not None
    assert state.mount is not None
    assert state.mount.close is True


async def test_requesting_a_credential_key_is_refused(session: SeestarSession) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    with pytest.raises(SessionError):
        await session.get_device_state(["device", "ap"])
    with pytest.raises(SessionError):
        await session.call("get_device_state", {"keys": ["setting"]})


async def test_a_credential_bearing_call_warns_and_never_logs_the_secret(
    session: JsonRpcSeestarSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    await session.wait_for_state({ConnectionState.READY}, timeout=5.0)

    with caplog.at_level(logging.DEBUG, logger="ssc.seestar"):
        response = await session.call("get_setting")

    assert isinstance(response.result, dict)
    assert response.result["password"] == PLACEHOLDER_SECRET  # the caller does get it
    assert PLACEHOLDER_SECRET not in caplog.text  # the log never does
    assert PLACEHOLDER_SECRET not in repr(response.redacted())
    assert any("cleartext credentials" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- #
# One-shot identify (what "Test connection" reports)
# --------------------------------------------------------------------------- #


async def test_identify_reports_the_real_result(emulator: SeestarEmulator) -> None:
    info = await identify(emulator.transport_factory(), timeout=2.0)

    assert info.reachable is True
    assert info.firmware == "8.46"
    assert info.serial == "EMULATED0"
    assert info.device_uptime_s is not None
    assert info.note is None


async def test_identify_reports_a_refusal_as_a_result_not_an_exception(
    emulator: SeestarEmulator,
) -> None:
    emulator.trigger_control_reset("test")

    info = await identify(emulator.transport_factory(), timeout=0.2)

    assert info.reachable is False
    assert info.note is not None
    assert "PortRefusedError" in info.note
