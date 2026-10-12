"""Fixtures for the API tests: a real app, a fake scope, an in-memory keyring.

Nothing here touches a socket or the developer's login keyring. The app is the
real one — real routers, real store, real migrations, real session cookies — with
only the device gateway substituted, which is the point of
:class:`~ssc.state.ports.DeviceGateway` existing.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime

import keyring
import keyring.backend
import keyring.errors
import pytest

with warnings.catch_warnings():
    # Starlette warns that httpx 0.x is deprecated in favour of httpx2. The
    # pinned httpx is a project-wide decision, and `filterwarnings = ["error"]`
    # would otherwise turn someone else's roadmap into a collection error here.
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient as StarletteTestClient

from ssc.api.deps import Services
from ssc.main import create_app
from ssc.settings.config import AppConfig
from ssc.state.models import (
    AlpacaDevicesState,
    AutofocusState,
    DeviceDescriptor,
    LinkState,
    LinkStatus,
    MountAxis,
    MountState,
    MountTrackingRate,
)
from ssc.state.ports import (
    DeviceGatewayError,
    DeviceSink,
    FilterWheelNotAvailableError,
    FilterWheelRefusedError,
    FocuserNotAvailableError,
    FocuserRefusedError,
    MountNotAvailableError,
    MountRefusedError,
    ProgressCallback,
)


class TestClient(StarletteTestClient):
    """Use a real configured service origin rather than an arbitrary Host."""
    def __init__(self, app, **kwargs):
        kwargs.setdefault("base_url", "http://127.0.0.1:7777")
        super().__init__(app, **kwargs)

    def websocket_connect(self, url, **kwargs):
        if url.startswith("/"):
            url = str(self.base_url).rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + url
        return super().websocket_connect(url, **kwargs)


CLIENT_ADDRESS = ("127.0.0.1", 45000)

__all__ = [
    "CLIENT_ADDRESS",
    "FakeGateway",
    "MemoryKeyring",
    "TestClient",
    "descriptor",
    "pairing_token",
    "services_of",
]


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A keyring that lives and dies with the test."""

    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        if (service, username) not in self._data:
            raise keyring.errors.PasswordDeleteError(username)
        del self._data[(service, username)]


@pytest.fixture(autouse=True)
def memory_keyring() -> Iterator[MemoryKeyring]:
    backend = MemoryKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        yield backend
    finally:
        keyring.set_keyring(previous)


