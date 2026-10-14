"""The composition-root adapter: Alpaca client responses in, ``ssc.state`` models out.

This is the seam ``ssc-core`` closes onto its own ``ssc-alpaca`` driver
(CLAUDE.md invariant 2). What is under test is the *conversion* — decoding
``ssc.status`` / ``ssc.probe`` / ``ssc.discover`` JSON into
:mod:`ssc.state.models` types — and the polling gateway's two load-bearing
properties: it never republishes an unchanged link/device (or the append-only
``seq`` log would grow once a second forever), and it never fabricates a
``disconnected`` event for something that was never connected.

No socket is opened for the gateway tests: a fake, duck-typed client stands in
for :class:`~ssc.client.alpaca.AlpacaClient`. :class:`AlpacaRuntime` itself
*does* bind a real loopback socket, on an OS-assigned ephemeral port (``0``),
so it never fights a real ``ssc-alpaca`` for the production port.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import ssc.main as ssc_main
from ssc.astro.frames import MountFrame
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError, AlpacaTransportError
from ssc.client.models import DeviceAddress, DiscoveredDevice, ProbeResult
from ssc.main import (
    AlpacaDeviceGateway,
    AlpacaHostingError,
    AlpacaRuntime,
    _descriptor_from_device_state,
    _descriptor_from_discovered,
    _descriptor_from_probe,
    _link_and_device_from_status,
)
from ssc.state.models import (
    AlpacaDevicesState,
    CameraFieldOfView,
    DeviceDescriptor,
    LinkState,
    LinkStatus,
    MountState,
)
from ssc.state.ports import DeviceGatewayError


class RecordingSink:
    def __init__(self) -> None:
        self.links: list[LinkState] = []
        self.devices: list[DeviceDescriptor | None] = []
        self.mounts: list[MountState | None] = []
        self.device_lists: list[AlpacaDevicesState | None] = []
        self.camera_fovs: list[CameraFieldOfView | None] = []

    def on_link(self, link: LinkState) -> None:
        self.links.append(link)

    def on_device(self, device: DeviceDescriptor | None) -> None:
        self.devices.append(device)

    def on_mount(self, mount: MountState | None) -> None:
        self.mounts.append(mount)

    def on_devices(self, devices: AlpacaDevicesState | None) -> None:
        self.device_lists.append(devices)

    def on_camera_fov(self, fov: CameraFieldOfView | None) -> None:
        self.camera_fovs.append(fov)

    def on_device_event(self, event: object) -> None:  # pragma: no cover - unused by the gateway
        raise AssertionError("AlpacaDeviceGateway has no raw device-event feed to push")


class FakeTelescope:
    """Enough of ``AlpacaDeviceClient`` for the gateway's own logic."""

    def __init__(self) -> None:
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.status_calls = 0
        self.connect_error: Exception | None = None
        self.status_error: Exception | None = None
        self._pending: list[Mapping[str, Any]] = []
        self._current: Mapping[str, Any] = _status_payload(state="disconnected")
        self.device_state_result: Mapping[str, Any] | Exception = {}
        self.device_state_calls: list[tuple[str, ...] | None] = []
        #: Standard-ASCOM-member ``get`` results, keyed by lower-case member
        #: name — the mount-telemetry/capabilities poll's own surface
        #: (``ssc.main.AlpacaDeviceGateway._read_mount_telemetry`` and
        #: ``_read_mount_capabilities``). A member not stubbed here answers
        #: exactly as an unmapped one does on the real driver: an error the
        #: gateway swallows to ``None`` per field, never a crash.
        self.get_values: dict[str, object] = {}
        self.get_calls: list[tuple[str, dict[str, object]]] = []
        self.put_calls: list[tuple[str, dict[str, object]]] = []
        self.put_error: Exception | None = None
        self.put_errors: dict[str, Exception] = {}

    def queue_status(self, payload: Mapping[str, Any]) -> None:
        """The next ``status()`` call consumes this, then it becomes the
        steady state every later call returns until something new is queued —
        a real device does not revert on its own between polls."""
        self._pending.append(payload)

    async def connect(self) -> None:
        self.connect_calls += 1
        if self.connect_error is not None:
            raise self.connect_error

    async def disconnect(self) -> None:
        self.disconnect_calls += 1

    async def status(self) -> Mapping[str, Any]:
        self.status_calls += 1
        if self.status_error is not None:
            raise self.status_error
        if self._pending:
            self._current = self._pending.pop(0)
        return self._current

    async def device_state(self, keys: tuple[str, ...] | None = None) -> Mapping[str, Any]:
        self.device_state_calls.append(keys)
        if isinstance(self.device_state_result, Exception):
            raise self.device_state_result
        return self.device_state_result

    async def get(self, member: str, **params: object) -> object:
        self.get_calls.append((member, params))
        key = member.lower()
        if key not in self.get_values:
            raise AlpacaTransportError(f"{member} is not stubbed in FakeTelescope")
        return self.get_values[key]

    async def put(self, member: str, **params: object) -> object:
        self.put_calls.append((member.lower(), params))
        if error := self.put_errors.get(member.lower()):
            raise error
        if self.put_error is not None:
            raise self.put_error
        return None


class FakeSubDevice:
    """Enough of ``AlpacaDeviceClient`` for one non-telescope device in the
    connections-footer poll (``AlpacaDeviceGateway._poll_devices``).

    ``get`` is read every tick, honestly. ``put`` is reached only when that
    same poll's own reconnect logic (``_maintain_reconnect``) has decided a
    connect attempt is due for a cleanly-``Connected: false`` device (task:
    connect/reconnect/keep-connected every device) — never for any other
    member, and never as a side effect of merely reading ``connected``.
    ``connect_error`` stands in for a device that refuses the attempt (or is
    held open by something else on the LAN); leaving it unset models a
    device that accepts the attempt and starts reporting ``connected: true``
    from then on, same as a real one would.
    """

    def __init__(
        self,
        *,
        connected: object = True,
        hangs: bool = False,
        connect_error: Exception | None = None,
        member_values: Mapping[str, object] | None = None,
        put_error: Exception | None = None,
    ) -> None:
        self.connected = connected
        self.hangs = hangs
        self.connect_error = connect_error
        self.member_values = dict(member_values or {})
        self.put_error = put_error
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, member: str, **params: object) -> object:
        del params
        key = member.lower()
        self.get_calls.append(key)
        if self.hangs:
            await asyncio.Event().wait()  # never resolves; wait_for is what must bound this
        if key in self.member_values:
            return self.member_values[key]
        if isinstance(self.connected, Exception):
            raise self.connected
        return self.connected

    async def put(self, member: str, **params: object) -> object:
        key = member.lower()
        if key not in {"connected", "position"}:
            raise AssertionError(f"the fake sub-device does not implement PUT {member}")
        self.put_calls.append((key, dict(params)))
        if self.put_error is not None:
            raise self.put_error
        if key == "connected" and self.connect_error is not None:
            raise self.connect_error
        if key == "connected":
            self.connected = True
        return None


