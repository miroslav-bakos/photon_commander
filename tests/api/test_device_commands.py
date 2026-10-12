"""``ssc/api/device_commands.py``: the executor's ports over the real driver.

Every test drives the actual ``ssc-alpaca`` app (over ASGI, no socket) with the
Seestar emulator behind it, so what is asserted is the *mapping*: which ASCOM
member or ``ssc.*`` action each ``DeviceCommands`` method issues, what the
emulator therefore receives, and that a verb the driver refuses on firmware 8.46
comes back as the scheduler's own ``DeviceCommandNotImplementedError`` carrying
the driver's reason — never an ``ssc.client.errors`` type, never a silent no-op.

Also the J2000 <-> mount-frame boundary on the scheduler's own device port
(``ssc.api.mount_target``, wired in here 2026-09-10, the same conversion
``ssc.api.services.DeviceService`` applies to the Mount tab's slew/sync): a
scheduled slew/sync target arrives as J2000 and must reach the emulator in
whatever frame the seeded ``MountState.equatorial_system`` declares. Most
tests below seed a mount already declaring J2000, so the coordinates they
assert are unconverted by construction; the conversion itself is proven
separately, as a *direction*.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from ssc.alpaca.server import ServerConfig, create_app
from ssc.api.astro_service import AstroService
from ssc.api.device_commands import (
    AlpacaDeviceCommands,
    AlpacaDeviceEvents,
    ControlQueue,
    DeviceEventNamed,
    WallClock,
)
from ssc.astro.catalog import load_catalog
from ssc.astro.events import load_ephemeris
from ssc.astro.frames import (
    EquatorialCoord,
    MountFrame,
    frame_offset_arcsec,
    j2000_coord,
    mount_site,
    to_j2000,
    to_mount_frame,
)
from ssc.astro.units import Deg, Hours, hours_to_deg
from ssc.client.alpaca import AlpacaClient
from ssc.client.errors import AlpacaClientError
from ssc.scheduler.executor import (
    ControlCommand,
    ControlKind,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    DeviceEventTimeoutError,
)
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.state.events import DeviceChanged, DeviceEventReceived, MountChanged
from ssc.state.models import DeviceDescriptor, DeviceEventRecord, MountState, StateSnapshot
from ssc.state.store import StateStore
from tests.alpaca.conftest import make_backend, record_calls
from tests.api.test_plans import requires_ephemeris

#: A real site — Vienna, the same one ``tests/api/test_mount.py`` uses — so the
#: outbound-conversion test exercises the full skyfield apparent-place chain.
_SITE_LATITUDE_DEG = 48.2
_SITE_LONGITUDE_DEG = 16.4

#: Loaded once for the module: only the direction test needs it, but building
#: an ``AstroService`` for the default (J2000, no conversion) fixture needs a
#: catalogue object regardless of whether any test ever resolves a name.
_CATALOG = load_catalog()

#: The observed lifecycle with the silences collapsed and a goto that settles
#: quickly, so ``Slewing`` is seen true-then-false within a test.
FAST = EmulatorConfig(
    event_stream_silence_s=0.01,
    close_event_stream_after_push=False,
    event_idle_timeout_s=None,
    reset_refusal_s=0.05,
    goto_settle_s=0.2,
)


@pytest.fixture
async def emulator() -> AsyncIterator[SeestarEmulator]:
    device = SeestarEmulator(FAST)
    try:
        yield device
    finally:
        await device.aclose()


@pytest.fixture
async def client(emulator: SeestarEmulator) -> AsyncIterator[AlpacaClient]:
    backend = make_backend(emulator, cache_ttl_s=0.0)
    app = create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as http:
            # The backend is already addressed at the emulator; the executor's
            # adapter never supplies an address itself (architecture policy invariant 11).
            yield AlpacaClient(http=http)
    finally:
        await backend.aclose()


def _state() -> StateStore:
    return StateStore(StateSnapshot(server_started_at=datetime(2026, 1, 1, tzinfo=UTC)))


def _astro_service(ephemeris: object | None = None) -> AstroService:
    """No ephemeris by default: a mount already declaring J2000 never reaches
    it (``ssc.api.mount_target``), which is exactly what most tests below seed."""
    return AstroService(
        catalog=_CATALOG,
        profiles={},
        ephemeris=ephemeris,  # type: ignore[arg-type]
        ephemeris_error=None if ephemeris is not None else "no ephemeris kernel for this test",
    )


def _seed_mount_frame(
    state: StateStore,
    *,
    frame: MountFrame,
    latitude_deg: float | None = _SITE_LATITUDE_DEG,
    longitude_deg: float | None = _SITE_LONGITUDE_DEG,
) -> None:
    """Publish a ``MountChanged`` declaring the mount's frame, mirroring
    ``tests/api/test_mount.py``'s own helper for the same boundary."""
    state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                equatorial_system=frame,
                site_latitude_deg=latitude_deg,
                site_longitude_deg=longitude_deg,
            )
        )
    )


