# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ``auth_required`` toggle (CLAUDE.md 2026-09-09): loginless by default,
with the pairing machinery kept behind a setting rather than deleted.

Three things this file exists to prove, because the owner asked for them by
name when the brief was widened mid-task:

1. ``ControlDep`` alone gives the toggle the right asymmetry: off, anyone can
   turn it on; on, only an already-paired control session can turn it off.
2. Turning it on from a browser that currently holds no session does not lock
   that browser out — a fresh session is minted and cookied on the same
   response.
3. Existing sessions are untouched by a toggle in either direction.
"""

from __future__ import annotations

import pytest

from ssc.api.security import SESSION_COOKIE
from ssc.settings.config import AppConfig
from tests.api.conftest import TestClient, pairing_token


def test_explicit_open_access_fixture_reports_loginless(client: TestClient) -> None:
    """No pairing needed to even ask — ``GET`` is always ``SessionDep``, which
    is a bypass with auth off."""
    body = client.get("/api/settings/security").json()
    assert body == {"auth_required": False, "auth_required_locked": False}


def test_the_state_snapshot_carries_the_same_field(client: TestClient) -> None:
    """``StateSnapshot.settings.auth_required`` — the field ``frontend-react``
    is contracted against — mirrors ``GET /api/settings/security`` exactly."""
    body = client.get("/api/state").json()
    assert body["snapshot"]["settings"]["auth_required"] is False


@pytest.mark.parametrize("config", [True], indirect=True)
def test_an_env_override_is_reported_and_locked(client: TestClient, config: AppConfig) -> None:
    assert config.auth_required is True

    # Auth is now genuinely required — GET /api/state 401s without a session.
    assert client.get("/api/state").status_code == 401

    # A session is required to even read /api/settings/security while
    # auth_required is on — use a paired one to isolate the lock check.
    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200
    security = client.get("/api/settings/security").json()
    assert security == {"auth_required": True, "auth_required_locked": True}
    state = client.get("/api/state").json()
    assert state["snapshot"]["settings"]["auth_required"] is True

    locked = client.put("/api/settings/security/auth-required", json={"auth_required": False})
    assert locked.status_code == 409


def test_with_auth_off_any_lan_caller_can_turn_pairing_on(client: TestClient) -> None:
    """The asymmetry ``ControlDep`` gives for free: off, turning it *on* needs
    no session at all — it only adds protection."""
    response = client.put("/api/settings/security/auth-required", json={"auth_required": True})
    assert response.status_code == 200
    assert response.json() == {"auth_required": True, "auth_required_locked": False}
    assert SESSION_COOKIE in response.cookies


def test_turning_it_on_does_not_lock_out_the_caller_making_the_change(
    client: TestClient,
) -> None:
    """The whole point of minting a bootstrap session: the same, previously
    unpaired ``client`` object keeps working on the very next request, with
    no separate pairing step, because ``TestClient`` carries the cookie the
    ``PUT`` response just set."""
    turned_on = client.put("/api/settings/security/auth-required", json={"auth_required": True})
    assert turned_on.status_code == 200

    # No manual pairing happened — only the cookie the PUT above set.
    assert client.get("/api/state").status_code == 200
    assert client.get("/api/auth/session").json()["authenticated"] is True


def test_turning_it_on_locks_out_every_other_caller(client: TestClient) -> None:
    """A second, unrelated caller — same running app, no cookie of its own —
    is exactly who this is meant to gate: it must now pair like any other
    device, while the caller that made the change keeps working."""
    turned_on = client.put("/api/settings/security/auth-required", json={"auth_required": True})
    assert turned_on.status_code == 200

    other = TestClient(client.app, client=("192.168.1.77", 51111))  # type: ignore[arg-type]
    assert other.get("/api/state").status_code == 401
    assert client.get("/api/state").status_code == 200


def test_the_toggle_takes_effect_on_the_very_next_request(client: TestClient) -> None:
    """Sanity: the toggle really is live on the very next request, not cached
    from the snapshot at connect time."""
    assert client.get("/api/settings/security").json()["auth_required"] is False
    assert (
        client.put("/api/settings/security/auth-required", json={"auth_required": True}).status_code
        == 200
    )
    assert client.get("/api/settings/security").json()["auth_required"] is True


def test_turning_it_back_off_requires_the_session_that_turned_it_on(
    client: TestClient,
) -> None:
    """Once on, ``ControlDep`` is real again: only the (now cookied) caller —
    or anyone else who separately paired — can turn it back off."""
    on = client.put("/api/settings/security/auth-required", json={"auth_required": True})
    assert on.status_code == 200
    off = client.put("/api/settings/security/auth-required", json={"auth_required": False})
    assert off.status_code == 200
    assert off.json()["auth_required"] is False


def test_a_session_paired_before_the_toggle_survives_it_in_both_directions(
    paired: TestClient,
) -> None:
    """Sessions are never purged by a toggle either way — this one, real
    session (from actual pairing, before any toggle) keeps validating."""
    assert (
        paired.put("/api/settings/security/auth-required", json={"auth_required": True}).status_code
        == 200
    )
    assert paired.get("/api/state").status_code == 200

    assert (
        paired.put(
            "/api/settings/security/auth-required", json={"auth_required": False}
        ).status_code
        == 200
    )
    assert paired.get("/api/state").status_code == 200
    assert paired.get("/api/auth/session").json()["authenticated"] is True
