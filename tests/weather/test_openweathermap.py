"""OpenWeatherMap provider, against a fixture built from OpenWeatherMap's own
published `/data/2.5/forecast` schema (`tests/weather/fixtures/
owm_forecast_51.48_0.0.json`). No live capture: this environment has no
OpenWeatherMap API key, and a fabricated key would violate the "never
approximate what you were not given" rule this package exists to enforce —
see the report accompanying this change for what was and was not verified
live (the 401-on-bad-key path *was* observed live, against the real
endpoint, with no key).
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import httpx
import pytest
import respx

from ssc.settings.secrets import BackendStatus
from ssc.weather.base import ProviderUnavailableError
from ssc.weather.cache import ForecastCache, make_cache_key
from ssc.weather.providers.openweathermap import (
    DEFAULT_BASE_URL,
    OpenWeatherMapConfig,
    OpenWeatherMapProvider,
)
from tests.weather.conftest import load_fixture

LAT, LON = 51.48, 0.0

AVAILABLE = BackendStatus(available=True, backend="fake.Keyring")
UNAVAILABLE = BackendStatus(
    available=False, backend="keyring.backends.fail.Keyring", reason="no OS keyring on this host"
)


class FakeSecretStore:
    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values = dict(values or {})

    async def get(self, name: str) -> str | None:
        return self._values.get(name)

    def set_value(self, name: str, value: str) -> None:
        self._values[name] = value

    def delete_value(self, name: str) -> None:
        self._values.pop(name, None)


def _config(**overrides: object) -> OpenWeatherMapConfig:
    return OpenWeatherMapConfig(**overrides)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_unavailable_when_secrets_backend_unavailable() -> None:
    provider = OpenWeatherMapProvider(
        _config(),
        cache=ForecastCache(),
        secrets=FakeSecretStore(),
        backend_status=UNAVAILABLE,
    )
    availability = await provider.availability()
    assert availability.available is False
    assert "no OS keyring on this host" in (availability.reason or "")


@pytest.mark.asyncio
async def test_unavailable_when_backend_ok_but_no_key_configured() -> None:
    provider = OpenWeatherMapProvider(
        _config(),
        cache=ForecastCache(),
        secrets=FakeSecretStore(),
        backend_status=AVAILABLE,
    )
    availability = await provider.availability()
    assert availability.available is False
    assert "no OpenWeatherMap API key" in (availability.reason or "")


@pytest.mark.asyncio
async def test_available_when_key_configured() -> None:
    provider = OpenWeatherMapProvider(
        _config(),
        cache=ForecastCache(),
        secrets=FakeSecretStore({"openweathermap_api_key": "s3cr3t"}),
        backend_status=AVAILABLE,
    )
    availability = await provider.availability()
    assert availability.available is True


@pytest.mark.asyncio
async def test_fetch_without_key_raises_without_hitting_network() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=False) as router:
            route = router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json={}))
            provider = OpenWeatherMapProvider(
                _config(),
                cache=ForecastCache(),
                secrets=FakeSecretStore(),
                backend_status=AVAILABLE,
                http=http,
            )
            with pytest.raises(ProviderUnavailableError, match="no OpenWeatherMap API key"):
                await provider.fetch(LAT, LON)
            assert not route.called


@pytest.mark.asyncio
async def test_fetch_normalises_and_leaves_altitude_split_seeing_and_transparency_none() -> None:
    payload = load_fixture("owm_forecast_51.48_0.0.json")
    async with httpx.AsyncClient() as http:
        with respx.mock(assert_all_called=True) as router:
            router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
            provider = OpenWeatherMapProvider(
                _config(),
                cache=ForecastCache(),
                secrets=FakeSecretStore({"openweathermap_api_key": "s3cr3t"}),
                backend_status=AVAILABLE,
                http=http,
            )
            series = await provider.fetch(LAT, LON)

    assert len(series.points) == len(payload["list"])
    first = series.points[0]
    assert first.source == "openweathermap"
    assert first.cloud_total == 40.0
    assert first.humidity == 63.0
    assert first.temp_c == 20.6
    assert first.wind_ms == 4.4
    assert first.visibility_m == 10000.0
    assert first.precip_prob == pytest.approx(24.0)
    # OpenWeatherMap has no altitude-split cloud cover, no dew point, and is
    # not one of the two excluded seeing/transparency providers either way.
    assert first.cloud_low is None
    assert first.cloud_mid is None
    assert first.cloud_high is None
    assert first.dew_point_c is None
    assert first.seeing_arcsec is None
    assert first.transparency is None


@pytest.mark.asyncio
async def test_key_never_appears_in_a_raised_error_message() -> None:
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(
                    401, json={"cod": 401, "message": "Invalid API key. secret-value-xyz"}
                )
            )
            provider = OpenWeatherMapProvider(
                _config(),
                cache=ForecastCache(),
                secrets=FakeSecretStore({"openweathermap_api_key": "secret-value-xyz"}),
                backend_status=AVAILABLE,
                http=http,
            )
            with pytest.raises(ProviderUnavailableError) as excinfo:
                await provider.fetch(LAT, LON)
            assert "secret-value-xyz" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_key_never_appears_in_the_cache_entry() -> None:
    payload = load_fixture("owm_forecast_51.48_0.0.json")
    cache = ForecastCache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
            provider = OpenWeatherMapProvider(
                _config(),
                cache=cache,
                secrets=FakeSecretStore({"openweathermap_api_key": "secret-value-xyz"}),
                backend_status=AVAILABLE,
                http=http,
            )
            await provider.fetch(LAT, LON)

    key = make_cache_key("openweathermap", LAT, LON)
    assert "secret-value-xyz" not in repr(key)
    entry = cache.get(key)
    assert entry is not None
    assert "secret-value-xyz" not in repr(entry)


@pytest.mark.asyncio
async def test_fresh_cache_hit_makes_no_request() -> None:
    payload = load_fixture("owm_forecast_51.48_0.0.json")
    cache = ForecastCache()
    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
            provider = OpenWeatherMapProvider(
                _config(),
                cache=cache,
                secrets=FakeSecretStore({"openweathermap_api_key": "s3cr3t"}),
                backend_status=AVAILABLE,
                http=http,
            )
            await provider.fetch(LAT, LON)
            assert router.calls.call_count == 1
            await provider.fetch(LAT, LON)
            assert router.calls.call_count == 1


@pytest.mark.asyncio
async def test_429_forces_circuit_open_and_serves_stale_cache() -> None:
    payload = load_fixture("owm_forecast_51.48_0.0.json")
    cache = ForecastCache()
    secrets = FakeSecretStore({"openweathermap_api_key": "s3cr3t"})
    already_expired = datetime(2020, 1, 1, tzinfo=UTC)

    async with httpx.AsyncClient() as http:
        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
            provider = OpenWeatherMapProvider(
                _config(),
                cache=cache,
                secrets=secrets,
                backend_status=AVAILABLE,
                http=http,
            )
            fresh = await provider.fetch(LAT, LON)

        key = make_cache_key("openweathermap", LAT, LON)
        entry = cache.get(key)
        assert entry is not None
        cache.put(key, dataclasses.replace(entry, expires_at=already_expired))

        with respx.mock() as router:
            router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(429, headers={"Retry-After": "60"})
            )
            stale = await provider.fetch(LAT, LON)

        assert stale.points == fresh.points
        assert stale.stale_since is not None

        with respx.mock(assert_all_called=False) as router:
            route = router.get(DEFAULT_BASE_URL).mock(
                return_value=httpx.Response(200, json=payload)
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
            provider = OpenWeatherMapProvider(
                _config(),
                cache=ForecastCache(),
                secrets=FakeSecretStore({"openweathermap_api_key": "s3cr3t"}),
                backend_status=AVAILABLE,
                http=http,
            )
            with pytest.raises(ProviderUnavailableError):
                await provider.fetch(LAT, LON)