@pytest.fixture
def commands(client: AlpacaClient) -> AlpacaDeviceCommands:
    """A mount already declaring J2000: every test using this fixture asserts
    coordinates unconverted by construction, not because no conversion runs."""
    state = _state()
    _seed_mount_frame(state, frame=MountFrame.J2000)
    return AlpacaDeviceCommands(client, state=state, astro=_astro_service())


# -- verified paths: the emulator receives the mapped verb -------------------- #


async def test_startup_enters_star_view(
    emulator: SeestarEmulator, commands: AlpacaDeviceCommands
) -> None:
    calls = record_calls(emulator, "iscope_start_view")
    await commands.startup()
    assert calls == [{"mode": "star"}]


async def test_slew_to_is_slewtocoordinatesasync_over_start_auto_goto(
    emulator: SeestarEmulator, commands: AlpacaDeviceCommands
) -> None:
    emulator.set_arm(closed=False)
    calls = record_calls(emulator, "start_auto_goto")
    await commands.startup()
    await commands.slew_to(5.5, -5.4)
    assert len(calls) == 1
    assert calls[0]["ra"] == pytest.approx(5.5)
    assert calls[0]["dec"] == pytest.approx(-5.4)
    assert calls[0]["is_j2000"] is True


async def test_slew_without_star_view_is_a_device_error_not_a_not_implemented(
    emulator: SeestarEmulator, commands: AlpacaDeviceCommands
) -> None:
    """Code 210 means the verb exists and is unavailable in this state — the
    driver maps it to InvalidOperation, and so must the adapter."""
    emulator.set_arm(closed=False)
    with pytest.raises(DeviceCommandError) as caught:
        await commands.slew_to(5.5, -5.4)
    assert not isinstance(caught.value, DeviceCommandNotImplementedError)
    assert "code 210" in str(caught.value)


async def test_stop_stack_sends_the_verified_stop_verb(
    emulator: SeestarEmulator, commands: AlpacaDeviceCommands
) -> None:
    calls = record_calls(emulator, "stop_stack")
    await commands.stop_stack()
    assert calls == [{}]


# -- the J2000 <-> mount-frame boundary (ssc.api.mount_target) ----------------- #


async def test_slew_to_refuses_when_the_mount_frame_is_not_known_yet(
    emulator: SeestarEmulator, client: AlpacaClient
) -> None:
    """Nothing has polled the mount yet: refusing beats guessing it is J2000
    (CLAUDE.md invariant 7) — a scheduled slew fails the item rather than
    landing ~20' off (``ssc/astro/frames.py``'s module docstring)."""
    emulator.set_arm(closed=False)
    calls = record_calls(emulator, "start_auto_goto")
    commands = AlpacaDeviceCommands(
        client,
        state=_state(),
        astro=_astro_service(),
        mount_frame_ready_retries=0,
    )
    with pytest.raises(DeviceCommandError) as caught:
        await commands.slew_to(5.5, -5.4)
    assert not isinstance(caught.value, DeviceCommandNotImplementedError)
    assert "EquatorialSystem" in str(caught.value)
    assert calls == []


