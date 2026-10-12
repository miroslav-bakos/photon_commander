# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

import ssc.api.capture_orchestrator as capture_orchestrator
from ssc.api.capture_orchestrator import CaptureDitherSettleError, CaptureService, _dither_due
from ssc.astro.frames import j2000_coord
from ssc.astro.units import Deg
from ssc.capture import FrameType
from ssc.platesolve import PlateSolveResult
from ssc.state.events import MountChanged
from ssc.state.models import CaptureState, MountState, StateSnapshot
from ssc.state.store import StateStore


@pytest.mark.asyncio
async def test_manual_capture_releases_scheduled_dither_origin(monkeypatch) -> None:
    service = object.__new__(CaptureService)
    now = datetime(2026, 10, 8, tzinfo=UTC)
    service._state = StateStore(StateSnapshot(server_started_at=now, mount=MountState(
        polled_at=now, stale=False, slewing=False, tracking=True,
        ra_hours=2.0, dec_deg=30.0, ra_j2000_h=2.0, dec_j2000_deg=30.0)))
    service._section = CaptureState()
    service._dither_object_name = "M31"
    service._dither_pixel_scale_arcsec = 3.6
    service.set_dither_center(0.711667, 41.266667)
    # While scheduled, neither offset telemetry nor object-name assignment
    # silently replaces the optically verified planned center.
    service._remember_dither_center("M31")
    assert service._dither_center_j2000 == (0.711667, 41.266667)
    async def accept(**kwargs):
        return service._section
    service._request_exposure = accept
    await service.request_exposure(duration_s=1.0, gain=200, object_name="M42", frame_type=FrameType.LIGHT)
    assert not service._dither_center_explicit
    service._remember_dither_center("M42")
    assert service._dither_center_j2000 == (2.0, 30.0)
    monkeypatch.setattr(capture_orchestrator.random, "random", lambda: 0.0)
    ra, dec = service._next_dither_target(pixels=10, max_pixels=200)
    assert dec == 30.0
    assert ra == pytest.approx(2.0 + 0.01 / (15 * math.cos(math.radians(30))))


@pytest.mark.parametrize(
    ("frame_type", "enabled", "completed_lights", "expected"),
    [
        (FrameType.LIGHT, True, 5, False),
        (FrameType.LIGHT, True, 6, True),
        (FrameType.LIGHT, True, 12, True),
        (FrameType.DARK, True, 6, False),
        (FrameType.LIGHT, False, 6, False),
    ],
)
def test_dither_cadence_counts_only_completed_light_frames(
    frame_type: FrameType, enabled: bool, completed_lights: int, expected: bool
) -> None:
    assert (
        _dither_due(
            frame_type=frame_type,
            enabled=enabled,
            completed_lights=completed_lights,
            every_n_frames=6,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_dither_waits_for_stopped_and_stable_fresh_mount_samples() -> None:
    polled_at = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at,
                stale=False,
                slewing=True,
                ra_hours=10.0,
                dec_deg=20.0,
                ra_j2000_h=10.0,
                dec_j2000_deg=20.0,
            ),
        )
    )
    sleeps = 0

    async def advance_mount(_delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        updates = {
            1: (10.0, False),
            # About 56 arcsec on sky at +20 degrees: not settled yet.
            2: (10.0011, False),
            3: (10.0011, False),
        }
        ra_hours, slewing = updates.get(sleeps, (10.0011, False))
        state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=polled_at + timedelta(seconds=sleeps),
                    stale=False,
                    slewing=slewing, tracking=True,
                    ra_hours=ra_hours,
                    dec_deg=20.0,
                    ra_j2000_h=ra_hours,
                    dec_j2000_deg=20.0,
                )
            )
        )

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = advance_mount

    await service._wait_for_dither_settle(
        target_j2000=(10.0011, 20.0), after_polled_at=polled_at
    )

    assert sleeps == 32


