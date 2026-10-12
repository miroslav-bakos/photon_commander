"""Unit tests for ``ssc.api.conditions``: pure, fixed-input scoring — a good
night and a bad night are each exercised with fixed inputs, per the brief.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ssc.api.conditions import (
    ConditionsCategory,
    ConditionFactorName,
    MoonContribution,
    assess_capture_conditions,
)
from ssc.weather.base import ForecastPoint

_WHEN = datetime(2026, 1, 15, 22, 0, tzinfo=UTC)


def _row(**overrides: object) -> ForecastPoint:
    defaults: dict[str, object] = {
        "t": _WHEN,
        "cloud_total": 0.0,
        "cloud_low": 0.0,
        "cloud_mid": 0.0,
        "cloud_high": 0.0,
        "precip_prob": 0.0,
        "humidity": 50.0,
        "dew_point_c": 5.0,
        "temp_c": 15.0,
        "wind_ms": 2.0,
        "visibility_m": None,
        "seeing_arcsec": None,
        "transparency": None,
        "source": "metno",
    }
    defaults.update(overrides)
    return ForecastPoint(**defaults)  # type: ignore[arg-type]


_MOON_DOWN_ALL_NIGHT = MoonContribution(
    illuminated_fraction=1.0, up_fraction=0.0, mean_altitude_above_horizon_deg=None
)
_MOON_UP_ALL_NIGHT = MoonContribution(
    illuminated_fraction=0.9, up_fraction=1.0, mean_altitude_above_horizon_deg=45.0
)


def test_a_clear_dry_night_with_the_moon_down_scores_excellent() -> None:
    result = assess_capture_conditions([_row()], _MOON_DOWN_ALL_NIGHT)
    assert result.category is ConditionsCategory.EXCELLENT
    assert result.score >= 80.0
    assert result.reasons  # always non-empty


def test_a_full_moon_below_the_horizon_all_night_costs_nothing() -> None:
    """Illumination alone is misleading: a full Moon that never rises must not
    tank the score the way a full Moon overhead all night would."""
    clear = [_row()]
    down = assess_capture_conditions(clear, _MOON_DOWN_ALL_NIGHT)
    up = assess_capture_conditions(clear, _MOON_UP_ALL_NIGHT)
    assert down.score > up.score
    assert down.category is ConditionsCategory.EXCELLENT


def test_bright_moon_and_moderate_cloud_are_not_called_good() -> None:
    """Regression for the reported 2026-09-22 forecast: generic deep-sky
    conditions must not call this combination good."""
    result = assess_capture_conditions(
        [_row(cloud_total=34.4, wind_ms=17.0 / 3.6)],
        MoonContribution(
            illuminated_fraction=0.84,
            up_fraction=0.70,
            mean_altitude_above_horizon_deg=35.0,
        ),
    )

    assert result.score < 60.0
    assert result.category is ConditionsCategory.FAIR
    assert any("moon 84% illuminated" in reason for reason in result.reasons)


def test_score_breakdown_reports_cloud_precipitation_and_all_factor_deductions() -> None:
    result = assess_capture_conditions(
        [
            _row(
                cloud_total=40.0,
                cloud_low=30.0,
                cloud_high=50.0,
                temp_c=10.0,
                dew_point_c=7.0,
                wind_ms=6.0,
                precip_prob=25.0,
                humidity=95.0,
            )
        ],
        MoonContribution(
            illuminated_fraction=0.5,
            up_fraction=0.4,
            mean_altitude_above_horizon_deg=25.0,
        ),
    )
    factors = {factor.name: factor for factor in result.factors}

    assert tuple(factors) == (
        ConditionFactorName.CLOUDS,
        ConditionFactorName.MOON,
        ConditionFactorName.DEW_POINT,
        ConditionFactorName.WIND,
        ConditionFactorName.PRECIPITATION,
        ConditionFactorName.HUMIDITY,
    )
    assert factors[ConditionFactorName.CLOUDS].points_deducted == pytest.approx(31.0)
    assert "avg total 40.0%" in factors[ConditionFactorName.CLOUDS].measurement
    assert factors[ConditionFactorName.PRECIPITATION].points_deducted == pytest.approx(3.0)
    assert "25%" in factors[ConditionFactorName.PRECIPITATION].measurement
    assert 100.0 - sum(f.points_deducted or 0.0 for f in result.factors) == pytest.approx(result.score)


def test_breakdown_distinguishes_zero_deduction_from_unavailable_data() -> None:
    row = _row(precip_prob=None, humidity=None, dew_point_c=None, temp_c=None, wind_ms=None)
    result = assess_capture_conditions([row], _MOON_DOWN_ALL_NIGHT)
    factors = {factor.name: factor for factor in result.factors}

    assert factors[ConditionFactorName.PRECIPITATION].points_deducted is None
    assert "unavailable" in factors[ConditionFactorName.PRECIPITATION].measurement
    assert factors[ConditionFactorName.MOON].points_deducted == 0.0


def test_heavy_low_cloud_and_a_bright_moon_up_all_night_scores_very_poor() -> None:
    overcast = [
        _row(
            cloud_total=95.0,
            cloud_low=90.0,
            cloud_mid=40.0,
            cloud_high=20.0,
            precip_prob=70.0,
            humidity=97.0,
            dew_point_c=9.5,
            temp_c=10.0,
            wind_ms=12.0,
        )
    ]
    moon = MoonContribution(
        illuminated_fraction=0.95, up_fraction=1.0, mean_altitude_above_horizon_deg=60.0
    )
    result = assess_capture_conditions(overcast, moon)
    assert result.category is ConditionsCategory.VERY_POOR
    assert result.score < 20.0
    joined = " ".join(result.reasons)
    assert "cloud" in joined
    assert "moon" in joined


def test_low_dew_point_margin_is_flagged_even_under_clear_skies() -> None:
    dewy = [_row(temp_c=10.0, dew_point_c=9.5)]
    result = assess_capture_conditions(dewy, _MOON_DOWN_ALL_NIGHT)
    assert any("dew" in reason for reason in result.reasons)
    assert result.score < 100.0


def test_thin_high_cloud_is_named_even_when_total_cover_is_low() -> None:
    cirrus = [_row(cloud_total=15.0, cloud_high=50.0, cloud_low=0.0, cloud_mid=0.0)]
    result = assess_capture_conditions(cirrus, _MOON_DOWN_ALL_NIGHT)
    assert any("high cloud" in reason for reason in result.reasons)


def test_missing_optional_fields_are_skipped_not_penalised() -> None:
    """None means unknown, never zero (CLAUDE.md): a row missing humidity,
    dew point, wind and precipitation must not be treated as if they were
    favourable *or* unfavourable — those factors simply do not contribute."""
    sparse = _row(precip_prob=None, humidity=None, dew_point_c=None, temp_c=None, wind_ms=None)
    with_data = _row()
    sparse_result = assess_capture_conditions([sparse], _MOON_DOWN_ALL_NIGHT)
    full_result = assess_capture_conditions([with_data], _MOON_DOWN_ALL_NIGHT)
    assert sparse_result.score == pytest.approx(full_result.score)


def test_requires_at_least_one_row() -> None:
    with pytest.raises(ValueError, match="at least one forecast row"):
        assess_capture_conditions([], _MOON_DOWN_ALL_NIGHT)


def test_requires_at_least_one_row_with_cloud_total() -> None:
    no_cloud = [_row(cloud_total=None)]
    with pytest.raises(ValueError, match="cloud_total"):
        assess_capture_conditions(no_cloud, _MOON_DOWN_ALL_NIGHT)


def test_score_is_clamped_to_0_100() -> None:
    worst = [
        _row(
            cloud_total=100.0,
            cloud_low=100.0,
            cloud_high=100.0,
            precip_prob=100.0,
            humidity=100.0,
            dew_point_c=14.9,
            temp_c=15.0,
            wind_ms=30.0,
        )
    ]
    full_moon_up = MoonContribution(
        illuminated_fraction=1.0, up_fraction=1.0, mean_altitude_above_horizon_deg=80.0
    )
    result = assess_capture_conditions(worst, full_moon_up)
    assert result.score == 0.0
    assert result.category is ConditionsCategory.VERY_POOR
