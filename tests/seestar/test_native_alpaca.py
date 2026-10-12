# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The device's own ASCOM Alpaca server, driven as a Seestar session.

The fake below is not a convenience: it reproduces what the S30 Pro actually did
when probed on 2026-09-10 (``docs/M0-FINDINGS.md``, "Firmware 9.31"), including
the two behaviours that would otherwise be papered over —

* properties are refused with ``1031`` until ``Connected`` is true, and
* ``Connected = False`` is accepted and then ignored.

A test that let SSC connect against a friendlier fake than the real device would
be worse than no test at all.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Final
from urllib.parse import parse_qs

import httpx
import pytest

from ssc.client.alpaca import AlpacaClient
from ssc.seestar.native import (
    FIRMWARE_UNKNOWN_NOTE,
    HEALTH_FAILURE_BUDGET,
    HEALTH_MEMBER,
    NO_EVENT_CHANNEL_NOTE,
    NativeAlpacaSession,
    split_device_name,
)
from ssc.seestar.session import (
    BackoffPolicy,
    ConnectionState,
    MethodNotAvailableError,
    SessionStatus,
)

#: Exactly the seven the device lists, in the order it listed them.
DEVICES: Final[tuple[dict[str, object], ...]] = (
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Wide Angle Camera",
        "DeviceType": "Camera",
        "DeviceNumber": 1,
        "UniqueID": "15587f3a",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Wide Angle Focuser",
        "DeviceType": "Focuser",
        "DeviceNumber": 1,
        "UniqueID": "24340a07",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telephoto Camera",
        "DeviceType": "Camera",
        "DeviceNumber": 0,
        "UniqueID": "8edcc07e",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telephoto Focuser",
        "DeviceType": "Focuser",
        "DeviceNumber": 0,
        "UniqueID": "fc6d1ef8",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Filter Wheel",
        "DeviceType": "FilterWheel",
        "DeviceNumber": 0,
        "UniqueID": "55abab3f",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Telescope",
        "DeviceType": "Telescope",
        "DeviceNumber": 0,
        "UniqueID": "9a901430",
    },
    {
        "DeviceName": "Seestar S30 Pro_SYNTHETIC000001 Switch",
        "DeviceType": "Switch",
        "DeviceNumber": 0,
        "UniqueID": "6fdaa72b",
    },
)

#: The mount's own flags, as read off the device.
CAPABILITIES: Final[Mapping[str, bool]] = {
    "canpark": True,
    "canunpark": True,
    "canfindhome": True,
    "canslew": True,
    "canslewasync": True,
    "canslewaltazasync": False,
    "cansettracking": True,
    "cansync": True,
    "cansetpark": False,
    "cansetpierside": False,
}

#: Mount telemetry, as the real device answered it on 2026-09-10 (the mount was
#: parked below the horizon at the time, hence the negative altitude — the
#: values are verbatim rather than tidied, so nothing here is a guess).
TELEMETRY: Final[Mapping[str, object]] = {
    "altitude": -48.72,
    "azimuth": 0,
    "rightascension": 17.445278,
    "declination": -90,
    "athome": True,
    "atpark": False,
    "tracking": False,
    "slewing": False,
}

_NOT_CONNECTED: Final = 0x407  # 1031
_NOT_IMPLEMENTED: Final = 0x400  # 1024
_UNSPECIFIED: Final = 0x4FF  # 1279

#: What the device says on every mount member when its own mount subsystem is
#: wedged, byte for byte. The trap is the pairing: it answers this *while*
#: ``telescope/0/connected`` reads ``true``.
WEDGED_MESSAGE: Final = "GET_SCOPE_GET_HORIZ_COORD fail: TELEPHOTO not connected"


