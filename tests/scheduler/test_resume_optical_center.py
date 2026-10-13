"""A fresh runtime must not adopt a pre-deployment dither as its field center."""
from pathlib import Path

import pytest

from ssc.capture import FrameType  # noqa: F401 -- initialize capture before state/store
from ssc.plan.models import CatalogTargetSpec, DitherSpec, DurationEndSpec
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandError,
    EffectCall,
    EffectName,
    EffectOutcome,
    JournalRecord,
    RecordKind,
    run,
)
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
from tests.scheduler.schedules import build_plan, build_schedule
from tests.scheduler.test_crash_resume import REFERENCE_JOURNAL, RUN_ID, _resolved_calls_in_prefix


@pytest.mark.parametrize("during_capture", [False, True])
@pytest.mark.parametrize("interrupt_center", [False, True])
async def test_sqlite_resume_recenters_before_interrupted_autofocus(
    tmp_path: Path, during_capture: bool, interrupt_center: bool
) -> None:
    from tests.scheduler.fakes import FakeJournal
    from tests.scheduler.schedules import BASE_UTC

    base = build_plan()
    plan = base.model_copy(update={"items": tuple(
        item.model_copy(update={"autofocus_before": not during_capture,
            "target": CatalogTargetSpec(name="M31", ra_j2000_h=0.711667, dec_j2000_deg=41.266667)})
        if item.id == "goto" else item for item in base.items)})
    schedule = build_schedule(plan)
    initial = FakeJournal(RUN_ID)
    clock = FakeClock(BASE_UTC)

    class InterruptedFocus(FakeDeviceCommands):
        focus_calls = 0

        async def autofocus(self):
            self.focus_calls += 1
            if self.focus_calls == 2 and not during_capture:
                raise RuntimeError("process interrupted during autofocus")
            await super().autofocus()

    device = InterruptedFocus()

    class InterruptedCapture(FakeCapturePort):
        async def focus_required(self):
            return during_capture

        async def autofocus(self):
            raise RuntimeError("process interrupted during autofocus")

    with pytest.raises(RuntimeError, match="process interrupted"):
        await run(run_id=RUN_ID, schedule=schedule, plan=plan, journal=initial,
                  device=device, events=FakeDeviceEvents(), clock=clock,
                  controls=FakeControls(), capture=InterruptedCapture(device, clock=clock, cycle_s=600),
                  gate_inputs=None, poll_interval_s=3600)
    database = await open_database(tmp_path / 'focus-resume.sqlite3')
    await migrate(database)
    repository = RunRepository(database)
    await repository.create(run_id=RUN_ID, plan=plan, plan_revision=1,
                            schedule=schedule, started_at=initial.records[0].at)
    for record in initial.records:
        await repository.journal.append(record)
    await database.aclose()
    database = await open_database(tmp_path / 'focus-resume.sqlite3')
    try:
        repository = RunRepository(database)
        saved = await repository.get(RUN_ID)
        centered = False
        focused = False

        class ReconnectedDevice(FakeDeviceCommands):
            async def autofocus(self):
                nonlocal focused
                assert centered, "autofocus restarted at unknown reconnected pointing"
                focused = True
                await super().autofocus()

        fresh = ReconnectedDevice()

        class ReconnectedCapture(FakeCapturePort):
            async def center_target(self, **kwargs):
                nonlocal centered
                centered = True
                await super().center_target(**kwargs)

            async def expose_frame(self, **kwargs):
                assert focused, "centering discarded the persisted autofocus intent"
                await super().expose_frame(**kwargs)

        if interrupt_center:
            class InterruptedCenter(ReconnectedCapture):
                async def center_target(self, **kwargs):
                    raise RuntimeError("interrupted centering detour")

            with pytest.raises(RuntimeError, match="interrupted centering detour"):
                await run(run_id=RUN_ID, schedule=saved.schedule, plan=saved.plan,
                          journal=repository.journal, device=fresh, events=FakeDeviceEvents(),
                          clock=clock, controls=FakeControls(),
                          capture=InterruptedCenter(fresh, clock=clock, cycle_s=600),
                          gate_inputs=None, poll_interval_s=3600)
            await database.aclose()
            database = await open_database(tmp_path / 'focus-resume.sqlite3')
            repository = RunRepository(database)

        await run(run_id=RUN_ID, schedule=saved.schedule, plan=saved.plan,
                  journal=repository.journal, device=fresh, events=FakeDeviceEvents(),
                  clock=clock, controls=FakeControls(),
                  capture=ReconnectedCapture(fresh, clock=clock, cycle_s=600),
                  gate_inputs=None, poll_interval_s=3600)
        records = (await repository.journal.read_all(RUN_ID))[len(initial.records):]
        first = next(record for record in records if record.effect is not None)
        assert first.effect.name is EffectName.CENTER_TARGET
        assert first.effect.args["resume_field_verification"] is True
        assert first.step == initial.records[-1].step
    finally:
        await database.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_once,stop", [(False,False), (True,False), (True,True)])
