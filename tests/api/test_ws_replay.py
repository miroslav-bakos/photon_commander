"""The acceptance gate for M1's state layer.

Drop the WS mid-stream, replay from ``seq``, and assert the client's
reconstructed snapshot is identical to a fresh ``GET /api/state``. If this ever
fails, a phone and a desktop can disagree about whether the mount is slewing,
which is the single requirement the whole state design exists to satisfy.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from ssc.settings.config import AppConfig
from ssc.state.events import STATE_EVENT_ADAPTER, CaptureChanged
from ssc.state.models import (
    AlpacaDeviceItem,
    AlpacaDevicesState,
    CaptureState,
    MountState,
    StateSnapshot,
)
from ssc.state.reducer import replay
from tests.api.conftest import FakeGateway, TestClient, services_of


def _put_fix(client: TestClient, latitude: float, *, source: str = "manual") -> None:
    response = client.put(
        "/api/settings/location/fix",
        json={
            "source": source,
            "latitude_deg": latitude,
            "longitude_deg": 17.1,
            "elevation_m": 150.0,
        },
    )
    assert response.status_code == 200, response.text


def _fresh_state(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/state")
    assert response.status_code == 200, response.text
    payload: dict[str, Any] = response.json()
    return payload


def _receive(websocket: Any) -> dict[str, Any]:
    frame: dict[str, Any] = json.loads(websocket.receive_text())
    return frame


def test_replay_from_seq_reconstructs_a_byte_identical_snapshot(paired: TestClient) -> None:
    client = paired

    # Some history before the client ever connects: it must not need it.
    _put_fix(client, 48.10)
    _put_fix(client, 48.11)

    with client.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        assert hello["type"] == "snapshot"
        assert hello["reason"] == "initial"
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        last_seq = hello["seq"]
        epoch = hello["epoch"]

        # Events while the socket is up.
        _put_fix(client, 48.20)
        for _ in range(2):  # location.changed + settings.changed
            frame = _receive(websocket)
            assert frame["type"] == "event"
            assert frame["epoch"] == epoch
            reconstructed = replay(
                reconstructed, [STATE_EVENT_ADAPTER.validate_python(frame["event"])]
            )
            last_seq = frame["seq"]

    # --- the socket is gone; the world moves on ---------------------------- #
    _put_fix(client, 48.30)
    _put_fix(client, 48.31)

    with client.websocket_connect(f"/ws?seq={last_seq}&epoch={epoch}") as websocket:
        missed = [_receive(websocket) for _ in range(4)]

    assert [frame["type"] for frame in missed] == ["event"] * 4, (
        "a resumable gap must replay events, never resnapshot"
    )
    assert [frame["seq"] for frame in missed] == [last_seq + n for n in (1, 2, 3, 4)]
    reconstructed = replay(
        reconstructed,
        [STATE_EVENT_ADAPTER.validate_python(frame["event"]) for frame in missed],
    )

    fresh = _fresh_state(client)
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]
    assert missed[-1]["seq"] == fresh["seq"]


def test_mount_changed_replays_to_a_byte_identical_snapshot(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """The Mount tab's own instance of the module docstring's acceptance gate
    (DESIGN.md §4.4b): a desktop and a phone must never disagree about where
    the mount is pointing, including its own freshness stamp."""
    client = paired

    with client.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        assert reconstructed.mount is None, "nothing has polled yet"
        epoch = hello["epoch"]

        gateway.push_mount(
            MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                ra_hours=12.5,
                dec_deg=45.0,
                alt_deg=30.0,
                az_deg=180.0,
                tracking=True,
                at_park=False,
                slewing=False,
            )
        )
        frame = _receive(websocket)
        assert frame["type"] == "event"
        assert frame["epoch"] == epoch
        assert frame["event"]["type"] == "mount.changed"
        reconstructed = replay(
            reconstructed, [STATE_EVENT_ADAPTER.validate_python(frame["event"])]
        )

    fresh = _fresh_state(client)
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]
    assert fresh["snapshot"]["mount"]["ra_hours"] == 12.5
    assert fresh["snapshot"]["mount"]["polled_at"] is not None
    assert fresh["snapshot"]["mount"]["stale"] is False, (
        "read immediately after the push: still well inside the freshness window"
    )


def test_devices_changed_replays_to_a_byte_identical_snapshot(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """The connections footer's own instance of the same acceptance gate: a
    desktop and a phone must never disagree about which devices are
    connected, including the tri-state ``connected`` per device."""
    client = paired

    with client.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        assert reconstructed.devices is None, "nothing has polled yet"
        epoch = hello["epoch"]

        gateway.push_devices(
            AlpacaDevicesState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                items=[
                    AlpacaDeviceItem(
                        id="telescope/0", kind="telescope", number=0, label="Mount", connected=True
                    ),
                    AlpacaDeviceItem(
                        id="camera/0",
                        kind="camera",
                        number=0,
                        label="Camera (tele)",
                        connected=None,
                        note="camera/0 did not answer within 3s",
                    ),
                ],
            )
        )
        frame = _receive(websocket)
        assert frame["type"] == "event"
        assert frame["epoch"] == epoch
        assert frame["event"]["type"] == "devices.changed"
        reconstructed = replay(
            reconstructed, [STATE_EVENT_ADAPTER.validate_python(frame["event"])]
        )

    fresh = _fresh_state(client)
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]
    assert fresh["snapshot"]["devices"]["items"][0]["connected"] is True
    assert fresh["snapshot"]["devices"]["items"][1]["connected"] is None
    assert fresh["snapshot"]["devices"]["stale"] is False, (
        "read immediately after the push: still well inside the freshness window"
    )


def test_capture_changed_replays_to_a_byte_identical_snapshot(paired: TestClient) -> None:
    """The capture API's own instance of the module docstring's acceptance
    gate: a desktop and a phone must never disagree about whether SSC is
    mid-exposure right now."""
    client = paired

    with client.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        reconstructed = StateSnapshot.model_validate(hello["snapshot"])
        # Populated at startup (module docstring), never null by the time a
        # client can reach it — unlike ``run``.
        assert reconstructed.capture is not None
        assert reconstructed.capture.in_progress is False
        epoch = hello["epoch"]

        services_of(client).state.apply(
            CaptureChanged(
                capture=CaptureState(
                    in_progress=True,
                    last_frame_id=7,
                    last_captured_at=datetime(2026, 9, 10, 14, 0, 0, tzinfo=UTC),
                    last_error=None,
                    folder_configured=True,
                )
            )
        )
        frame = _receive(websocket)
        assert frame["type"] == "event"
        assert frame["epoch"] == epoch
        assert frame["event"]["type"] == "capture.changed"
        reconstructed = replay(
            reconstructed, [STATE_EVENT_ADAPTER.validate_python(frame["event"])]
        )

    fresh = _fresh_state(client)
    assert reconstructed.model_dump(mode="json") == fresh["snapshot"]
    assert fresh["snapshot"]["capture"]["in_progress"] is True
    assert fresh["snapshot"]["capture"]["last_frame_id"] == 7


def test_a_gap_larger_than_the_buffer_forces_a_resnapshot_and_says_so(paired: TestClient) -> None:
    client = paired
    with client.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        stale_seq = hello["seq"]
        epoch = hello["epoch"]

    # The buffer is 16 events in the test config; 12 fixes is 24 events.
    for index in range(12):
        _put_fix(client, 48.0 + index / 100.0)

    with client.websocket_connect(f"/ws?seq={stale_seq}&epoch={epoch}") as websocket:
        frame = _receive(websocket)

    assert frame["type"] == "snapshot"
    assert frame["reason"] == "gap"
    assert frame["requested_seq"] == stale_seq
    assert frame["oldest_replayable_seq"] > stale_seq
    fresh = _fresh_state(client)
    assert frame["seq"] == fresh["seq"]
    assert frame["snapshot"] == fresh["snapshot"]


def test_a_client_ahead_of_the_server_is_told_the_server_restarted(paired: TestClient) -> None:
    client = paired
    with client.websocket_connect("/ws?seq=9999") as websocket:
        frame = _receive(websocket)
    assert frame["type"] == "snapshot"
    assert frame["reason"] == "server_restarted"


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_ws_requires_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    """The desktop shell and the phone must never disagree about whether a
    session is needed (module docstring) — so this mirrors ``GET /api/state``
    (``tests/api/test_auth.py::test_state_requires_a_session_iff_auth_required``)
    exactly: gated when ``auth_required`` is on, open when it is off."""
    from starlette.websockets import WebSocketDisconnect

    from ssc.api.ws import WS_UNAUTHORISED

    if config.auth_required:
        try:
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_text()
        except WebSocketDisconnect as exc:
            assert exc.code == WS_UNAUTHORISED
        else:  # pragma: no cover - the assertion above is the test
            raise AssertionError("an unpaired client must not receive state")
    else:
        with client.websocket_connect("/ws") as websocket:
            frame = json.loads(websocket.receive_text())
        assert frame["type"] == "snapshot"


async def test_a_subscriber_that_falls_behind_is_resnapshotted_and_told_why() -> None:
    """The overflow path, unit-tested: it cannot be provoked through a real socket.

    A client slow enough to overflow a 256-frame queue would take longer to fall
    behind than a test should run, but the branch is exactly the one that must
    never silently skip events.
    """
    from datetime import UTC, datetime

    from ssc.api.ws import _resnapshot
    from ssc.state.events import LinkChanged
    from ssc.state.models import LinkState, LinkStatus, StateSnapshot
    from ssc.state.store import StateStore

    class StubSocket:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_text(self, text: str) -> None:
            self.sent.append(text)

    store = StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC)), queue_size=2)
    subscription = store.subscribe()
    for _ in range(5):
        store.apply(LinkChanged(link=LinkState(status=LinkStatus.READY)))
    assert subscription.overflowed is True

    socket = StubSocket()
    seq = await _resnapshot(socket, store, subscription)  # type: ignore[arg-type]

    assert seq == store.seq
    assert subscription.overflowed is False
    assert subscription.queue.empty()
    frame = json.loads(socket.sent[0])
    assert frame["type"] == "snapshot"
    assert frame["reason"] == "overflow"


def test_idle_socket_heartbeat_preserves_sequence_and_does_not_replace_snapshot(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ssc.api.ws as ws_module

    monkeypatch.setattr(ws_module, "HEARTBEAT_SECONDS", 0.02)
    with paired.websocket_connect("/ws") as websocket:
        hello = _receive(websocket)
        heartbeat = _receive(websocket)
        assert heartbeat == {"type": "heartbeat", "epoch": hello["epoch"], "seq": hello["seq"]}
        _put_fix(paired, 48.42)
        events = []
        while len(events) < 2:
            frame = _receive(websocket)
            if frame["type"] == "event":
                events.append(frame)
        assert [frame["seq"] for frame in events] == [hello["seq"] + 1, hello["seq"] + 2]
        heartbeat = _receive(websocket)
        assert heartbeat["type"] == "heartbeat"
        assert heartbeat["seq"] == events[-1]["seq"]
        assert _fresh_state(paired)["seq"] == events[-1]["seq"]
