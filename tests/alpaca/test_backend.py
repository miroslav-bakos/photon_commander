"""The seam itself: the verb gate, error translation, caching and refcounting."""

from __future__ import annotations

from dataclasses import replace

import pytest

from ssc.alpaca.backend import SeestarBackend, translate_device_error
from ssc.alpaca.errors import (
    AlpacaNotImplementedError,
    InvalidOperationError,
    NotConnectedError,
    UnspecifiedError,
    ValueNotSetError,
)
from ssc.seestar.emulator import SeestarEmulator
from ssc.seestar.rpc import (
    CODE_METHOD_NOT_FOUND,
    CODE_NO_SOLVE_DATA,
    CODE_NOT_SUPPORTED,
    DeviceError,
)
from ssc.seestar.session import ConnectionState
from tests.alpaca.conftest import QUIET, make_backend


def test_device_codes_map_to_the_right_ascom_numbers() -> None:
    assert isinstance(
        translate_device_error(
            "start_stack", DeviceError(CODE_METHOD_NOT_FOUND, "method not found")
        ),
        AlpacaNotImplementedError,
    )
    not_supported = translate_device_error(
        "start_auto_goto", DeviceError(CODE_NOT_SUPPORTED, "not supported")
    )
    assert isinstance(not_supported, InvalidOperationError)
    assert "star view" in not_supported.message, "the precondition is named, not implied"
    assert isinstance(
        translate_device_error(
            "get_solve_result", DeviceError(CODE_NO_SOLVE_DATA, "no solve data")
        ),
        ValueNotSetError,
    )
    unknown = translate_device_error("get_albums", DeviceError(999, "who knows"))
    assert isinstance(unknown, UnspecifiedError)
    assert "not in SSC's observed error table" in unknown.message


async def test_an_unverified_method_never_reaches_the_wire(backend: SeestarBackend) -> None:
    await backend.acquire("test")
    with pytest.raises(AlpacaNotImplementedError, match="unverified"):
        await backend.call("start_stack")
    with pytest.raises(AlpacaNotImplementedError):
        await backend.call("scope_park")


async def test_calls_before_connecting_are_notconnected(backend: SeestarBackend) -> None:
    with pytest.raises(NotConnectedError):
        await backend.call("get_camera_info")
    with pytest.raises(NotConnectedError):
        await backend.device_state()


async def test_the_session_is_refcounted_across_devices(backend: SeestarBackend) -> None:
    await backend.acquire("telescope/0")
    await backend.acquire("camera/0")
    assert backend.is_connected

    await backend.release("telescope/0")
    assert backend.is_connected, "the camera still holds it open"
    await backend.release("camera/0")
    assert not backend.is_connected


async def test_acquire_is_idempotent_per_holder(backend: SeestarBackend) -> None:
    await backend.acquire("telescope/0")
    await backend.acquire("telescope/0")
    assert backend.holders == {"telescope/0"}
    await backend.release("telescope/0")
    assert not backend.is_connected


async def test_reads_are_cached_and_a_command_clears_the_cache(
    emulator: SeestarEmulator,
) -> None:
    calls: list[str] = []
    delegate = emulator._methods["get_camera_state"]

    def counting(params: object) -> object:
        calls.append("get_camera_state")
        return delegate(params)  # type: ignore[arg-type]

    emulator.add_method("get_camera_state", counting)  # type: ignore[arg-type]

    backend = make_backend(emulator, cache_ttl_s=60.0)
    try:
        await backend.acquire("test")
        await backend.call("get_camera_state", cache=True)
        await backend.call("get_camera_state", cache=True)
        assert len(calls) == 1, "the second read came from the cache"

        await backend.call("iscope_start_view", {"mode": "star"})
        await backend.call("get_camera_state", cache=True)
        assert len(calls) == 2, "a command verb invalidates every cached read"
    finally:
        await backend.aclose()


async def test_a_credential_bearing_key_is_refused_without_leaking_it(
    backend: SeestarBackend,
) -> None:
    await backend.acquire("test")
    with pytest.raises(Exception) as caught:
        await backend.device_state(("ap", "setting"))
    message = str(caught.value)
    assert "credential" in message
    assert "EMULATOR" not in message and "passwd" not in message


