import pytest

from ssc.api.scheduled_focus import ScheduledFocusMonitor, adaptive_autofocus
from ssc.scheduler.executor import DeviceCommandError, DeviceFocusConditionsError


async def test_adaptive_focus_increases_exposure_then_succeeds() -> None:
    exposures = []
    reports = []

    async def focus(exposure):
        exposures.append(exposure)
        if exposure < 4:
            raise DeviceFocusConditionsError("too few stars")

    await adaptive_autofocus(focus, reports.append)
    assert exposures == [2, 4]
    assert len(reports) == 1


async def test_cloudy_focus_exhausts_longer_exposures_as_temporary_condition() -> None:
    exposures = []
    reports = []

    async def focus(exposure):
        exposures.append(exposure)
        raise DeviceFocusConditionsError("clouds")

    with pytest.raises(DeviceFocusConditionsError):
        await adaptive_autofocus(focus, reports.append)
    assert exposures == [2, 4, 8]
    assert len(reports) == 3


async def test_hardware_focus_failure_is_not_treated_as_clouds() -> None:
    exposures = []

    async def focus(exposure):
        exposures.append(exposure)
        raise DeviceCommandError("focuser could not restore starting position", recoverable=False)

    with pytest.raises(DeviceCommandError):
        await adaptive_autofocus(focus, lambda _: None)
    assert exposures == [2]


def test_three_poor_hfr_frames_request_focus_and_duplicates_do_not_count() -> None:
    monitor = ScheduledFocusMonitor()
    for frame in range(3):
        assert not monitor.observe(frame, 2, 30)
    assert not monitor.observe(3, 3.5, 30)
    assert not monitor.observe(3, 3.5, 30)
    assert not monitor.observe(4, 3.5, 30)
    assert monitor.observe(5, 3.5, 30)
    monitor.focused()
    assert not monitor.observe(5, 3.5, 30)
    assert not monitor.observe(6, 2.1, 30)


def test_clouds_and_weak_stars_request_focus_without_hfr_baseline() -> None:
    monitor = ScheduledFocusMonitor()
    assert not monitor.observe(0, None, 0)
    assert not monitor.observe(1, 0.5, 1)
    assert monitor.observe(2, None, 0)


def test_one_clear_frame_resets_consecutive_bad_samples() -> None:
    monitor = ScheduledFocusMonitor()
    assert not monitor.observe(0, None, 0)
    assert not monitor.observe(1, None, 0)
    assert not monitor.observe(2, 2.1, 40)
    assert not monitor.observe(3, None, 0)
