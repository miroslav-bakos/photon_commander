"""October8 M45 handoff through real compilation and SQLite journal replay."""
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from ssc.api.run_service import RunService
from ssc.astro.events import Site
from ssc.astro.units import Deg, Metres
from ssc.plan.models import (
    AbsoluteTimeSpec,
    CaptureCalibrationItem,
    DitherSpec,
    DurationEndSpec,
    GotoAndStackItem,
    Plan,
    RelativeTimeSpec,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import DeviceWindowEndedError, EffectName, EffectOutcome, reduce, run
from ssc.settings.store import SettingsStore
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


async def test_m45_window_expiry_is_persisted_and_recompiled_before_dark_handoff(tmp_path, ephemeris, profile):
    start = datetime(2026, 10, 7, 22, 36, 38, 807412, tzinfo=UTC)
    end = datetime(2026, 10, 8, 2, 14, 0, 699412, tzinfo=UTC)
    now = datetime(2026, 10, 7, 12, tzinfo=UTC)
    item_id = "320c81893cbc4519a94d2aafb41ca213"
    dark_id = "47c3cd6bbf83e3c02cabff04d69c2422"
    plan = Plan(id="oct7-night", name="Plan for 2026-10-07", created_at=now, updated_at=now, items=(
        GotoAndStackItem(id=item_id, start=AbsoluteTimeSpec(local_datetime=start.replace(tzinfo=None)),
            end=DurationEndSpec(minutes=(end-start).total_seconds()/60), target_name="M45",
            ra_j2000_h=3.79, dec_j2000_deg=24.2, dither=DitherSpec(pixels=15, every_n_frames=1)),
        CaptureCalibrationItem(id=dark_id, start=RelativeTimeSpec(offset_min=0),
            end=DurationEndSpec(minutes=59.3329), frame_type="dark", count=60, exposure_sec=60, gain=200),
    ))
    def compile_saved(value):
        return compile_plan(value, site=Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-.0098), elevation_m=Metres(45)),
            tz=ZoneInfo("UTC"), night_date=date(2026,10,7), ephemeris=ephemeris, now=now, profile=profile)
    db = await open_database(tmp_path / "night.sqlite3")
    try:
        await migrate(db)
        settings = SettingsStore(db)
        await settings.load()
        assert settings.loaded
        repo = RunRepository(db)
        schedule = compile_saved(plan)
        await repo.create(run_id="night", plan=plan, plan_revision=52, schedule=schedule, started_at=start)
        saved = await repo.get("night")
        schedule = compile_saved(saved.plan)
        assert schedule.items[0].end_utc == end
        clock = FakeClock(datetime(2026,10,8,1,37,22,tzinfo=UTC))
        class SafeHomeDevice(FakeDeviceCommands):
            async def park(self):
                await super().park()
                return "safe Home confirmed; physical Park was not confirmed"
        device = SafeHomeDevice()
        class ExpiringCapture(FakeCapturePort):
            async def dither(self, **_args):
                recovery = object.__new__(RunService)
                recovery._clock = clock
                recovery._publishing_journal = repo.journal
                recovery._state = SimpleNamespace(snapshot=SimpleNamespace(run=SimpleNamespace(
                    run_id="night", current_item_id=item_id)))
                assert await recovery.record_dither_tracking_recovery() is True
                pending = reduce(await repo.journal.read_all("night")).items[item_id].pending_effect
                assert pending.name is EffectName.DITHER
                # Process restart reads the same durable journal and cannot
                # repeat a physical tracking reset for this dither attempt.
                resumed = object.__new__(RunService)
                resumed.__dict__.update(recovery.__dict__)
                assert await resumed.record_dither_tracking_recovery() is False
                clock.advance((end - clock.now()).total_seconds())
                raise DeviceWindowEndedError("observing window ended during dither recovery")
        capture = ExpiringCapture(device, clock=clock, cycle_s=60)
        state = await run(run_id="night", plan=saved.plan, schedule=schedule,
            journal=repo.journal, device=device, events=FakeDeviceEvents(), clock=clock,
            controls=FakeControls(), capture=capture)
        entries = await repo.journal.read_all("night")
        expired = [e for e in entries if e.effect_outcome is EffectOutcome.WINDOW_ENDED]
        assert len(expired) == 1
        assert any(e.effect is not None and e.effect.name is EffectName.PARK
                   and "physical Park was not confirmed" in e.reason for e in entries)
        assert state.items[item_id].window_exhausted
        assert "window ended" in state.items[item_id].reason
        calls = [name for name,_ in device.calls]
        assert EffectName.STOP_LIVE_STACK in calls
        assert EffectName.CAPTURE_CALIBRATION in calls
        assert calls.index(EffectName.STOP_LIVE_STACK) < calls.index(EffectName.CAPTURE_CALIBRATION)
        reloaded = await repo.journal.read_all("night")
        assert reloaded == entries
        assert reduce(reloaded).items[item_id].window_exhausted
    finally:
        await db.aclose()
