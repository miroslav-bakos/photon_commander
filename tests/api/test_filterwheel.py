# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Manual filter-wheel HTTP surface (bead ssc-bkv.6)."""

from __future__ import annotations

import pytest

from ssc.settings.config import AppConfig
from ssc.state.ports import FilterWheelNotAvailableError, FilterWheelRefusedError
from tests.api.conftest import FakeGateway, TestClient, services_of


def _unlimited(client: TestClient) -> None:
    services_of(client).limiters.pop("filterwheel", None)
    services_of(client).limiters.pop("filterwheel-read", None)


def test_state_reads_names_and_position_independently(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.filterwheel_names_value = ["Dark", "LP", "IR"]
    gateway.filterwheel_position_value = 1

    response = paired.get("/api/filterwheel")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "names": ["Dark", "LP", "IR"],
        "position": 1,
        "names_note": None,
        "position_note": None,
    }


def test_state_preserves_unknown_reasons(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.filterwheel_names_note = "Names not implemented"
    gateway.filterwheel_position_value = -1

    response = paired.get("/api/filterwheel")

    assert response.status_code == 200, response.text
    assert response.json()["names"] is None
    assert response.json()["names_note"] == "Names not implemented"
    assert response.json()["position"] == -1


def test_set_position_is_202_and_reaches_the_gateway(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.put("/api/filterwheel/position", json={"position": 2})
    assert response.status_code == 202, response.text
    assert gateway.filterwheel_calls == [("set_position", {"position": 2})]


def test_set_position_refusal_surfaces_device_message_and_code(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.filterwheel_error = FilterWheelRefusedError("wheel is busy", device_code=1025)
    response = paired.put("/api/filterwheel/position", json={"position": 2})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "wheel is busy", "device_code": 1025}


def test_set_position_not_available_is_501(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.filterwheel_error = FilterWheelNotAvailableError(
        "FilterWheel.Position: not implemented"
    )
    response = paired.put("/api/filterwheel/position", json={"position": 2})
    assert response.status_code == 501, response.text
    assert response.json()["detail"] == "FilterWheel.Position: not implemented"


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_set_position_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    _unlimited(client)
    response = client.put("/api/filterwheel/position", json={"position": 0})
    assert response.status_code == (401 if config.auth_required else 202)