@pytest.mark.parametrize("after_dither", [False, True])
async def test_sqlite_resume_reverifies_center_before_first_science(tmp_path: Path, fail_once: bool, stop: bool, after_dither: bool) -> None:
    base = build_plan()
    plan = base.model_copy(update={"items": tuple(
        item.model_copy(update={"end": DurationEndSpec(minutes=30), "dither": DitherSpec(pixels=50, every_n_frames=1) if after_dither else None, "target": CatalogTargetSpec(name="M31", ra_j2000_h=0.711667, dec_j2000_deg=41.266667)})
        if item.id == "goto" else item for item in base.items)})
    schedule = build_schedule(plan)
    cutoff = next(i for i, r in enumerate(REFERENCE_JOURNAL)
                  if r.effect is not None and r.effect.name is EffectName.EXPOSE_FRAME
                  and r.effect_outcome is EffectOutcome.COMPLETED and r.item_id == "goto")
    prefix = REFERENCE_JOURNAL[:cutoff + 1]
    if after_dither:
        previous = prefix[-1]
        prefix.append(JournalRecord(at=previous.at, run_id=RUN_ID, kind=RecordKind.ITEM_TRANSITION,
            item_id=previous.item_id, step=previous.step, panel_index=previous.panel_index,
            attempt=previous.attempt, effect=EffectCall(name=EffectName.DITHER,
                args={"pixels": 50, "max_pixels": 200, "object_name": "M31", "after_frame": 1}),
            effect_outcome=EffectOutcome.COMPLETED, reason="completed offset before deployment"))
    database = await open_database(tmp_path / 'resume.sqlite3')
    await migrate(database)
    repository = RunRepository(database)
    await repository.create(run_id=RUN_ID, plan=plan, plan_revision=1,
                            schedule=schedule, started_at=prefix[0].at)
    for record in prefix:
        await repository.journal.append(record)
    await database.aclose()
    database = await open_database(tmp_path / 'resume.sqlite3')
    try:
        repository = RunRepository(database)
        saved = await repository.get(RUN_ID)
        device = FakeDeviceCommands()
        device.seed(_resolved_calls_in_prefix(prefix))
        clock = FakeClock(prefix[-1].at)
        controls = FakeControls()
        centering_attempts = 0
        class FreshCapture(FakeCapturePort):
            centered = False
            async def center_target(self, **kwargs):
                nonlocal centering_attempts
                centering_attempts += 1
                if fail_once and centering_attempts == 1:
                    if stop:
                        controls.push(ControlCommand(kind=ControlKind.STOP))
                    raise DeviceCommandError("optical field 12.8 arcmin off; retry required")
                self.centered = True
                await super().center_target(**kwargs)
            async def expose_frame(self, **kwargs):
                assert self.centered, 'fresh runtime retained science 12.8 arcmin from planned center'
                await super().expose_frame(**kwargs)
        capture = FreshCapture(device, clock=clock, cycle_s=600)
        await run(run_id=RUN_ID, schedule=saved.schedule, plan=saved.plan,
                  journal=repository.journal, device=device, events=FakeDeviceEvents(),
                  clock=clock, controls=controls, capture=capture,
                  gate_inputs=None, poll_interval_s=3600)
        records = await repository.journal.read_all(RUN_ID)
        new = records[len(prefix):]
        recovery = next(r for r in new if r.effect is not None
                        and r.effect.name is EffectName.CENTER_TARGET)
        assert recovery.step == prefix[-1].step
        assert recovery.effect.args["ra_j2000_h"] == 0.711667
        assert recovery.effect.args["dec_j2000_deg"] == 41.266667
        first_science = next((i for i, r in enumerate(new) if r.effect is not None and r.effect.name is EffectName.EXPOSE_FRAME), len(new))
        assert not any(r.effect is not None and r.effect.name is EffectName.DITHER for r in new[:first_science]), "completed dither must not replay"
        if stop:
            assert not any(r.effect is not None and r.effect.name is EffectName.EXPOSE_FRAME for r in new)
        elif fail_once:
            assert centering_attempts == 2
        assert sum(1 for name, _ in device.calls if name is EffectName.EXPOSE_FRAME) == sum(
            1 for r in records if r.effect is not None and r.effect.name is EffectName.EXPOSE_FRAME
            and r.effect_outcome is EffectOutcome.COMPLETED)
    finally:
        await database.aclose()