@pytest.mark.asyncio
async def test_dither_accepts_stable_mount_with_normal_pointing_residual() -> None:
    """A stopped mount may finish tens of arcseconds from the requested offset.

    That is harmless for dithering and must not turn a healthy sequence into the
    settle-timeout failure seen during IC 1805 first light.
    """
    polled_at = datetime(2026, 9, 16, 2, 12, tzinfo=UTC)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at,
                stale=False,
                slewing=True,
                ra_hours=10.0,
                dec_deg=20.0,
                ra_j2000_h=10.0,
                dec_j2000_deg=20.0,
            ),
        )
    )
    sleeps = 0

    async def advance_mount(_delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        # A stable offset outside the former 90-arcsec gate must not fail a panel.
        ra_hours = 10.01
        state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=polled_at + timedelta(seconds=sleeps),
                    stale=False,
                    slewing=False, tracking=True,
                    ra_hours=ra_hours,
                    dec_deg=20.0,
                    ra_j2000_h=ra_hours,
                    dec_j2000_deg=20.0,
                )
            )
        )

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = advance_mount

    await service._wait_for_dither_settle(target_j2000=(10.0, 20.0), after_polled_at=polled_at)

    assert sleeps == 31


@pytest.mark.asyncio
async def test_scheduled_dither_rechecks_timeout_without_reissuing_move() -> None:
    from unittest.mock import AsyncMock, Mock

    polled_at = datetime(2026, 10, 6, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=polled_at))
    service._publish = Mock()
    service._sleep = AsyncMock()
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(10.0, 20.0))
    service._dither = AsyncMock()
    service._restore_dither_tracking = AsyncMock()
    service._wait_for_dither_settle = AsyncMock(side_effect=[
        capture_orchestrator.CaptureDitherSettleError("settling delayed"), None,
    ])

    await service.dither_scheduled(15, 200, "NGC 7000 1")

    service._dither.assert_awaited_once_with(10.0, 20.0)
    assert service._wait_for_dither_settle.await_count == 2
    service._publish.assert_any_call(last_error="Dither recovery: settling delayed")
    service._publish.assert_called_with(
        in_progress=False, last_error=None, stage=None, duration_s=None,
        started_at=None, object_name=None, frame_type=None, gain=None,
    )


@pytest.mark.asyncio
async def test_dither_stability_is_bounded_over_whole_interval() -> None:
    polled_at = datetime(2026, 10, 4, tzinfo=UTC)
    state = StateStore(StateSnapshot(server_started_at=polled_at))
    sleeps = 0

    async def advance(_delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        assert sleeps < 35
        # Every step is less than 5 arcsec, but their sum is not stable.
        ra = 10 + 4 * min(sleeps - 1, 3) / 54000
        state.apply(MountChanged(mount=MountState(
            polled_at=polled_at + timedelta(seconds=sleeps), stale=False,
            slewing=False, tracking=True, ra_hours=ra, dec_deg=0,
            ra_j2000_h=ra, dec_j2000_deg=0,
        )))

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = advance
    await service._wait_for_dither_settle(target_j2000=(10, 0), after_polled_at=polled_at)
    assert sleeps == 33


@pytest.mark.asyncio
async def test_dither_timeout_tries_to_stop_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(capture_orchestrator, "_DITHER_SETTLE_TIMEOUT_S", 0.001)
    polled_at = datetime(2026, 9, 18, 23, 42, tzinfo=UTC)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at + timedelta(seconds=1),
                stale=False,
                slewing=True,
                ra_hours=10.0,
                dec_deg=20.0,
                ra_j2000_h=10.0,
                dec_j2000_deg=20.0,
            ),
        )
    )
    stopped = False

    async def no_wait(_delay_s: float) -> None:
        return None

    async def stop_mount() -> None:
        nonlocal stopped
        stopped = True

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = no_wait
    service._dither_abort = stop_mount

    with pytest.raises(capture_orchestrator.CaptureDitherSettleError):
        await service._wait_for_dither_settle(
            target_j2000=(10.001, 20.0), after_polled_at=polled_at
        )

    assert stopped is True


@pytest.mark.asyncio
async def test_scheduled_dither_waits_for_fresh_coordinates_instead_of_failing() -> None:
    polled_at = datetime(2026, 9, 24, 2, 24, tzinfo=UTC)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at,
                stale=True,
                slewing=False, tracking=True,
                ra_j2000_h=10.0,
                dec_j2000_deg=20.0,
            ),
        )
    )
    sleeps = 0

    async def publish_fresh_mount(_delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1
        state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=polled_at + timedelta(seconds=1),
                    stale=False,
                    slewing=False, tracking=True,
                    ra_j2000_h=10.0,
                    dec_j2000_deg=20.0,
                )
            )
        )

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = publish_fresh_mount

    await service._wait_for_fresh_dither_coordinates()

    assert sleeps == 1


