"""Scheduled admission failures cannot steal another acquisition owner's camera."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from ssc.api.capture_orchestrator import CaptureService
from ssc.scheduler.executor import DeviceCommandError
from ssc.state.models import CaptureState, StateSnapshot
from ssc.state.store import StateStore
from tests.api.test_capture_port import (
    _port,
)
from tests.api.test_capture_port import (
    database as database,
)
from tests.api.test_capture_port import (
    migrated as migrated,
)
from tests.api.test_capture_port import (
    repo as repo,
)
from tests.api.test_capture_port import (
    settings as settings,
)
from tests.api.test_capture_port import (
    state as state,
)


def admission_service(*, manual=False, auxiliary=False):
    service = object.__new__(CaptureService)
    service._state = StateStore(StateSnapshot(server_started_at=datetime(2026, 10, 8, tzinfo=UTC)))
    service._section = CaptureState(in_progress=manual)
    service._calibration_task = None
    service._auxiliary_camera_busy = lambda: auxiliary
    service.drain_wide_preview = AsyncMock()
    service._camera = Mock(put=AsyncMock())
    return service


@pytest.mark.parametrize("owner", ["manual", "autofocus", "polar_alignment"])
async def test_scheduled_admission_does_not_begin_or_latch_other_owner(
    repo, settings, state, owner
):
    service = admission_service(manual=owner == "manual", auxiliary=owner != "manual")
    begin = Mock()
    port = _port(
        repo=repo,
        settings=settings,
        state=state,
        begin_capture=begin,
        fail_capture=service.fail_scheduled_exposure,
    )
    port._before_capture = service.prepare_scheduled_capture
    with pytest.raises(DeviceCommandError):
        await port.expose_frame(
            exposure_sec=1,
            gain=100,
            frame_type="light",
            filter_name=None,
            block_id="science",
            frame_index=0,
        )
    begin.assert_not_called()
    service._camera.put.assert_not_awaited()
    assert getattr(service, "_failed_exposure_camera", None) is None
    assert service._section.in_progress is (owner == "manual")


async def test_cpu_master_failure_does_not_latch_camera_ownership(tmp_path):
    service = admission_service()
    service._select_dark_filter_if_available = AsyncMock()
    service._context = Mock()
    service._calibration_target_count = None
    service._calibration_cancel_requested = False
    service._now = lambda: datetime(2026, 10, 8, tzinfo=UTC)

    async def fail_master(**kwargs):
        service._publish(
            calibration=service._section.calibration.model_copy(update={"phase": "integrating"})
        )
        raise RuntimeError("CPU master failed after camera acquisition finished")

    service._generate_calibration_masters = fail_master
    await service._run_calibration(
        kind="dark",
        count=0,
        requested_duration_s=10,
        gain=100,
        filter_positions=(),
        filter_names=(),
        optic_id=None,
        flat_source=None,
        generate_master=True,
        keep_only_master=False,
        minimum_s=1,
        maximum_s=120,
        max_adu=None,
        folder=tmp_path,
        folder_template="%TARGET%",
        filename_template="frame",
        tz=ZoneInfo("UTC"),
    )
    assert service._section.calibration.status == "failed"
    assert service._section.in_progress is False
    assert getattr(service, "_failed_exposure_camera", None) is None
    await service.prepare_scheduled_capture()
    assert service._camera.put.await_count == 1  # original Gain write; no AbortExposure