async def test_slew_waits_for_mount_frame_metadata_after_reconnect(
    emulator: SeestarEmulator, client: AlpacaClient
) -> None:
    emulator.set_arm(closed=False)
    calls = record_calls(emulator, "start_auto_goto")
    state = _state()
    state.apply(
        DeviceChanged(device=DeviceDescriptor(host="seestar", control_port=32323, reachable=True))
    )
    sleeps: list[float] = []

    async def publish_mount(seconds: float) -> None:
        sleeps.append(seconds)
        _seed_mount_frame(state, frame=MountFrame.J2000)

    commands = AlpacaDeviceCommands(
        client,
        state=state,
        astro=_astro_service(),
        mount_frame_ready_retries=2,
        mount_frame_ready_interval_s=0.25,
        sleep=publish_mount,
    )
    await commands.startup()

    await commands.slew_to(5.5, -5.4)

    assert sleeps == [0.25]
    assert len(calls) == 1
    assert calls[0]["ra"] == pytest.approx(5.5)
    assert calls[0]["dec"] == pytest.approx(-5.4)
    assert calls[0]["is_j2000"] is True


@requires_ephemeris
async def test_slew_to_converts_a_j2000_target_to_the_mounts_topocentric_frame(
    emulator: SeestarEmulator, client: AlpacaClient
) -> None:
    """The direction that matters most: a scheduled *sync* would write an
    unconverted target into the mount's pointing model, where it is durable
    (``ssc/astro/frames.py``'s module docstring). Asserted as a direction, not
    just a magnitude — inverting the conversion would double the error rather
    than cancel it, so this checks the emulator received the forward
    conversion's own answer, not its reverse (mirrors
    ``tests/api/test_mount.py::test_slew_converts_a_j2000_target_to_the_mounts_topocentric_frame``,
    the Mount tab's own copy of this same check).
    """
    emulator.set_arm(closed=False)
    calls = record_calls(emulator, "start_auto_goto")
    with load_ephemeris() as ephemeris:
        state = _state()
        _seed_mount_frame(state, frame=MountFrame.TOPOCENTRIC)
        commands = AlpacaDeviceCommands(client, state=state, astro=_astro_service(ephemeris))

        before = datetime.now(tz=UTC)
        await commands.startup()
        await commands.slew_to(5.5, -5.4)
        after = datetime.now(tz=UTC)

        assert len(calls) == 1
        sent_ra_hours = float(calls[0]["ra"])
        sent_dec_deg = float(calls[0]["dec"])

        observer = mount_site(Deg(_SITE_LATITUDE_DEG), Deg(_SITE_LONGITUDE_DEG))
        target = j2000_coord(hours_to_deg(Hours(5.5)), Deg(-5.4))
        midpoint = before + (after - before) / 2
        expected = to_mount_frame(ephemeris, observer, target, midpoint, MountFrame.TOPOCENTRIC)

        # Correct direction: within a fraction of an arcsec of wall-clock
        # slop, not the ~20' a skipped or backwards conversion would leave.
        assert abs(sent_ra_hours - float(expected.ra_h)) < 1e-4
        assert abs(sent_dec_deg - float(expected.dec_deg)) < 1e-4
        offset_arcsec = frame_offset_arcsec(target, expected)
        assert offset_arcsec > 60.0, (
            f"expected an arcminute-scale shift, got {offset_arcsec} arcsec"
        )

        # The inverse map applied to the same nominal input — what "the
        # conversion is backwards" would actually send — lands roughly twice
        # as far from the true answer, not on top of it: inverting doubles
        # the error rather than cancelling it, so this is what tells the two
        # apart.
        wrong = to_j2000(
            ephemeris,
            observer,
            EquatorialCoord(
                ra_deg=target.ra_deg, dec_deg=target.dec_deg, frame=MountFrame.TOPOCENTRIC
            ),
            midpoint,
        )
        wrong_offset_from_correct = frame_offset_arcsec(expected, wrong)
        assert wrong_offset_from_correct > 1.5 * offset_arcsec