class FakeDeviceAlpaca:
    """The device's Alpaca server, with its observed quirks kept in."""

    def __init__(
        self,
        *,
        ignores_disconnect: bool = True,
        api_versions: tuple[int, ...] = (1,),
        unreadable: frozenset[str] = frozenset(),
    ) -> None:
        #: Mount subsystem down while the Alpaca server is up: every telemetry
        #: member answers ``0x4ff`` and ``connected`` still reads ``true``.
        self.wedged = False
        #: Members the device accepts and then never answers. Raises the same
        #: ``httpx.ReadTimeout('')`` a real unanswered request produces —
        #: empty message included, since that emptiness is the bug under test.
        self.stalls: set[str] = set()
        self.connected = False
        self.api_versions = api_versions
        self.ignores_disconnect = ignores_disconnect
        self.unreadable = unreadable
        self.offline = False
        self.paths: list[str] = []
        #: Members read while the device was still disconnected — must stay empty.
        self.read_while_disconnected: list[str] = []

    def client_factory(self, host: str, port: int) -> AlpacaClient:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(self._handle), base_url=f"http://{host}:{port}"
        )
        return AlpacaClient(http=http)

    # -- the server ---------------------------------------------------------- #

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        if self.offline:
            raise httpx.ConnectError("connection refused", request=request)
        if path.rsplit("/", 1)[-1] in self.stalls:
            raise httpx.ReadTimeout("", request=request)
        transaction = self._transaction_id(request)

        if path == "/management/apiversions":
            return self._value(list(self.api_versions), transaction)
        if path == "/management/v1/configureddevices":
            return self._value([dict(entry) for entry in DEVICES], transaction)
        if not path.startswith("/api/v1/telescope/0/"):
            return self._error(_NOT_IMPLEMENTED, f"{path} is not implemented", transaction)

        member = path.rsplit("/", 1)[-1]
        if member == "connected":
            if request.method == "PUT":
                wanted = self._form(request).get("Connected") == "True"
                self.connected = True if self.ignores_disconnect else wanted
                return self._value(None, transaction)
            return self._value(self.connected, transaction)
        if not self.connected:
            self.read_while_disconnected.append(member)
            return self._error(_NOT_CONNECTED, f"{member} requires a connected device", transaction)
        if member in self.unreadable:
            return self._error(_NOT_IMPLEMENTED, f"{member} is not implemented", transaction)
        if member in CAPABILITIES:
            return self._value(CAPABILITIES[member], transaction)
        if member in TELEMETRY:
            if self.wedged:
                return self._error(_UNSPECIFIED, WEDGED_MESSAGE, transaction)
            return self._value(TELEMETRY[member], transaction)
        return self._error(_NOT_IMPLEMENTED, f"{member} is not implemented", transaction)

    @staticmethod
    def _form(request: httpx.Request) -> dict[str, str]:
        return {
            key: values[0]
            for key, values in parse_qs(request.content.decode("utf-8")).items()
            if values
        }

    def _transaction_id(self, request: httpx.Request) -> int:
        raw = (
            self._form(request).get("ClientTransactionID")
            if request.method == "PUT"
            else parse_qs(request.url.query.decode("utf-8")).get("ClientTransactionID", ["0"])[0]
        )
        return int(raw or 0)

    @staticmethod
    def _value(value: object, transaction: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "Value": value,
                "ClientTransactionID": transaction,
                "ServerTransactionID": transaction,
                "ErrorNumber": 0,
                "ErrorMessage": "",
            },
        )

    @staticmethod
    def _error(number: int, message: str, transaction: int) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ClientTransactionID": transaction,
                "ServerTransactionID": transaction,
                "ErrorNumber": number,
                "ErrorMessage": message,
            },
        )


class Ticker:
    """A sleeper that records what it was asked to wait and never really waits.

    After ``stop_after`` calls it parks forever, so a supervisor loop under test
    settles instead of spinning.
    """

    def __init__(self, *, stop_after: int = 2) -> None:
        self.delays: list[float] = []
        self._stop_after = stop_after
        self._parked = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if len(self.delays) >= self._stop_after:
            await self._parked.wait()
        await asyncio.sleep(0)


@pytest.fixture
def device() -> FakeDeviceAlpaca:
    return FakeDeviceAlpaca()


@pytest.fixture
async def session(device: FakeDeviceAlpaca) -> AsyncIterator[NativeAlpacaSession]:
    live = NativeAlpacaSession(
        "10.0.0.9",
        port=32323,
        client_factory=device.client_factory,
        sleeper=Ticker(),
        health_poll_s=0.01,
    )
    try:
        yield live
    finally:
        await live.aclose()


async def _connected(live: NativeAlpacaSession) -> SessionStatus:
    await live.start()
    return await live.wait_for_state({ConnectionState.DEGRADED, ConnectionState.READY}, timeout=2.0)


def test_model_and_serial_come_off_the_device_name() -> None:
    assert split_device_name("Seestar S30 Pro_SYNTHETIC000001 Telescope") == (
        "Seestar S30 Pro",
        "SYNTHETIC000001",
    )
    # Nothing to split: the model is what there is, and the serial stays unknown
    # rather than being invented out of the remainder.
    assert split_device_name("Some Other Scope") == ("Some Other Scope", None)
    assert split_device_name("") == (None, None)


async def test_a_healthy_native_link_is_ready_with_protocol_limits_as_device_information(
    session: NativeAlpacaSession, device: FakeDeviceAlpaca
) -> None:
    status = await _connected(session)

    assert status.state is ConnectionState.READY
    assert status.control_connected is True
    assert status.events_connected is False
    assert status.reason is None
    assert status.device is not None
    assert FIRMWARE_UNKNOWN_NOTE in (status.device.note or "")
    assert NO_EVENT_CHANNEL_NOTE in (status.device.note or "")
    assert device.connected is True