@pytest.mark.asyncio
async def test_scheduled_dither_fails_after_bounded_stale_coordinate_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(capture_orchestrator, "_DITHER_COORDINATE_RETRIES", 2)
    polled_at = datetime(2026, 9, 24, 2, 24, tzinfo=UTC)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at,
                stale=True,
                slewing=False, tracking=True,
                ra_j2000_h=10.0,
                dec_j2000_deg=20.0,
            ),
        )
    )
    sleeps = 0

    async def remain_stale(_delay_s: float) -> None:
        nonlocal sleeps
        sleeps += 1

    service = object.__new__(CaptureService)
    service._state = state
    service._sleep = remain_stale

    with pytest.raises(
        capture_orchestrator.CaptureDitherSettleError,
        match="waiting 60 seconds for telemetry to recover",
    ):
        await service._wait_for_fresh_dither_coordinates()

    assert sleeps == 2


def test_dither_targets_are_random_absolute_offsets_around_fixed_center(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    polled_at = datetime(2026, 9, 13, 20, 0, tzinfo=UTC)
    center = (10.0, 20.0)
    state = StateStore(
        StateSnapshot(
            server_started_at=polled_at,
            mount=MountState(
                polled_at=polled_at,
                stale=False,
                slewing=False, tracking=True,
                ra_j2000_h=center[0],
                dec_j2000_deg=center[1],
            ),
        )
    )

    directions = iter((0.0, 0.25, 0.125))
    monkeypatch.setattr("ssc.api.capture_orchestrator.random.random", lambda: next(directions))
    service = object.__new__(CaptureService)
    service._state = state
    service._dither_center_explicit = False
    service._dither_pixel_scale_arcsec = 3.6
    service._dither_center_j2000 = center
    service._last_dither_target_j2000 = center

    east = service._next_dither_target(pixels=100.0, max_pixels=200.0)
    state.apply(
        MountChanged(
            mount=MountState(
                polled_at=polled_at + timedelta(seconds=1),
                stale=False,
                slewing=False, tracking=True,
                ra_j2000_h=east[0],
                dec_j2000_deg=east[1],
            )
        )
    )
    north = service._next_dither_target(pixels=100.0, max_pixels=200.0)
    diagonal = service._next_dither_target(pixels=100.0, max_pixels=200.0)

    assert east[1] == pytest.approx(center[1])
    assert east[0] > center[0]
    assert north[0] == pytest.approx(center[0])
    assert north[1] == pytest.approx(center[1] + 0.1)
    assert diagonal[0] > center[0]
    assert diagonal[1] > center[1]


def test_a_dither_larger_than_thirty_arcseconds_is_not_treated_as_drift() -> None:
    service = object.__new__(CaptureService)
    reference = PlateSolveResult(
        coord=j2000_coord(Deg(150.0), Deg(20.0)),
        date_obs=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
        pixel_scale_arcsec=3.6,
        rotation_deg=0.0,
        solver="fake",
        solver_version="1",
    )
    dithered = PlateSolveResult(
        coord=j2000_coord(Deg(150.1), Deg(20.0)),
        date_obs=datetime(2026, 9, 13, 20, 1, tzinfo=UTC),
        pixel_scale_arcsec=3.6,
        rotation_deg=0.0,
        solver="fake",
        solver_version="1",
    )
    service._center_reference = reference

    drift = service._drift_pixels(dithered, dither_applied=True)

    assert drift is None
    assert service._center_reference is dithered


@pytest.mark.asyncio
async def test_post_dither_tracking_is_commanded_and_verified_after_motion_stops() -> None:
    from unittest.mock import AsyncMock

    polled_at = datetime(2026, 10, 6, tzinfo=UTC)
    state = StateStore(StateSnapshot(server_started_at=polled_at, mount=MountState(
        polled_at=polled_at, stale=False, slewing=True, tracking=False,
    )))
    service = object.__new__(CaptureService)
    service._state = state
    service._telescope = AsyncMock()
    service._telescope.get.return_value = True

    async def stopped(_delay: float) -> None:
        service._telescope.put.assert_not_awaited()
        state.apply(MountChanged(mount=MountState(
            polled_at=polled_at + timedelta(seconds=1), stale=False,
            slewing=False, tracking=False,
        )))

    service._sleep = stopped
    await service._restore_dither_tracking(after_polled_at=polled_at)
    service._telescope.put.assert_awaited_once_with("tracking", Tracking=True)
    service._telescope.get.assert_awaited_once_with("tracking")


@pytest.mark.asyncio
async def test_unconfirmed_post_dither_tracking_remains_recoverable() -> None:
    from unittest.mock import AsyncMock

    polled_at = datetime(2026, 10, 6, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=polled_at, mount=MountState(
        polled_at=polled_at + timedelta(seconds=1), stale=False,
        slewing=False, tracking=False,
    )))
    service._telescope = AsyncMock()
    service._telescope.get.return_value = False
    with pytest.raises(capture_orchestrator.CaptureDitherSettleError, match="confirm tracking"):
        await service._restore_dither_tracking(after_polled_at=polled_at)