@requires_ephemeris
async def test_sync_to_also_converts_to_the_mounts_topocentric_frame(
    monkeypatch: pytest.MonkeyPatch, client: AlpacaClient
) -> None:
    """A sync writes straight into the mount's pointing model, so this is the
    conversion that must never be skipped (module docstring). ``scope_sync``
    is not a verb this emulator implements at all — see the ``sync_to`` row of
    ``_REFUSED`` below, which is the real firmware's own behaviour — so a
    round trip through the device can only ever show the refusal, never the
    coordinates. What has to be checked instead is what reaches
    ``AlpacaClient.put``, the boundary the conversion crosses before the
    driver gets a say; ``_put`` is patched to capture its arguments and raise,
    standing in for the device's own refusal.
    """
    with load_ephemeris() as ephemeris:
        state = _state()
        _seed_mount_frame(state, frame=MountFrame.TOPOCENTRIC)
        commands = AlpacaDeviceCommands(client, state=state, astro=_astro_service(ephemeris))

        sent: dict[str, object] = {}

        async def fake_put(device: object, member: str, **params: object) -> None:
            del device, member
            sent.update(params)
            raise DeviceCommandNotImplementedError("stub: standing in for the device's refusal")

        monkeypatch.setattr(commands, "_put", fake_put)

        before = datetime.now(tz=UTC)
        with pytest.raises(DeviceCommandNotImplementedError):
            await commands.sync_to(5.5, -5.4)
        after = datetime.now(tz=UTC)

        observer = mount_site(Deg(_SITE_LATITUDE_DEG), Deg(_SITE_LONGITUDE_DEG))
        target = j2000_coord(hours_to_deg(Hours(5.5)), Deg(-5.4))
        midpoint = before + (after - before) / 2
        expected = to_mount_frame(ephemeris, observer, target, midpoint, MountFrame.TOPOCENTRIC)

        assert float(sent["RightAscension"]) == pytest.approx(float(expected.ra_h), abs=1e-4)
        assert float(sent["Declination"]) == pytest.approx(float(expected.dec_deg), abs=1e-4)
        offset_arcsec = frame_offset_arcsec(target, expected)
        assert offset_arcsec > 60.0, (
            f"expected an arcminute-scale shift, got {offset_arcsec} arcsec"
        )


# -- refused paths: NotImplemented with the driver's own reason ---------------- #

Verb = Callable[[AlpacaDeviceCommands], Coroutine[object, object, None]]

_REFUSED: list[tuple[str, Verb, str]] = [
    (
        "start_stack",
        lambda c: c.start_stack(target_name="M42", ra_j2000_h=5.5, dec_j2000_deg=-5.4),
        "unverified",
    ),
    ("autofocus", lambda c: c.autofocus(), "unverified"),
    ("set_filter", lambda c: c.set_filter("Dark"), "no verb that moves the filter wheel"),
    ("set_heater", lambda c: c.set_heater(True), "not an SSC action"),
    ("set_exposure", lambda c: c.set_exposure(10.0), "not an SSC action"),
    ("set_gain", lambda c: c.set_gain(80), "no verb that writes a device control"),
    ("set_dither", lambda c: c.set_dither(2.0), "not an SSC action"),
    ("solve", lambda c: c.solve(), "unverified"),
    ("sync_to", lambda c: c.sync_to(5.5, -5.4), "scope_sync returns 103"),
    ("sync_to_last_solve", lambda c: c.sync_to(None, None), "solve-result payload"),
    ("park", lambda c: c.park(), "scope_park"),
    ("unpark", lambda c: c.unpark(), "scope_park"),
    ("find_home", lambda c: c.find_home(), "no home position"),
    (
        "capture_calibration",
        lambda c: c.capture_calibration(frame_type="dark", count=20),
        "not an SSC action",
    ),
    ("shutdown", lambda c: c.shutdown(), "not an SSC action"),
]