async def test_identity_is_what_the_device_advertises_and_no_more(
    session: NativeAlpacaSession,
) -> None:
    await _connected(session)
    info = session.device

    assert info is not None
    assert info.model == "Seestar S30 Pro"
    assert info.serial == "SYNTHETIC000001"
    assert info.control_port == 32323
    # The device cannot name its firmware over this surface, so SSC does not
    # either — and never marks it verified.
    assert info.firmware is None
    assert info.firmware_int is None
    assert info.firmware_is_verified is False
    assert dict(info.capabilities) == dict(CAPABILITIES)


async def test_nothing_is_read_before_connected_is_true(
    session: NativeAlpacaSession, device: FakeDeviceAlpaca
) -> None:
    await _connected(session)
    # Several properties answer a plausible *wrong* value while disconnected
    # (camera bayeroffsetx/y read 0/0, focuser maxstep reads 0), so a read that
    # happened before the connect would be silently believable.
    assert device.read_while_disconnected == []


async def test_an_unreadable_capability_is_absent_rather_than_false() -> None:
    device = FakeDeviceAlpaca(unreadable=frozenset({"canunpark"}))
    live = NativeAlpacaSession(
        "10.0.0.9", client_factory=device.client_factory, sleeper=Ticker(), health_poll_s=0.01
    )
    try:
        await _connected(live)
        info = live.device
        assert info is not None
        assert "canunpark" not in info.capabilities
        assert info.capabilities["canpark"] is True
    finally:
        await live.aclose()


async def test_a_json_rpc_verb_is_refused_by_name(session: NativeAlpacaSession) -> None:
    await _connected(session)

    with pytest.raises(MethodNotAvailableError) as caught:
        await session.call("scope_get_equ_coord")
    assert "scope_get_equ_coord" in str(caught.value)
    assert "10.0.0.9:32323" in str(caught.value)

    with pytest.raises(MethodNotAvailableError):
        await session.get_device_state(("device",))


async def test_closing_reports_that_the_device_ignored_the_disconnect(
    session: NativeAlpacaSession,
) -> None:
    await _connected(session)
    await session.aclose()

    status = session.status
    assert status.state is ConnectionState.DISCONNECTED
    assert status.control_connected is False
    assert status.reason is not None
    assert "ignored Connected = False" in status.reason


async def test_closing_is_quiet_when_the_device_honours_the_disconnect() -> None:
    device = FakeDeviceAlpaca(ignores_disconnect=False)
    live = NativeAlpacaSession(
        "10.0.0.9", client_factory=device.client_factory, sleeper=Ticker(), health_poll_s=0.01
    )
    await _connected(live)
    await live.aclose()

    assert device.connected is False
    assert live.status.reason == "session closed"


async def test_a_server_that_never_answers_stays_disconnected_and_says_why() -> None:
    device = FakeDeviceAlpaca()
    device.offline = True
    ticker = Ticker(stop_after=2)
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=ticker,
        backoff=BackoffPolicy(initial_s=1.0, factor=2.0, cap_s=30.0),
    )
    seen: list[SessionStatus] = []
    live.subscribe_state(seen.append)
    try:
        await live.start()
        with pytest.raises(TimeoutError):
            await live.wait_for_state({ConnectionState.DEGRADED}, timeout=0.2)

        assert live.status.state is ConnectionState.CONNECTING
        assert live.status.control_connected is False
        assert live.device is None
        reasons = [status.reason for status in seen if status.reason]
        assert any("handshake failed" in reason for reason in reasons)
        assert any("retrying in" in reason for reason in reasons)
        # Backoff, not a hot loop against a device that is not answering.
        assert ticker.delays and all(delay > 0 for delay in ticker.delays)
    finally:
        await live.aclose()


async def test_a_dropped_link_transitions_before_reconnecting(
    device: FakeDeviceAlpaca,
) -> None:
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=3),
        health_poll_s=0.0,
    )
    seen: list[ConnectionState] = []
    live.subscribe_state(lambda status: seen.append(status.state))
    try:
        await _connected(live)
        # The device dropped us — which on an HTTP surface is only visible by
        # asking. It must be published, never silently re-established.
        device.connected = False
        await asyncio.sleep(0.05)
        assert ConnectionState.CONNECTING in seen
    finally:
        await live.aclose()


