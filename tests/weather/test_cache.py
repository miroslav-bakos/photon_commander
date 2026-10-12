from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ssc.weather.base import ForecastPoint, ForecastSeries
from ssc.weather.cache import CacheEntry, ForecastCache, make_cache_key, stale_copy


def _series(t: datetime) -> ForecastSeries:
    point = ForecastPoint(
        t=t,
        cloud_total=50.0,
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
        source="metno",
    )
    return ForecastSeries(points=(point,), fetched_at=t)


def test_key_rounds_lat_lon_so_gps_jitter_hits_the_same_entry() -> None:
    a = make_cache_key("metno", 51.4812, 0.0031)
    b = make_cache_key("metno", 51.4834, -0.0009)
    assert a == b


def test_different_provider_is_a_different_key() -> None:
    a = make_cache_key("metno", 51.48, 0.0)
    b = make_cache_key("openweathermap", 51.48, 0.0)
    assert a != b


def test_get_put_roundtrip_in_memory() -> None:
    cache = ForecastCache()
    key = make_cache_key("metno", 51.48, 0.0)
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    entry = CacheEntry(series=_series(now), fetched_at=now, expires_at=now + timedelta(minutes=30))

    assert cache.get(key) is None
    cache.put(key, entry)
    fetched = cache.get(key)
    assert fetched is not None
    assert fetched.series.points[0].cloud_total == 50.0


def test_persists_to_disk_and_reloads(tmp_path) -> None:
    path = tmp_path / "weather-cache.json"
    key = make_cache_key("metno", 51.48, 0.0)
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    entry = CacheEntry(
        series=_series(now),
        fetched_at=now,
        expires_at=now + timedelta(minutes=30),
        last_modified="Fri, 28 Aug 2026 10:00:00 GMT",
        model_run="2026-08-28T09:20:37Z",
    )

    first = ForecastCache(path=path)
    first.put(key, entry)
    assert path.exists()

    second = ForecastCache(path=path)
    reloaded = second.get(key)
    assert reloaded is not None
    assert reloaded.model_run == "2026-08-28T09:20:37Z"
    assert reloaded.last_modified == "Fri, 28 Aug 2026 10:00:00 GMT"
    assert reloaded.fetched_at == now
    assert reloaded.series.points[0].t == now
    assert reloaded.series.points[0].cloud_total == 50.0


def test_corrupt_cache_file_does_not_crash_startup(tmp_path) -> None:
    path = tmp_path / "weather-cache.json"
    path.write_text("{not valid json")

    cache = ForecastCache(path=path)
    key = make_cache_key("metno", 51.48, 0.0)
    assert cache.get(key) is None


def test_stale_copy_carries_the_original_fetch_time_as_stale_since() -> None:
    now = datetime(2026, 8, 28, 10, 0, tzinfo=UTC)
    entry = CacheEntry(series=_series(now), fetched_at=now, expires_at=None)

    stale = stale_copy(entry)
    assert stale.stale_since == now
    assert stale.points == entry.series.points