@pytest.mark.parametrize(("name", "call", "reason"), _REFUSED, ids=[row[0] for row in _REFUSED])
async def test_an_unverified_verb_is_not_implemented_with_the_drivers_reason(
    commands: AlpacaDeviceCommands, name: str, call: Verb, reason: str
) -> None:
    del name
    with pytest.raises(DeviceCommandNotImplementedError) as caught:
        await call(commands)
    assert reason in str(caught.value), str(caught.value)
    # The reason is the driver's, in the scheduler's exception — no client type leaks.
    assert not isinstance(caught.value, AlpacaClientError)


async def test_scheduler_autofocus_uses_the_hosted_focus_curve_runner(
    commands: AlpacaDeviceCommands,
) -> None:
    calls = 0

    async def run_focus_curve() -> None:
        nonlocal calls
        calls += 1

    commands.set_autofocus_runner(run_focus_curve)
    await commands.autofocus()
    assert calls == 1


async def test_park_treats_known_below_horizon_refusal_as_success_when_already_home(
    monkeypatch: pytest.MonkeyPatch, client: AlpacaClient
) -> None:
    state = _state()
    state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                at_home=True,
                at_park=False,
                slewing=False,
                tracking=False,
                equatorial_system=MountFrame.J2000,
            )
        )
    )
    commands = AlpacaDeviceCommands(client, state=state, astro=_astro_service())

    async def refused_park(device: object, member: str, **params: object) -> None:
        del device, params
        assert member == "park"
        raise DeviceCommandError(
            "telescope.park: [0x4ff] SET_SCOPE_SET_TRACK_STATE fail: "
            "telescope was pointed below the horizon"
        )

    monkeypatch.setattr(commands, "_put", refused_park)

    reason = await commands.park()
    assert "safe Home" in reason and "physical Park was not confirmed" in reason
    assert state.snapshot.mount.at_park is False
    assert any(n.code == "park_safe_home" for n in state.snapshot.notices)


async def test_park_keeps_below_horizon_refusal_when_mount_is_not_safely_home(
    monkeypatch: pytest.MonkeyPatch, client: AlpacaClient
) -> None:
    state = _state()
    state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                at_home=False,
                at_park=False,
                slewing=False,
                tracking=False,
                equatorial_system=MountFrame.J2000,
            )
        )
    )
    commands = AlpacaDeviceCommands(
        client,
        state=state,
        astro=_astro_service(),
        park_state_ready_retries=1,
        park_state_ready_interval_s=0.0,
        sleep=lambda _seconds: asyncio.sleep(0),
    )

    async def refused_park(device: object, member: str, **params: object) -> None:
        del device, member, params
        raise DeviceCommandError(
            "telescope.park: [1279] SET_SCOPE_SET_TRACK_STATE fail: "
            "telescope was pointed below the horizon"
        )

    monkeypatch.setattr(commands, "_put", refused_park)

    with pytest.raises(DeviceCommandError, match="below the horizon"):
        await commands.park()


async def test_park_waits_for_delayed_safe_home_telemetry(
    monkeypatch: pytest.MonkeyPatch, client: AlpacaClient
) -> None:
    state = _state()
    state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=True,
                at_home=False,
                at_park=False,
                slewing=False,
                tracking=False,
                equatorial_system=MountFrame.J2000,
            )
        )
    )
    sleeps = 0

    async def refresh(_seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=datetime.now(tz=UTC),
                    stale=False,
                    at_home=True,
                    at_park=False,
                    slewing=False,
                    tracking=False,
                    equatorial_system=MountFrame.J2000,
                )
            )
        )

    commands = AlpacaDeviceCommands(
        client,
        state=state,
        astro=_astro_service(),
        park_state_ready_retries=2,
        park_state_ready_interval_s=0.0,
        sleep=refresh,
    )

    async def refused_park(device: object, member: str, **params: object) -> None:
        del device, member, params
        raise DeviceCommandError(
            "telescope.park: [0x4ff] SET_SCOPE_SET_TRACK_STATE fail: "
            "telescope was pointed below the horizon"
        )

    monkeypatch.setattr(commands, "_put", refused_park)

    await commands.park()
    assert sleeps == 1


