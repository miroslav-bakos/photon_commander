"""A physical dither with an unresolved journal intent must never be replayed.

Real SQLite and plan recompilation span a process boundary; the mount is fake.
A BaseException cuts power after the move, bypassing normal error recovery.
"""

from contextlib import suppress
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, Mock, patch
from zoneinfo import ZoneInfo

import pytest

import ssc.capture  # noqa: F401 -- initialize capture before state/store
from ssc.api.capture_orchestrator import CaptureService
from ssc.astro.events import Site
from ssc.astro.units import Deg, Metres
from ssc.plan.models import (
    AbsoluteTimeSpec,
    CatalogTargetSpec,
    DitherSpec,
    DurationEndSpec,
    GotoAndStackItem,
    Plan,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandError,
    EffectName,
    reduce,
    run,
)
from ssc.state.models import MountState, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.runs import RunRepository
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
)


class PowerCut(BaseException):
    pass


@pytest.mark.parametrize("stop_during_center", [False, True])
async def test_pending_physical_dither_reopen_never_moves_twice(
    tmp_path,
    ephemeris,
    profile,
    stop_during_center,
):
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    start = datetime(2026, 10, 7, 21, tzinfo=UTC)
    plan = Plan(
        id="pending-dither",
        name="IC 1805",
        created_at=now,
        updated_at=now,
        items=(
            GotoAndStackItem(
                id="target",
                start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
                end=DurationEndSpec(minutes=5),
                target=CatalogTargetSpec(name="IC 1805", ra_j2000_h=2.55, dec_j2000_deg=61.45),
                dither=DitherSpec(pixels=15, every_n_frames=1),
            ),
        ),
    )

    def recompile(saved_plan):
        return compile_plan(
            saved_plan,
            site=Site(
                latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45)
            ),
            tz=ZoneInfo("UTC"),
            night_date=date(2026, 10, 7),
            ephemeris=ephemeris,
            now=now,
            profile=profile,
        )

    moves = []
    device = FakeDeviceCommands()
    clock = FakeClock(start)

    def fresh_service(point):
        service = object.__new__(CaptureService)
        service._state = StateStore(
            StateSnapshot(
                server_started_at=start,
                mount=MountState(
                    polled_at=start,
                    stale=False,
                    slewing=False,
                    tracking=True,
                    ra_hours=point[0],
                    dec_deg=point[1],
                    ra_j2000_h=point[0],
                    dec_j2000_deg=point[1],
                ),
            )
        )
        service._publish = Mock()
        service._wait_for_fresh_dither_coordinates = AsyncMock()
        service._restore_dither_tracking = AsyncMock()
        service._wait_for_dither_settle = AsyncMock(side_effect=PowerCut())
        service._wait_for_image_dither_settle = AsyncMock()
        service._dither_center_explicit = False
        service._dither_center_j2000 = None
        service._last_dither_target_j2000 = None
        service._dither_object_name = None
        service._dither_pixel_scale_arcsec = 3.6

        async def move(ra, dec):
            moves.append((ra, dec))

        service._dither = move
        return service

    service = fresh_service((2.55, 61.45))
    controls = FakeControls()
    resumed = False
    centered = False
    center_attempts = 0
    resumed_science = 0

    class Capture(FakeCapturePort):
        async def dither(self, *, pixels, max_pixels, object_name):
            await service.dither_scheduled(pixels, max_pixels, object_name)

        async def recover_dither(self, *, object_name):
            await service.recover_scheduled_dither(object_name)

        async def center_target(self, **kwargs):
            nonlocal centered, center_attempts
            if resumed:
                center_attempts += 1
                if center_attempts == 1:
                    if stop_during_center:
                        controls.push(ControlCommand(kind=ControlKind.STOP))
                    raise DeviceCommandError("optical verification failed; do not expose")
                assert kwargs == {"ra_j2000_h": 2.55, "dec_j2000_deg": 61.45}
                centered = True
            await super().center_target(**kwargs)

        async def expose_frame(self, **kwargs):
            nonlocal resumed_science
            if resumed:
                assert centered, "science exposed before independent center verification"
                resumed_science += 1
            await super().expose_frame(**kwargs)

    path = tmp_path / "night.sqlite3"
    db = await open_database(path)
    await migrate(db)
    repo = RunRepository(db)
    schedule = recompile(plan)
    await repo.create(
        run_id=plan.id, plan=plan, plan_revision=64, schedule=schedule, started_at=start
    )
    try:
        with (
            patch("ssc.api.capture_orchestrator.random.random", return_value=0),
            pytest.raises(PowerCut),
        ):
            await run(
                run_id=plan.id,
                plan=plan,
                schedule=schedule,
                journal=repo.journal,
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=controls,
                capture=Capture(device, clock=clock, cycle_s=60),
            )
        before = await repo.journal.read_all(plan.id)
        pending = reduce(before).items["target"].pending_effect
        assert pending is not None and pending.name is EffectName.DITHER
        assert len(moves) == 1
    finally:
        await db.aclose()

    db = await open_database(path)
    try:
        repo = RunRepository(db)
        saved = await repo.get(plan.id)
        assert saved.plan_revision == 64
        service = fresh_service(moves[0])
        resumed = True
        with (
            patch("ssc.api.capture_orchestrator.random.random", return_value=0.25),
            suppress(PowerCut),
        ):
            await run(
                run_id=plan.id,
                plan=saved.plan,
                schedule=recompile(saved.plan),
                journal=repo.journal,
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=controls,
                capture=Capture(device, clock=clock, cycle_s=300),
            )
        assert len(moves) == 1, "pending DITHER repeated a physical offset after process restart"
        if stop_during_center:
            assert center_attempts == 1
            assert resumed_science == 0
        else:
            assert center_attempts == 2
            assert resumed_science == 1
    finally:
        await db.aclose()


