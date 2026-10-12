"""Boundary cleanup must retain the outgoing mosaic integration identity."""
from unittest.mock import AsyncMock

from ssc.api.device_commands import AlpacaCapturePort
from ssc.scheduler.executor import (
    EffectCall,
    EffectName,
    EffectOutcome,
    IssueEffect,
    ItemStatus,
    _issue_effect,
)
from tests.scheduler.fakes import FakeClock, FakeDeviceCommands, FakeDeviceEvents, FakeJournal
from tests.scheduler.schedules import BASE_UTC


async def test_boundary_stop_preserves_outgoing_panel_context():
    restore = AsyncMock(return_value=True)
    stop = AsyncMock()
    device = FakeDeviceCommands()
    port = AlpacaCapturePort(
        run_id="mosaic-handoff", device_commands=device,
        camera=None, telescope=None, filterwheel=None,
        repo=None, settings=None, state=None,
        restore_live_stack=restore, stop_live_stack=stop,
    )
    port.set_stack_panel_context(0, 0)
    assert await port.restore_live_stack("mosaic")
    restore.reset_mock()
    journal = FakeJournal("mosaic-handoff")
    decision = IssueEffect(
        item_id="mosaic", step=10, panel_index=1, attempt=1,
        effect=EffectCall(name=EffectName.STOP_LIVE_STACK,
                          args={"if_active": True, "require_retained": True}),
        resumed=False, new_status=ItemStatus.RUNNING,
    )

    await _issue_effect(journal, device, FakeDeviceEvents(), FakeClock(BASE_UTC),
                        "mosaic-handoff", decision, capture=port)

    restore.assert_awaited_once_with("mosaic-handoff", "mosaic", 0, 0)
    stop.assert_awaited_once_with(require_retained=True)
    assert journal.records[-1].effect_outcome is EffectOutcome.COMPLETED
    # New-panel admission changes identity only after outgoing cleanup completes.
    port.set_stack_panel_context(1, 6)
    await port.restore_live_stack("mosaic")
    assert restore.await_args.args == ("mosaic-handoff", "mosaic", 1, 6)