async def test_a_wedged_mount_subsystem_is_published_and_not_reconnected(
    device: FakeDeviceAlpaca,
) -> None:
    """The exact live pathology of 2026-09-10: ``connected`` reads ``true``,
    every mount member answers ``[0x4ff] ... TELEPHOTO not connected``.

    A link that is up but useless must *say* what is wrong (invariant 7), and
    must not answer it with a reconnect: reconnecting cannot unwedge a device
    subsystem, and the churn is what emptied the Mount tab in the first place.
    """
    device.wedged = True
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=4),
        health_poll_s=0.0,
    )
    seen: list[ConnectionState] = []
    live.subscribe_state(lambda status: seen.append(status.state))
    try:
        status = await _connected(live)

        assert status.state is ConnectionState.DEGRADED
        assert status.reason is not None
        assert WEDGED_MESSAGE in status.reason
        assert "0x4ff" in status.reason
        assert FIRMWARE_UNKNOWN_NOTE not in status.reason
        assert status.device is not None
        assert FIRMWARE_UNKNOWN_NOTE in (status.device.note or "")
        assert NO_EVENT_CHANNEL_NOTE in (status.device.note or "")

        await asyncio.sleep(0.05)
        assert ConnectionState.CONNECTING not in seen[1:]
        assert live.status.control_connected is True
    finally:
        await live.aclose()


async def test_the_mount_recovering_clears_the_reason(device: FakeDeviceAlpaca) -> None:
    device.wedged = True
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=200),
        health_poll_s=0.0,
    )
    try:
        await _connected(live)
        assert WEDGED_MESSAGE in (live.status.reason or "")

        device.wedged = False
        await asyncio.sleep(0.05)

        assert WEDGED_MESSAGE not in (live.status.reason or "")
        assert live.status.state is ConnectionState.READY
        assert live.status.control_connected is True
    finally:
        await live.aclose()


async def test_connected_alone_is_not_taken_as_proof_the_mount_works(
    device: FakeDeviceAlpaca,
) -> None:
    """``Connected`` is true when the mount is unusable and false when it is
    fine — both measured on the same device the same afternoon. So the health
    poll must actually read a mount member, not just ``connected``."""
    device.wedged = True
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=4),
        health_poll_s=0.0,
    )
    try:
        await _connected(live)
        await asyncio.sleep(0.05)
        assert f"/api/v1/telescope/0/{HEALTH_MEMBER}" in device.paths
    finally:
        await live.aclose()


async def test_one_unanswered_health_read_degrades_instead_of_dropping_the_link(
    device: FakeDeviceAlpaca,
) -> None:
    """A device that goes quiet for one poll is slow, not gone.

    Tearing the session down on the first unanswered read is what produced the
    ~15 s reconnect loop: two 5 s polls plus one 5 s request budget, forever.
    """
    device.stalls = {HEALTH_MEMBER}
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=200),
        health_poll_s=0.0,
    )
    seen: list[SessionStatus] = []
    live.subscribe_state(seen.append)
    try:
        await _connected(live)
        await asyncio.sleep(0.05)

        degraded = [s for s in seen if s.state is ConnectionState.DEGRADED and s.reason]
        assert degraded
        assert any("has not answered the last" in (s.reason or "") for s in degraded)
        # And it does give up eventually, rather than degrading for ever.
        assert any(s.state is ConnectionState.CONNECTING for s in seen[1:])
        lost = next(s for s in seen[1:] if s.state is ConnectionState.CONNECTING)
        assert lost.control_connected is False
        assert f"{HEALTH_FAILURE_BUDGET} consecutive health reads" in (lost.reason or "")
    finally:
        await live.aclose()


async def test_an_unanswered_read_is_never_reported_as_an_empty_reason(
    device: FakeDeviceAlpaca,
) -> None:
    """``telescope.connected: `` told the owner nothing for a whole session.

    ``str(httpx.ReadTimeout(...))`` is the empty string, so a message built by
    interpolating one is a colon and a space. Every published reason must name
    what happened.
    """
    live = NativeAlpacaSession(
        "10.0.0.9",
        client_factory=device.client_factory,
        sleeper=Ticker(stop_after=200),
        health_poll_s=0.0,
    )
    seen: list[SessionStatus] = []
    live.subscribe_state(seen.append)
    try:
        await _connected(live)
        # After the handshake, so the stall lands on a health poll rather than
        # on the connect — which is where the owner saw it.
        device.stalls = {"connected"}
        await asyncio.sleep(0.05)

        reasons = [s.reason for s in seen if s.reason]
        assert reasons
        assert not any(reason.rstrip().endswith(":") for reason in reasons)
        assert any("read timed out" in reason for reason in reasons)
        assert any("ReadTimeout" in reason for reason in reasons)
    finally:
        await live.aclose()


async def test_an_event_subscription_is_accepted_and_never_fires(
    session: NativeAlpacaSession,
) -> None:
    received: list[object] = []
    unsubscribe: Callable[[], None] = session.subscribe_events(received.append)
    await _connected(session)
    await asyncio.sleep(0.05)

    assert received == []
    assert session.status.events_connected is False
    unsubscribe()
