# SPDX-License-Identifier: GPL-3.0-only
"""Dawn safety uses fresh standard Alpaca readback, never cached Home."""
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ssc.api.device_commands import AlpacaDeviceCommands
from ssc.scheduler.executor import DeviceCommandError
from ssc.state.models import MountState, StateSnapshot
from ssc.state.store import StateStore


def commands_at(atpark=True, slewing=False, tracking=False):
    command = object.__new__(AlpacaDeviceCommands)
    command._telescope = SimpleNamespace(get=AsyncMock(side_effect=lambda name: {
        'atpark': atpark, 'slewing': slewing, 'tracking': tracking}[name]))
    command._camera = SimpleNamespace(put=AsyncMock())
    command._autofocus_task = None
    command._park_state_ready_retries = 1
    command._park_state_ready_interval_s = 0
    command._sleep = AsyncMock()
    command._state = StateStore(StateSnapshot(server_started_at=datetime.now(UTC),
        mount=MountState(at_home=True, at_park=True, slewing=False, tracking=False)))
    command._put = AsyncMock()
    return command


async def test_dawn_verifies_park_tracking_and_slew_with_fresh_reads():
    command = commands_at()
    await command.ensure_safety_parked()
    assert [call.args[1] for call in command._put.await_args_list] == ['tracking', 'abortslew', 'park']
    assert command._put.await_args_list[0].kwargs == {'Tracking': False}
    assert [call.args[0] for call in command._telescope.get.await_args_list] == ['atpark', 'slewing', 'tracking']
    command._camera.put.assert_awaited_once_with('abortexposure')


@pytest.mark.parametrize('atpark,slewing,tracking', [(False, False, False), (1, False, False),
                                                   (True, True, False), (True, False, True)])
async def test_dawn_rejects_home_or_unverified_safe_members(atpark, slewing, tracking):
    command = commands_at(atpark, slewing, tracking)
    with pytest.raises(DeviceCommandError, match='Dawn park unverified'):
        await command.ensure_safety_parked()


async def test_failed_camera_abort_does_not_prevent_park_attempt(caplog):
    command = commands_at()
    command._camera.put.side_effect = DeviceCommandError('camera transport unavailable')
    await command.ensure_safety_parked()
    assert command._put.await_args_list[-1].args[1] == 'park'
    assert 'camera_abort' in caplog.text and 'camera transport unavailable' in caplog.text


async def test_dark_filter_bool_false_is_not_slot_zero_confirmation():
    command = commands_at()
    command._filterwheel = SimpleNamespace(is_connected=AsyncMock(return_value=True),
        get=AsyncMock(side_effect=lambda name: ['Dark', 'IRCUT'] if name == 'names' else False),
        put=AsyncMock())
    with pytest.raises(DeviceCommandError, match='integer'):
        await command.set_filter('Dark')
