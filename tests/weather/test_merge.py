from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ssc.weather.base import ForecastPoint, ForecastSeries
from ssc.weather.merge import merge_series

T0 = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
T1 = T0 + timedelta(hours=1)
T2 = T0 + timedelta(hours=2)


def _point(t: datetime, source: str, cloud_total: float) -> ForecastPoint:
    return ForecastPoint(
        t=t,
        cloud_total=cloud_total,
        cloud_low=None,
        cloud_mid=None,
        cloud_high=None,
        precip_prob=None,
        humidity=None,
        dew_point_c=None,
        temp_c=None,
        wind_ms=None,
        visibility_m=None,
        seeing_arcsec=None,
        transparency=None,
        source=source,
    )


def test_primary_wins_on_overlapping_timestamps() -> None:
    primary = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T0)
    fallback = ForecastSeries(points=(_point(T0, "openweathermap", 90.0),), fetched_at=T0)

    merged = merge_series(primary, fallback)

    assert len(merged.points) == 1
    assert merged.points[0].source == "metno"
    assert merged.points[0].cloud_total == 10.0


def test_fallback_fills_gaps_the_primary_does_not_cover() -> None:
    primary = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T0)
    fallback = ForecastSeries(
        points=(_point(T0, "openweathermap", 90.0), _point(T1, "openweathermap", 20.0)),
        fetched_at=T0,
    )

    merged = merge_series(primary, fallback)

    assert [p.t for p in merged.points] == [T0, T1]
    assert merged.points[0].source == "metno"
    assert merged.points[1].source == "openweathermap"


def test_rows_are_never_field_spliced_across_providers() -> None:
    """A merged row always carries exactly one provider's values — never a
    cloud figure from one provider stitched to a humidity figure from
    another. Provenance would lie if that ever happened."""
    primary = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T0)
    fallback = ForecastSeries(points=(_point(T0, "openweathermap", 90.0),), fetched_at=T0)

    merged = merge_series(primary, fallback)

    (row,) = merged.points
    assert row.source == "metno"
    assert row.cloud_total == 10.0  # metno's own value, not blended with 90.0


def test_result_stale_since_follows_the_primary_only() -> None:
    primary = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T0, stale_since=T0)
    fallback = ForecastSeries(points=(_point(T1, "openweathermap", 20.0),), fetched_at=T1)

    merged = merge_series(primary, fallback)
    assert merged.stale_since == T0


def test_fetched_at_is_the_earliest_of_the_inputs() -> None:
    primary = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T2)
    fallback = ForecastSeries(points=(_point(T1, "openweathermap", 20.0),), fetched_at=T0)

    merged = merge_series(primary, fallback)
    assert merged.fetched_at == T0


def test_requires_at_least_one_series() -> None:
    with pytest.raises(ValueError):
        merge_series()


def test_single_series_passes_through() -> None:
    only = ForecastSeries(points=(_point(T0, "metno", 10.0),), fetched_at=T0)
    merged = merge_series(only)
    assert merged.points == only.points