@pytest.mark.asyncio
async def test_scheduled_dither_stop_interrupts_recovery_without_another_move() -> None:
    import asyncio
    from unittest.mock import AsyncMock, Mock

    from ssc.state.events import RunChanged
    from ssc.state.models import RunLifecycle, RunState

    at = datetime(2026, 10, 7, 1, 27, tzinfo=UTC)
    active = RunState(run_id="live", plan_id="plan", plan_name="night", plan_revision=64,
                      night_date=at.date(), status=RunLifecycle.RUNNING, started_at=at)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at, run=active))
    service._publish = Mock()
    async def yield_recovery(delay: float) -> None:
        await asyncio.sleep(0.001)

    service._sleep = AsyncMock(side_effect=yield_recovery)
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(10.0, 20.0))
    service._dither = AsyncMock()
    service._restore_dither_tracking = AsyncMock()

    async def interrupted_settle(**kwargs: object) -> None:
        service._state.apply(RunChanged(run=active.model_copy(update={"status": RunLifecycle.STOPPED})))
        raise capture_orchestrator.CaptureDitherSettleError("settling delayed")

    service._wait_for_dither_settle = AsyncMock(side_effect=interrupted_settle)
    with pytest.raises(capture_orchestrator.CaptureDitherSettleError, match="durable run Stop"):
        await asyncio.wait_for(service.dither_scheduled(15, 200, "NGC 1499 2"), timeout=0.05)
    service._dither.assert_awaited_once()
    service._restore_dither_tracking.assert_awaited_once()
    service._wait_for_dither_settle.assert_awaited_once()
    assert service._publish.call_args.kwargs["in_progress"] is False


@pytest.mark.asyncio
async def test_stop_interrupts_pre_dither_coordinate_wait_before_motion() -> None:
    from unittest.mock import AsyncMock, Mock

    from ssc.state.events import RunChanged
    from ssc.state.models import RunLifecycle, RunState

    at = datetime(2026, 10, 7, tzinfo=UTC)
    active = RunState(run_id="live", plan_id="plan", plan_name="night", plan_revision=64,
                      night_date=at.date(), status=RunLifecycle.RUNNING, started_at=at)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at, run=active))
    service._publish = Mock()
    service._dither = AsyncMock()

    async def stop(_delay: float) -> None:
        service._state.apply(RunChanged(run=active.model_copy(update={"status": RunLifecycle.STOPPED})))

    service._sleep = AsyncMock(side_effect=stop)
    with pytest.raises(capture_orchestrator.CaptureDitherStoppedError, match="durable run Stop"):
        await service.dither_scheduled(15, 200, "NGC 1499 2")
    assert service._sleep.await_count == 1
    service._dither.assert_not_awaited()


@pytest.mark.asyncio
async def test_scheduled_dither_recovers_missing_coordinates_before_single_move() -> None:
    from unittest.mock import AsyncMock, Mock

    at = datetime(2026, 10, 7, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at))
    service._publish = Mock()
    service._sleep = AsyncMock()
    service._wait_for_fresh_dither_coordinates = AsyncMock(side_effect=[
        capture_orchestrator.CaptureDitherSettleError("telemetry unavailable after 60 seconds"),
        capture_orchestrator.CaptureDitherSettleError("telemetry still unavailable"), None,
    ])
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(10.0, 20.0))
    service._dither = AsyncMock()
    service._restore_dither_tracking = AsyncMock()
    service._wait_for_dither_settle = AsyncMock()

    await service.dither_scheduled(15, 200, "NGC 1499 2")

    assert service._wait_for_fresh_dither_coordinates.await_count == 3
    service._next_dither_target.assert_called_once()
    service._dither.assert_awaited_once_with(10.0, 20.0)
    service._publish.assert_any_call(
        last_error="Dither recovery: telemetry unavailable after 60 seconds"
    )
    service._publish.assert_called_with(
        in_progress=False, last_error=None, stage=None, duration_s=None,
        started_at=None, object_name=None, frame_type=None, gain=None,
    )