class FakeCamera:
    """Enough of ``AlpacaDeviceClient`` for the one-time camera-geometry
    probe (``AlpacaDeviceGateway._read_camera_fov``) — member-keyed, unlike
    ``FakeSubDevice``'s single ``connected`` value, because this probe reads
    more than one member and each needs its own stubbed answer (or none, to
    exercise the "unknown stays unknown" path)."""

    def __init__(self) -> None:
        self.get_values: dict[str, object] = {}
        self.get_calls: list[str] = []
        self.hangs_on: set[str] = set()

    async def get(self, member: str, **params: object) -> object:
        del params
        key = member.lower()
        self.get_calls.append(key)
        if key in self.hangs_on:
            await asyncio.Event().wait()  # never resolves; wait_for is what must bound this
        if key not in self.get_values:
            raise AlpacaTransportError(f"{member} is not stubbed in FakeCamera")
        return self.get_values[key]

    async def put(self, member: str, **params: object) -> object:
        del params
        raise AssertionError(f"the camera-geometry probe PUT {member} — it must only ever read")


class FakeClient:
    """Enough of ``AlpacaClient`` for the gateway's own logic."""

    def __init__(self, telescope: FakeTelescope) -> None:
        self._telescope = telescope
        self.set_device_address_calls: list[tuple[str, int]] = []
        self.probe_result: ProbeResult | Exception = ProbeResult(
            reachable=True,
            handshake=True,
            firmware="8.46",
            model="Seestar S30 Pro",
            serial="SN1",
            verified=True,
            reason=None,
        )
        self.discover_result: list[DiscoveredDevice] | Exception = []
        #: The connections-footer poll's own surface
        #: (``AlpacaDeviceGateway._poll_devices``). Empty by default so every
        #: test written before this poll existed is unaffected by it.
        self.configured_devices_result: list[Mapping[str, Any]] | Exception = []
        self.configured_devices_calls = 0
        #: Non-telescope devices the footer poll may ask for, keyed exactly
        #: as ``configured_devices_result`` entries name them.
        self._sub_devices: dict[tuple[str, int], FakeSubDevice] = {}

    def add_sub_device(self, device_type: str, device_number: int, device: FakeSubDevice) -> None:
        self._sub_devices[(device_type, device_number)] = device

    def device(self, device_type: str, device_number: int = 0) -> object:
        if (device_type, device_number) == ("telescope", 0):
            return self._telescope
        # Mirrors the real ``AlpacaClient.device()``: building a device handle
        # is never a live call, so it never raises for an unregistered type.
        # ``AlpacaDeviceGateway.__init__`` now eagerly asks for "focuser" the
        # same way it already does for "telescope"; tests that care about a
        # sub-device's behaviour still call ``add_sub_device`` to replace this
        # default.
        return self._sub_devices.setdefault((device_type, device_number), FakeSubDevice())

    async def configured_devices(self) -> list[Mapping[str, Any]]:
        self.configured_devices_calls += 1
        if isinstance(self.configured_devices_result, Exception):
            raise self.configured_devices_result
        return self.configured_devices_result

    async def set_device_address(self, host: str, port: int | None = None) -> DeviceAddress:
        self.set_device_address_calls.append((host, port or 4701))
        return DeviceAddress(host=host, port=port or 4701)

    async def probe(self, host: str, port: int | None = None) -> ProbeResult:
        del host, port
        if isinstance(self.probe_result, Exception):
            raise self.probe_result
        return self.probe_result

    async def discover(
        self, *, timeout_s: float | None = None, subnets: Sequence[str] | None = None
    ) -> list[DiscoveredDevice]:
        del timeout_s, subnets
        if isinstance(self.discover_result, Exception):
            raise self.discover_result
        return self.discover_result