@pytest.mark.parametrize("stop_after_failure", [False, True])
async def test_ambiguous_dither_image_failure_waits_without_motion(stop_after_failure):
    from ssc.api.capture_orchestrator import (
        CaptureDitherSettleError,
        CaptureDitherStoppedError,
    )

    start = datetime(2026, 10, 7, 21, tzinfo=UTC)
    service = object.__new__(CaptureService)
    service._state = StateStore(
        StateSnapshot(
            server_started_at=start,
            mount=MountState(
                polled_at=start,
                stale=False,
                slewing=False,
                tracking=True,
                ra_j2000_h=2.55,
                dec_j2000_deg=61.45,
            ),
        )
    )
    service._publish = Mock()
    service._dither = AsyncMock()
    service._wait_for_fresh_dither_coordinates = AsyncMock()
    service._restore_dither_tracking = AsyncMock()
    service._wait_for_image_dither_settle = AsyncMock(
        side_effect=[
            CaptureDitherSettleError("probe trail exceeds pixel tolerance"),
            None,
        ]
    )
    service._sleep = AsyncMock()
    service._check_dither_run_stop = Mock(
        side_effect=[
            None,
            CaptureDitherStoppedError("durable owner Stop") if stop_after_failure else None,
        ]
    )
    if stop_after_failure:
        with pytest.raises(CaptureDitherStoppedError):
            await service.recover_scheduled_dither("IC 1805")
        assert service._wait_for_image_dither_settle.await_count == 1
    else:
        await service.recover_scheduled_dither("IC 1805")
        assert service._wait_for_image_dither_settle.await_count == 2
    service._dither.assert_not_awaited()
    service._sleep.assert_awaited_once()
    assert any(
        "probe trail exceeds" in str(call.kwargs.get("last_error", ""))
        for call in service._publish.call_args_list
    )
    assert service._publish.call_args.kwargs["in_progress"] is False