@pytest.mark.asyncio
async def test_settle_timeout_does_not_abort_an_already_stopped_tracking_mount(monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    monkeypatch.setattr(capture_orchestrator, "_DITHER_SETTLE_TIMEOUT_S", 0.001)
    polled_at = datetime(2026, 10, 7, 19, 40, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=polled_at, mount=MountState(
        polled_at=polled_at + timedelta(seconds=1), stale=False,
        slewing=False, tracking=True, ra_hours=10, dec_deg=20,
        ra_j2000_h=10, dec_j2000_deg=20,
    )))
    service._sleep = AsyncMock()
    service._dither_abort = AsyncMock()
    with pytest.raises(capture_orchestrator.CaptureDitherSettleError):
        await service._wait_for_dither_settle(target_j2000=(10, 20), after_polled_at=polled_at)
    service._dither_abort.assert_not_awaited()


@pytest.mark.asyncio
async def test_dither_recovery_preserves_already_confirmed_tracking() -> None:
    from unittest.mock import AsyncMock

    polled_at = datetime(2026, 10, 7, 19, 40, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=polled_at, mount=MountState(
        polled_at=polled_at + timedelta(seconds=1), stale=False,
        slewing=False, tracking=True,
    )))
    service._telescope = AsyncMock()
    service._telescope.get.return_value = True
    await service._restore_dither_tracking(after_polled_at=polled_at)
    service._telescope.put.assert_not_awaited()
    service._telescope.get.assert_awaited_once_with("tracking")


@pytest.mark.asyncio
async def test_completed_scheduled_dither_releases_capture_for_dark_calibration() -> None:
    from unittest.mock import AsyncMock, Mock

    from ssc.state.models import CaptureState

    polled_at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._section = CaptureState()
    service._state = StateStore(StateSnapshot(server_started_at=polled_at))
    service._sleep = AsyncMock()
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(10, 20))
    service._dither = AsyncMock()
    service._restore_dither_tracking = AsyncMock()
    service._wait_for_dither_settle = AsyncMock()

    await service.dither_scheduled(15, 200, "M45")

    # This is the actual guard used by start_calibration before opening darks.
    assert not service._wide_preview_busy()
    capture = service._state.snapshot.capture
    assert capture is not None
    assert capture.stage is None
    assert capture.object_name is None
    assert capture.frame_type is None


async def test_m45_dither_hands_off_at_science_window_end_without_exposing() -> None:
    from unittest.mock import AsyncMock, Mock

    from ssc.state.models import CaptureState, RunItemState, RunLifecycle, RunState

    at = datetime(2026, 10, 8, 2, 13, 55, tzinfo=UTC)
    now = at
    active = RunState(run_id="night", plan_id="plan", plan_name="night", plan_revision=52,
        night_date=at.date(), status=RunLifecycle.RUNNING, started_at=at,
        current_item_id="320c81893cbc4519a94d2aafb41ca213",
        items=[RunItemState(item_id="320c81893cbc4519a94d2aafb41ca213", kind="goto_and_stack",
            enabled=True, status="running", end_utc=at + timedelta(seconds=5))])
    service = object.__new__(CaptureService)
    service._section = CaptureState()
    service._state = StateStore(StateSnapshot(server_started_at=at, run=active,
        mount=MountState(polled_at=at, stale=False, slewing=False, tracking=True)))
    service._now = lambda: now
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(3.8, 24))
    service._telescope = AsyncMock()
    service._telescope.get.return_value = True
    service._dither_abort = AsyncMock()
    async def publish(_delay=0):
        nonlocal now
        now += timedelta(seconds=1)
        service._state.apply(MountChanged(mount=MountState(polled_at=now, stale=False,
            slewing=False, tracking=True, ra_hours=3.8, dec_deg=24,
            ra_j2000_h=3.8, dec_j2000_deg=24)))
    service._sleep = publish
    service._dither = AsyncMock(side_effect=lambda *_: None)
    # Publish a post-motion reading, not the pre-command stopped sample.
    async def motion(*_args):
        await publish()
    service._dither.side_effect = motion
    with pytest.raises(capture_orchestrator.CaptureDitherWindowEndedError):
        await service.dither_scheduled(15, 200, "M45")
    assert now == at + timedelta(seconds=5)
    service._dither.assert_awaited_once()
    service._dither_abort.assert_not_awaited()
    assert not service._wide_preview_busy()


