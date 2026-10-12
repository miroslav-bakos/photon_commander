# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The focuser panel's controls (bead ssc-bkv.7) — mirrors ``test_mount.py``'s
shape: the two failure paths a UI has to render specifically (a device
refusal, 409 with the device's own message and error number; a member the
current driver mapping does not have, 501), plus the read endpoint's own
``(position, note)`` pair.
"""

from __future__ import annotations

import logging
import time

import pytest

from ssc.settings.config import AppConfig
from ssc.state.events import AutofocusChanged
from ssc.state.models import AutofocusSampleState, AutofocusState
from ssc.state.ports import FocuserNotAvailableError, FocuserRefusedError
from tests.api.conftest import FakeGateway, TestClient, services_of


def _unlimited(client: TestClient) -> None:
    """Endpoint tests fire several requests back to back; the focuser buckets
    are deliberately mean (CLAUDE.md invariant "rate-limit action
    endpoints") and are exercised on their own in a dedicated test."""
    limiters = services_of(client).limiters
    limiters.pop("focuser", None)
    limiters.pop("focuser-read", None)


def _terminal_operation(client: TestClient, response: object) -> dict[str, object]:
    operation_id = response.json()["id"]
    operation: dict[str, object] = {}
    for _ in range(100):
        operation = client.get(f"/api/operations/{operation_id}").json()
        if operation.get("status") in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    return operation


def test_position_reads_the_gateways_value(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.focuser_position_value = 12345
    response = paired.get("/api/focuser/position")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "position": 12345,
        "note": None,
        "is_moving": None,
        "is_moving_note": None,
        "step_size": None,
        "step_size_note": None,
    }


def test_position_is_null_with_a_note_when_not_connected(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_position_value = None
    gateway.focuser_position_note = "not connected"
    response = paired.get("/api/focuser/position")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "position": None,
        "note": "not connected",
        "is_moving": None,
        "is_moving_note": None,
        "step_size": None,
        "step_size_note": None,
    }


def test_position_reads_step_size_independently_of_position(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """A device that answers ``position`` but declares ``StepSize``
    unimplemented (this hardware — ``ssc/alpaca/devices/focuser.py``) must
    still get an honest ``position`` back, not both collapsed to ``None``."""
    gateway.focuser_position_value = 12345
    gateway.focuser_step_size_note = "Focuser.StepSize: not available"
    response = paired.get("/api/focuser/position")
    assert response.status_code == 200, response.text
    assert response.json() == {
        "position": 12345,
        "note": None,
        "is_moving": None,
        "is_moving_note": None,
        "step_size": None,
        "step_size_note": "Focuser.StepSize: not available",
    }


def test_position_reads_step_size_when_the_device_reports_one(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_position_value = 12345
    gateway.focuser_step_size_value = 10
    response = paired.get("/api/focuser/position")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["step_size"] == 10
    assert body["step_size_note"] is None


def test_position_reports_focuser_motion_independently(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_position_value = 12345
    gateway.focuser_is_moving_value = True
    response = paired.get("/api/focuser/position")
    assert response.status_code == 200, response.text
    assert response.json()["is_moving"] is True
    assert response.json()["is_moving_note"] is None


def test_move_accepts_and_calls_the_gateway_with_the_given_position(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/focuser/move", json={"position": 500})
    assert response.status_code == 202, response.text
    assert gateway.focuser_calls == [("move", {"position": 500})]


def test_halt_is_204_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/focuser/halt")
    assert response.status_code == 204, response.text
    assert gateway.focuser_calls == [("halt", {})]


def test_start_autofocus_is_202_and_reaches_the_gateway(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/focuser/start-autofocus", json={"step": 10, "settle_seconds": 2.0})
    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "succeeded"
    assert gateway.focuser_calls == [
        (
            "start_autofocus",
            {"step": 10, "fine_step": 2, "exposure_s": 2.0, "settle_s": 2.0},
        )
    ]


def test_autofocus_progress_returns_live_samples(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.autofocus_state = AutofocusState(
        status="running",
        stage="coarse",
        samples=(
            AutofocusSampleState(
                position=1210,
                hfr_px=1.7,
                star_count=24,
                stage="coarse",
            ),
        ),
    )
    services_of(paired).state.apply(AutofocusChanged(autofocus=gateway.autofocus_state))
    response = paired.get("/api/focuser/autofocus")

    assert response.status_code == 200, response.text
    assert response.json()["samples"] == [
        {"position": 1210, "hfr_px": 1.7, "star_count": 24, "stage": "coarse"}
    ]
    assert response.json() == services_of(paired).state.snapshot.autofocus.model_dump(mode="json")


def test_start_autofocus_rejects_settle_shorter_than_two_seconds(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/focuser/start-autofocus", json={"step": 10, "settle_seconds": 1.9})
    assert response.status_code == 422, response.text
    assert gateway.focuser_calls == []


def test_start_autofocus_rejects_fine_step_not_smaller_than_coarse(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post(
        "/api/focuser/start-autofocus",
        json={"step": 2, "fine_step": 2, "settle_seconds": 2.0},
    )
    assert response.status_code == 422, response.text
    assert gateway.focuser_calls == []


# -- refusal: 409, with the device's own message and error number ---------- #


def test_move_refused_surfaces_the_devices_own_message(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_error = FocuserRefusedError("not connected to the focuser")
    response = paired.post("/api/focuser/move", json={"position": 500})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "not connected to the focuser", "device_code": None}


def test_halt_refused_carries_the_devices_error_number(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_error = FocuserRefusedError("[1025] busy", device_code=1025)
    response = paired.post("/api/focuser/halt")
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "[1025] busy", "device_code": 1025}


def test_start_autofocus_refusal_is_retained_on_the_operation(
    paired: TestClient, gateway: FakeGateway, caplog: pytest.LogCaptureFixture
) -> None:
    gateway.focuser_error = FocuserRefusedError("not connected to the focuser")
    with caplog.at_level(logging.WARNING, logger="ssc.api.routes.focuser"):
        response = paired.post("/api/focuser/start-autofocus", json={})
    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "failed"
    assert "not connected to the focuser" in str(operation["error"])


# -- a member the current driver mapping does not have ---------------------- #


def test_move_not_available_is_501_naming_the_member(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_error = FocuserNotAvailableError(
        "Focuser.Move: not available before firmware 9.16"
    )
    response = paired.post("/api/focuser/move", json={"position": 500})
    assert response.status_code == 501, response.text
    assert response.json()["detail"] == "Focuser.Move: not available before firmware 9.16"


def test_halt_not_available_is_501(paired: TestClient, gateway: FakeGateway) -> None:
    gateway.focuser_error = FocuserNotAvailableError("Focuser.Halt: not implemented")
    response = paired.post("/api/focuser/halt")
    assert response.status_code == 501, response.text


def test_start_autofocus_not_available_is_retained(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.focuser_error = FocuserNotAvailableError(
        "autofocus needs a live ssc-alpaca runtime, not available with a substituted device gateway"
    )
    response = paired.post("/api/focuser/start-autofocus", json={})
    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "failed"
    assert "not available" in str(operation["error"])


# -- every control is control-scoped, matching every other mutating route --- #


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_halt_requires_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    """Mirrors ``test_mount.py``'s own ``test_stop_requires_a_session_iff_auth_required``
    — one representative focuser endpoint standing in for the other two
    controls, which share the same ``ControlDep``. ``/position`` is read-only
    (``SessionDep``, not ``ControlDep``) and is not this test's concern."""
    _unlimited(client)
    response = client.post("/api/focuser/halt")
    assert response.status_code == (401 if config.auth_required else 204)


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_position_requires_a_session_iff_auth_required(
    client: TestClient, config: AppConfig
) -> None:
    _unlimited(client)
    response = client.get("/api/focuser/position")
    assert response.status_code == (401 if config.auth_required else 200)
