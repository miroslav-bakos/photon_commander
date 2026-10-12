"""ForecastPoint/ForecastSeries: the shape itself, independent of any provider."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest

from ssc.weather.base import ForecastPoint, ForecastSeries

_UNKNOWN_FIELDS = ("seeing_arcsec", "transparency")


def _point(**overrides: object) -> ForecastPoint:
    defaults: dict[str, object] = {
        "t": datetime(2026, 8, 28, 12, 0, tzinfo=UTC),
        "cloud_total": 40.0,
        "cloud_low": 10.0,
        "cloud_mid": 20.0,
        "cloud_high": 10.0,
        "precip_prob": None,
        "humidity": 60.0,
        "dew_point_c": 12.0,
        "temp_c": 20.0,
        "wind_ms": 4.0,
        "visibility_m": None,
        "seeing_arcsec": None,
        "transparency": None,
        "source": "metno",
    }
    defaults.update(overrides)
    return ForecastPoint(**defaults)  # type: ignore[arg-type]


def test_seeing_and_transparency_are_always_none() -> None:
    """No shipped provider fills these (THIRD_PARTY_NOTICES.md). A future
    contributor "helpfully" fabricating one of them is exactly the failure
    this test exists to catch."""
    point = _point()
    for name in _UNKNOWN_FIELDS:
        assert getattr(point, name) is None


def test_forecast_point_is_frozen() -> None:
    point = _point()
    with pytest.raises(dataclasses.FrozenInstanceError):
        point.cloud_total = 0.0  # type: ignore[misc]


def test_as_stale_marks_series_without_mutating_points() -> None:
    fetched_at = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    series = ForecastSeries(points=(_point(),), fetched_at=fetched_at)
    assert series.stale_since is None

    stale = series.as_stale(since=fetched_at)
    assert stale.stale_since == fetched_at
    assert stale.points == series.points
    assert series.stale_since is None  # original untouched
