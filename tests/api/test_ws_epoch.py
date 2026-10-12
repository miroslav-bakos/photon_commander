"""Regression test for the server-instance epoch (code-reviewer finding 2).

A ``seq`` alone cannot tell a restarted process from the one before it: the
counter resets to 0 on every restart, so a client resuming with ``?seq=N``
shortly afterwards can land on a *plausible* seq that in fact belongs to an
entirely different process's history. Stitching that process's events onto a
snapshot this one never sent is exactly the stale-render CLAUDE.md invariant 4
forbids — a phone and a desktop must never disagree about whether the mount is
slewing because one of them silently kept a previous process's world alive.
"""

from __future__ import annotations

import json
from typing import Any

from ssc.main import create_app
from ssc.settings.config import AppConfig
from tests.api.conftest import CLIENT_ADDRESS, FakeGateway, TestClient, pairing_token


def _receive(websocket: Any) -> dict[str, Any]:
    frame: dict[str, Any] = json.loads(websocket.receive_text())
    return frame


def _put_fix(client: TestClient, latitude: float) -> None:
    response = client.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": latitude,
            "longitude_deg": 17.1,
            "elevation_m": 150.0,
        },
    )
    assert response.status_code == 200, response.text


def test_resuming_after_a_restart_forces_a_resnapshot_not_a_stitched_replay(
    config: AppConfig,
) -> None:
    # --- process 1: a client follows it to some seq ------------------------ #
    app_one = create_app(config, gateway=FakeGateway())
    with TestClient(app_one, client=CLIENT_ADDRESS) as client_one:
        token = pairing_token(client_one)
        pair = client_one.post("/api/auth/pair", json={"token": token})
        assert pair.status_code == 200, pair.text
        session_cookie = client_one.cookies.get("ssc_session")
        assert session_cookie

        with client_one.websocket_connect("/ws") as websocket:
            hello = _receive(websocket)
            old_epoch = hello["epoch"]
            _put_fix(client_one, 48.10)
            followed = _receive(websocket)
        followed_seq = followed["seq"]
        assert followed_seq >= 1

    # --- process 2: a restart. A fresh StateStore, a new epoch. ------------ #
    app_two = create_app(config, gateway=FakeGateway())
    with TestClient(app_two, client=CLIENT_ADDRESS) as client_two:
        client_two.cookies.set("ssc_session", session_cookie)
        state = client_two.get("/api/state")
        assert state.status_code == 200, state.text
        new_epoch = state.json()["epoch"]
        assert new_epoch != old_epoch, "two StateStore instances must not share an epoch"

        # This process's own history happens to reach the same seq the old
        # client followed process 1 to.
        for index in range(followed_seq):
            _put_fix(client_two, 49.0 + index / 100.0)

        with client_two.websocket_connect(f"/ws?seq={followed_seq}&epoch={old_epoch}") as websocket:
            frame = _receive(websocket)

        fresh = client_two.get("/api/state")
        assert fresh.status_code == 200, fresh.text

    assert frame["type"] == "snapshot", (
        "an old process's epoch must never be treated as resumable — "
        "a client presenting it must always be resnapshotted, never handed a replay"
    )
    assert frame["reason"] == "server_restarted"
    assert frame["epoch"] == new_epoch
    assert frame["epoch"] != old_epoch
    assert frame["snapshot"] == fresh.json()["snapshot"], (
        "the forced snapshot must be this process's real, current state"
    )


def test_a_seq_with_no_epoch_at_all_is_also_refused_a_replay(config: AppConfig) -> None:
    """An old or careless client that omits ``epoch`` gets the safe default.

    Trusting a bare ``seq`` because the caller did not say which process it
    came from is exactly the hole finding 2 describes — silence must not be
    read as "same process".
    """
    app = create_app(config, gateway=FakeGateway())
    with TestClient(app, client=CLIENT_ADDRESS) as client:
        token = pairing_token(client)
        pair = client.post("/api/auth/pair", json={"token": token})
        assert pair.status_code == 200, pair.text

        with client.websocket_connect("/ws") as websocket:
            hello = _receive(websocket)
            _put_fix(client, 48.20)
            followed = _receive(websocket)
        followed_seq = followed["seq"]
        assert followed_seq >= 1

        with client.websocket_connect(f"/ws?seq={followed_seq}") as websocket:
            frame = _receive(websocket)

    assert frame["type"] == "snapshot"
    assert frame["reason"] == "server_restarted"
    assert frame["epoch"] == hello["epoch"]
