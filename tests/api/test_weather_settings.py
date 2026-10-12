"""Configuring the two shipped weather providers from Settings (DESIGN.md §6).

They are configured differently on purpose and these tests hold that line:
Met.no needs an identifying *contact*, which is published in the User-Agent of
every request and is therefore not a secret; OpenWeatherMap needs the user's
own *key*, which is a secret and must never come back over the API
(CLAUDE.md invariant 8).
"""

from __future__ import annotations

import pytest

from ssc.api.weather_service import resolve_metno_contact
from ssc.settings.config import AppConfig
from tests.api.conftest import CLIENT_ADDRESS, TestClient, services_of


class TestContactPrecedence:
    """The stored setting beats the environment. That order is the whole
    reason the Settings field is not a lie on a deployment that already
    exports SSC_WEATHER_CONTACT."""

    def test_stored_wins_over_environment(self) -> None:
        contact = resolve_metno_contact(
            stored="me@example.invalid", environment="env@example.invalid"
        )
        assert contact.value == "me@example.invalid"
        assert contact.source == "settings"

    def test_environment_is_the_fallback(self) -> None:
        contact = resolve_metno_contact(stored=None, environment="env@example.invalid")
        assert contact.value == "env@example.invalid"
        assert contact.source == "environment"

    def test_unset_is_unset_not_empty_string(self) -> None:
        contact = resolve_metno_contact(stored=None, environment=None)
        assert contact.value is None
        assert contact.source == "unset"

    @pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
    def test_whitespace_is_not_a_contact(self, blank: str) -> None:
        """A contact of " " passes a truthiness check and then fails Met.no's
        own validation at request time — the almost-configured state
        invariant 7 exists to prevent."""
        assert resolve_metno_contact(stored=blank, environment=None).value is None
        assert resolve_metno_contact(stored=blank, environment="env@example.invalid").source == (
            "environment"
        )


def test_unconfigured_reports_both_providers_unavailable_with_reasons(paired: TestClient) -> None:
    body = paired.get("/api/settings/weather").json()
    assert body["provider"] == "metno"
    assert body["metno_contact"] is None
    assert body["metno_contact_source"] == "unset"
    assert body["openweathermap_key_set"] is False
    assert body["weatherapi_key_set"] is False
    assert body["visualcrossing_key_set"] is False

    # Rank order, not construction order: Met.no outranks OpenWeatherMap
    # (DESIGN.md 6) and must be listed first even while it is the disabled one.
    assert [p["name"] for p in body["providers"]] == [
        "metno", "openweathermap", "weatherapi", "visualcrossing"
    ]
    by_name = {p["name"]: p for p in body["providers"]}
    for provider in by_name.values():
        assert provider["available"] is False
        # Invariant 7: unavailable always says why.
        assert provider["reason"]


def test_selected_provider_persists_and_applies_without_a_restart(
    config: AppConfig, paired: TestClient
) -> None:
    response = paired.put(
        "/api/settings/weather/provider", json={"provider": "openweathermap"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "openweathermap"
    assert services_of(paired).weather._selected_provider == "openweathermap"
    assert paired.get("/api/settings/weather").json()["provider"] == "openweathermap"

    from ssc.main import create_app

    gateway = services_of(paired).device._gateway
    with TestClient(create_app(config, gateway=gateway), client=CLIENT_ADDRESS) as restarted:
        restarted.post(
            "/api/auth/pair", json={"token": restarted.get("/api/auth/pairing").json()["token"]}
        )
        assert restarted.get("/api/settings/weather").json()["provider"] == "openweathermap"


def test_unknown_weather_provider_is_rejected(paired: TestClient) -> None:
    response = paired.put("/api/settings/weather/provider", json={"provider": "imaginary"})
    assert response.status_code == 422


def test_setting_a_contact_enables_metno_without_a_restart(paired: TestClient) -> None:
    response = paired.put(
        "/api/settings/weather/metno-contact", json={"contact": "obs@example.invalid"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["metno_contact"] == "obs@example.invalid"
    assert body["metno_contact_source"] == "settings"

    metno = next(p for p in body["providers"] if p["name"] == "metno")
    assert metno["available"] is True
    assert metno["reason"] is None

    # And the live service, not merely the stored setting, was reconfigured.
    names = [p.name for p in services_of(paired).weather._providers]
    assert "metno" in names


def test_clearing_the_contact_disables_metno_again(paired: TestClient) -> None:
    paired.put("/api/settings/weather/metno-contact", json={"contact": "obs@example.invalid"})
    body = paired.put("/api/settings/weather/metno-contact", json={"contact": None}).json()

    assert body["metno_contact"] is None
    assert body["metno_contact_source"] == "unset"
    metno = next(p for p in body["providers"] if p["name"] == "metno")
    assert metno["available"] is False
    assert "contact" in (metno["reason"] or "")


def test_a_stored_contact_survives_a_restart(config: AppConfig, paired: TestClient) -> None:
    """The point of storing it at all. Rebuilds the app against the same data
    directory and asserts Met.no comes up enabled with no environment help."""
    from ssc.main import create_app

    paired.put("/api/settings/weather/metno-contact", json={"contact": "obs@example.invalid"})
    assert config.weather_contact is None  # nothing in the environment to lean on

    gateway = services_of(paired).device._gateway
    with TestClient(create_app(config, gateway=gateway), client=CLIENT_ADDRESS) as restarted:
        restarted.post(
            "/api/auth/pair", json={"token": restarted.get("/api/auth/pairing").json()["token"]}
        )
        body = restarted.get("/api/settings/weather").json()

    assert body["metno_contact"] == "obs@example.invalid"
    assert body["metno_contact_source"] == "settings"
    assert next(p for p in body["providers"] if p["name"] == "metno")["available"] is True


@pytest.mark.parametrize(
    ("secret_name", "flag_name", "provider_name"),
    [
        ("openweathermap_api_key", "openweathermap_key_set", "openweathermap"),
        ("weatherapi_api_key", "weatherapi_key_set", "weatherapi"),
        ("visualcrossing_api_key", "visualcrossing_key_set", "visualcrossing"),
    ],
)
def test_weather_api_keys_never_come_back(
    paired: TestClient, secret_name: str, flag_name: str, provider_name: str
) -> None:
    """It is written through the ordinary secrets endpoint and reported only
    as a boolean (invariant 8)."""
    secret = "not-a-real-key-0123456789"
    response = paired.put(
        "/api/settings/secrets", json={"name": secret_name, "value": secret}
    )
    assert response.status_code == 200, response.text

    body = paired.get("/api/settings/weather").json()
    assert body[flag_name] is True
    assert secret not in response.text
    assert secret not in paired.get("/api/settings/weather").text

    provider = next(p for p in body["providers"] if p["name"] == provider_name)
    assert provider["available"] is True
