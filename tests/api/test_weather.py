"""HTTP surface for ``GET /api/weather/forecast`` and ``GET /api/tonight``.

The happy path needs a Met.no contact configured (``AppConfig.weather_contact``,
CLAUDE.md invariant 7 — no default, no fallback) and a mocked HTTP response;
the degraded paths (no location, no contact configured) need neither.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import respx

from ssc.astro.events import EphemerisMissingError, load_ephemeris
from ssc.main import create_app
from ssc.settings.config import AppConfig
from ssc.weather.providers.metno import DEFAULT_BASE_URL
from tests.api.conftest import CLIENT_ADDRESS, FakeGateway, TestClient, pairing_token

FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "weather" / "fixtures" / "metno_complete_51.48_0.0.json"
)


def _ephemeris_available() -> bool:
    try:
        with load_ephemeris():
            return True
    except EphemerisMissingError:
        return False


requires_ephemeris = pytest.mark.skipif(
    not _ephemeris_available() and os.environ.get("SSC_REQUIRE_EPHEMERIS") != "1",
    reason="ephemeris kernel not present; see ssc.astro.events.load_ephemeris",
)


def _set_location(
    client: TestClient,
    *,
    latitude: float = 45.0,
    longitude: float = 10.0,
    timezone: str | None = None,
) -> None:
    response = client.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": latitude,
            "longitude_deg": longitude,
            "elevation_m": 100.0,
            "timezone": timezone,
        },
    )
    assert response.status_code == 200, response.text


def _pair(client: TestClient) -> TestClient:
    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200, response.text
    return client


def _synthetic_metno_payload(*, start: datetime, hours: int) -> dict:
    """A minimal ``locationforecast/2.0/complete`` payload spanning
    ``[start, start + hours)``, one row per hour — unlike the committed
    fixture (a real capture pinned to 2026-08-28), timestamps are relative to
    the caller's clock, so a test asserting on "tonight's" rows keeps working
    regardless of what day it actually runs on.
    """
    timeseries = []
    for h in range(hours):
        t = start + timedelta(hours=h)
        timeseries.append(
            {
                "time": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "data": {
                    "instant": {
                        "details": {
                            "air_temperature": 12.0,
                            "cloud_area_fraction": 20.0,
                            "cloud_area_fraction_high": 15.0,
                            "cloud_area_fraction_low": 5.0,
                            "cloud_area_fraction_medium": 5.0,
                            "dew_point_temperature": 4.0,
                            "relative_humidity": 55.0,
                            "wind_speed": 3.0,
                        }
                    }
                },
            }
        )
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [0.0, 51.48, 0.0]},
        "properties": {
            "meta": {"updated_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")},
            "timeseries": timeseries,
        },
    }


@pytest.fixture
def contact_client(tmp_path: Path) -> Iterator[TestClient]:
    """A client whose ``AppConfig.weather_contact`` is set — the happy-path
    fixture. ``tests/api/conftest.py``'s shared ``config``/``client`` leave it
    unset on purpose, to exercise the "unavailable" path by default."""
    config = AppConfig(
        host="127.0.0.1",
        data_dir=tmp_path,
        mdns_enabled=False,
        event_buffer_size=16,
        weather_contact="ssc-dev@example.invalid",
    )
    app = create_app(config, gateway=FakeGateway())
    with TestClient(app, client=CLIENT_ADDRESS) as test_client:
        yield test_client


# -- no location: both endpoints refuse loudly, never guess ------------------- #


def test_forecast_refuses_without_a_location(paired: TestClient) -> None:
    response = paired.get("/api/weather/forecast")
    assert response.status_code == 409


def test_tonight_refuses_without_a_location(paired: TestClient) -> None:
    response = paired.get("/api/tonight")
    assert response.status_code == 409


# -- no Met.no contact and no OpenWeatherMap key: degrade, do not crash ------- #


def test_forecast_is_503_with_no_provider_configured(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get("/api/weather/forecast")
    assert response.status_code == 503
    assert "metno" in response.json()["detail"]


@requires_ephemeris
def test_tonight_degrades_when_weather_is_unavailable(paired: TestClient) -> None:
    _set_location(paired)
    response = paired.get("/api/tonight")
    assert response.status_code == 200
    body = response.json()
    assert body["weather"] is None
    assert body["weather_unavailable_reason"] is not None
    assert body["conditions"] is None
    assert body["conditions_unavailable_reason"] is not None
    # The Moon is astro-only and must still answer even with no weather at all.
    assert 0.0 <= body["moon"]["illuminated_fraction"] <= 1.0
    assert body["moon"]["moonrise"]["outcome"] in {
        "occurs",
        "always_above",
        "always_below",
        "not_in_window",
    }


@requires_ephemeris
def test_tonight_can_predict_a_specific_local_night(paired: TestClient) -> None:
    _set_location(
        paired,
        latitude=48.15,
        longitude=17.1,
        timezone="Europe/Bratislava",
    )
    response = paired.get("/api/tonight?night_date=2026-09-24")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dark_window_start"].startswith("2026-09-24T")
    assert body["dark_window_end"].startswith("2026-09-25T")
    assert body["conditions"] is None
    assert body["conditions_unavailable_reason"] is not None


# -- happy path: a real Met.no contact plus a mocked response ----------------- #


def test_forecast_returns_merged_points_with_provenance(contact_client: TestClient) -> None:
    payload = json.loads(FIXTURE_PATH.read_text())
    client = _pair(contact_client)
    _set_location(client, latitude=51.48, longitude=0.0)
    with respx.mock(assert_all_called=False) as router:
        router.get(DEFAULT_BASE_URL).mock(
            return_value=httpx.Response(
                200,
                json=payload,
                headers={
                    "Last-Modified": "Fri, 28 Aug 2026 10:34:25 GMT",
                    "Expires": "Fri, 28 Aug 2026 11:06:19 GMT",
                },
            )
        )
        response = client.get("/api/weather/forecast")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["points"]
    assert body["points"][0]["source"] == "metno"
    assert body["points"][0]["cloud_total"] == pytest.approx(31.2)
    assert body["stale_since"] is None


@requires_ephemeris
@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 1, 15, 12, 0, tzinfo=UTC),
        datetime(2026, 1, 15, 23, 0, tzinfo=UTC),
        datetime(2026, 9, 14, 2, 0, tzinfo=UTC),
        datetime(2026, 9, 14, 14, 0, tzinfo=UTC),
    ],
    ids=("winter-day", "winter-night", "autumn-pre-dawn", "autumn-day"),
)
def test_tonight_produces_conditions_when_weather_is_available(
    contact_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
) -> None:
    """The route and forecast share one fixed clock across representative
    seasons and both sides of dusk/dawn. A wall-clock-relative payload can
    straddle a different astronomical night when the test crosses a boundary
    between payload construction and request handling, producing no rows even
    though the fixture intends to cover the whole search window."""

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[no-untyped-def]
            return now.replace(tzinfo=None) if tz is None else now.astimezone(tz)

    monkeypatch.setattr("ssc.api.routes.tonight.datetime", FrozenDateTime)
    payload = _synthetic_metno_payload(start=now - timedelta(hours=6), hours=42)
    client = _pair(contact_client)
    _set_location(client, latitude=51.48, longitude=0.0)
    with respx.mock(assert_all_called=False) as router:
        router.get(DEFAULT_BASE_URL).mock(return_value=httpx.Response(200, json=payload))
        response = client.get("/api/tonight")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["dark_window_start"] is not None
    assert body["dark_window_end"] is not None
    assert body["dark_window_note"] is None

    assert body["weather"] is not None
    assert body["weather_unavailable_reason"] is None
    assert body["weather"]["rows"]

    assert body["conditions"] is not None
    assert body["conditions_unavailable_reason"] is None
    assert body["conditions"]["reasons"]
    assert [factor["name"] for factor in body["conditions"]["factors"]] == [
        "clouds",
        "moon",
        "dew_point",
        "wind",
        "precipitation",
        "humidity",
    ]
    assert body["conditions"]["category"] in {"excellent", "good", "fair", "poor", "very_poor"}
    assert 0.0 <= body["conditions"]["score"] <= 100.0


@requires_ephemeris
def test_forecast_and_tonight_never_carry_both_a_value_and_a_reason(paired: TestClient) -> None:
    """The route contract: ``weather``/``weather_unavailable_reason`` (and
    ``conditions``/``conditions_unavailable_reason``) are mutually exclusive,
    with no provider configured — the simplest case to prove it in."""
    _set_location(paired)
    response = paired.get("/api/tonight")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["weather"] is None) != (body["weather_unavailable_reason"] is None)
    assert (body["conditions"] is None) != (body["conditions_unavailable_reason"] is None)