async def test_drift_recovery_journals_before_one_tracking_reset_and_never_replays_dither() -> None:
    from unittest.mock import AsyncMock
    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at,
        mount=MountState(polled_at=at, stale=False, slewing=False, tracking=True)))
    calls = []
    async def record():
        calls.append("journal")
        return True
    async def put(member, **args):
        calls.append(args["Tracking"])
    service._dither_recovery_recorder = record
    service._telescope = AsyncMock()
    service._telescope.put.side_effect = put
    service._telescope.get.return_value = True
    assert await service._recover_dither_tracking(None)
    assert calls == ["journal", False, True]


async def test_expired_window_requires_new_stopped_telemetry_before_handoff() -> None:
    from unittest.mock import AsyncMock, Mock
    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at,
        mount=MountState(polled_at=at, stale=False, slewing=False)))
    service._dither_abort = AsyncMock()
    service._publish = Mock()
    polls = 0
    async def poll(_delay):
        nonlocal polls
        polls += 1
        service._state.apply(MountChanged(mount=MountState(
            polled_at=at + timedelta(seconds=polls), stale=False, slewing=polls == 1)))
    service._sleep = poll
    await service._finish_expired_dither(None, after_polled_at=at)
    service._dither_abort.assert_awaited_once()
    assert polls == 2  # The pre-command stopped sample and fresh moving sample are insufficient.


async def test_window_handoff_recovers_stop_transport_failure_without_exposing() -> None:
    from unittest.mock import AsyncMock, Mock
    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at,
        mount=MountState(polled_at=at, stale=True, slewing=None)))
    service._dither_abort = AsyncMock(side_effect=[TimeoutError("offline"), None])
    service._publish = Mock()
    polls = 0
    async def poll(_delay):
        nonlocal polls
        polls += 1
        if polls == 2:
            service._state.apply(MountChanged(mount=MountState(
                polled_at=at + timedelta(seconds=2), stale=False, slewing=False)))
    service._sleep = poll
    await service._finish_expired_dither(None, after_polled_at=at)
    assert service._dither_abort.await_count == 2
    assert "stop confirmation unavailable" in service._publish.call_args_list[0].kwargs["last_error"]


@pytest.mark.parametrize("fault", [None, "drift", "cumulative", "residual", "stale", "tracking", "transient", "gap", "solver", "nan"])
async def test_image_settle_uses_independent_positions_and_all_mount_events(fault):
    from types import SimpleNamespace

    stamp = datetime(2026, 10, 8, tzinfo=UTC)
    current = stamp
    calls = 0
    state = StateStore(StateSnapshot(server_started_at=stamp), buffer_size=1 if fault == "gap" else 512)

    def publish(*, tracking=True, stale=False):
        state.apply(MountChanged(mount=MountState(
            polled_at=current, stale=stale, slewing=False, tracking=tracking,
            ra_hours=3.0 + (calls % 2) / 3600, dec_deg=24.2,
            ra_j2000_h=3.0, dec_j2000_deg=24.2,
        )))

    publish()
    service = object.__new__(CaptureService)
    service._state = state
    service._now = lambda: current
    service._monotonic = lambda: (current - stamp).total_seconds()
    service._check_dither_run_stop = lambda _run_id: None

    async def probe():
        nonlocal calls, current
        calls += 1
        current += timedelta(seconds=10)
        if fault == "solver":
            raise RuntimeError("no solved stars")
        if fault == "transient":
            publish(tracking=False)
        if fault == "gap":
            publish()
        publish(tracking=fault != "tracking", stale=fault == "stale")
        offset_arcsec = 10 if fault == "drift" else (calls - 1) * 2 if fault == "cumulative" else 0
        ra = 3.0 + offset_arcsec / (54000 * math.cos(math.radians(24.2)))
        if fault == "residual":
            ra += 0.01
        if fault == "nan":
            ra = float("nan")
        return SimpleNamespace(coord=SimpleNamespace(ra_h=ra, dec_deg=24.2))

    async def sleep(_seconds):
        pass

    service._solve_dither_probe = probe
    service._sleep = sleep
    if fault is None:
        await service._wait_for_image_dither_settle(target_j2000=(3.0, 24.2), after_polled_at=stamp - timedelta(seconds=1), stop_run_id=None)
        assert calls == 5
    elif fault == "drift":
        # Constant displacement is stable, but a changing anchor-relative center is not.
        first = True
        async def drifting_probe():
            nonlocal first
            solved = await probe()
            if first:
                first = False
                solved.coord.ra_h = 3.0
            return solved
        service._solve_dither_probe = drifting_probe
        with pytest.raises(CaptureDitherSettleError, match="position changed"):
            await service._wait_for_image_dither_settle(target_j2000=(3.0, 24.2), after_polled_at=stamp - timedelta(seconds=1), stop_run_id=None)
    else:
        with pytest.raises(CaptureDitherSettleError):
            await service._wait_for_image_dither_settle(target_j2000=(3.0, 24.2), after_polled_at=stamp - timedelta(seconds=1), stop_run_id=None)