class FakeGateway:
    """A scope that answers exactly what a test tells it to."""

    default_control_port = 4701

    def __init__(self) -> None:
        self.sinks: list[DeviceSink] = []
        self.reachable: dict[str, DeviceDescriptor] = {}
        self.discovered: list[DeviceDescriptor] = []
        self.discover_error: str | None = None
        self.progress_hosts: int = 0
        self.connected: tuple[str, int] | None = None
        #: When set, ``connect``/``disconnect`` emit no link event whatever —
        #: a device that accepts the socket and then never finishes its
        #: handshake. Nothing can be inferred from a callback that never fires.
        self.handshake_never_completes = False
        #: When set, ``connect`` raises — the shape a scope that has dropped
        #: off the network produces (``OSError: No route to host``). Distinct
        #: from ``handshake_never_completes``, which returns quietly: an
        #: exception escaping ``connect`` is what killed the auto-connect
        #: supervisor outright.
        self.connect_raises: Exception | None = None
        self.connect_errors: dict[str, Exception] = {}
        self.probe_calls: list[tuple[str, int]] = []
        #: Every ``connect()`` call, in order — distinct from ``connected``
        #: (the *current* address) so a test can tell a fresh reconnect attempt
        #: apart from one that never happened (the auto-connect supervisor).
        self.connect_calls: list[tuple[str, int]] = []
        self.mount_equ_mode: bool | None = None
        #: What ``mount_alignment_mode`` answers — the raw ASCOM
        #: ``AlignmentMode`` code. ``None`` (not connected/unknown) by
        #: default; a test sets ``1`` (polar) or ``0`` (alt-az) directly.
        self.mount_alignment_mode_value: int | None = None
        #: Every ``set_site`` call, in order — ``(latitude_deg, longitude_deg, elevation_m)``.
        self.site_calls: list[tuple[float, float, float | None]] = []
        #: Every mount control call, in order — ``(name, kwargs)``.
        self.mount_calls: list[tuple[str, dict[str, object]]] = []
        #: When set, every ``mount_*`` control raises this instead of doing
        #: anything — a test's way of exercising the refusal/not-available paths.
        self.mount_error: MountRefusedError | MountNotAvailableError | None = None
        #: What ``mount_set_tracking`` reads back and returns.
        self.mount_state: MountState = MountState()
        #: What ``focuser_position`` answers — ``(value, note)``, the same
        #: shape ``AlpacaDeviceGateway._focuser_get_int`` returns.
        self.focuser_position_value: int | None = None
        self.focuser_position_note: str | None = None
        self.focuser_is_moving_value: bool | None = None
        self.focuser_is_moving_note: str | None = None
        #: What ``focuser_step_size`` answers — same ``(value, note)`` shape,
        #: read independently of ``focuser_position`` (bead ssc-bkv.10).
        self.focuser_step_size_value: int | None = None
        self.focuser_step_size_note: str | None = None
        #: Every focuser control call, in order — ``(name, kwargs)``.
        self.focuser_calls: list[tuple[str, dict[str, object]]] = []
        #: When set, every focuser control call raises this instead of doing
        #: anything — a test's way of exercising the refusal/not-available
        #: paths, mirroring ``mount_error`` above.
        self.focuser_error: FocuserRefusedError | FocuserNotAvailableError | None = None
        self.autofocus_state = AutofocusState()
        self.filterwheel_names_value: list[str] | None = None
        self.filterwheel_names_note: str | None = None
        self.filterwheel_position_value: int | None = None
        self.filterwheel_position_note: str | None = None
        self.filterwheel_calls: list[tuple[str, dict[str, object]]] = []
        self.heater_state_value: bool | None = None
        self.heater_state_note: str | None = None
        self.heater_calls: list[bool] = []
        self.filterwheel_error: FilterWheelRefusedError | FilterWheelNotAvailableError | None = None

    # -- DeviceGateway ------------------------------------------------------ #

    def attach(self, sink: DeviceSink) -> Callable[[], None]:
        self.sinks.append(sink)
        return lambda: self.sinks.remove(sink)

    async def probe(
        self,
        host: str,
        *,
        control_port: int,
        timeout: float,  # noqa: ASYNC109 - matches the DeviceGateway protocol
    ) -> DeviceDescriptor:
        self.probe_calls.append((host, control_port))
        known = self.reachable.get(host)
        if known is not None:
            return known
        return DeviceDescriptor(
            host=host,
            control_port=control_port,
            reachable=False,
            note="TimeoutError: no response within 3.0s",
        )

    async def discover(
        self,
        *,
        timeout: float,  # noqa: ASYNC109 - matches the DeviceGateway protocol
        on_progress: ProgressCallback | None = None,
    ) -> list[DeviceDescriptor]:
        if self.discover_error is not None:
            raise DeviceGatewayError(self.discover_error)
        if on_progress is not None:
            for completed in range(1, self.progress_hosts + 1):
                on_progress(completed, self.progress_hosts)
        return list(self.discovered)

    async def connect(self, host: str, *, control_port: int) -> None:
        self.connect_calls.append((host, control_port))
        if host in self.connect_errors:
            raise self.connect_errors[host]
        if self.connect_raises is not None:
            raise self.connect_raises
        if self.handshake_never_completes:
            # No link event at all — the shape firmware 9.31 produces, where
            # the handshake stalls and the transport never brings a link up
            # or tears one down.
            return
        self.connected = (host, control_port)
        self.push_link(LinkStatus.READY, endpoint=f"{host}:{control_port}")
        live = self.reachable.get(host)
        if live is not None:
            self.push_device(live)

    async def disconnect(self) -> None:
        if self.handshake_never_completes:
            return  # nothing was ever up, so the real gateway emits nothing
        self.connected = None
        self.push_link(LinkStatus.DISCONNECTED)

    async def mount_alignment(self) -> bool | None:
        return self.mount_equ_mode

    async def mount_alignment_mode(self) -> int | None:
        return self.mount_alignment_mode_value

    async def set_site(
        self, *, latitude_deg: float, longitude_deg: float, elevation_m: float | None
    ) -> None:
        self.site_calls.append((latitude_deg, longitude_deg, elevation_m))

    # -- mount control -------------------------------------------------------- #

    def _record_mount_call(self, name: str, **kwargs: object) -> None:
        self.mount_calls.append((name, kwargs))
        if self.mount_error is not None:
            raise self.mount_error

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        self._record_mount_call("slew", ra_hours=ra_hours, dec_deg=dec_deg)

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None:
        self._record_mount_call("sync", ra_hours=ra_hours, dec_deg=dec_deg)

    async def mount_move_axis(self, *, axis: MountAxis, rate_deg_s: float) -> None:
        self._record_mount_call("move_axis", axis=axis, rate_deg_s=rate_deg_s)

    async def mount_stop(self) -> None:
        self._record_mount_call("stop")

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        self._record_mount_call("set_tracking", enabled=enabled, rate=rate)
        self.mount_state = self.mount_state.model_copy(
            update={
                "tracking": enabled,
                "tracking_rate": rate or self.mount_state.tracking_rate,
                "polled_at": datetime.now(tz=UTC),
            }
        )
        self.push_mount(self.mount_state)
        return self.mount_state

    async def mount_park(self) -> None:
        self._record_mount_call("park")

    async def mount_unpark(self) -> None:
        self._record_mount_call("unpark")

    async def mount_find_home(self) -> None:
        self._record_mount_call("find_home")

    # -- focuser control ------------------------------------------------------ #

    async def focuser_position(self) -> tuple[int | None, str | None]:
        return self.focuser_position_value, self.focuser_position_note

    async def focuser_is_moving(self) -> tuple[bool | None, str | None]:
        return self.focuser_is_moving_value, self.focuser_is_moving_note

    async def focuser_step_size(self) -> tuple[int | None, str | None]:
        return self.focuser_step_size_value, self.focuser_step_size_note

    async def heater_get_state(self) -> tuple[bool | None, str | None]:
        return self.heater_state_value, self.heater_state_note

    async def heater_set_state(self, *, enabled: bool) -> None:
        self.heater_calls.append(enabled)

    def _record_focuser_call(self, name: str, **kwargs: object) -> None:
        self.focuser_calls.append((name, kwargs))
        if self.focuser_error is not None:
            raise self.focuser_error

    async def focuser_move(self, *, position: int) -> None:
        self._record_focuser_call("move", position=position)

    async def focuser_halt(self) -> None:
        self._record_focuser_call("halt")

    async def focuser_start_autofocus(
        self, *, step: int, fine_step: int, exposure_s: float, settle_s: float
    ) -> None:
        self._record_focuser_call(
            "start_autofocus",
            step=step,
            fine_step=fine_step,
            exposure_s=exposure_s,
            settle_s=settle_s,
        )

    async def focuser_autofocus_state(self) -> AutofocusState:
        return self.autofocus_state

    # -- filter-wheel control ------------------------------------------------ #

    async def filterwheel_get_names(self) -> tuple[list[str] | None, str | None]:
        return self.filterwheel_names_value, self.filterwheel_names_note

    async def filterwheel_get_position(self) -> tuple[int | None, str | None]:
        return self.filterwheel_position_value, self.filterwheel_position_note

    async def filterwheel_set_position(self, *, position: int) -> None:
        self.filterwheel_calls.append(("set_position", {"position": position}))
        if self.filterwheel_error is not None:
            raise self.filterwheel_error

    # -- test helpers ------------------------------------------------------- #

    def push_link(self, status: LinkStatus, *, endpoint: str | None = None) -> None:
        link = LinkState(
            status=status,
            endpoint=endpoint,
            control_connected=status is LinkStatus.READY,
            changed_at=datetime.now(tz=UTC),
        )
        for sink in self.sinks:
            sink.on_link(link)

    def push_device(self, device: DeviceDescriptor | None) -> None:
        for sink in self.sinks:
            sink.on_device(device)

    def push_mount(self, mount: MountState | None) -> None:
        for sink in self.sinks:
            sink.on_mount(mount)

    def push_devices(self, devices: AlpacaDevicesState | None) -> None:
        for sink in self.sinks:
            sink.on_devices(devices)


