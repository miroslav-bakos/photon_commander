"""Scheduled focus selects its own filter through the durable effect machinery."""
from datetime import timedelta

import pytest

from ssc.plan.models import AutofocusItem, DurationEndSpec
from ssc.scheduler.executor import DeviceCommandError, EffectName, EffectOutcome, ItemStatus, run
from tests.scheduler.fakes import (
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


def focus_plan(filter_name: str | None):
    original = build_plan()
    return original.model_copy(update={"items": (
        AutofocusItem(id="focus", start=original.items[0].start,
                      end=DurationEndSpec(minutes=5), filter_name=filter_name),
        original.items[-1],
    )})


async def drive(plan, journal, device, clock=None):
    return await run(run_id=journal.run_id, schedule=build_schedule(plan), plan=plan,
                     journal=journal, device=device, events=FakeDeviceEvents(),
                     clock=clock or FakeClock(BASE_UTC), controls=FakeControls())


@pytest.mark.parametrize("filter_name", [None, "LP"])
async def test_focus_filter_is_selected_before_focus(filter_name):
    device = FakeDeviceCommands()
    journal = FakeJournal("focus-filter")
    state = await drive(focus_plan(filter_name), journal, device)
    assert state.items["focus"].status is ItemStatus.DONE
    names = [name for name, _ in device.calls]
    if filter_name is None:
        assert EffectName.SET_FILTER not in names
    else:
        assert names[:2] == [EffectName.SET_FILTER, EffectName.AUTOFOCUS]
        assert device.calls[0][1] == {"position": "LP"}


async def test_failed_filter_never_starts_autofocus():
    device = FakeDeviceCommands(raise_on={EffectName.SET_FILTER: DeviceCommandError("unknown position", recoverable=False)})
    journal = FakeJournal("focus-filter-failure")
    await drive(focus_plan("missing"), journal, device)
    assert not any(name is EffectName.AUTOFOCUS for name, _ in device.calls)
    assert any(record.reason and "unknown position" in record.reason for record in journal.records)


async def test_filter_retry_completes_before_focus():
    class RecoveringFilter(FakeDeviceCommands):
        attempts = 0
        async def set_filter(self, position):
            await super().set_filter(position)
            self.attempts += 1
            if self.attempts == 1:
                raise DeviceCommandError("wheel readback unavailable")

    device = RecoveringFilter()
    journal = FakeJournal("focus-filter-retry")
    state = await drive(focus_plan("LP"), journal, device)
    assert state.items["focus"].status is ItemStatus.DONE
    assert [name for name, _ in device.calls][:3] == [EffectName.SET_FILTER, EffectName.SET_FILTER, EffectName.AUTOFOCUS]


@pytest.mark.parametrize("confirmed", [False, True])
async def test_restart_keeps_filter_before_focus_and_repeats_only_unconfirmed_filter(confirmed):
    class CrashJournal(FakeJournal):
        async def append(self, record):
            await super().append(record)
            if record.effect is not None and record.effect.name is EffectName.SET_FILTER and record.effect_outcome is (EffectOutcome.COMPLETED if confirmed else None):
                raise RuntimeError("power loss after filter confirmation")

    plan = focus_plan("LP")
    journal = CrashJournal("focus-filter-restart")
    with pytest.raises(RuntimeError, match="power loss"):
        await drive(plan, journal, FakeDeviceCommands())
    resumed = FakeJournal(journal.run_id, journal.records)
    device = FakeDeviceCommands()
    state = await drive(plan, resumed, device, FakeClock(BASE_UTC + timedelta(seconds=1)))
    assert state.items["focus"].status is ItemStatus.DONE
    if confirmed:
        assert device.calls[0][0] is EffectName.AUTOFOCUS
        assert not any(name is EffectName.SET_FILTER for name, _ in device.calls)
    else:
        assert [name for name, _ in device.calls][:2] == [EffectName.SET_FILTER, EffectName.AUTOFOCUS]
