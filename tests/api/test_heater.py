# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from tests.api.conftest import FakeGateway, TestClient, services_of


def test_heater_state_is_read_from_switch_zero(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.heater_state_value = True
    response = paired.get("/api/heater")
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": True, "note": None}


def test_unknown_heater_state_keeps_device_reason(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.heater_state_note = "not connected"
    response = paired.get("/api/heater")
    assert response.status_code == 200, response.text
    assert response.json() == {"enabled": None, "note": "not connected"}


def test_heater_can_be_turned_on_and_off(paired: TestClient, gateway: FakeGateway) -> None:
    assert paired.put("/api/heater", json={"enabled": True}).status_code == 202
    assert paired.put("/api/heater", json={"enabled": False}).status_code == 202
    assert gateway.heater_calls == [True, False]


def test_heater_automation_policy_is_persisted(paired: TestClient) -> None:
    response = paired.put(
        "/api/heater/automation",
        json={"enabled": False, "on_margin_c": 4.0, "off_margin_c": 6.5},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "enabled": False,
        "on_margin_c": 4.0,
        "off_margin_c": 6.5,
        "evaluated_at": None,
        "dew_margin_c": None,
        "action": None,
        "note": None,
    }
    assert services_of(paired).settings.heater.on_margin_c == 4.0


def test_heater_automation_requires_hysteresis(paired: TestClient) -> None:
    response = paired.put(
        "/api/heater/automation",
        json={"enabled": True, "on_margin_c": 5.0, "off_margin_c": 5.0},
    )

    assert response.status_code == 422