def descriptor(host: str, **overrides: object) -> DeviceDescriptor:
    fields: dict[str, object] = {
        "host": host,
        "control_port": FakeGateway.default_control_port,
        "reachable": True,
        "firmware": "8.46",
        "firmware_verified": True,
        "model": "Seestar S30 Pro",
        "serial": "TESTSERIAL",
        "capabilities": {"can_wide_cam_af": True},
    }
    fields.update(overrides)
    return DeviceDescriptor.model_validate(fields)


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def config(tmp_path: object, request: pytest.FixtureRequest) -> AppConfig:
    """``auth_required`` defaults to ``None`` (loginless — CLAUDE.md
    2026-09-09). A test that needs the other side of the toggle parametrizes
    this fixture directly, e.g.
    ``@pytest.mark.parametrize("config", [True], indirect=True)``, which lands
    here as ``request.param``.
    """
    auth_required = getattr(request, "param", None)
    return AppConfig(
        host="127.0.0.1",
        data_dir=tmp_path,  # type: ignore[arg-type]
        mdns_enabled=False,
        event_buffer_size=16,
        # Small enough that a test waiting on the auto-connect supervisor's
        # backoff does not itself take real minutes — see tests/api/test_device.py.
        reconnect_initial_delay_s=0.02,
        reconnect_max_delay_s=0.05,
        auth_required=auth_required,
    )


@pytest.fixture
def client(config: AppConfig, gateway: FakeGateway) -> Iterator[TestClient]:
    app = create_app(config, gateway=gateway)
    with TestClient(app, base_url=config.origin, client=CLIENT_ADDRESS) as test_client:
        # Existing API tests deliberately exercise open LAN access. Runtime
        # defaults and upgrade pairing are covered by dedicated tests.
        if config.auth_required is None:
            from ssc.api.routes.settings import _publish
            services = services_of(test_client)
            test_client.portal.call(services.settings.set_auth_required, False)
            _publish(services)
        yield test_client


def services_of(client: TestClient) -> Services:
    services = client.app.state.services  # type: ignore[union-attr]
    assert isinstance(services, Services)
    return services


def pairing_token(client: TestClient) -> str:
    response = client.get("/api/auth/pairing")
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


@pytest.fixture
def paired(client: TestClient) -> TestClient:
    """A client holding a control-scoped session cookie."""
    response = client.post("/api/auth/pair", json={"token": pairing_token(client)})
    assert response.status_code == 200, response.text
    assert response.json()["scope"] == "control"
    return client
