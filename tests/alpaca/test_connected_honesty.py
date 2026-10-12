# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``Connected`` must never read ``true`` once the shared session is down.

Field report, 2026-09-10: against the owner's live scope, ``telescope/0/connected``
read ``true`` while ``altitude``, ``atpark`` and ``rightascension`` all answered
``0x407`` ("linked but the session is down") in the same breath. Investigated here
at the ``ssc/alpaca`` layer: ``AlpacaDevice._get_connected`` and the ``_guard`` every
other member passes through both read ``AlpacaDevice.is_connected`` — the same
property, computed fresh, with no caching — so they cannot structurally disagree
about a session that is down *at the instant either is evaluated*. This module
locks that invariant in with the same fault the field report describes: a control
channel reset while a device still holds ``Connected = True``.

If a future change ever lets ``Connected`` answer from the per-device flag alone
(``self._connected``) without consulting the backend, this whole module goes red.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator

import httpx
import pytest

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.server import ServerConfig, create_app
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.seestar.session import (
    BackoffPolicy,
    ConnectionState,
    JsonRpcSeestarSession,
    SessionError,
    SessionStatus,
)
from tests.alpaca.conftest import api_get, api_put

#: A control-server reset that stays refused long enough to make every
#: assertion below inside the same outage window — the emulator's own default
#: (``QUIET``, 0.05s refusal) would have already reconnected by the time a
#: second HTTP round trip lands.
_STUCK_RESET = EmulatorConfig(
    event_stream_silence_s=0.01,
    close_event_stream_after_push=False,
    event_idle_timeout_s=None,
    reset_refusal_s=5.0,
    goto_settle_s=60.0,
)


@pytest.fixture
async def wedged() -> AsyncIterator[httpx.AsyncClient]:
    """Connected, then the control channel is reset out from under it."""
    emulator = SeestarEmulator(_STUCK_RESET)
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(1),
        call_timeout=2.0,
        event_reattach_delay_s=0.01,
    )
    backend = SeestarBackend(session, cache_ttl_s=0.0, ready_budget_s=5.0)
    app = create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        connected = await api_put(http, "telescope", "connected", Connected=True)
        assert connected.json()["ErrorNumber"] == 0, connected.text

        await session.wait_for_state({ConnectionState.READY}, timeout=5.0)
        emulator.trigger_control_reset("wedged mount, field report 2026-09-10")
        await session.wait_for_state({ConnectionState.DISCONNECTED}, timeout=5.0)

        assert not backend.is_connected, "the fixture's own premise: the session must be down"
        yield http

    await session.aclose()
    await emulator.aclose()


async def test_connected_reads_false_once_the_session_is_down(
    wedged: httpx.AsyncClient,
) -> None:
    """The exact field shape: ``Connected`` must not read ``true`` here."""
    response = await api_get(wedged, "telescope", "connected")
    body = response.json()
    assert body["ErrorNumber"] == 0, body
    assert body["Value"] is False, (
        "Connected read true with the session down — this is the field bug: "
        "SSC's own poller, NINA and ConformU would all believe the mount is "
        "reachable when every other member already knows it is not"
    )


@pytest.mark.parametrize("member", ["altitude", "atpark", "rightascension"])
async def test_members_refuse_with_the_same_truth_connected_reports(
    wedged: httpx.AsyncClient, member: str
) -> None:
    """Every member the field report captured, reproduced against the emulator."""
    response = await api_get(wedged, "telescope", member)
    body = response.json()
    assert body["ErrorNumber"] == 0x407
    assert "linked but the session is down" in body["ErrorMessage"]

    connected = (await api_get(wedged, "telescope", "connected")).json()["Value"]
    assert connected is False, (
        f"telescope.{member} refused as NotConnected while Connected still read "
        "true — the two must never disagree"
    )


async def test_a_session_that_never_comes_up_leaves_connected_false() -> None:
    """``Connected = True`` fails loudly rather than latching a flag it cannot honour.

    Judgement call from the field report: a ``PUT connected`` that could not
    establish a session must not succeed and leave ``self._connected`` set —
    that is exactly the shape that would let a later ``GET connected`` disagree
    with every other member. Auth-locked here (``is_verified=False``), reached
    through the real HTTP surface ConformU and NINA use, not the backend directly.
    """
    emulator = SeestarEmulator(EmulatorConfig(is_verified=False))
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(1),
        call_timeout=2.0,
        event_reattach_delay_s=0.01,
    )
    backend = SeestarBackend(session, cache_ttl_s=0.0, ready_budget_s=0.3)
    app = create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
            response = await api_put(http, "telescope", "connected", Connected=True)
            body = response.json()
            assert body["ErrorNumber"] == 0x407, body
            assert body["ErrorMessage"], "a non-zero ErrorNumber must carry a message"

            connected = (await api_get(http, "telescope", "connected")).json()["Value"]
            assert connected is False, (
                "Connected=True answered NotConnected but left the device's own flag "
                "latched true"
            )
    finally:
        await backend.aclose()
        await emulator.aclose()


async def test_a_start_failure_is_an_actionable_not_connected_response() -> None:
    class RefusingSession:
        status = SessionStatus(
            state=ConnectionState.DISCONNECTED,
            endpoint="192.0.2.30:32323",
            reason="Enable Alpaca Protocol in the Seestar app, then reconnect.",
        )
        state = ConnectionState.DISCONNECTED
        device = None

        async def start(self) -> None:
            raise SessionError(self.status.reason or "connection refused")

        async def aclose(self) -> None:
            return None

    backend = SeestarBackend(RefusingSession())  # type: ignore[arg-type]
    app = create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
        response = await api_put(http, "telescope", "connected", Connected=True)

    body = response.json()
    assert body["ErrorNumber"] == 0x407
    assert "Enable Alpaca Protocol" in body["ErrorMessage"]