async def test_degraded_but_control_connected_session_still_serves_members() -> None:
    """A live control channel must not be reported as ``NotConnected``.

    ``code-reviewer`` finding 1: firmware unverified against this driver (or an
    event stream that failed to re-attach) leaves the session ``DEGRADED``, but
    ``control_connected`` stays ``True`` — the mount is still reachable. Before
    the fix, ``acquire`` refused to consider the session connected unless it
    reached ``READY``, so it timed out and tore the session back down, and
    every subsequent call — most of all ``AbortSlew``'s ``stop_auto_goto`` —
    answered ``NotConnected`` for a mount that was perfectly capable of being
    stopped.
    """
    emulator = SeestarEmulator(replace(QUIET, firmware="9.99"))
    backend = make_backend(emulator)
    try:
        await backend.acquire("telescope/0")

        assert backend.is_connected, "control is up; DEGRADED must still count as connected"
        assert backend.status.state is ConnectionState.DEGRADED
        assert backend.status.control_connected is True
        assert "9.99" in (backend.status.reason or "")

        # The emergency stop must be reachable while the mount could be moving.
        result = await backend.call("stop_auto_goto")
        assert result == 0
    finally:
        await backend.aclose()
        await emulator.aclose()


async def test_a_session_that_never_comes_up_is_still_notconnected() -> None:
    """Genuinely not connected — auth locked — must still answer ``0x407``.

    The fix for finding 1 widens ``acquire`` to accept ``DEGRADED`` alongside
    ``READY``; it must not also accept a session that never reaches a
    control-up state at all.
    """
    emulator = SeestarEmulator(replace(QUIET, is_verified=False))
    backend = make_backend(emulator, cache_ttl_s=0.0, ready_budget_s=0.3)
    try:
        with pytest.raises(NotConnectedError):
            await backend.acquire("telescope/0")
        assert not backend.is_connected
    finally:
        await backend.aclose()
        await emulator.aclose()


@pytest.mark.parametrize("camera_state", [0, 2, 3, 4])
async def test_battery_refresh_only_when_both_cameras_idle(
    monkeypatch: pytest.MonkeyPatch, camera_state: int,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.settings.secrets import SecretStore

    backend = SeestarBackend()
    session = object()
    monkeypatch.setattr(backend, "_native_session", lambda: session)
    monkeypatch.setattr(SeestarBackend, "device_info", property(lambda _: SimpleNamespace(host="scope.test")))
    monkeypatch.setattr(SeestarBackend, "is_connected", property(lambda _: True))
    monkeypatch.setattr(backend, "native_device", lambda _, number: SimpleNamespace(get=AsyncMock(return_value=0 if number == 0 else camera_state)))
    monkeypatch.setattr(SecretStore, "get", AsyncMock(return_value="secret"))
    read = AsyncMock(return_value={"percent": 80, "measured_at": "2026-10-06T05:00:00Z", "note": None})
    monkeypatch.setattr("ssc.alpaca.backend.read_battery", read)
    await backend._refresh_battery()
    assert read.await_count == (1 if camera_state == 0 else 0)
    if camera_state != 0:
        assert backend._battery["percent"] is None
        assert backend._battery["note"] == "Waiting for an idle camera gap"


async def test_battery_failure_does_not_clear_last_measurement_or_expose_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.settings.secrets import SecretStore

    backend = SeestarBackend()
    session = object()
    monkeypatch.setattr(backend, "_native_session", lambda: session)
    monkeypatch.setattr(SeestarBackend, "device_info", property(lambda _: SimpleNamespace(host="scope.test")))
    monkeypatch.setattr(backend, "native_device", lambda *_: SimpleNamespace(get=AsyncMock(return_value=0)))
    monkeypatch.setattr(SecretStore, "get", AsyncMock(return_value="secret"))
    monkeypatch.setattr("ssc.alpaca.backend.read_battery", AsyncMock(side_effect=TimeoutError("secret-value")))
    backend._battery = {"percent": 80, "measured_at": "2026-10-06T05:00:00Z", "note": None}
    await backend._refresh_battery()
    assert backend._battery["percent"] == 80
    assert backend._battery["measured_at"] == "2026-10-06T05:00:00Z"
    assert "secret-value" not in str(backend._battery)
