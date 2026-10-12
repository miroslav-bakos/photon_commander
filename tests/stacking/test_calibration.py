from pathlib import Path

import pytest

from ssc.stacking.calibration import CalibrationFrame, CalibrationMatchError, match_dark


def _frame(frame_id: int, *, gain: int | None = 100, exposure: float | None = 10.0, temp: float | None = -10.0) -> CalibrationFrame:
    return CalibrationFrame(frame_id, Path(f"{frame_id}.fits"), gain, exposure, temp)


def test_exact_temperature_wins_and_reports_zero_delta() -> None:
    match = match_dark(_frame(1, temp=-10.0), [_frame(2, temp=-9.0), _frame(3, temp=-10.0)])
    assert match.dark.id == 3
    assert match.exact_temperature
    assert match.temperature_delta_c == 0.0


def test_nearest_temperature_is_returned_with_visible_mismatch() -> None:
    match = match_dark(_frame(1, temp=-10.0), [_frame(2, temp=-7.0), _frame(3, temp=-12.0)])
    assert match.dark.id == 3
    assert match.temperature_delta_c == pytest.approx(-2.0)
    assert not match.exact_temperature


def test_gain_and_exposure_are_hard_requirements() -> None:
    with pytest.raises(CalibrationMatchError, match="gain and exposure"):
        match_dark(_frame(1), [_frame(2, gain=200), _frame(3, exposure=30.0)])


def test_unknown_temperature_is_not_silently_matched() -> None:
    with pytest.raises(CalibrationMatchError, match="CCD-TEMP"):
        match_dark(_frame(1, temp=None), [_frame(2, temp=-10.0)])