@pytest.mark.parametrize("interrupt", ["stop", "window"])
async def test_image_probe_cancellation_waits_for_camera_cleanup(interrupt):
    import asyncio

    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at, mount=MountState(
        polled_at=at, stale=False, tracking=True, slewing=False,
    )))
    service._now = lambda: at
    started = False
    cleaned = False

    def check(_run_id):
        if started:
            error = capture_orchestrator.CaptureDitherStoppedError if interrupt == "stop" else capture_orchestrator.CaptureDitherWindowEndedError
            raise error("interrupted")

    async def probe():
        nonlocal started, cleaned
        started = True
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned = True

    service._check_dither_run_stop = check
    service._solve_dither_probe = probe
    with pytest.raises(CaptureDitherSettleError, match="interrupted"):
        await asyncio.wait_for(service._wait_for_image_dither_settle(
            target_j2000=(3.0, 24.2), after_polled_at=at - timedelta(seconds=1), stop_run_id="run",
        ), timeout=1)
    assert cleaned


async def test_scheduled_dither_image_confirmation_never_repeats_offset():
    from unittest.mock import AsyncMock, Mock

    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at))
    service._publish = Mock()
    service._sleep = AsyncMock()
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._remember_dither_center = Mock()
    service._next_dither_target = Mock(return_value=(3.0, 24.2))
    service._dither = AsyncMock()
    service._restore_dither_tracking = AsyncMock()
    service._solve_dither_probe = AsyncMock()
    service._wait_for_dither_settle = AsyncMock(side_effect=CaptureDitherSettleError("position changed 13.7 arcsec"))
    service._wait_for_image_dither_settle = AsyncMock()
    await service.dither_scheduled(15, 200, "M45")
    service._dither.assert_awaited_once_with(3.0, 24.2)
    service._wait_for_image_dither_settle.assert_awaited_once()
    service._solve_dither_probe.assert_not_awaited()
    service._publish.assert_called_with(in_progress=False, last_error=None, stage=None,
                                       duration_s=None, started_at=None, object_name=None,
                                       frame_type=None, gain=None)


@pytest.mark.parametrize("pending_stop", [False, True])
async def test_probe_cleanup_failure_keeps_ownership_until_idle(pending_stop):
    from unittest.mock import AsyncMock, Mock

    service = object.__new__(CaptureService)
    service._publish = Mock()
    service._sleep = AsyncMock()
    service._cleanup_dither_probe = AsyncMock(side_effect=[RuntimeError("offline"), None])
    observed = []
    def check(_run_id):
        observed.append(service._dither_probe_cleanup_pending)
        if pending_stop:
            raise capture_orchestrator.CaptureDitherStoppedError("durable Stop")
    service._check_dither_run_stop = check
    if pending_stop:
        with pytest.raises(capture_orchestrator.CaptureDitherStoppedError):
            await service._recover_dither_probe_camera("run")
    else:
        await service._recover_dither_probe_camera("run")
    assert service._cleanup_dither_probe.await_count == 2
    assert service._sleep.await_count == 1
    assert observed == [False]
    assert not service._dither_probe_cleanup_pending
    assert all(call.kwargs["in_progress"] for call in service._publish.call_args_list)