def _status_payload(
    *,
    state: str = "ready",
    endpoint: str | None = "10.0.0.7:4701",
    reason: str | None = None,
    control_connected: bool = True,
    events_connected: bool = True,
    events_dropped: int = 0,
    connect_attempts: int = 1,
    changed_at: str = "2026-08-28T21:00:00+00:00",
    device: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    return {
        "connected": True,
        "state": state,
        "endpoint": endpoint,
        "reason": reason,
        "control_connected": control_connected,
        "events_connected": events_connected,
        "events_dropped": events_dropped,
        "connect_attempts": connect_attempts,
        "changed_at": changed_at,
        "device": device,
        "holders": ["telescope/0"],
    }


# --------------------------------------------------------------------------- #
# Pure conversion
# --------------------------------------------------------------------------- #


def test_probe_result_converts_without_leaking_a_client_type() -> None:
    result = ProbeResult(
        reachable=True,
        handshake=True,
        firmware="8.46",
        model="Seestar S30 Pro",
        serial="SN1",
        verified=True,
        reason=None,
    )
    descriptor = _descriptor_from_probe("10.0.0.7", 4701, result)
    assert isinstance(descriptor, DeviceDescriptor)
    assert descriptor.host == "10.0.0.7"
    assert descriptor.control_port == 4701
    assert descriptor.reachable is True
    assert descriptor.firmware_verified is True
    assert descriptor.note is None
    assert descriptor.capabilities == {}, "ssc.probe carries no capabilities to report"


def test_probe_tcp_acceptance_without_handshake_is_not_a_reachable_device() -> None:
    reason = "connected but did not answer test_connection within 3.0s"
    result = ProbeResult(
        reachable=True,
        handshake=False,
        firmware=None,
        model=None,
        serial=None,
        verified=False,
        reason=reason,
    )

    descriptor = _descriptor_from_probe("10.0.0.7", 4701, result)

    assert descriptor.reachable is False
    assert descriptor.firmware_verified is False
    assert descriptor.note == reason


def test_discovered_device_converts_and_never_claims_a_verified_firmware() -> None:
    """ssc.discover's JSON has no 'verified' field — only ssc.probe's does — so
    a caller wanting that badge has to probe the address it found."""
    device = DiscoveredDevice(
        host="10.0.0.7", port=4701, firmware="8.46", model="Seestar S30 Pro", serial="SN1"
    )
    descriptor = _descriptor_from_discovered(device)
    assert descriptor.reachable is True
    assert descriptor.firmware == "8.46"
    assert descriptor.firmware_verified is False


def test_status_converts_to_a_link_state_and_a_device_descriptor() -> None:
    payload = _status_payload(
        device={
            "host": "10.0.0.7",
            "control_port": 4701,
            "reachable": True,
            "firmware": "8.46",
            "firmware_int": 846,
            "model": "Seestar S30 Pro",
            "serial": "SN1",
            "is_verified": True,
            "capabilities": {"can_wide_cam_af": True},
            "device_uptime_s": 12.5,
            "note": None,
        }
    )
    link, device = _link_and_device_from_status(payload)
    assert link.status is LinkStatus.READY
    assert link.endpoint == "10.0.0.7:4701"
    assert link.control_connected is True
    assert link.changed_at == datetime(2026, 8, 28, 21, 0, tzinfo=UTC)
    assert device is not None
    assert device.host == "10.0.0.7"
    assert device.firmware_verified is True
    assert device.capabilities == {"can_wide_cam_af": True}


def test_status_with_no_device_yet_decodes_to_none() -> None:
    link, device = _link_and_device_from_status(_status_payload(state="disconnected", device=None))
    assert link.status is LinkStatus.DISCONNECTED
    assert device is None


def test_device_state_with_an_unverified_or_unknown_firmware_never_claims_verified() -> None:
    descriptor = _descriptor_from_device_state(
        {
            "host": "10.0.0.7",
            "control_port": 4701,
            "reachable": True,
            "is_verified": None,
            "capabilities": {},
        }
    )
    assert descriptor.firmware_verified is False


# --------------------------------------------------------------------------- #
# AlpacaDeviceGateway: polling turned back into an edge-triggered stream
# Freshness transitions are server events even when the poll itself stalls.


async def test_gateway_publishes_staleness_once_and_recovers_without_retimestamping() -> None:
    gateway = AlpacaDeviceGateway(FakeClient(FakeTelescope()), tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    now = datetime.now(tz=UTC)
    mount = MountState(polled_at=now, stale=False, tracking=True)
    devices = AlpacaDevicesState(polled_at=now, stale=False, items=[])
    gateway._publish_mount(mount)
    gateway._publish_devices(devices)
    gateway._publish_elapsed_staleness(now + timedelta(seconds=6))
    gateway._publish_elapsed_staleness(now + timedelta(seconds=7))
    assert len(sink.mounts) == len(sink.device_lists) == 2
    assert sink.mounts[-1] == mount.model_copy(update={"stale": True})
    assert sink.device_lists[-1] == devices.model_copy(update={"stale": True})
    gateway._publish_mount(mount)
    gateway._publish_devices(devices)
    assert sink.mounts[-1] is not None and sink.mounts[-1].stale is False
    assert sink.device_lists[-1] is not None and sink.device_lists[-1].stale is False


async def test_slow_acquisition_is_never_published_as_fresh() -> None:
    gateway = AlpacaDeviceGateway(FakeClient(FakeTelescope()), tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    acquired = datetime.now(tz=UTC) - timedelta(seconds=6)
    gateway._publish_mount(MountState(polled_at=acquired, stale=False, tracking=True))
    assert sink.mounts[-1] is not None
    assert sink.mounts[-1].stale is True
    assert sink.mounts[-1].polled_at == acquired


async def test_unexpected_poll_exit_marks_retained_readings_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = AlpacaDeviceGateway(FakeClient(FakeTelescope()), tasks=_tasks())  # type: ignore[arg-type]
    monkeypatch.setattr(gateway, "_POLL_INTERVAL_S", 0.001)
    sink = RecordingSink()
    gateway.attach(sink)
    now = datetime.now(tz=UTC)
    gateway._publish_mount(MountState(polled_at=now, stale=False, tracking=True))
    gateway._publish_devices(AlpacaDevicesState(polled_at=now, stale=False, items=[]))

    async def fail() -> bool:
        raise RuntimeError("unexpected decoder failure")

    monkeypatch.setattr(gateway, "_poll_once", fail)
    with pytest.raises(RuntimeError, match="unexpected decoder"):
        await gateway._poll_loop()
    assert sink.mounts[-1] is not None and sink.mounts[-1].stale
    assert sink.mounts[-1].polled_at == now
    assert sink.device_lists[-1] is not None and sink.device_lists[-1].stale


async def test_malformed_battery_is_contained_by_status_decode_guard() -> None:
    telescope = FakeTelescope()
    payload = dict(_status_payload(state="ready", device={
        "host": "10.0.0.7", "control_port": 4701, "reachable": True,
    }))
    payload["battery"] = {"percent": "bad"}
    telescope.queue_status(payload)
    gateway = AlpacaDeviceGateway(FakeClient(telescope), tasks=_tasks())  # type: ignore[arg-type]
    assert await gateway._poll_once() is True


@pytest.mark.parametrize("failure", ["blocked", "transport"])
async def test_freshness_watchdog_runs_during_failed_poll_and_stops_with_it(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    telescope = FakeTelescope()
    telescope.status_error = AlpacaTransportError("unreachable")
    gateway = AlpacaDeviceGateway(FakeClient(telescope), tasks=_tasks())  # type: ignore[arg-type]
    monkeypatch.setattr(gateway, "_POLL_INTERVAL_S", 0.001)
    current = datetime.now(tz=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return current

    monkeypatch.setattr(ssc_main, "datetime", Clock)
    stale_seen = asyncio.Event()

    class Sink(RecordingSink):
        def on_mount(self, mount: MountState | None) -> None:
            super().on_mount(mount)
            if mount is not None and mount.stale:
                stale_seen.set()

    sink = Sink()
    gateway.attach(sink)
    gateway._publish_mount(MountState(polled_at=current, stale=False, tracking=True))
    blocked = asyncio.Event()
    original_poll = gateway._poll_once

    async def poll() -> bool:
        blocked.set()
        if failure == "transport":
            return await original_poll()
        await asyncio.Event().wait()
        return True

    monkeypatch.setattr(gateway, "_poll_once", poll)
    task = asyncio.create_task(gateway._poll_loop())
    try:
        await asyncio.wait_for(blocked.wait(), 1)
        watchdogs = [t for t in asyncio.all_tasks() if t.get_name() == "alpaca-freshness"]
        assert len(watchdogs) == 1
        current += timedelta(seconds=6)
        await asyncio.wait_for(stale_seen.wait(), 1)
        assert sink.mounts[-1] is not None and sink.mounts[-1].tracking is True
    finally:
        task.cancel()
        await task
    assert all(t.done() for t in watchdogs)
# --------------------------------------------------------------------------- #


async def test_connect_publishes_the_resulting_link_and_device_once() -> None:
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        assert client.set_device_address_calls == [("10.0.0.7", 4701)]
        assert telescope.connect_calls == 1
        assert len(sink.links) == 1
        assert sink.links[0].status is LinkStatus.READY
    finally:
        await gateway.disconnect()


@pytest.mark.parametrize("failure_stage", ["address", "connect"])
async def test_connect_preserves_alpaca_failure(
    failure_stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    telescope = FakeTelescope()
    client = FakeClient(telescope)
    error = AlpacaTransportError("Alpaca service refused the connection")
    telescope.status_error = AlpacaRemoteError(
        AlpacaErrorNumber.NOT_CONNECTED, "No telescope connection", member="ssc.status"
    )
    if failure_stage == "address":

        async def fail_address(host: str, port: int) -> None:
            raise error

        monkeypatch.setattr(client, "set_device_address", fail_address)
    else:
        telescope.connect_error = error
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]

    with pytest.raises(DeviceGatewayError, match="Alpaca service refused the connection") as caught:
        await gateway.connect("10.0.0.7", control_port=4701)

    assert caught.value.__cause__ is error
    assert telescope.status_calls == 0


async def test_a_first_disconnect_does_not_fabricate_a_disconnected_event() -> None:
    """Nothing was ever connected: publishing DISCONNECTED here would be
    fabricated, and a client mid connect could read it as the connect having
    already failed — the same property SeestarDeviceGateway's disconnect()
    used to guard, restated for the polling gateway."""
    client = FakeClient(FakeTelescope())
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    await gateway.disconnect()
    assert sink.links == []


async def test_disconnect_after_a_real_connect_publishes_disconnected_once() -> None:
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    await gateway.connect("10.0.0.7", control_port=4701)
    await gateway.disconnect()
    assert telescope.disconnect_calls == 1
    assert [link.status for link in sink.links] == [LinkStatus.READY, LinkStatus.DISCONNECTED]


async def test_mount_alignment_is_none_before_anything_connects() -> None:
    client = FakeClient(FakeTelescope())
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    assert await gateway.mount_alignment() is None


@pytest.mark.parametrize(("mode", "is_equatorial"), [(0, False), (1, True), (2, True)])
async def test_mount_alignment_reads_standard_ascom_mode_once_connected(
    mode: int, is_equatorial: bool
) -> None:
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["alignmentmode"] = mode
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        assert await gateway.mount_alignment() is is_equatorial
        assert ("alignmentmode", {}) in telescope.get_calls
        assert telescope.device_state_calls == []
    finally:
        await gateway.disconnect()


async def test_mount_alignment_is_none_when_the_read_fails() -> None:
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        assert await gateway.mount_alignment() is None
    finally:
        await gateway.disconnect()


async def test_mount_alignment_mode_reads_live_ascom_alignmentmode() -> None:
    """The standard ASCOM member itself — distinct from ``mount_alignment``'s
    ``mount.equ_mode`` reading (module docstring on the method). SSC did not
    read this member before ``ssc.api.polar_alignment_service`` needed it."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["alignmentmode"] = 1
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        assert await gateway.mount_alignment_mode() == 1
        assert ("alignmentmode", {}) in telescope.get_calls
    finally:
        await gateway.disconnect()


async def test_mount_alignment_mode_is_none_when_unreadable() -> None:
    client = FakeClient(FakeTelescope())
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    assert await gateway.mount_alignment_mode() is None


async def test_mount_telemetry_reads_the_devices_own_site_and_frame() -> None:
    """``ssc.astro.frames``' inbound/outbound conversion needs both of these —
    read here, the same way as every other mount member, never assumed."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["sitelatitude"] = 48.2
    telescope.get_values["sitelongitude"] = 16.4
    telescope.get_values["equatorialsystem"] = 1  # equTopocentric
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        assert sink.mounts, "connecting polls the mount at least once"
        mount = sink.mounts[-1]
        assert mount is not None
        assert mount.site_latitude_deg == 48.2
        assert mount.site_longitude_deg == 16.4
        assert mount.equatorial_system is MountFrame.TOPOCENTRIC
    finally:
        await gateway.disconnect()


async def test_mount_telemetry_treats_an_ascom_undefined_frame_code_as_unknown() -> None:
    """A code outside ASCOM's own 0-4 is a driver bug, not a value to map to
    ``"other"`` and act on — CLAUDE.md invariant 7: unknown stays unknown."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["equatorialsystem"] = 9
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        mount = sink.mounts[-1]
        assert mount is not None
        assert mount.equatorial_system is None
    finally:
        await gateway.disconnect()


async def test_set_site_puts_latitude_and_longitude_and_elevation_when_known() -> None:
    telescope = FakeTelescope()
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    await gateway.set_site(latitude_deg=48.2, longitude_deg=16.4, elevation_m=180.0)
    assert telescope.put_calls == [
        ("sitelatitude", {"SiteLatitude": 48.2}),
        ("sitelongitude", {"SiteLongitude": 16.4}),
        ("siteelevation", {"SiteElevation": 180.0}),
    ]


async def test_set_site_leaves_elevation_unset_when_unknown() -> None:
    telescope = FakeTelescope()
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    await gateway.set_site(latitude_deg=48.2, longitude_deg=16.4, elevation_m=None)
    assert telescope.put_calls == [
        ("sitelatitude", {"SiteLatitude": 48.2}),
        ("sitelongitude", {"SiteLongitude": 16.4}),
    ]


async def test_set_site_caches_unsupported_elevation_but_keeps_coordinates_fresh() -> None:
    telescope = FakeTelescope()
    telescope.put_errors["siteelevation"] = AlpacaRemoteError(
        AlpacaErrorNumber.NOT_IMPLEMENTED,
        "SiteElevation is not implemented",
        member="telescope.siteelevation",
    )
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]

    await gateway.set_site(latitude_deg=48.2, longitude_deg=16.4, elevation_m=180.0)
    await gateway.set_site(latitude_deg=48.3, longitude_deg=16.5, elevation_m=181.0)

    assert telescope.put_calls == [
        ("sitelatitude", {"SiteLatitude": 48.2}),
        ("sitelongitude", {"SiteLongitude": 16.4}),
        ("siteelevation", {"SiteElevation": 180.0}),
        ("sitelatitude", {"SiteLatitude": 48.3}),
        ("sitelongitude", {"SiteLongitude": 16.5}),
    ]


async def test_set_site_failure_is_logged_not_raised() -> None:
    telescope = FakeTelescope()
    telescope.put_error = AlpacaTransportError("no response")
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    # Must not raise.
    await gateway.set_site(latitude_deg=48.2, longitude_deg=16.4, elevation_m=None)


async def test_polling_only_republishes_on_an_actual_change() -> None:
    """Otherwise the append-only seq log grows once a second forever."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    await gateway.connect("10.0.0.7", control_port=4701)
    assert len(sink.links) == 1

    # Same status, polled again directly (bypassing the sleep-based loop):
    # must not republish.
    await gateway._poll_once()
    assert len(sink.links) == 1

    # A genuine change must still get through.
    telescope.queue_status(_status_payload(state="ready", events_dropped=1))
    await gateway._poll_once()
    assert len(sink.links) == 2
    assert sink.links[-1].events_dropped == 1

    await gateway.disconnect()


async def test_not_connected_status_ends_polling_and_clears_device_state() -> None:
    """The local Alpaca server survives a physical scope drop. Its 0x407 is
    therefore the edge that must end this poll session and wake the outer
    reconnect supervisor, not become one warning every second forever."""
    telescope = FakeTelescope()
    telescope.queue_status(
        _status_payload(
            state="ready",
            device={
                "host": "10.0.0.7",
                "control_port": 4701,
                "reachable": True,
                "model": "Seestar S30 Pro",
            },
        )
    )
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        telescope.status_error = AlpacaRemoteError(
            0x407,
            "telescope.action requires Connected = True (this device is not connected)",
            member="telescope.action",
        )

        assert await gateway._poll_once() is False
        assert sink.links[-1].status is LinkStatus.DISCONNECTED
        assert "device is not connected" in (sink.links[-1].reason or "")
        assert sink.devices[-1] is None
        assert sink.mounts[-1] is None
        assert sink.device_lists[-1] is None
        assert sink.camera_fovs[-1] is None
    finally:
        await gateway.disconnect()


async def test_not_connected_status_stops_the_background_poll_loop() -> None:
    """After the disconnect edge, no further one-second status requests run."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    gateway = AlpacaDeviceGateway(FakeClient(telescope), tasks=_tasks())  # type: ignore[arg-type]
    gateway._POLL_INTERVAL_S = 0.01  # type: ignore[misc]
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        poll_task = gateway._poll_task
        assert poll_task is not None
        telescope.status_error = AlpacaRemoteError(
            0x407, "requires Connected = True", member="telescope.action"
        )

        await asyncio.wait_for(poll_task, timeout=0.5)
        calls_after_disconnect = telescope.status_calls
        await asyncio.sleep(0.04)

        assert poll_task.done()
        assert telescope.status_calls == calls_after_disconnect == 2
    finally:
        await gateway.disconnect()


async def test_connect_refuses_to_start_poller_when_alpaca_is_not_connected() -> None:
    telescope = FakeTelescope()
    telescope.status_error = AlpacaRemoteError(
        0x407, "requires Connected = True", member="telescope.action"
    )
    gateway = AlpacaDeviceGateway(FakeClient(telescope), tasks=_tasks())  # type: ignore[arg-type]
    with pytest.raises(DeviceGatewayError, match="not connected"):
        await gateway.connect("10.0.0.7", control_port=4701)
    assert gateway._poll_task is None


def _configured(name: str, device_type: str, number: int) -> Mapping[str, Any]:
    """One ``/management/v1/configureddevices`` entry, shaped as
    ``ssc-alpaca``'s own registry serves it (``ssc/alpaca/server.py``)."""
    return {
        "DeviceName": name,
        "DeviceType": device_type,
        "DeviceNumber": number,
        "UniqueID": f"{device_type}-{number}",
    }


async def test_devices_poll_enumerates_from_configureddevices_with_tristate_connected() -> None:
    """Never a hardcoded list (CLAUDE.md invariant 11) — whatever
    ``configureddevices`` reports is what the snapshot carries, kind, label
    and all, and ``connected`` is genuinely tri-state per device."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["connected"] = True
    client = FakeClient(telescope)
    client.configured_devices_result = [
        _configured("SSC Seestar Mount", "Telescope", 0),
        _configured("Seestar S30 Pro_SYNTHETIC000001 Telephoto Camera", "Camera", 0),
        _configured("Seestar S30 Pro_SYNTHETIC000001 Wide Camera", "Camera", 1),
        _configured("SSC Seestar Focuser", "Focuser", 0),
        _configured("Dew heater", "Switch", 0),
    ]
    client.add_sub_device("camera", 0, FakeSubDevice(connected=True))
    client.add_sub_device("camera", 1, FakeSubDevice(connected=False))
    client.add_sub_device("focuser", 0, FakeSubDevice(connected=AlpacaTransportError("no reply")))
    client.add_sub_device("switch", 0, FakeSubDevice(connected=True))
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    devices = sink.device_lists[-1]
    assert devices is not None
    by_id = {item.id: item for item in devices.items}
    assert by_id["telescope/0"].kind == "telescope"
    assert by_id["telescope/0"].label == "Mount"
    assert by_id["telescope/0"].connected is True
    assert by_id["camera/0"].label == "Camera (tele)"
    assert by_id["camera/0"].connected is True
    assert by_id["camera/0"].temperature_c is None
    assert by_id["camera/1"].label == "Camera (wide)"
    assert by_id["camera/1"].connected is False
    # First sighting of a cleanly-disconnected device: reported honestly
    # rather than acted on silently mid-discovery (task requirement 3).
    assert by_id["camera/1"].note == "never attempted"
    assert by_id["focuser/0"].connected is None, "a transport failure is unknown, never false"
    assert by_id["focuser/0"].note is not None and "no reply" in by_id["focuser/0"].note
    assert by_id["switch/0"].kind == "switch"
    assert by_id["switch/0"].label == "Switch"
    assert devices.stale is False
    assert devices.polled_at is not None


@pytest.mark.parametrize("elapsed_s, stale", [(1, False), (6, True)])
async def test_devices_poll_retains_acquisition_time_when_batch_expires(
    monkeypatch: pytest.MonkeyPatch, elapsed_s: int, stale: bool,
) -> None:
    acquired = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
    current = acquired

    class Clock(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> datetime:
            return current

    class DelayedClient(FakeClient):
        async def configured_devices(self) -> list[Mapping[str, Any]]:
            nonlocal current
            result = await super().configured_devices()
            current += timedelta(seconds=elapsed_s)
            return result

    monkeypatch.setattr(ssc_main, "datetime", Clock)
    client = DelayedClient(FakeTelescope())
    client.configured_devices_result = [_configured("Dew heater", "Switch", 0)]
    client.add_sub_device("switch", 0, FakeSubDevice(connected=True))
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)

    await gateway._poll_devices()

    sample = sink.device_lists[-1]
    assert sample is not None
    assert sample.items[0].connected is True
    assert sample.polled_at == acquired
    assert sample.stale is stale


async def test_devices_poll_reads_primary_camera_sensor_temperature() -> None:
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=True, member_values={"ccdtemperature": 24.875})
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    devices = sink.device_lists[-1]
    assert devices is not None
    assert devices.items[0].temperature_c == pytest.approx(24.875)
    assert devices.items[0].temperature_note is None
    assert {"connected", "ccdtemperature"}.issubset(camera.get_calls)


async def test_devices_poll_first_sighting_of_a_disconnected_device_does_not_yet_attempt() -> None:
    """A device seen disconnected for the first time is reported as ``"never
    attempted"`` on the very tick it is discovered — the attempt itself is
    scheduled, not made mid-discovery — so ``FakeSubDevice.put`` (which
    raises on anything but ``Connected``, and is otherwise fine to call) is
    never reached this tick."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=False)
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()
    assert camera.put_calls == []
    devices = sink.device_lists[-1]
    assert devices is not None
    assert devices.items[0].connected is False
    assert devices.items[0].note == "never attempted"


async def test_devices_poll_reconnects_a_disconnected_device_once_due_and_stops_once_up() -> None:
    """The actual bug fix: a cleanly-disconnected device is reconnected from
    the same poll (never a second timer), and once it reports connected it
    is never attempted again (task requirements 1-3)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=False)  # accepts the attempt and comes up
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)  # tick 1: never attempted
        assert camera.put_calls == []
        # The next tick's own due-check would find this attempt scheduled for
        # "now" already in the past; force that here rather than sleeping.
        gateway._reconnects["camera/0"].next_attempt_at = datetime.now(tz=UTC)
        await gateway._poll_once()  # tick 2: due -> attempts, and succeeds
        assert len(camera.put_calls) == 1
        devices = sink.device_lists[-1]
        assert devices is not None
        assert devices.items[0].connected is True
        assert devices.items[0].note is None
        assert "camera/0" not in gateway._reconnects, "a connected device carries no retry state"

        await gateway._poll_once()  # tick 3
        await gateway._poll_once()  # tick 4
        assert len(camera.put_calls) == 1, "a device already reporting connected must not be re-put"
    finally:
        await gateway.disconnect()


async def test_devices_poll_distinguishes_never_attempted_from_a_failed_attempt() -> None:
    """The task's own deliverable: ``None`` must never stand in for either of
    these — a device never attempted and a device whose attempt failed must
    carry two different, real notes."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=False, connect_error=AlpacaTransportError("refused"))
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        never_attempted_note = sink.device_lists[-1].items[0].note
        assert never_attempted_note == "never attempted"

        gateway._reconnects["camera/0"].next_attempt_at = datetime.now(tz=UTC)
        await gateway._poll_once()
        failed_note = sink.device_lists[-1].items[0].note
        assert failed_note is not None
        assert "refused" in failed_note
        assert failed_note != never_attempted_note
        assert sink.device_lists[-1].items[0].connected is False
    finally:
        await gateway.disconnect()


async def test_devices_poll_backs_off_a_permanently_failing_device() -> None:
    """Repeated polls against a device that never comes up must not attempt
    once per poll — that is the tight retry loop DESIGN.md §9 rules out, and
    concretely what would happen if SSC held a camera the owner's own ZWO
    Seestar app has open (task requirement 4). A frozen, test-controlled
    clock stands in for wall time so this does not sleep for real minutes."""

    class _FrozenDatetime(datetime):
        _frozen: datetime

        @classmethod
        def now(cls, tz: Any = None) -> datetime:  # type: ignore[override]
            del tz
            return cls._frozen

    _FrozenDatetime._frozen = datetime(2026, 1, 1, tzinfo=UTC)

    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=False, connect_error=AlpacaTransportError("held elsewhere"))
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    original_datetime = ssc_main.datetime
    ssc_main.datetime = _FrozenDatetime  # type: ignore[assignment]
    try:
        await gateway.connect("10.0.0.7", control_port=4701)  # tick 1: never attempted
        polls = 40
        for _ in range(polls):
            _FrozenDatetime._frozen += timedelta(seconds=AlpacaDeviceGateway._POLL_INTERVAL_S)
            await gateway._poll_once()
        assert 0 < len(camera.put_calls) < polls // 2, (
            f"expected sub-linear backoff over {polls} polls, got {len(camera.put_calls)} attempts"
        )
    finally:
        ssc_main.datetime = original_datetime
        await gateway.disconnect()


async def test_devices_poll_leaves_each_sub_devices_connected_flag_exactly_as_it_was() -> None:
    """A status poll must never write anything to a device that already
    reports connected — only ever read it (task requirement 2; the
    write-on-reconnect path is covered separately, above)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeSubDevice(connected=True)
    client.add_sub_device("camera", 0, camera)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    before = camera.connected
    await gateway.connect("10.0.0.7", control_port=4701)
    await gateway._poll_once()
    await gateway.disconnect()
    assert camera.connected == before, "polling read Connected; it must never have set it"
    # One ``Connected`` read per tick, same as before this test was written —
    # plus the one-time camera-geometry probe (``_poll_camera_fov``), which
    # shares this same fake camera object and reads three more members on
    # the first tick only (cached thereafter, see ``_camera_fov_attempted``).
    # ``FakeSubDevice.get`` answers every member with its own ``connected``
    # bool regardless of name, so the geometry probe finds nothing usable
    # and never republishes; this test's own concern — one ``Connected`` read
    # per tick — is asserted directly rather than via the full call list.
    assert camera.get_calls.count("connected") == 2, "one Connected read per tick"


async def test_devices_poll_bounds_a_hung_member_without_failing_the_others() -> None:
    """One member that never answers must degrade to ``connected: null`` for
    that device alone — never wedge the tick, or fail every other device's
    read (task requirement 3; measured live, this stuck SSC for half an hour
    on 2026-09-10)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [
        _configured("SSC Seestar Camera", "Camera", 0),
        _configured("SSC Seestar Focuser", "Focuser", 0),
    ]
    client.add_sub_device("camera", 0, FakeSubDevice(hangs=True))
    client.add_sub_device("focuser", 0, FakeSubDevice(connected=True))
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    # A short bound so this test does not itself wait out the real 3s default.
    gateway._DEVICE_READ_TIMEOUT_S = 0.05  # type: ignore[misc]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await asyncio.wait_for(gateway.connect("10.0.0.7", control_port=4701), timeout=2.0)
    finally:
        await gateway.disconnect()

    devices = sink.device_lists[-1]
    assert devices is not None
    by_id = {item.id: item for item in devices.items}
    assert by_id["camera/0"].connected is None
    assert by_id["camera/0"].note is not None and "0.05" in by_id["camera/0"].note
    assert by_id["focuser/0"].connected is True, "one hung device must not fail the others"


async def test_devices_poll_unrecognised_configureddevices_is_dropped_not_guessed() -> None:
    """A ``DeviceType`` this table does not know is left out rather than
    mislabelled (CLAUDE.md invariant 7) — and does not fail the rest of the
    poll."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = [
        _configured("Some Rotator", "Rotator", 0),
        _configured("SSC Seestar Camera", "Camera", 0),
    ]
    client.add_sub_device("camera", 0, FakeSubDevice(connected=True))
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    devices = sink.device_lists[-1]
    assert devices is not None
    assert [item.id for item in devices.items] == ["camera/0"]


# --------------------------------------------------------------------------- #
# Camera field of view (DESIGN.md §3.5.1's rotated FOV rectangle)
# --------------------------------------------------------------------------- #


async def test_camera_fov_matches_the_measured_field_from_device_geometry() -> None:
    """The device's real numbers observed live, 2026-09-10: ``CameraXSize``
    2160, ``CameraYSize`` 3840, ``PixelSizeX`` 2.9 microns, and
    ``Telescope.FocalLength`` read back as ``0.16`` — ASCOM defines that
    member in *metres*, so this is the FITS ``FOCALLEN`` of 160mm.

    Through ``fov = 2 * atan(n_px * pixel_um * 1e-3 / (2 * focal_mm))`` this
    lands at 3.9862 x 2.2428 degrees. That is close to, but not within a
    hundredth of a degree of, the 3.92 x 2.20 the feature request quoted from
    the same night's plate solves (3.672 arcsec/px): that figure is the
    *measured* pixel scale, which implies an effective focal length of about
    162.7mm, not the nominal 160mm ``Telescope.FocalLength`` itself reports.
    This test pins the formula against the device's own nominal numbers,
    which is what this dispatch was scoped to use — sourcing a corrected
    focal length from a plate solve would mean importing ``ssc.platesolve``,
    out of bounds for this dispatch. See the dispatch notes for this gap.
    """
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["focallength"] = 0.16  # ASCOM: metres
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeCamera()
    camera.get_values["cameraxsize"] = 2160
    camera.get_values["cameraysize"] = 3840
    camera.get_values["pixelsizex"] = 2.9
    client.add_sub_device("camera", 0, camera)  # type: ignore[arg-type]
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    assert sink.camera_fovs, "a fully-known geometry must publish a field"
    fov = sink.camera_fovs[-1]
    assert fov is not None
    # X is FITS NAXIS1/width and Y is NAXIS2/height: the field is portrait.
    assert fov.height_deg > fov.width_deg
    assert fov.width_deg == pytest.approx(2.2428, abs=0.001)
    assert fov.height_deg == pytest.approx(3.9862, abs=0.001)
    assert fov.rotation_deg is None, "no plate solve feeds this yet"


async def test_camera_fov_publishes_nothing_when_focal_length_is_unknown() -> None:
    """A camera that reports its own size, but a focal length that cannot be
    read, must publish no field at all — never a half-known one (CLAUDE.md
    invariant 7)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    # telescope.get_values deliberately has no "focallength" entry.
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeCamera()
    camera.get_values["cameraxsize"] = 2160
    camera.get_values["cameraysize"] = 3840
    camera.get_values["pixelsizex"] = 2.9
    client.add_sub_device("camera", 0, camera)  # type: ignore[arg-type]
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        await gateway._poll_once()
    finally:
        await gateway.disconnect()

    assert sink.camera_fovs == [], "a partial reading must never be published"


async def test_camera_fov_publishes_nothing_when_no_camera_is_configured() -> None:
    """No ``configureddevices`` entry of kind ``camera`` at all — the probe
    must never guess at a device it has not seen (CLAUDE.md invariant 11)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["focallength"] = 0.16
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Mount", "Telescope", 0)]
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    assert sink.camera_fovs == []


async def test_camera_fov_is_read_once_per_connection_not_every_tick() -> None:
    """Sensor size and pixel size do not change while connected, so the
    geometry probe must not repeat its reads on every poll tick."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    telescope.get_values["focallength"] = 0.16
    client = FakeClient(telescope)
    client.configured_devices_result = [_configured("SSC Seestar Camera", "Camera", 0)]
    camera = FakeCamera()
    camera.get_values["cameraxsize"] = 2160
    camera.get_values["cameraysize"] = 3840
    camera.get_values["pixelsizex"] = 2.9
    client.add_sub_device("camera", 0, camera)  # type: ignore[arg-type]
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    geometry_members = {"cameraxsize", "cameraysize", "pixelsizex"}
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
        await gateway._poll_once()
        await gateway._poll_once()
    finally:
        await gateway.disconnect()

    # The footer poll's own "connected" read (unrelated to this feature)
    # legitimately repeats every tick; only the geometry members must not.
    geometry_calls = [call for call in camera.get_calls if call in geometry_members]
    assert geometry_calls == ["cameraxsize", "cameraysize", "pixelsizex"], (
        "cached — no repeat geometry reads while connected"
    )


async def test_devices_poll_failure_does_not_kill_the_mount_poll() -> None:
    """``configureddevices`` failing must not stop the mount half of the same
    tick — CLAUDE.md invariant 7, mirroring the malformed-status test above."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="ready"))
    client = FakeClient(telescope)
    client.configured_devices_result = AlpacaTransportError("no response")
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)
    try:
        await gateway.connect("10.0.0.7", control_port=4701)
    finally:
        await gateway.disconnect()

    assert sink.device_lists == [], "a failed enumeration must not publish a fabricated list"
    assert sink.mounts, "the mount half of the same tick must still have run"


async def test_a_malformed_status_payload_does_not_kill_the_poll_loop() -> None:
    """Protocol drift is a reported failure, not a reason the gateway stops
    updating link state forever (CLAUDE.md invariant 7)."""
    telescope = FakeTelescope()
    telescope.queue_status(_status_payload(state="not-a-real-state"))
    client = FakeClient(telescope)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    sink = RecordingSink()
    gateway.attach(sink)

    await gateway._poll_once()
    assert sink.links == [], "an undecodable payload must not be published as if it were real"

    telescope.queue_status(_status_payload(state="ready"))
    await gateway._poll_once()
    assert sink.links and sink.links[-1].status is LinkStatus.READY, "polling must recover"


async def test_probe_reports_an_unreachable_result_rather_than_raising() -> None:
    client = FakeClient(FakeTelescope())
    client.probe_result = AlpacaTransportError("connection refused")
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    descriptor = await gateway.probe("10.0.0.9", control_port=4701, timeout=3.0)
    assert descriptor.reachable is False
    assert descriptor.note is not None and "connection refused" in descriptor.note


async def test_discover_translates_a_client_error_into_a_gateway_error() -> None:
    from ssc.state.ports import DeviceGatewayError

    client = FakeClient(FakeTelescope())
    client.discover_result = AlpacaRemoteError(0x401, "not RFC 1918", member="ssc.discover")
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]
    with pytest.raises(DeviceGatewayError, match="not RFC 1918"):
        await gateway.discover(timeout=8.0)


async def test_filterwheel_reads_names_and_position_and_sets_a_slot() -> None:
    client = FakeClient(FakeTelescope())
    wheel = FakeSubDevice(
        connected=True,
        member_values={"names": ["Dark", "LP", "IR"], "position": 1},
    )
    client.add_sub_device("filterwheel", 0, wheel)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]

    assert await gateway.filterwheel_get_names() == (["Dark", "LP", "IR"], None)
    assert await gateway.filterwheel_get_position() == (1, None)
    await gateway.filterwheel_set_position(position=2)

    assert wheel.get_calls == ["names", "position", "connected"]
    assert wheel.put_calls == [("position", {"Position": 2})]


async def test_filterwheel_position_refusal_preserves_the_device_error() -> None:
    client = FakeClient(FakeTelescope())
    wheel = FakeSubDevice(
        connected=True,
        put_error=AlpacaRemoteError(1025, "wheel is busy", member="FilterWheel.Position"),
    )
    client.add_sub_device("filterwheel", 0, wheel)
    gateway = AlpacaDeviceGateway(client, tasks=_tasks())  # type: ignore[arg-type]

    with pytest.raises(ssc_main.FilterWheelRefusedError) as excinfo:
        await gateway.filterwheel_set_position(position=2)
    assert str(excinfo.value) == "wheel is busy"
    assert excinfo.value.device_code == 1025


# --------------------------------------------------------------------------- #
# AlpacaRuntime: a real loopback socket, on an ephemeral port
# --------------------------------------------------------------------------- #


async def test_alpaca_runtime_starts_listens_and_stops_cleanly(socket_enabled: None) -> None:
    del socket_enabled  # the suite runs with the network off by default
    runtime = AlpacaRuntime(port=0)
    await runtime.start()
    try:
        assert runtime.port != 0
        versions = await runtime.client.api_versions()
        assert versions == [1]
    finally:
        await runtime.aclose()


async def test_alpaca_runtime_refuses_to_start_twice_on_the_same_port(
    socket_enabled: None,
) -> None:
    """A bind failure is a loud, fatal error — never a silent fallback past the
    driver (CLAUDE.md invariant 7)."""
    del socket_enabled
    first = AlpacaRuntime(port=0)
    await first.start()
    try:
        second = AlpacaRuntime(port=first.port)
        with pytest.raises(AlpacaHostingError, match=str(first.port)):
            await second.start()
    finally:
        await first.aclose()


def _tasks() -> object:
    from ssc.api.tasks import TaskRegistry

    return TaskRegistry()

@pytest.mark.asyncio
async def test_mount_telemetry_reads_overlap_but_bound_driver_load(monkeypatch: pytest.MonkeyPatch) -> None:
    gateway = object.__new__(AlpacaDeviceGateway)
    gateway._mount_capabilities = None
    active = peak = 0
    members: set[str] = set()

    async def read(member: str) -> Any:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        members.add(member)
        await asyncio.sleep(0.001)
        active -= 1
        if member in {"tracking", "atpark", "athome", "slewing"}:
            return member == "tracking"
        return 0

    for name in ("_get_float", "_get_bool", "_get_int"):
        monkeypatch.setattr(gateway, name, read)
    started = datetime.now(UTC) - timedelta(seconds=7)
    sample = await gateway._read_mount_telemetry(started)
    assert peak == 3
    assert len(members) == 13
    assert sample.polled_at == started
    assert sample.tracking is True
    assert sample.slewing is False


@pytest.mark.asyncio
@pytest.mark.parametrize("recovers", [True, False])
async def test_mount_poll_rechecks_transient_missing_coordinates_before_publishing(recovers: bool) -> None:
    """One firmware property failure must not reset a healthy dither interval."""
    from collections import Counter

    gateway = object.__new__(AlpacaDeviceGateway)
    gateway._mount_capabilities = None
    calls: Counter[str] = Counter()

    async def read(member: str) -> Any:
        calls[member] += 1
        if member == "rightascension":
            return 10.0 if recovers and calls[member] > 1 else None
        if member == "declination":
            return 20.0
        if member in {"tracking", "atpark", "athome", "slewing"}:
            return member == "tracking"
        return 0

    gateway._get_float = read
    gateway._get_bool = read
    gateway._get_int = read
    started = datetime.now(UTC) - timedelta(seconds=2)
    sample = await gateway._read_mount_telemetry(started)
    assert calls["rightascension"] == 2
    # Re-read the complete position pair together, never fill from an old sample.
    assert calls["declination"] == 2
    assert sample.ra_hours == (10.0 if recovers else None)
    assert sample.dec_deg == 20.0
    assert sample.polled_at == started
    assert calls["slewing"] == 2
    assert calls["tracking"] == 2
    assert max(calls.values()) == 2


@pytest.mark.asyncio
async def test_transient_coordinate_read_does_not_restart_post_dither_stability() -> None:
    from collections import Counter

    from ssc.api.capture_orchestrator import CaptureService
    from ssc.state.events import MountChanged
    from ssc.state.models import StateSnapshot
    from ssc.state.store import StateStore

    at = datetime(2026, 10, 13, 20, 9, 43, tzinfo=UTC)
    gateway = object.__new__(AlpacaDeviceGateway)
    gateway._mount_capabilities = None
    calls: Counter[str] = Counter()

    async def read(member: str) -> Any:
        calls[member] += 1
        if member == "rightascension":
            # A single missing firmware field every few telemetry ticks,
            # followed immediately by a valid reading from the stopped mount.
            return None if calls[member] % 11 == 0 else 10.0
        if member == "declination":
            return 20.0
        if member in {"tracking", "atpark", "athome", "slewing"}:
            return member == "tracking"
        return 0

    gateway._get_float = read
    gateway._get_bool = read
    gateway._get_int = read
    state = StateStore(StateSnapshot(server_started_at=at))
    service = object.__new__(CaptureService)
    service._state = state
    elapsed = 0

    async def poll(_delay_s: float) -> None:
        nonlocal elapsed
        elapsed += 1
        sample = await gateway._read_mount_telemetry(at + timedelta(seconds=elapsed))
        # This fixture's driver coordinates are already J2000.
        sample = sample.model_copy(update={"ra_j2000_h": sample.ra_hours,
                                           "dec_j2000_deg": sample.dec_deg})
        state.apply(MountChanged(mount=sample))

    service._sleep = poll
    service._monotonic = lambda: float(elapsed)
    await service._wait_for_dither_settle(target_j2000=(10.0, 20.0), after_polled_at=at)
    assert elapsed == 31  # the full required 30-second stable interval, once