async def test_set_filter_resolves_the_slot_name_before_the_refusal(
    emulator: SeestarEmulator, commands: AlpacaDeviceCommands
) -> None:
    """A slot the wheel does not have is *that* error, not a NotImplemented
    that would hide a typo in the plan."""
    del emulator
    with pytest.raises(DeviceCommandError) as caught:
        await commands.set_filter("Nebula-Boost-9000")
    assert not isinstance(caught.value, DeviceCommandNotImplementedError)
    assert "Nebula-Boost-9000" in str(caught.value)


# -- DeviceEvents ----------------------------------------------------------------- #


async def test_slew_complete_is_slewing_read_false(
    emulator: SeestarEmulator, client: AlpacaClient, commands: AlpacaDeviceCommands
) -> None:
    emulator.set_arm(closed=False)
    events = AlpacaDeviceEvents(client, _state(), poll_interval_s=0.05)
    await commands.startup()
    await commands.slew_to(5.5, -5.4)
    assert await client.device("telescope").get("slewing") is True
    await events.wait_for(
        "slew_complete", item_id="goto", deadline=datetime.now(tz=UTC) + timedelta(seconds=5)
    )
    assert await client.device("telescope").get("slewing") is False


async def test_a_missed_deadline_is_a_timeout_error_never_a_success(
    emulator: SeestarEmulator, client: AlpacaClient, commands: AlpacaDeviceCommands
) -> None:
    emulator.set_arm(closed=False)
    events = AlpacaDeviceEvents(client, _state(), poll_interval_s=0.05)
    await commands.startup()
    await commands.slew_to(5.5, -5.4)
    with pytest.raises(DeviceEventTimeoutError, match="slew_complete"):
        await events.wait_for("slew_complete", item_id="goto", deadline=datetime.now(tz=UTC))


async def test_startup_complete_is_satisfied_by_the_verbs_own_return(
    client: AlpacaClient,
) -> None:
    events = AlpacaDeviceEvents(client, _state())
    await events.wait_for("startup_complete", item_id="startup", deadline=datetime.now(tz=UTC))


@pytest.mark.parametrize(
    "kind", ["autofocus_complete", "solve_complete", "calibration_complete", "made_up"]
)
async def test_a_kind_with_no_known_signal_is_refused_not_guessed(
    client: AlpacaClient, kind: str
) -> None:
    events = AlpacaDeviceEvents(client, _state())
    with pytest.raises(DeviceCommandNotImplementedError, match=r"8\.46"):
        await events.wait_for(kind, item_id="x", deadline=datetime.now(tz=UTC) + timedelta(hours=1))


async def test_a_named_device_event_completes_through_the_state_store(
    client: AlpacaClient,
) -> None:
    """The push path, for a firmware that does push completions: the event
    arrives through ``ssc.state`` exactly as ``DeviceSink.on_device_event``
    delivers it."""
    state = _state()
    events = AlpacaDeviceEvents(
        client, state, signals={"stack_started": DeviceEventNamed(frozenset({"Stack"}))}
    )
    deadline = datetime.now(tz=UTC) + timedelta(seconds=5)
    waiter = asyncio.create_task(
        events.wait_for("stack_started", item_id="goto", deadline=deadline)
    )
    await asyncio.sleep(0.01)
    state.apply(
        DeviceEventReceived(
            event=DeviceEventRecord(name="PiStatus", received_at=datetime.now(tz=UTC))
        )
    )
    state.apply(
        DeviceEventReceived(event=DeviceEventRecord(name="Stack", received_at=datetime.now(tz=UTC)))
    )
    await asyncio.wait_for(waiter, timeout=2.0)
    assert state.subscriber_count == 0


# -- Clock and controls ------------------------------------------------------------- #