async def test_forward_utc_jump_does_not_shorten_optical_proof():
    from types import SimpleNamespace

    stamp = datetime(2026, 10, 8, tzinfo=UTC)
    utc = stamp
    elapsed = 0.0
    count = 0
    service = object.__new__(CaptureService)
    state = StateStore(StateSnapshot(server_started_at=stamp))
    def publish():
        state.apply(MountChanged(mount=MountState(polled_at=utc, stale=False,
                                                  tracking=True, slewing=False)))
    publish()
    service._state = state
    service._now = lambda: utc
    service._monotonic = lambda: elapsed
    service._check_dither_run_stop = lambda _: None
    async def probe():
        nonlocal count, elapsed, utc
        count += 1
        elapsed += 10
        utc += timedelta(seconds=100 if count == 2 else 10)
        publish()
        return SimpleNamespace(coord=SimpleNamespace(ra_h=3.0, dec_deg=24.2))
    async def sleep(_):
        pass
    service._solve_dither_probe = probe
    service._sleep = sleep
    await service._wait_for_image_dither_settle(target_j2000=(3.0, 24.2),
                                               after_polled_at=stamp-timedelta(seconds=1),
                                               stop_run_id=None)
    assert count == 5  # Final acquisition starts30s after the first solve completed.


@pytest.mark.parametrize("cancel_probe", [False, True])
async def test_image_cleanup_error_is_latched_even_from_cancelled_probe(cancel_probe):
    import asyncio
    from unittest.mock import AsyncMock, Mock

    at = datetime(2026, 10, 8, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=at, mount=MountState(
        polled_at=at, stale=False, tracking=True, slewing=False)))
    service._now = lambda: at
    service._publish = Mock()
    service._sleep = AsyncMock()
    started = False
    def check(_):
        if started and cancel_probe:
            raise capture_orchestrator.CaptureDitherStoppedError("durable Stop")
    service._check_dither_run_stop = check
    async def probe():
        nonlocal started
        started = True
        try:
            if cancel_probe:
                await asyncio.Event().wait()
        finally:
            raise capture_orchestrator.CaptureDitherProbeCleanupError("camera idle unknown")
    async def cleanup():
        assert service._dither_probe_cleanup_pending
        if cleanup_calls[0] == 0:
            cleanup_calls[0] += 1
            raise RuntimeError("idle query offline")
        cleanup_calls[0] += 1
    cleanup_calls = [0]
    service._cleanup_dither_probe = cleanup
    service._solve_dither_probe = probe
    with pytest.raises(CaptureDitherSettleError):
        await asyncio.wait_for(service._wait_for_image_dither_settle(
            target_j2000=(3.0,24.2), after_polled_at=at-timedelta(seconds=1),
            stop_run_id="run"), timeout=1)
    assert cleanup_calls == [2]
    assert not service._dither_probe_cleanup_pending
    assert service._sleep.await_count == 1


async def test_slow_last_solver_cannot_substitute_for_optical_acquisition_span():
    from types import SimpleNamespace

    stamp = datetime(2026, 10, 8, tzinfo=UTC)
    elapsed = 0.0
    starts = []
    service = object.__new__(CaptureService)
    state = StateStore(StateSnapshot(server_started_at=stamp))
    def publish():
        state.apply(MountChanged(mount=MountState(
            polled_at=stamp+timedelta(seconds=elapsed), stale=False,
            tracking=True, slewing=False)))
    publish()
    service._state = state
    service._now = lambda: stamp + timedelta(seconds=elapsed)
    service._monotonic = lambda: elapsed
    service._check_dither_run_stop = lambda _: None
    async def probe():
        nonlocal elapsed
        starts.append(elapsed)
        # Third image was acquired at4s, but its solver finishes at36s.
        elapsed += 32 if len(starts) == 3 else 2
        publish()
        return SimpleNamespace(coord=SimpleNamespace(ra_h=3.0, dec_deg=24.2))
    async def sleep(_):
        pass
    service._solve_dither_probe = probe
    service._sleep = sleep
    await service._wait_for_image_dither_settle(
        target_j2000=(3.0,24.2), after_polled_at=stamp-timedelta(seconds=1),
        stop_run_id=None)
    assert len(starts) == 4
    assert starts[-1] - 2 >= 30  # First image solve completed at2s.
