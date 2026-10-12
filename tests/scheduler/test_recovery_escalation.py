from datetime import timedelta

import pytest

from ssc.scheduler.executor import (
    CompleteItem,
    DeviceCommandError,
    EffectCall,
    EffectName,
    EffectOutcome,
    ExecutorRecoveryRestartError,
    IssueEffect,
    ItemStatus,
    JournalRecord,
    RecordKind,
    RunState,
    WaitUntil,
    _issue_effect,
    decide,
    reduce,
)
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule


def test_missing_panel_configuration_waits_visibly_until_its_window():
    schedule = build_schedule(build_plan())
    item = schedule.items[0]
    decision = decide(RunState(), schedule=schedule, plan_items={}, now=BASE_UTC, gate=None)
    assert isinstance(decision, WaitUntil)
    assert "absent from the plan" in decision.reason
    assert decision.until_utc <= item.end_utc
    expired = decide(RunState(), schedule=schedule, plan_items={}, now=item.end_utc, gate=None)
    assert isinstance(expired, CompleteItem)
    assert "configuration unavailable" in expired.reason


@pytest.mark.parametrize("failures,repair_fails", [(1, False), (2, True), (3, False)])
async def test_durable_repair_precedes_dispatch_and_supervised_restart(failures, repair_fails):
    journal = FakeJournal("escalation")
    effect = EffectCall(name=EffectName.CENTER_TARGET,
                        args={"ra_j2000_h": 3.79, "dec_j2000_deg": 24.2,
                              "resume_field_verification": True})
    await journal.append(JournalRecord(
        at=BASE_UTC, run_id=journal.run_id, kind=RecordKind.ITEM_TRANSITION,
        item_id="panel", step=2, status=ItemStatus.RUNNING, effect=effect,
        effect_outcome=EffectOutcome.FAILED, recovery_attempts=failures,
        retry_at=BASE_UTC, reason="previous camera fault",
    ))
    device = FakeDeviceCommands()
    repairs = []

    class Capture(FakeCapturePort):
        async def recover_operation(self, *, level, operation):
            assert journal.records[-1].effect_outcome is None
            assert not device.calls, "a device action preceded recovery"
            repairs.append((level, operation))
            if repair_fails:
                raise DeviceCommandError("camera still busy; recovery blocked")

    capture = Capture(device)
    decision = IssueEffect(item_id="panel", step=2, panel_index=0, attempt=4,
                           effect=effect, resumed=False, new_status=ItemStatus.RUNNING)
    kwargs = {"capture": capture, "recovery_deadline": BASE_UTC + timedelta(minutes=5),
              "supervisor_restarts": True}
    if failures == 3:
        with pytest.raises(ExecutorRecoveryRestartError, match="same run"):
            await _issue_effect(journal, device, FakeDeviceEvents(), FakeClock(BASE_UTC),
                                journal.run_id, decision, **kwargs)
        assert not device.calls and not repairs
        pending = reduce(journal.records).items["panel"].pending_effect
        assert pending == effect
        # Supervisor reconciles the connection/camera before re-entering the
        # same journal. Re-entry must execute the retained intent once.
        from dataclasses import replace
        await _issue_effect(journal, device, FakeDeviceEvents(), FakeClock(BASE_UTC),
                            journal.run_id, replace(decision, resumed=True), **kwargs)
        assert [name for name, _args in device.calls] == [EffectName.CENTER_TARGET]
    else:
        state = await _issue_effect(journal, device, FakeDeviceEvents(), FakeClock(BASE_UTC),
                                    journal.run_id, decision, **kwargs)
        assert repairs == [(failures, "center_target")]
        if repair_fails:
            assert not device.calls
            assert state.items["panel"].status is ItemStatus.RUNNING
            assert "camera still busy" in state.items["panel"].reason
        else:
            assert [name for name, _args in device.calls] == [EffectName.CENTER_TARGET]
            # A fresh solve is proof for the next frame, not evidence that the
            # repeated science failure disappeared. Its escalation survives.
            assert state.items["panel"].recovery_attempts == failures


def test_unavailable_moving_target_saves_existing_stack_before_window_handoff():
    from ssc.astro.bodies import BodyId
    from ssc.plan.models import BodyTargetSpec

    plan = build_plan()
    target = plan.items[1].model_copy(update={"target": BodyTargetSpec(body=BodyId.MOON)})
    schedule = build_schedule(plan)
    current = schedule.items[1]
    # Earlier items are already complete; existing science must survive an
    # ephemeris failure at this panel's deadline.
    records = [JournalRecord(at=BASE_UTC, run_id="cleanup", kind=RecordKind.ITEM_TRANSITION,
                             item_id=schedule.items[0].item_id, status=ItemStatus.DONE)]
    state = reduce(records)
    kwargs = {"schedule": schedule, "plan_items": {target.id: target}, "now": current.end_utc,
              "gate": None, "resolution_error": "ephemeris unavailable", "frames_captured": 6}
    cleanup = decide(state, **kwargs)
    assert isinstance(cleanup, IssueEffect)
    assert cleanup.effect.name is EffectName.STOP_LIVE_STACK
    assert cleanup.effect.args["require_retained"] is True
    # A failed save must not authorize completion.
    for outcome in (EffectOutcome.FAILED, EffectOutcome.COMPLETED):
        folded = reduce([*records, JournalRecord(
            at=current.end_utc, run_id="cleanup", kind=RecordKind.ITEM_TRANSITION,
            item_id=target.id, step=cleanup.step, status=ItemStatus.RUNNING,
            effect=cleanup.effect, effect_outcome=outcome)])
        assert folded.items[target.id].window_cleanup_completed is (outcome is EffectOutcome.COMPLETED)
        if outcome is EffectOutcome.COMPLETED:
            assert isinstance(decide(folded, **kwargs), CompleteItem)