async def test_a_pushed_control_wakes_the_sleeping_clock() -> None:
    controls = ControlQueue()
    clock = WallClock(wake=controls.wait)
    assert await controls.poll() is None
    started = clock.now()
    sleeper = asyncio.create_task(clock.sleep_until(started + timedelta(seconds=30)))
    await asyncio.sleep(0.01)
    controls.push(ControlCommand(kind=ControlKind.PAUSE))
    await asyncio.wait_for(sleeper, timeout=2.0)
    assert (clock.now() - started).total_seconds() < 5.0
    assert await controls.poll() == ControlCommand(kind=ControlKind.PAUSE)
    assert await controls.poll() is None


async def test_a_push_between_poll_and_sleep_is_not_lost() -> None:
    controls = ControlQueue()
    clock = WallClock(wake=controls.wait)
    assert await controls.poll() is None
    controls.push(ControlCommand(kind=ControlKind.STOP))
    started = clock.now()
    await asyncio.wait_for(clock.sleep_until(started + timedelta(seconds=30)), timeout=2.0)
    assert (clock.now() - started).total_seconds() < 5.0


@pytest.mark.parametrize("operation", ["slew", "tracking"])
@pytest.mark.parametrize("parked", [True, False])
async def test_imaging_unparks_before_command_when_parked(
    commands: AlpacaDeviceCommands,
    emulator: SeestarEmulator,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    parked: bool,
) -> None:
    emulator.set_arm(closed=parked)
    state = commands._state
    mount = state.snapshot.mount
    assert mount is not None
    state.apply(MountChanged(mount=mount.model_copy(update={"at_park": parked})))
    calls: list[str] = []

    async def unpark() -> None:
        calls.append("unpark")

    async def put(_device: object, member: str, **_params: object) -> None:
        calls.append(member)

    monkeypatch.setattr(commands, "unpark", unpark)
    monkeypatch.setattr(commands, "_put", put)
    if operation == "slew":
        await commands.slew_to(5.5, -5.4)
        expected = "slewtocoordinatesasync"
    else:
        await commands.set_tracking_rate("sidereal")
        expected = "trackingrate"
    assert calls == (["unpark"] if parked else []) + [expected]


async def test_imaging_rechecks_cached_park_before_unparking(
    commands: AlpacaDeviceCommands, emulator: SeestarEmulator, monkeypatch: pytest.MonkeyPatch,
) -> None:
    emulator.set_arm(closed=False)
    mount = commands._state.snapshot.mount
    assert mount is not None
    commands._state.apply(MountChanged(mount=mount.model_copy(update={"at_park": True})))
    calls: list[str] = []
    async def unpark() -> None:
        calls.append("unpark")
    async def put(_device: object, member: str, **_params: object) -> None:
        calls.append(member)
    monkeypatch.setattr(commands, "unpark", unpark)
    monkeypatch.setattr(commands, "_put", put)
    await commands.set_tracking_rate("sidereal")
    assert calls == ["trackingrate"]


@pytest.mark.parametrize("unsafe", [None, "stale", "tracking", "slewing", "at_home"])
async def test_park_completion_accepts_only_fresh_stopped_home(
    client: AlpacaClient, emulator: SeestarEmulator, unsafe: str | None,
) -> None:
    from ssc.scheduler.executor import DeviceEventTimeoutError
    emulator.set_arm(closed=False)
    await client.device("telescope").connect()
    state = _state()
    _seed_mount_frame(state, frame=MountFrame.J2000)
    mount = state.snapshot.mount
    assert mount is not None
    flags = {"at_park": False, "at_home": True, "tracking": False, "slewing": False, "stale": False}
    if unsafe is not None:
        flags[unsafe] = unsafe != "at_home"
    state.apply(MountChanged(mount=mount.model_copy(update=flags)))
    now = datetime.now(UTC)
    events = AlpacaDeviceEvents(client, state, now=lambda: now)
    if unsafe is None:
        await events.wait_for("park_complete", item_id="darks", deadline=now)
        assert state.snapshot.mount.at_park is False
    else:
        with pytest.raises(DeviceEventTimeoutError):
            await events.wait_for("park_complete", item_id="darks", deadline=now)
