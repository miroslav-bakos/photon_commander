"""Met.no provider, against the recorded fixture at
tests/weather/fixtures/metno_complete_51.48_0.0.json — captured live from
``https://api.met.no/weatherapi/locationforecast/2.0/complete?lat=51.48&lon=0.0``
on 2026-08-28, with a real identifying User-Agent
(``SSC-WeatherFixtureCapture/0.1 ...``) carrying a real maintainer contact, as
Met.no's terms require. The contact used in these tests is a placeholder — a real
address does not belong in a checked-in file, and RFC 2606 reserves ``.invalid``
precisely so a placeholder can never accidentally resolve. Headers
below (Expires/Last-Modified, the 304 behaviour, and the absence of an ETag)
were observed on that same live call, not assumed.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ssc.weather.base import ProviderUnavailableError
from ssc.weather.cache import ForecastCache, make_cache_key
from ssc.weather.providers.metno import (
    DEFAULT_BASE_URL,
    MetNoConfig,
    MetNoConfigError,
    MetNoProvider,
    build_user_agent,
)
from tests.weather.conftest import load_fixture

LAT, LON = 51.48, 0.0

#: An instant inside ``LIVE_HEADERS``' ``Expires`` window. The captured headers
#: are real, so their ``Expires`` is a real moment in the past — a test that
#: compared them against the wall clock passed on the afternoon it was written
#: and failed permanently thereafter. The provider takes an injected ``now``
#: precisely so freshness can be tested without that time bomb (architecture policy:
#: "Determinism or it does not merge").
FIXTURE_NOW = datetime(2026, 8, 28, 10, 40, 0, tzinfo=UTC)

LIVE_HEADERS = {
    "Last-Modified": "Fri, 28 Aug 2026 10:34:25 GMT",
    "Expires": "Fri, 28 Aug 2026 11:06:19 GMT",
    "Content-Type": "application/json",
}


def _config(**overrides: object) -> MetNoConfig:
    defaults: dict[str, object] = {
        "contact": "ssc-dev@example.invalid",
        "app_name": "ssc-core-test",
    }
    defaults.update(overrides)
    return MetNoConfig(**defaults)  # type: ignore[arg-type]


def test_build_user_agent_refuses_empty_contact() -> None:
    with pytest.raises(MetNoConfigError):
        build_user_agent(app_name="ssc-core", contact="")


def test_build_user_agent_refuses_whitespace_only_contact() -> None:
    with pytest.raises(MetNoConfigError):
        build_user_agent(app_name="ssc-core", contact="   ")


def test_build_user_agent_includes_app_and_contact() -> None:
    ua = build_user_agent(app_name="ssc-core", contact="ops@example.org")
    assert "ssc-core" in ua
    assert "ops@example.org" in ua


def test_provider_construction_refuses_empty_contact() -> None:
    with pytest.raises(MetNoConfigError):
        MetNoProvider(_config(contact=""), cache=ForecastCache())


@pytest.mark.asyncio
async def test_fetch_sends_identifying_user_agent_and_normalises() -> None:
    payload = load_fixture("metno_complete_51.48_0.0.json")

    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            route = router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload, headers=LIVE_HEADERS)
            )
            provider = MetNoProvider(_config(), cache=ForecastCache(), http=http)
            series = await provider.fetch(LAT, LON)

    assert route.called
    sent_headers = route.calls[0].request.headers
    assert "ssc-core-test" in sent_headers["user-agent"]
    assert "ssc-dev@example.invalid" in sent_headers["user-agent"]

    assert len(series.points) == len(payload["properties"]["timeseries"])
    first = series.points[0]
    assert first.source == "metno"
    assert first.cloud_total == 31.2
    assert first.cloud_low == 9.4
    assert first.cloud_mid == 6.2
    assert first.cloud_high == 19.5
    assert first.dew_point_c == 13.3
    assert first.temp_c == 20.6
    assert first.humidity == 63.1
    assert first.wind_ms == 4.4
    # Never reported by locationforecast at any detail level.
    assert first.precip_prob is None
    assert first.visibility_m is None
    assert first.seeing_arcsec is None
    assert first.transparency is None
    assert series.stale_since is None


@pytest.mark.asyncio
async def test_fresh_cache_hit_makes_no_request() -> None:
    payload = load_fixture("metno_complete_51.48_0.0.json")
    cache = ForecastCache()

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload, headers=LIVE_HEADERS)
            )
            provider = MetNoProvider(_config(), cache=cache, http=http, now=lambda: FIXTURE_NOW)
            await provider.fetch(LAT, LON)  # populates the cache
            assert router.calls.call_count == 1

            await provider.fetch(LAT, LON)  # within Expires: no second request
            assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_304_refreshes_expiry_and_keeps_cached_points() -> None:
    payload = load_fixture("metno_complete_51.48_0.0.json")
    cache = ForecastCache()
    key = make_cache_key("metno", LAT, LON)
    already_expired = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload, headers=LIVE_HEADERS)
            )
            provider = MetNoProvider(_config(), cache=cache, http=http)
            first = await provider.fetch(LAT, LON)

        # Force the cache entry to look expired so the next fetch revalidates.
        entry = cache.get(key)
        assert entry is not None
        cache.put(key, dataclasses.replace(entry, expires_at=already_expired))

        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(304, headers=LIVE_HEADERS)
            )
            second = await provider.fetch(LAT, LON)
            sent = router.calls[0].request
            assert "if-modified-since" in sent.headers

    assert second.points == first.points
    assert second.stale_since is None


@pytest.mark.asyncio
async def test_429_forces_circuit_open_and_serves_stale_cache() -> None:
    payload = load_fixture("metno_complete_51.48_0.0.json")
    cache = ForecastCache()
    already_expired = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload, headers=LIVE_HEADERS)
            )
            provider = MetNoProvider(_config(), cache=cache, http=http)
            fresh = await provider.fetch(LAT, LON)

        key = make_cache_key("metno", LAT, LON)
        entry = cache.get(key)
        assert entry is not None
        cache.put(key, dataclasses.replace(entry, expires_at=already_expired))

        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "120"})
            )
            stale = await provider.fetch(LAT, LON)

        assert stale.points == fresh.points
        assert stale.stale_since is not None

        # The breaker is now open: a second call must not hit the network again.
        with respx.mock(assert_all_called=False) as router:
            route = router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload, headers=LIVE_HEADERS)
            )
            cache.put(key, dataclasses.replace(entry, expires_at=already_expired))
            again = await provider.fetch(LAT, LON)
            assert not route.called

    assert again.stale_since is not None


@pytest.mark.asyncio
async def test_network_failure_with_no_cache_raises_provider_unavailable() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(side_effect=httpx.ConnectError("no route"))
            provider = MetNoProvider(_config(), cache=ForecastCache(), http=http)
            with pytest.raises(ProviderUnavailableError):
                await provider.fetch(LAT, LON)


@pytest.mark.asyncio
async def test_availability_is_always_true_when_configured() -> None:
    provider = MetNoProvider(_config(), cache=ForecastCache())
    availability = await provider.availability()
    assert availability.available is True
    await provider.aclose()
