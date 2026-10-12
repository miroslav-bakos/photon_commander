# SPDX-License-Identifier: GPL-3.0-only
"""Combined fault/restart night simulation: real reopened SQLite, fake hardware.

No FITS, Alpaca socket, calibration pixels or physical safe-state are exercised.
The device call log is retained external fake-world evidence, while all executor
progress and retry deadlines must survive closing/reopening the production store.
"""

from datetime import timedelta

import pytest

import ssc.capture  # noqa: F401  # Initialize capture before the state/store import cycle.
from ssc.scheduler.executor import (
    DeviceCommandError,
    EffectName,
    EffectOutcome,
    ItemStatus,
    RunStatus,
    reduce,
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
from tests.scheduler.schedules import (
    BASE_UTC,
    EXPOSURE_FRAMES,
    build_exposure_plan,
    build_exposure_schedule,
    build_plan,
)


class PowerCut(BaseException):
    """Abrupt interruption must escape ordinary device-error recovery."""


class FaultDevice(FakeDeviceCommands):
    def __init__(self, effect, failures):
        super().__init__()
        self.effect = effect
        self.failures = failures
        self.failed_attempts = 0

    async def _record(self, name, args):
        if (
            name is self.effect
            and self.failures
            and (name is not EffectName.EXPOSE_FRAME or args.get("block_id") == "lights")
        ):
            self.failures -= 1
            self.failed_attempts += 1
            # Failure before the fake action: no acquired frame or physical move.
            raise DeviceCommandError("simulated transient connection loss")
        await super()._record(name, args)


class CuttingJournal:
    def __init__(self, journal, attempt):
        self.journal = journal
        self.attempt = attempt

    async def read_all(self, run_id):
        return await self.journal.read_all(run_id)

    async def append(self, record):
        await self.journal.append(record)
        if record.retry_at is not None and record.recovery_attempts == self.attempt:
            raise PowerCut()


@pytest.mark.parametrize(
    "effect,cut_attempt",
    [
        (EffectName.SLEW_TO, 1),
        (EffectName.SLEW_TO, 2),
        (EffectName.EXPOSE_FRAME, 1),
        (EffectName.EXPOSE_FRAME, 2),
    ],
)
async def test_transient_fault_and_power_cut_reopen_real_night_journal(
    tmp_path,
    effect,
    cut_attempt,
):
    plan = build_exposure_plan()
    if effect is EffectName.SLEW_TO:
        plan = plan.model_copy(
            update={"items": (plan.items[0], build_plan().items[1], *plan.items[1:])}
        )
    schedule = build_exposure_schedule(plan)
    path = tmp_path / "night.sqlite3"
    run_id = "fault-night"
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    device = FaultDevice(effect, 2)
    db = await open_database(path)
    await migrate(db)
    repo = RunRepository(db)
    await repo.create(
        run_id=run_id, plan=plan, plan_revision=64, schedule=schedule, started_at=clock.now()
    )
    try:
        with pytest.raises(PowerCut):
            await run(
                run_id=run_id,
                plan=plan,
                schedule=schedule,
                journal=CuttingJournal(repo.journal, cut_attempt),
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=FakeControls(),
                capture=FakeCapturePort(device, clock=clock, cycle_s=10),
            )
        prefix = list(await repo.journal.read_all(run_id))
        recovery = [record for record in prefix if record.retry_at is not None]
        assert [record.recovery_attempts for record in recovery] == list(range(1, cut_attempt + 1))
        assert [(record.retry_at - record.at).total_seconds() for record in recovery] == [5, 15][
            :cut_attempt
        ]
        pending = next(item for item in reduce(prefix).items.values() if item.retry_at is not None)
        assert pending.recovery_attempts == cut_attempt
        assert pending.retry_at == recovery[-1].retry_at
        calls_before_restart = list(device.calls)
        failed_before_restart = device.failed_attempts
    finally:
        await db.aclose()

    # New repository, connection, device adapter, controls and capture port.
    db = await open_database(path)
    try:
        repo = RunRepository(db)
        assert list(await repo.journal.read_all(run_id)) == prefix
        saved = await repo.get(run_id)
        assert saved.plan == plan and saved.plan_revision == 64
        device = FaultDevice(effect, 2 - failed_before_restart)
        device.seed(calls_before_restart)
        state = await run(
            run_id=run_id,
            plan=saved.plan,
            schedule=saved.schedule,
            journal=repo.journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=FakeCapturePort(device, clock=clock, cycle_s=10),
        )
        assert state.run_status is RunStatus.COMPLETED
        records = await repo.journal.read_all(run_id)
        retries = [record for record in records if record.retry_at is not None]
        assert [record.recovery_attempts for record in retries] == [1, 2]
        assert [(record.retry_at - record.at).total_seconds() for record in retries] == [5, 15]
        frames = [
            args
            for name, args in device.calls
            if name is EffectName.EXPOSE_FRAME and args.get("block_id") == "lights"
        ]
        assert len(frames) == EXPOSURE_FRAMES
        assert len({(args["block_id"], args["frame_index"]) for args in frames}) == EXPOSURE_FRAMES
        names = [name for name, _ in device.calls]
        assert names.count(EffectName.STARTUP) == 1
        assert names.count(EffectName.PARK) == 1
        assert names[-1] is EffectName.PARK
        assert list(records[: len(prefix)]) == prefix
        assert (await repo.get(run_id)).plan_revision == 64
    finally:
        await db.aclose()


async def test_dither_failure_power_cut_does_not_replay_relative_move(tmp_path):
    class CutAfterDitherFailure(CuttingJournal):
        async def append(self, record):
            await self.journal.append(record)
            if (
                record.effect is not None
                and record.effect.name is EffectName.DITHER
                and record.effect_outcome is None
                and record.reason is not None
                and "ambiguous dither retained" in record.reason
            ):
                raise PowerCut()

    plan = build_exposure_plan()
    schedule = build_exposure_schedule(plan)
    clock = FakeClock(BASE_UTC - timedelta(minutes=5))
    path = tmp_path / "dither-night.sqlite3"
    run_id = "dither-fault-night"
    device = FaultDevice(EffectName.DITHER, 1)
    db = await open_database(path)
    try:
        await migrate(db)
        repo = RunRepository(db)
        await repo.create(
            run_id=run_id, plan=plan, plan_revision=64, schedule=schedule, started_at=clock.now()
        )
        with pytest.raises(PowerCut):
            await run(
                run_id=run_id,
                plan=plan,
                schedule=schedule,
                journal=CutAfterDitherFailure(repo.journal, 0),
                device=device,
                events=FakeDeviceEvents(),
                clock=clock,
                controls=FakeControls(),
                capture=FakeCapturePort(device, clock=clock, cycle_s=10),
            )
        prefix = list(await repo.journal.read_all(run_id))
        assert reduce(prefix).items["lights"].status is ItemStatus.RUNNING
        assert reduce(prefix).items["lights"].pending_effect.name is EffectName.DITHER
        assert device.failed_attempts == 1
        prior_calls = list(device.calls)
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        repo = RunRepository(db)
        saved = await repo.get(run_id)
        assert list(await repo.journal.read_all(run_id)) == prefix
        device = FaultDevice(EffectName.DITHER, 0)
        device.seed(prior_calls)
        recovery_calls = []
        class RecoveringCapture(FakeCapturePort):
            async def recover_dither(self, *, object_name):
                recovery_calls.append("recover")
            async def dither(self, **kwargs):
                recovery_calls.append("offset")
                await super().dither(**kwargs)
        recovered_capture = RecoveringCapture(device, clock=clock, cycle_s=10)
        state = await run(
            run_id=run_id,
            plan=saved.plan,
            schedule=saved.schedule,
            journal=repo.journal,
            device=device,
            events=FakeDeviceEvents(),
            clock=clock,
            controls=FakeControls(),
            capture=recovered_capture,
        )
        assert recovery_calls[0] == "recover"
        assert recovery_calls.count("recover") == 1
        assert state.items["lights"].status is ItemStatus.DONE
        assert state.items["shutdown"].status is ItemStatus.DONE
        records = await repo.journal.read_all(run_id)
        recovered = [record for record in records if record.reason is not None
                     and "ambiguous prior dither reconciled" in record.reason]
        assert len(recovered) == 1
        assert recovered[0].effect_outcome is EffectOutcome.COMPLETED
        assert sum("ambiguous dither retained" in (record.reason or "") for record in records) == 1
        assert [name for name, _ in device.calls].count(EffectName.STARTUP) == 1
        assert device.calls[-1][0] is EffectName.PARK
    finally:
        await db.aclose()
