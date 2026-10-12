# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.api.polar_alignment_service``: the measurement-run orchestration
around ``ssc.astro.polar`` (untouched here).

No live driver, no real ASTAP: the camera/telescope are the same structural
fakes ``tests/api/test_capture_service.py`` drives ``drive_exposure`` with,
and the plate solver is a scripted callable — this module tests the
orchestration (slew/settle/capture/solve/retry/publish/cancel), not
``ssc.astro.polar``'s maths (``tests/astro/test_polar.py``'s job) or a real
ASTAP binary (``tests/platesolve/``'s job).

Every solved position shares one ``date_obs`` (module-level ``WHEN``) rather
than a real, advancing wall clock: ``ssc.astro.polar`` projects each point
into the local horizontal frame at its own instant, so if every point is
timestamped identically, an RA-only rotation *is* a rotation about the true
pole, and the fit converges to a small, well-conditioned error — enough to
prove the pipeline reaches a real, published :class:`PolarAlignmentResult`,
which is all this file needs. The declination (+5) and the RA span (target
transit at ``WHEN``, +-60 degrees) are chosen so every point stays well above
the horizon at the site's latitude (48.15) — see the module docstring's own
rise/set arithmetic for why a wider spread would need a shallower dec.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from ssc.api.capture_service import ExposureNotAvailableError, ExposureTimeoutError
from ssc.api.polar_alignment_service import (
    _DEFAULT_POST_SLEW_SETTLE_S,
    _DEFAULT_SETTLE_POLL_INTERVAL_S,
    PolarAlignmentAlreadyRunningError,
    PolarAlignmentDeviceUnavailableError,
    PolarAlignmentNoMeasurementError,
    PolarAlignmentNotPolarModeError,
    PolarAlignmentNotRunningError,
    PolarAlignmentService,
    PolarAlignmentSolverUnavailableError,
    PolarAlignmentStartAmbiguousError,
    PolarRefinementNoSweepError,
    PolarRefinementNotRunningError,
    PolarRefinementTrackingError,
    _assess_sweep,
    _build_targets,
)
from ssc.api.tasks import TaskRegistry
from ssc.astro.events import Ephemeris, EphemerisMissingError, Site, load_ephemeris
from ssc.astro.frames import MountFrame, j2000_coord
from ssc.astro.polar import DegeneracyReason, best_conditioned_field, j2000_coord_at_hour_angle
from ssc.astro.units import Deg, Hours, Metres, hours_to_deg, wrap_hours
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError
from ssc.client.imagebytes import ImageArrayElementType, ImageBytesFrame
from ssc.platesolve import (
    PlateSolveResult,
    SolveFailedError,
    SolveIssue,
    SolverAvailability,
    SolveTimedOutError,
)
from ssc.state.events import MountChanged, PolarAlignmentChanged
from ssc.state.models import (
    MountState,
    MountTrackingRate,
    PolarAlignmentPointState,
    PolarAlignmentPointStatus,
    PolarAlignmentRunKind,
    PolarAlignmentStartMode,
    PolarAlignmentState,
    PolarAlignmentStatus,
    PolarRefinementStatus,
    StateSnapshot,
)
from ssc.state.ports import MountRefusedError
from ssc.state.store import StateStore

SITE = Site(latitude_deg=Deg(48.15), longitude_deg=Deg(17.11), elevation_m=Metres(0.0))
WHEN = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
DEC_DEG = 5.0
STEP_DEG = 30.0


@pytest.fixture(scope="module")
def ephemeris() -> Ephemeris:
    """The pinned de440s kernel — absent means skipped, never a silent
    download (mirrors ``tests/astro/conftest.py``'s own fixture; duplicated
    rather than imported across test packages)."""
    try:
        loaded = load_ephemeris()
    except EphemerisMissingError as exc:
        pytest.skip(str(exc))
    with loaded as handle:
        yield handle


def _lst_hours(ephemeris: Ephemeris, when: datetime) -> float:
    time: Any = ephemeris.timescale.from_datetime(when)
    return float(wrap_hours(float(time.gast) + float(SITE.longitude_deg) / 15.0))


async def instant_sleep(_seconds: float) -> None:
    """No real delay — every test drives its own clock/state transitions
    directly, so nothing here needs to wait on wall time."""


def fixed_now(when: datetime = WHEN) -> Callable[[], datetime]:
    counter = {"n": 0}

    def _now() -> datetime:
        counter["n"] += 1
        # Advances a little on every call so ``started_at``/``ended_at`` are
        # never identical, without ever approaching a settle/slew timeout.
        return when + timedelta(milliseconds=counter["n"])

    return _now


class FakeDevice:
    """Enough of ``ssc.api.capture_service.Device`` to satisfy
    ``drive_exposure`` — mirrors ``tests/api/test_capture_service.py``'s own
    fake, minus anything this module never reads."""

    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.get_calls: list[str] = []

    async def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def get(self, member: str, **params: object) -> object:
        del params
        self.get_calls.append(member)
        raise AlpacaRemoteError(
            int(AlpacaErrorNumber.NOT_IMPLEMENTED), "not implemented", member=f"Fake.{member}"
        )


class FakeCamera(FakeDevice):
    def __init__(self) -> None:
        super().__init__()
        self.start_calls = 0
        self.durations: list[float] = []
        self.put_calls: list[tuple[str, dict[str, object]]] = []

    async def put(self, member: str, **params: object) -> object:
        self.put_calls.append((member, params))
        return None

    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
        del light
        self.start_calls += 1
        self.durations.append(duration_s)

    async def image_ready(self) -> bool:
        return True

    async def image_array(self) -> ImageBytesFrame:
        array = np.arange(64, dtype=np.uint16).reshape(8, 8)
        return ImageBytesFrame(
            metadata_version=1,
            client_transaction_id=1,
            server_transaction_id=1,
            image_element_type=ImageArrayElementType.UINT16,
            transmission_element_type=ImageArrayElementType.UINT16,
            array=array,
        )


class FakeMountControl:
    """``ssc.api.polar_alignment_service.MountControl`` — records every
    call and, on a successful slew, publishes a settled ``MountState``
    directly (no separate poll loop to simulate: the settle loop only ever
    reads ``state.snapshot.mount``)."""

    def __init__(self, state: StateStore, *, alignment_mode: int | None = 1) -> None:
        self._state = state
        self.alignment_mode = alignment_mode
        self.slew_calls: list[tuple[float, float]] = []
        self.actions: list[str] = []
        self.stop_calls = 0
        self.polar_declination_calls: list[float | None] = []
        #: Point index -> number of remaining ``MountRefusedError``s before a
        #: slew to that target succeeds. Consumed one at a time.
        self.refuse_next: dict[int, int] = {}
        #: Point index -> accepted slew commands that do not move the mount.
        self.miss_next: dict[int, int] = {}
        self._targets: list[tuple[float, float]] = []
        #: What the mount reports back as tracking, updated by
        #: ``mount_set_tracking`` — starts ``True`` since a sweep leaves the
        #: mount as it found it (never touches tracking itself).
        self.tracking: bool | None = True
        self.tracking_calls = 0
        #: When set, ``mount_set_tracking(enabled=False, ...)`` still reports
        #: the drive as tracking — a mount that refuses to actually stop.
        self.refuse_tracking_stop = False
        self.refuse_tracking_start_count = 0

    def expect_targets(self, targets: list[tuple[float, float]]) -> None:
        """So the fake can tell *which* point index a given ``(ra, dec)``
        slew belongs to, without the service having to say so."""
        self._targets = targets

    async def mount_alignment_mode(self) -> int | None:
        return self.alignment_mode

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        self.actions.append("slew")
        self.slew_calls.append((ra_hours, dec_deg))
        index = self._targets.index((ra_hours, dec_deg))
        remaining = self.refuse_next.get(index, 0)
        if remaining > 0:
            self.refuse_next[index] = remaining - 1
            raise MountRefusedError("mount is busy (simulated)")
        missed = self.miss_next.get(index, 0)
        if missed > 0:
            self.miss_next[index] = missed - 1
            return
        self._state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=WHEN,
                    ra_hours=ra_hours,
                    dec_deg=dec_deg,
                    slewing=False,
                    tracking=self.tracking,
                    tracking_rate="sidereal",
                    equatorial_system=MountFrame.J2000,
                    site_latitude_deg=float(SITE.latitude_deg),
                    site_longitude_deg=float(SITE.longitude_deg),
                    stale=False,
                )
            )
        )

    async def mount_slew_polar(
        self, *, ra_hours: float, dec_deg: float, fixed_mount_dec_deg: float | None
    ) -> float:
        self.polar_declination_calls.append(fixed_mount_dec_deg)
        await self.mount_slew(ra_hours=ra_hours, dec_deg=dec_deg)
        return dec_deg if fixed_mount_dec_deg is None else fixed_mount_dec_deg

    async def mount_stop(self) -> None:
        self.stop_calls += 1

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        del rate
        self.actions.append(f"tracking:{enabled}")
        self.tracking_calls += 1
        if enabled and self.refuse_tracking_start_count > 0:
            self.refuse_tracking_start_count -= 1
            raise MountRefusedError("tracking transition busy (simulated)")
        if not (enabled is False and self.refuse_tracking_stop):
            self.tracking = enabled
        mount = self._state.snapshot.mount
        assert mount is not None
        updated = mount.model_copy(update={"tracking": self.tracking})
        self._state.apply(MountChanged(mount=updated))
        return updated


class FakeFilterWheel(FakeDevice):
    def __init__(self, names: list[str]) -> None:
        super().__init__()
        self.names = names
        self.put_calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, member: str, **params: object) -> object:
        del params
        if member == "names":
            return self.names
        return await super().get(member)

    async def put(self, member: str, **params: object) -> object:
        self.put_calls.append((member, params))
        return None


class ScriptedSolver:
    """One outcome per call, in order — a raised :class:`SolveFailedError`
    or a canned :class:`PlateSolveResult` for a known target."""

    def __init__(self) -> None:
        self._script: list[Callable[[], PlateSolveResult]] = []
        self.calls = 0

    def expect_success(self, ra_h: float, dec_deg: float) -> None:
        def _make() -> PlateSolveResult:
            return PlateSolveResult(
                coord=j2000_coord(hours_to_deg(Hours(ra_h)), Deg(dec_deg)),
                date_obs=WHEN,
                pixel_scale_arcsec=2.4,
                rotation_deg=0.0,
                solver="fake",
                solver_version="1.0",
            )

        self._script.append(_make)

    def expect_failure(self, message: str = "not enough stars") -> None:
        def _raise() -> PlateSolveResult:
            raise SolveFailedError(SolveIssue.NOT_SOLVED, message)

        self._script.append(_raise)

    def expect(self, result: PlateSolveResult) -> None:
        """A pre-built result — the refinement tests' own way of controlling
        ``date_obs``/coordinates precisely instead of ``expect_success``'s
        fixed ``WHEN``."""
        self._script.append(lambda: result)

    async def __call__(self, fits_path: Path) -> PlateSolveResult:
        assert await asyncio.to_thread(fits_path.is_file)
        self.calls += 1
        action = self._script.pop(0)
        return action()


async def _available() -> SolverAvailability:
    return SolverAvailability(available=True, version="fake-1.0")


async def _unavailable() -> SolverAvailability:
    return SolverAvailability(
        available=False, issue=SolveIssue.SOLVER_NOT_FOUND, reason="ASTAP not installed"
    )


def _build_service(
    *,
    ephemeris: Ephemeris,
    mount: FakeMountControl,
    solver: Callable[[Path], Awaitable[PlateSolveResult]],
    camera: FakeCamera | None = None,
    telescope: FakeDevice | None = None,
    filterwheel: FakeDevice | None = None,
    section: PolarAlignmentState | None = None,
    now: Callable[[], datetime] | None = None,
    sleep: Callable[[float], Any] = instant_sleep,
    post_slew_settle_s: float = 0.0,
    settle_poll_interval_s: float = 0.0,
    preflight_timeout_s: float = 30.0,
    preflight_poll_interval_s: float = 0.0,
    interactive_solve_timeout_s: float = 30.0,
    refresh_mount_polls: bool = True,
    settle_timeout_s: float = 5.0,
) -> tuple[PolarAlignmentService, StateStore]:
    state = mount._state
    clock_now = now if now is not None else fixed_now()

    async def poll_sleep(seconds: float) -> None:
        await sleep(seconds)
        if refresh_mount_polls and state.snapshot.mount is not None:
            state.apply(
                MountChanged(
                    mount=state.snapshot.mount.model_copy(update={"polled_at": clock_now()})
                )
            )

    service = PolarAlignmentService(
        camera=camera if camera is not None else FakeCamera(),
        telescope=telescope if telescope is not None else FakeDevice(),
        filterwheel=filterwheel,
        device=mount,
        state=state,
        astro=_FakeAstro(ephemeris),
        tasks=TaskRegistry(),
        section=section if section is not None else PolarAlignmentState(),
        solve=solver,
        availability=_available,
        now=clock_now,
        sleep=poll_sleep,
        max_slew_attempts=3,
        slew_retry_delay_s=0.0,
        preflight_timeout_s=preflight_timeout_s,
        preflight_poll_interval_s=preflight_poll_interval_s,
        settle_timeout_s=settle_timeout_s,
        settle_poll_interval_s=settle_poll_interval_s,
        post_slew_settle_s=post_slew_settle_s,
        max_solve_attempts=3,
        solve_retry_delay_s=0.0,
        interactive_solve_timeout_s=interactive_solve_timeout_s,
    )
    return service, state


class _FakeAstro:
    def __init__(self, ephemeris: Ephemeris | None, error: str | None = None) -> None:
        self.ephemeris = ephemeris
        self.ephemeris_error = error


def _initial_mount_state(*, ra_h: float, dec_deg: float) -> MountState:
    return MountState(
        polled_at=WHEN,
        ra_hours=ra_h,
        dec_deg=dec_deg,
        slewing=False,
        equatorial_system=MountFrame.J2000,
        ra_j2000_h=ra_h,
        dec_j2000_deg=dec_deg,
        site_latitude_deg=float(SITE.latitude_deg),
        site_longitude_deg=float(SITE.longitude_deg),
        stale=False,
    )


def _state_with_mount(mount: MountState) -> StateStore:
    return StateStore(StateSnapshot(server_started_at=WHEN, mount=mount))


def _targets_for(
    ephemeris: Ephemeris, *, count: int
) -> tuple[float, float, list[tuple[float, float]]]:
    lst = _lst_hours(ephemeris, WHEN)
    start_ra_h = float(wrap_hours(lst - (STEP_DEG / 15.0) * (count // 2)))
    return start_ra_h, DEC_DEG, _build_targets(start_ra_h, DEC_DEG, count, STEP_DEG)


# --- refusals, before anything moves ---------------------------------------- #


async def test_alt_az_mode_refuses_before_any_slew(ephemeris: Ephemeris) -> None:
    mount_state = _initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)
    state = _state_with_mount(mount_state)
    mount = FakeMountControl(state, alignment_mode=0)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())

    with pytest.raises(PolarAlignmentNotPolarModeError):
        await service.start(point_count=5, step_deg=STEP_DEG, exposure_s=1.0)

    assert mount.slew_calls == []
    assert service.section.status is None


async def test_no_camera_refuses(ephemeris: Ephemeris) -> None:
    mount_state = _initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)
    state = _state_with_mount(mount_state)
    mount = FakeMountControl(state)
    service = PolarAlignmentService(
        camera=None,
        telescope=FakeDevice(),
        filterwheel=None,
        device=mount,
        state=state,
        astro=_FakeAstro(ephemeris),
        tasks=TaskRegistry(),
        section=PolarAlignmentState(),
        solve=ScriptedSolver(),
        availability=_available,
    )
    with pytest.raises(PolarAlignmentDeviceUnavailableError):
        await service.start(point_count=5, step_deg=STEP_DEG, exposure_s=1.0)


async def test_already_running_refuses(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=ScriptedSolver(),
        section=PolarAlignmentState(status=PolarAlignmentStatus.RUNNING),
    )
    with pytest.raises(PolarAlignmentAlreadyRunningError):
        await service.start(point_count=5, step_deg=STEP_DEG, exposure_s=1.0)


async def test_cancel_without_a_run_refuses(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())
    with pytest.raises(PolarAlignmentNotRunningError):
        await service.cancel()


# --- the full run, and its progress ------------------------------------------ #


async def test_settle_allows_mechanical_damping_after_stable_mount(ephemeris: Ephemeris) -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=ScriptedSolver(),
        section=PolarAlignmentState(
            points=[
                PolarAlignmentPointState(
                    index=0,
                    status=PolarAlignmentPointStatus.SLEWING,
                    target_ra_j2000_h=10.0,
                    target_dec_j2000_deg=DEC_DEG,
                )
            ]
        ),
        sleep=record_sleep,
        post_slew_settle_s=_DEFAULT_POST_SLEW_SETTLE_S,
        settle_poll_interval_s=_DEFAULT_SETTLE_POLL_INTERVAL_S,
    )

    await service._settle(0)

    assert sleeps == [_DEFAULT_SETTLE_POLL_INTERVAL_S, _DEFAULT_POST_SLEW_SETTLE_S]


async def test_settle_does_not_accept_the_previous_point_as_the_new_target(
    ephemeris: Ephemeris,
) -> None:
    target_ra = 12.0
    state = _state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG))
    mount = FakeMountControl(state)
    sleeps = 0
    statuses_during_poll: list[PolarAlignmentPointStatus] = []

    async def arrive_after_first_poll(_seconds: float) -> None:
        nonlocal sleeps
        statuses_during_poll.append(service.section.points[0].status)
        sleeps += 1
        if sleeps == 1:
            state.apply(
                MountChanged(
                    mount=_initial_mount_state(ra_h=target_ra, dec_deg=DEC_DEG)
                )
            )

    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=ScriptedSolver(),
        section=PolarAlignmentState(
            points=[
                PolarAlignmentPointState(
                    index=0,
                    status=PolarAlignmentPointStatus.SLEWING,
                    target_ra_j2000_h=target_ra,
                    target_dec_j2000_deg=DEC_DEG,
                )
            ]
        ),
        sleep=arrive_after_first_poll,
    )

    await service._settle(0)

    assert sleeps == 2
    assert statuses_during_poll == [
        PolarAlignmentPointStatus.SLEWING,
        PolarAlignmentPointStatus.SETTLING,
    ]
    assert service.section.points[0].status is PolarAlignmentPointStatus.SETTLING


async def test_settle_requires_distinct_polls_and_restarts_after_motion(
    ephemeris: Ephemeris,
) -> None:
    state = _state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG))
    mount = FakeMountControl(state)
    polls = 0

    async def telemetry_tick(_seconds: float) -> None:
        nonlocal polls
        polls += 1
        # The first two wakeups see exactly the same cached report. The next
        # report reveals movement; only two subsequent stable reports suffice.
        if polls >= 3:
            ra = 10.01 if polls == 3 else 10.0
            state.apply(
                MountChanged(
                    mount=_initial_mount_state(ra_h=ra, dec_deg=DEC_DEG).model_copy(
                        update={"polled_at": WHEN + timedelta(milliseconds=polls)}
                    )
                )
            )

    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=ScriptedSolver(),
        section=PolarAlignmentState(
            points=[
                PolarAlignmentPointState(
                    index=0,
                    status=PolarAlignmentPointStatus.SLEWING,
                    target_ra_j2000_h=10.0,
                    target_dec_j2000_deg=DEC_DEG,
                )
            ]
        ),
        sleep=telemetry_tick,
        refresh_mount_polls=False,
    )
    await service._settle(0)
    assert polls == 5


async def test_accepted_slew_that_stops_off_target_is_retried(
    ephemeris: Ephemeris,
) -> None:
    target_ra = 12.0
    state = _state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG))
    mount = FakeMountControl(state)
    mount.expect_targets([(target_ra, DEC_DEG)])
    mount.miss_next[0] = 1
    clock = WHEN
    observed_statuses: list[PolarAlignmentPointStatus] = []

    def advancing_now() -> datetime:
        nonlocal clock
        result = clock
        clock += timedelta(seconds=3)
        return result

    async def observe_sleep(_seconds: float) -> None:
        observed_statuses.append(service.section.points[0].status)

    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=ScriptedSolver(),
        section=PolarAlignmentState(
            points=[
                PolarAlignmentPointState(
                    index=0,
                    status=PolarAlignmentPointStatus.SLEWING,
                    target_ra_j2000_h=target_ra,
                    target_dec_j2000_deg=DEC_DEG,
                )
            ]
        ),
        now=advancing_now,
        sleep=observe_sleep,
        settle_timeout_s=20.0,
    )

    attempts = await service._slew_with_retry(0, target_ra, DEC_DEG)

    assert attempts == 2
    assert mount.slew_calls == [(target_ra, DEC_DEG), (target_ra, DEC_DEG)]
    assert PolarAlignmentPointStatus.SETTLING in observed_statuses
    point = service.section.points[0]
    assert point.status is PolarAlignmentPointStatus.SETTLING
    assert point.error is None


async def test_tracking_transition_retries_only_when_not_already_tracking(
    ephemeris: Ephemeris,
) -> None:
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG))
    )
    service, _ = _build_service(
        ephemeris=ephemeris, mount=mount, solver=ScriptedSolver()
    )
    current = mount._state.snapshot.mount
    assert current is not None
    mount._state.apply(MountChanged(mount=current.model_copy(update={"tracking": True})))
    await service._ensure_tracking_after_slew()
    assert mount.tracking_calls == 0

    mount.tracking = False
    current = mount._state.snapshot.mount
    assert current is not None
    mount._state.apply(MountChanged(mount=current.model_copy(update={"tracking": False})))
    mount.refuse_tracking_start_count = 2

    await service._ensure_tracking_after_slew()

    assert mount.tracking_calls == 3
    assert mount.tracking is True


async def test_full_run_completes_and_publishes_progress(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=5)
    mount_state = _initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg)
    mount = FakeMountControl(_state_with_mount(mount_state))
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, state = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    section = await service.start(
        point_count=5,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    assert section.status is PolarAlignmentStatus.RUNNING
    await service._task  # wait for the background run to finish

    final = service.section
    assert final.status is PolarAlignmentStatus.COMPLETED
    assert final.result is not None
    assert final.result.point_count == 5
    assert final.result.has_residual_check is True
    assert len(final.history) == 1
    assert final.history[0].run_kind is PolarAlignmentRunKind.MEASURE
    assert final.history[0].completed_at == final.ended_at
    assert final.history[0].altitude_error_deg == final.result.altitude_error_deg
    assert final.history[0].azimuth_error_deg == final.result.azimuth_error_deg
    assert final.history[0].total_error_deg == final.result.total_error_deg
    assert all(p.status is PolarAlignmentPointStatus.SOLVED for p in final.points)
    assert mount.slew_calls == targets
    assert solver.calls == 5
    assert mount.tracking_calls == 0

    # Progress was published incrementally, not only at the end — a client
    # connected mid-run would have seen every one of these.
    published = [
        event.event.polar_alignment
        for event in state.since(0).events  # type: ignore[union-attr]
        if isinstance(event.event, PolarAlignmentChanged)
    ]
    assert len(published) > len(final.points)  # more than one event per point
    statuses_seen = {point.status for section in published for point in section.points}
    assert PolarAlignmentPointStatus.SLEWING in statuses_seen
    assert PolarAlignmentPointStatus.SETTLING in statuses_seen
    assert PolarAlignmentPointStatus.CAPTURING in statuses_seen
    assert PolarAlignmentPointStatus.SOLVING in statuses_seen
    assert PolarAlignmentPointStatus.SOLVED in statuses_seen
    point_zero_statuses = [section.points[0].status for section in published]
    ordered = [
        PolarAlignmentPointStatus.SLEWING,
        PolarAlignmentPointStatus.SETTLING,
        PolarAlignmentPointStatus.CAPTURING,
        PolarAlignmentPointStatus.SOLVING,
        PolarAlignmentPointStatus.SOLVED,
    ]
    assert [point_zero_statuses.index(status) for status in ordered] == sorted(
        point_zero_statuses.index(status) for status in ordered
    )
    assert published[-1].status is PolarAlignmentStatus.COMPLETED


async def test_preflight_waits_for_fresh_stationary_mount_without_using_an_attempt(
    ephemeris: Ephemeris,
) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    initial = _initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg).model_copy(
        update={"stale": True, "slewing": True}
    )
    mount = FakeMountControl(_state_with_mount(initial))
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    observed_reason: list[str | None] = []

    async def make_mount_ready(_seconds: float) -> None:
        if service.section.preflight_reason is not None:
            observed_reason.append(service.section.preflight_reason)
            assert mount.slew_calls == []
            mount._state.apply(
                MountChanged(
                    mount=initial.model_copy(
                        update={"polled_at": WHEN, "stale": False, "slewing": False}
                    )
                )
            )

    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=solver,
        sleep=make_mount_ready,
    )

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    assert observed_reason == ["Waiting for fresh site coordinates and a stationary mount"]
    assert service.section.preflight_reason is None
    assert service.section.points[0].attempt == 2
    assert service.section.status is PolarAlignmentStatus.COMPLETED


async def test_solved_axis_point_uses_exposure_midpoint(ephemeris: Ephemeris) -> None:
    ra_h, dec_deg, _ = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=ra_h, dec_deg=dec_deg)))
    mount.expect_targets([(ra_h, dec_deg)])
    solver = ScriptedSolver()
    solver.expect_success(ra_h, dec_deg)
    service, _ = _build_service(
        ephemeris=ephemeris, mount=mount, solver=solver,
        section=PolarAlignmentState(points=[PolarAlignmentPointState(
            index=0, status=PolarAlignmentPointStatus.SLEWING,
            target_ra_j2000_h=ra_h, target_dec_j2000_deg=dec_deg,
        )]),
    )
    point = await service._run_one_point(0, ra_h, dec_deg, 4.0)
    assert point.when == WHEN + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_three_point_run_is_allowed_but_has_no_residual_check(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    final = service.section
    assert final.status is PolarAlignmentStatus.COMPLETED
    assert final.result is not None
    assert final.result.point_count == 3
    assert final.result.has_residual_check is False
    assert final.result.rms_residual_arcsec is None


# --- failure handling --------------------------------------------------------- #


async def test_a_slew_refusal_is_retried_not_fatal(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    mount.refuse_next[0] = 1  # point 0's first slew answers a simulated 409
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    final = service.section
    assert final.status is PolarAlignmentStatus.COMPLETED
    # 2 slew attempts + 1 solve attempt: the counter is cumulative across
    # the whole point, not reset when the solve phase starts.
    assert final.points[0].attempt == 3
    # point 0 was slewed to twice (refused, then retried); points 1 and 2 once each.
    assert mount.slew_calls.count(targets[0]) == 2


async def test_a_solve_failure_is_retried_then_the_point_is_skipped(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    solver.expect_success(*targets[0])
    # point 1: two "not enough stars" failures, then a good solve.
    solver.expect_failure()
    solver.expect_failure()
    solver.expect_success(*targets[1])
    # point 2: exhausts every attempt (max_solve_attempts=3) and is skipped.
    solver.expect_failure()
    solver.expect_failure()
    solver.expect_failure()
    solver.expect_success(*targets[3])
    solver.expect_success(*targets[4])
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=5,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    final = service.section
    assert final.status is PolarAlignmentStatus.COMPLETED
    assert final.result is not None
    assert final.result.point_count == 4  # one of five was unusable
    assert final.points[1].status is PolarAlignmentPointStatus.SOLVED
    # 1 slew attempt + 3 solve attempts (two failures, then success).
    assert final.points[1].attempt == 4
    assert final.points[2].status is PolarAlignmentPointStatus.FAILED
    assert final.points[2].error is not None and "not enough stars" in final.points[2].error


async def test_too_few_usable_points_refuses_clearly(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    solver.expect_success(*targets[0])
    solver.expect_success(*targets[1])
    for _ in targets[2:]:
        for _attempt in range(3):
            solver.expect_failure()
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=5,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    final = service.section
    assert final.status is PolarAlignmentStatus.FAILED
    assert final.result is None
    assert final.error is not None
    assert "only 2 of 5" in final.error


async def test_a_camera_failure_is_retried_and_recovers(ephemeris: Ephemeris) -> None:
    """Not only a solve failure — a device hiccup mid-capture must not kill
    the run either (module docstring: tolerate what fails in the field)."""

    class FlakyCamera(FakeCamera):
        def __init__(self) -> None:
            super().__init__()
            self.failures_left = 2

        async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
            if self.failures_left > 0:
                self.failures_left -= 1
                raise ExposureNotAvailableError("Camera.StartExposure: simulated")
            await super().start_exposure(duration_s, light=light)

    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(
        ephemeris=ephemeris, mount=mount, solver=solver, camera=FlakyCamera()
    )

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    final = service.section
    assert final.status is PolarAlignmentStatus.COMPLETED
    assert final.points[0].status is PolarAlignmentPointStatus.SOLVED
    # 1 slew attempt + 3 capture attempts (two camera failures, then success).
    assert final.points[0].attempt == 4


# --- cancellation -------------------------------------------------------------- #


async def test_cancel_stops_the_mount_and_leaves_state_coherent(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)

    reached = asyncio.Event()
    gate = asyncio.Event()

    class BlockingSolver(ScriptedSolver):
        async def __call__(self, fits_path: Path) -> PlateSolveResult:
            reached.set()
            await gate.wait()
            return await super().__call__(fits_path)

    solver = BlockingSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await asyncio.wait_for(reached.wait(), timeout=5.0)

    await service.cancel()

    final = service.section
    assert final.status is PolarAlignmentStatus.CANCELLED
    assert [point.status for point in final.points] == [
        PolarAlignmentPointStatus.ABORTED,
        PolarAlignmentPointStatus.SKIPPED,
        PolarAlignmentPointStatus.SKIPPED,
    ]
    assert final.points[0].error == "Polar alignment cancelled during this point"
    assert all(point.error is None for point in final.points[1:])
    assert mount.stop_calls == 1
    gate.set()  # release the blocked solve so nothing is left dangling


async def test_gain_is_applied_once_before_the_first_exposure(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    camera = FakeCamera()
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver, camera=camera)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
        gain=250,
    )
    await service._task

    assert service.section.status is PolarAlignmentStatus.COMPLETED
    assert camera.put_calls == [("gain", {"Gain": 250})]


async def test_uvircut_filter_is_selected_before_the_sweep(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    wheel = FakeFilterWheel(["Dark", "IR", "LP"])
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver, filterwheel=wheel)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task

    assert wheel.put_calls == [("position", {"Position": 1})]


async def test_solver_unavailable_refuses_before_slewing(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())
    service._availability = _unavailable  # type: ignore[method-assign]

    with pytest.raises(PolarAlignmentSolverUnavailableError):
        await service.start(point_count=5, step_deg=STEP_DEG, exposure_s=1.0)
    assert mount.slew_calls == []


# --- live refinement (the rest of DESIGN.md M10) ------------------------------ #


async def _completed_sweep(
    ephemeris: Ephemeris,
    *,
    point_count: int = 5,
) -> tuple[PolarAlignmentService, StateStore, FakeMountControl, list[tuple[float, float]]]:
    """A real, completed 3(+)-point sweep — the fixed baseline every
    refinement test in this section refines from."""
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=point_count)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    sweep_solver = ScriptedSolver()
    for ra_h, dec in targets:
        sweep_solver.expect_success(ra_h, dec)
    service, state = _build_service(ephemeris=ephemeris, mount=mount, solver=sweep_solver)

    await service.start(
        point_count=point_count,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )
    await service._task
    assert service.section.status is PolarAlignmentStatus.COMPLETED
    assert service._sweep_model is not None
    return service, state, mount, targets


async def test_interactive_sweep_can_continue_into_live_refinement(
    ephemeris: Ephemeris,
) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    ref_ra_h, ref_dec_deg = targets[-1]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    ref_when = WHEN + timedelta(seconds=180)
    solver.expect(_plate_solve(ref_ra_deg, ref_dec_deg, ref_when))
    for _ in range(100):
        solver.expect(_plate_solve(ref_ra_deg + 0.02, ref_dec_deg + 0.02, ref_when))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
        auto_refine=True,
    )
    await service._task
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    assert service.section.status is PolarAlignmentStatus.COMPLETED
    assert service.section.refinement.status is PolarRefinementStatus.RUNNING
    assert mount.tracking is False
    await service.stop_refinement()
    assert mount.tracking is True


async def _wait_until(predicate: Callable[[], bool], *, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def _plate_solve(ra_deg: float, dec_deg: float, when: datetime) -> PlateSolveResult:
    return PlateSolveResult(
        coord=j2000_coord(Deg(ra_deg), Deg(dec_deg)),
        date_obs=when,
        pixel_scale_arcsec=2.4,
        rotation_deg=0.0,
        solver="fake",
        solver_version="1.0",
    )


async def test_full_night_details_survive_real_app_restart(ephemeris, config, gateway, tmp_path):
    from zoneinfo import ZoneInfo

    from ssc.api.plans_service import current_night_date
    from ssc.main import create_app
    from ssc.settings.models import LocationFix
    from ssc.settings.store import SettingsStore
    from ssc.state.models import LocationSource, PolarRefinementResult, PolarRefinementState
    from ssc.store.database import open_database
    from ssc.store.migrations import migrate
    from ssc.store.polar_alignment import PolarAlignmentRepository
    from tests.api.conftest import CLIENT_ADDRESS, TestClient, pairing_token, services_of

    completed, _, _, _ = await _completed_sweep(ephemeris)
    at = datetime.now(UTC)
    section = completed.section.model_copy(update={"started_at": at, "ended_at": at})
    shared = section.result.model_dump()
    refinement_result = PolarRefinementResult(**{
        k: v for k, v in shared.items() if k in PolarRefinementResult.model_fields
    }, altitude_step_deg=.1, azimuth_step_deg=-.2, axis_shift_deg=.3,
        boresight_shift_deg=.4, elapsed_seconds=30, sweep_age_seconds=60,
        refinement_uncertainty_arcsec=12, trusted=False, warnings=["Independent recheck needed"])
    section = section.model_copy(update={"refinement": PolarRefinementState(
        status="stopped", started_at=at, ended_at=at, iteration=4,
        degenerate_count=2, last_degenerate_reason="small_arc", error="Retained diagnostic",
        result=refinement_result)})
    db = await open_database(config.database_path)
    try:
        await migrate(db)
        settings = SettingsStore(db)
        await settings.load()
        await settings.record_fix(LocationSource.MANUAL, LocationFix(
            latitude_deg=48.2, longitude_deg=16.4, timezone="Europe/Bratislava", acquired_at=at))
        repo = PolarAlignmentRepository(db)
        night = current_night_date(at, ZoneInfo("Europe/Bratislava"))
        await repo.save(night, "Europe/Bratislava", section)
        assert await repo.get(night, "UTC") is None
        assert await repo.get(night + timedelta(days=1), "Europe/Bratislava") is None
    finally:
        await db.aclose()
    # Production lifespan restores the complete section on its first snapshot;
    # reconnecting two browsers cannot reset it or start physical work.
    for _ in range(2):
        with TestClient(create_app(config, gateway=gateway), client=CLIENT_ADDRESS) as client:
            assert client.post("/api/auth/pair", json={"token": pairing_token(client)}).status_code == 200
            snapshot = client.get("/api/state").json()["snapshot"]
            assert snapshot["polar_alignment"] == section.model_dump(mode="json")
            live = services_of(client).polar_alignment
            assert live._task is None and live._refinement_task is None
            assert live._sweep_model is None  # No resurrected physical baseline.
            client.portal.call(lambda live=live: live._publish_refinement(iteration=5))
            client.portal.call(live.flush_persistence)
            section = client.portal.call(lambda live=live: live.section)


async def test_running_night_restore_retains_points_and_results_without_resuming(ephemeris):
    from ssc.api.polar_alignment_service import restored_night_state
    service, _, _, _ = await _completed_sweep(ephemeris)
    original = service.section
    running = original.model_copy(update={"status": PolarAlignmentStatus.RUNNING,
        "refinement": original.refinement.model_copy(update={
            "status": PolarRefinementStatus.RUNNING, "iteration": 7})})
    restored = restored_night_state(running, WHEN + timedelta(hours=1))
    assert restored.status is PolarAlignmentStatus.FAILED
    assert restored.refinement.status is PolarRefinementStatus.STOPPED
    assert restored.refinement.iteration == 7
    assert restored.points == original.points and restored.result == original.result
    assert restored.history == original.history and restored.assessment == original.assessment
    assert "restart" in restored.error and "restart" in restored.refinement.error


async def test_night_retention_is_ordered_and_rollover_keeps_previous_night(ephemeris, tmp_path):
    from zoneinfo import ZoneInfo

    from ssc.api.plans_service import current_night_date
    from ssc.store.database import open_database
    from ssc.store.migrations import migrate
    from ssc.store.polar_alignment import PolarAlignmentRepository
    service, _, _, _ = await _completed_sweep(ephemeris)
    db = await open_database(tmp_path / "night.sqlite3")
    try:
        await migrate(db)
        repo = PolarAlignmentRepository(db)
        def key(at):
            return current_night_date(at, ZoneInfo("Europe/Bratislava")), "Europe/Bratislava"
        service._night_key = key
        service._retained_night = key(WHEN)
        service._persist = lambda section: repo.save(*key(section.started_at), section)
        service._publish_refinement(iteration=1)
        service._publish_refinement(iteration=2)
        service._publish_refinement(iteration=3)
        await service.flush_persistence()
        retained = await repo.get(*key(WHEN))
        assert retained.refinement.iteration == 3
        # Midnight and the DST repeated hour stay within one observing night.
        assert key(WHEN) == key(WHEN + timedelta(hours=6))
        dst_a = datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
        assert key(dst_a) == key(dst_a + timedelta(hours=1))
        noon = datetime(2026, 10, 8, 10, tzinfo=UTC)  # 12:00 CEST
        assert key(noon - timedelta(seconds=1))[0] != key(noon)[0]
        service._now = lambda: WHEN + timedelta(days=1)
        assert service.section.status is None and service.section.history == []
        assert await repo.get(*key(WHEN)) == retained
    finally:
        await db.aclose()


async def test_night_retention_failure_is_visible_and_next_update_retries(ephemeris):
    from unittest.mock import AsyncMock
    service, state, _, _ = await _completed_sweep(ephemeris)
    service._persist = AsyncMock(side_effect=[OSError("disk temporarily unavailable"), None])
    service._publish_refinement(iteration=1)
    await service._persist_task
    assert service._persist_pending is not None
    assert any(n.id == "polar-alignment-retention" for n in state.snapshot.notices)
    service._publish_refinement(iteration=2)
    await service.flush_persistence()
    assert service._persist_pending is None
    assert service._persist.call_args.args[0].refinement.iteration == 2
    assert not any(n.id == "polar-alignment-retention" for n in state.snapshot.notices)


async def test_refinement_baseline_never_chains(ephemeris: Ephemeris) -> None:
    """Two identical re-solves against the same fixed reference must produce
    byte-identical refinements: the loop must feed
    :func:`ssc.astro.polar.refine_polar_alignment` the *sweep's* model every
    time, never the previous refinement's own
    (``ssc.astro.polar.PolarRefinement.model``'s own comment on why chaining
    would compound the reference solve's noise once per re-solve). This is
    the anti-chaining test: a service that fed the previous iteration's own
    refined model back in would measure a (however slightly) different step
    the second time, even off an identical re-solve."""
    service, state, mount, targets = await _completed_sweep(ephemeris)

    ref_when = WHEN + timedelta(seconds=300.0)
    ref_ra_h, ref_dec_deg = targets[2]  # the meridian point, best conditioned
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    reference_result = _plate_solve(ref_ra_deg, ref_dec_deg, ref_when)
    # A real (non-trivial) tripod nudge: +0.05 deg in both coordinates —
    # large enough that a chained baseline would visibly disagree with an
    # unchained one, small enough to stay well clear of every refusal.
    resolved_result = _plate_solve(ref_ra_deg + 0.05, ref_dec_deg + 0.05, ref_when)

    refine_solver = ScriptedSolver()
    refine_solver.expect(reference_result)
    for _ in range(200):  # far more than this test needs, so stopping never races an IndexError
        refine_solver.expect(resolved_result)
    mount.tracking = True  # a tracking mount, until the loop itself stops it
    service._solve = refine_solver

    section = await service.start_refinement(exposure_s=1.0)
    assert section.refinement.status is PolarRefinementStatus.RUNNING
    assert mount.tracking is False  # the loop stopped it and confirmed the read-back

    await _wait_until(lambda: service.section.refinement.iteration >= 2)

    published = [
        event.event.polar_alignment.refinement
        for event in state.since(0).events  # type: ignore[union-attr]
        if isinstance(event.event, PolarAlignmentChanged)
    ]
    results = [refinement.result for refinement in published if refinement.result is not None]
    assert len(results) >= 2
    # Byte-identical: same model, same reference, same re-solve in, same
    # numbers out — twice.
    assert results[0] == results[1]
    assert results[0].trusted is False
    assert any("single solved field" in warning for warning in results[0].warnings)

    await service.stop_refinement()
    assert service.section.refinement.status is PolarRefinementStatus.STOPPED
    assert mount.tracking is True
    with pytest.raises(PolarRefinementNoSweepError, match="new measurement"):
        await service.start_refinement(exposure_s=1.0)


async def test_refinement_retries_a_bad_capture_and_keeps_running(
    ephemeris: Ephemeris,
) -> None:
    service, state, mount, targets = await _completed_sweep(ephemeris)
    camera = service._camera
    assert isinstance(camera, FakeCamera)
    ref_when = WHEN + timedelta(seconds=300.0)
    ref_ra_h, ref_dec_deg = targets[2]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    solved = _plate_solve(ref_ra_deg, ref_dec_deg, ref_when)
    solver = ScriptedSolver()
    solver.expect(solved)
    solver.expect_failure()
    for _ in range(200):
        solver.expect(solved)
    service._solve = solver

    await service.start_refinement(exposure_s=5.0)
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    assert all(duration == 2.0 for duration in camera.durations[-3:])
    assert mount.tracking is False
    assert service.section.refinement.status is PolarRefinementStatus.RUNNING
    assert service.section.refinement.error is None
    published = [
        event.event.polar_alignment.refinement
        for event in state.since(0).events  # type: ignore[union-attr]
        if isinstance(event.event, PolarAlignmentChanged)
    ]
    assert any(
        item.error is not None and "capturing a fresh frame" in item.error for item in published
    )

    await service.stop_refinement()
    assert mount.tracking is True


async def test_refinement_caps_each_solve_then_captures_a_new_frame(
    ephemeris: Ephemeris,
) -> None:
    service, state, _mount, targets = await _completed_sweep(ephemeris)
    ref_when = WHEN + timedelta(seconds=300.0)
    ref_ra_h, ref_dec_deg = targets[2]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    solved = _plate_solve(ref_ra_deg, ref_dec_deg, ref_when)

    class HangingOnceSolver:
        def __init__(self) -> None:
            self.calls = 0
            self.cancelled = False

        async def __call__(self, fits_path: Path) -> PlateSolveResult:
            assert await asyncio.to_thread(fits_path.is_file)
            self.calls += 1
            if self.calls == 2:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled = True
                    raise
            return solved

    solver = HangingOnceSolver()
    service._solve = solver
    service._refinement_solve_timeout_s = 0.01

    await service.start_refinement(exposure_s=1.0)
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    assert solver.cancelled
    assert solver.calls >= 3  # reference, timed-out frame, fresh frame
    assert service.section.refinement.status is PolarRefinementStatus.RUNNING
    published = [
        event.event.polar_alignment.refinement
        for event in state.since(0).events  # type: ignore[union-attr]
        if isinstance(event.event, PolarAlignmentChanged)
    ]
    assert any(item.error is not None and "0.01s" in item.error for item in published)
    await service.stop_refinement()


async def test_refinement_stops_after_repeated_unsolved_frames_and_restores_tracking(
    ephemeris: Ephemeris,
) -> None:
    service, _state, mount, _targets = await _completed_sweep(ephemeris)
    camera = service._camera
    assert isinstance(camera, FakeCamera)
    solver = ScriptedSolver()
    for _ in range(3):
        solver.expect_failure()
    service._solve = solver

    await service.start_refinement(exposure_s=2.0)
    assert service._refinement_task is not None
    await service._refinement_task

    assert solver.calls == 3
    assert camera.durations[-3:] == [2.0, 2.0, 2.0]
    assert service.section.refinement.status is PolarRefinementStatus.FAILED
    assert "could not solve or capture 3 consecutive frames" in (service.section.refinement.error or "")
    assert mount.tracking is True


async def test_refinement_retries_a_late_camera_frame_only_after_it_is_ready(
    ephemeris: Ephemeris,
) -> None:
    service, _state, mount, targets = await _completed_sweep(ephemeris)
    camera = service._camera
    assert isinstance(camera, FakeCamera)
    ref_ra_h, ref_dec_deg = targets[2]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    after_sweep = WHEN + timedelta(seconds=180)
    solved = _plate_solve(ref_ra_deg, ref_dec_deg, after_sweep)
    resolved = _plate_solve(ref_ra_deg + 0.05, ref_dec_deg + 0.05, after_sweep)
    calls = 0

    async def flaky_capture(
        exposure_s: float, object_name: str, *, publish_refinement_stage: bool,
        solve_timeout_s: float | None,
    ) -> PlateSolveResult:
        del exposure_s, object_name, publish_refinement_stage, solve_timeout_s
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        if calls == 1:
            raise ExposureTimeoutError("late ImageReady")
        return solved if calls == 2 else resolved

    service._expose_and_solve = flaky_capture  # type: ignore[method-assign]
    await service.start_refinement(exposure_s=2.0)
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    assert calls >= 3  # timed-out reference, fresh reference, live solve
    assert service.section.refinement.status is PolarRefinementStatus.RUNNING
    await service.stop_refinement()
    assert mount.tracking is True


async def test_start_refinement_refuses_if_tracking_will_not_stop(ephemeris: Ephemeris) -> None:
    service, _state, mount, _targets = await _completed_sweep(ephemeris)
    mount.refuse_tracking_stop = True

    with pytest.raises(PolarRefinementTrackingError):
        await service.start_refinement(exposure_s=1.0)

    # Refused before anything was published as running (architecture policy invariant 7:
    # a refusal must not leave a half-started section behind).
    assert service.section.refinement.status is None


async def test_start_refinement_refuses_while_mount_motion_is_unconfirmed(
    ephemeris: Ephemeris,
) -> None:
    service, state, _mount, _targets = await _completed_sweep(ephemeris)
    mount_state = state.snapshot.mount
    assert mount_state is not None
    state.apply(MountChanged(mount=mount_state.model_copy(update={"slewing": True})))

    with pytest.raises(PolarRefinementTrackingError, match="motion is not confirmed stopped"):
        await service.start_refinement(exposure_s=1.0)

    assert service.section.refinement.status is None


async def test_degenerate_iteration_survives_and_the_loop_keeps_going(
    ephemeris: Ephemeris,
) -> None:
    """A tripod shoved far enough to look like a slew is a per-iteration
    outcome, not a broken precondition: the loop surfaces the reason and
    keeps re-solving (module docstring; ``DegeneracyReason.STEP_TOO_LARGE``)."""
    service, state, _mount, targets = await _completed_sweep(ephemeris)

    ref_when = WHEN + timedelta(seconds=300.0)
    ref_ra_h, ref_dec_deg = targets[2]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    reference_result = _plate_solve(ref_ra_deg, ref_dec_deg, ref_when)
    # Far past DEFAULT_MAX_REFINEMENT_STEP_DEG (20 deg) — reads as a slew.
    degenerate_result = _plate_solve(ref_ra_deg, ref_dec_deg + 30.0, ref_when)
    # A real, small tripod nudge afterwards; the one-field result is diagnostic.
    good_result = _plate_solve(ref_ra_deg + 0.05, ref_dec_deg + 0.05, ref_when)

    refine_solver = ScriptedSolver()
    refine_solver.expect(reference_result)
    refine_solver.expect(degenerate_result)
    for _ in range(200):
        refine_solver.expect(good_result)
    service._solve = refine_solver

    await service.start_refinement(exposure_s=1.0)
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    final = service.section.refinement
    assert final.status is PolarRefinementStatus.RUNNING  # survived, still going
    # Exactly one degenerate item was ever scripted, so this stays 1 no
    # matter how many further (identical, good) iterations have since run —
    # unlike ``iteration``, which keeps advancing in the background.
    assert final.degenerate_count == 1
    assert final.iteration >= 1
    assert final.last_degenerate_reason is None  # cleared by the clean iteration that followed
    assert final.result is not None
    assert final.result.trusted is False

    published = [
        event.event.polar_alignment.refinement
        for event in state.since(0).events  # type: ignore[union-attr]
        if isinstance(event.event, PolarAlignmentChanged)
    ]
    degenerate_seen = [p for p in published if p.degenerate_count == 1 and p.iteration == 0]
    assert degenerate_seen, "the degenerate iteration was never published on its own"
    assert degenerate_seen[0].last_degenerate_reason == DegeneracyReason.STEP_TOO_LARGE.value
    assert degenerate_seen[0].error is not None and "20.0 deg" in degenerate_seen[0].error

    await service.stop_refinement()


async def test_a_doubtful_refinement_is_published_trusted_false_with_warnings(
    ephemeris: Ephemeris,
) -> None:
    """A large-but-not-degenerate move is *reported*, not swallowed or
    silently upgraded (CLAUDE.md invariant 7): ``trusted=False`` and its
    warnings must reach the published state exactly as
    ``ssc.astro.polar.refine_polar_alignment`` returned them."""
    service, _state, _mount, targets = await _completed_sweep(ephemeris)

    ref_when = WHEN + timedelta(seconds=300.0)
    ref_ra_h, ref_dec_deg = targets[2]
    ref_ra_deg = float(hours_to_deg(Hours(ref_ra_h)))
    reference_result = _plate_solve(ref_ra_deg, ref_dec_deg, ref_when)
    # 15 deg: past the "large move" warning threshold, short of the 20 deg
    # refusal — doubtful, not degenerate.
    doubtful_result = _plate_solve(ref_ra_deg, ref_dec_deg + 15.0, ref_when)

    refine_solver = ScriptedSolver()
    refine_solver.expect(reference_result)
    for _ in range(200):
        refine_solver.expect(doubtful_result)
    service._solve = refine_solver

    await service.start_refinement(exposure_s=1.0)
    await _wait_until(lambda: service.section.refinement.iteration >= 1)

    result = service.section.refinement.result
    assert result is not None
    assert result.trusted is False
    assert result.warnings  # never swallowed
    assert any("large move" in warning for warning in result.warnings)

    await service.stop_refinement()


async def test_stop_refinement_without_one_running_refuses(ephemeris: Ephemeris) -> None:
    service, _state, _mount, _targets = await _completed_sweep(ephemeris)
    with pytest.raises(PolarRefinementNotRunningError):
        await service.stop_refinement()


async def test_start_refinement_without_a_completed_sweep_refuses(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())

    with pytest.raises(PolarRefinementNoSweepError):
        await service.start_refinement(exposure_s=1.0)
    assert mount.tracking_calls == 0  # refused before touching the mount at all


# --- start_point: best / current / explicit (DESIGN.md §4.5, 2026-09-11) ---- #


async def test_default_start_point_is_best_conditioned_field(ephemeris: Ephemeris) -> None:
    """No ``start_point`` and no coordinates: the run starts from
    ``ssc.astro.polar.best_conditioned_field`` (the default since
    2026-09-11), never from the mount's own current position — which here is
    deliberately set somewhere the computed field would *not* choose, so a
    silent fall-back to "current" would show up as a mismatch."""
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=-40.0)))
    service, _ = _build_service(
        ephemeris=ephemeris, mount=mount, solver=ScriptedSolver(), now=lambda: WHEN
    )

    expected = best_conditioned_field(ephemeris, SITE, WHEN, point_count=5, step_deg=Deg(STEP_DEG))

    section = await service.start(point_count=5, step_deg=STEP_DEG, exposure_s=1.0)

    assert section.start is not None
    assert section.start.mode is PolarAlignmentStartMode.BEST
    assert section.start.ideal is True
    assert section.start.declination_offset_deg == 0.0
    assert section.start.meridian_altitude_deg == pytest.approx(
        90.0 - abs(float(SITE.latitude_deg))
    )
    assert section.start.warnings == []

    first = section.points[0]
    assert first.target_ra_j2000_h == pytest.approx(float(expected.coord.ra_h))
    assert first.target_dec_j2000_deg == pytest.approx(float(expected.coord.dec_deg))

    # A wrong hours<->degrees conversion would be a 15x error and would
    # still look "plausible" as a bare number, so pin it against the
    # independently-known relationship too.
    assert first.target_ra_j2000_h * 15.0 == pytest.approx(float(expected.coord.ra_deg))


async def test_start_point_current_uses_mount_position_and_records_mode(
    ephemeris: Ephemeris,
) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    section = await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.CURRENT,
    )

    assert section.start is not None
    assert section.start.mode is PolarAlignmentStartMode.CURRENT
    assert section.start.declination_offset_deg is None
    assert section.points[0].target_ra_j2000_h == pytest.approx(start_ra_h)
    assert section.points[0].target_dec_j2000_deg == pytest.approx(dec_deg)

    await service._task


async def test_seestar_start_point_reproduces_recorded_relative_field(
    ephemeris: Ephemeris,
) -> None:
    clock = fixed_now()
    coord = j2000_coord_at_hour_angle(
        ephemeris,
        SITE,
        WHEN + timedelta(milliseconds=1),
        declination_deg=SITE.latitude_deg,
        hour_angle_deg=Deg(15.0),
    )
    targets = _build_targets(float(coord.ra_h), float(coord.dec_deg), 3, 25.0)
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=-40.0)))
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec_deg in targets:
        solver.expect_success(ra_h, dec_deg)
    service, _ = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=solver,
        now=clock,
    )

    section = await service.start(
        point_count=3,
        step_deg=25.0,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.SEESTAR,
    )

    assert section.start is not None
    assert section.start.mode is PolarAlignmentStartMode.SEESTAR
    assert section.start.start_altitude_deg == pytest.approx(80.0, abs=0.2)
    assert section.start.start_azimuth_deg == pytest.approx(275.6, abs=0.2)
    assert section.points[0].target_ra_j2000_h == pytest.approx(float(coord.ra_h))
    assert section.points[0].target_dec_j2000_deg == pytest.approx(float(coord.dec_deg))

    await service.cancel()


async def test_start_point_explicit_uses_given_coords_and_records_mode(
    ephemeris: Ephemeris,
) -> None:
    start_ra_h, dec_deg, targets = _targets_for(ephemeris, count=3)
    # The mount's own position is deliberately different from the explicit
    # coordinate, so a bug that ignored the explicit value in favour of the
    # mount's own position would be caught.
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=1.0, dec_deg=-60.0)))
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec in targets:
        solver.expect_success(ra_h, dec)
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)

    section = await service.start(
        point_count=3,
        step_deg=STEP_DEG,
        exposure_s=1.0,
        start_point=PolarAlignmentStartMode.EXPLICIT,
        start_ra_j2000_h=start_ra_h,
        start_dec_j2000_deg=dec_deg,
    )

    assert section.start is not None
    assert section.start.mode is PolarAlignmentStartMode.EXPLICIT
    assert section.points[0].target_ra_j2000_h == pytest.approx(start_ra_h)
    assert section.points[0].target_dec_j2000_deg == pytest.approx(dec_deg)

    await service._task


async def test_explicit_without_both_coordinates_refuses(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())

    with pytest.raises(PolarAlignmentStartAmbiguousError):
        await service.start(
            point_count=5,
            step_deg=STEP_DEG,
            exposure_s=1.0,
            start_point=PolarAlignmentStartMode.EXPLICIT,
            start_ra_j2000_h=10.0,
            # start_dec_j2000_deg omitted
        )
    with pytest.raises(PolarAlignmentStartAmbiguousError):
        await service.start(
            point_count=5,
            step_deg=STEP_DEG,
            exposure_s=1.0,
            start_point=PolarAlignmentStartMode.EXPLICIT,
            start_dec_j2000_deg=10.0,
            # start_ra_j2000_h omitted
        )
    assert mount.slew_calls == []


@pytest.mark.parametrize(
    "start_point", [PolarAlignmentStartMode.BEST, PolarAlignmentStartMode.CURRENT]
)
async def test_a_coordinate_with_a_non_explicit_start_point_refuses(
    ephemeris: Ephemeris, start_point: PolarAlignmentStartMode
) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())

    with pytest.raises(PolarAlignmentStartAmbiguousError):
        await service.start(
            point_count=5,
            step_deg=STEP_DEG,
            exposure_s=1.0,
            start_point=start_point,
            start_ra_j2000_h=10.0,
        )
    with pytest.raises(PolarAlignmentStartAmbiguousError):
        await service.start(
            point_count=5,
            step_deg=STEP_DEG,
            exposure_s=1.0,
            start_point=start_point,
            start_dec_j2000_deg=10.0,
        )
    assert mount.slew_calls == []


async def test_best_field_adaptively_reduces_arc_to_clear_altitude_floor(
    ephemeris: Ephemeris,
) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(
        ephemeris=ephemeris, mount=mount, solver=ScriptedSolver(), now=lambda: WHEN
    )

    section = await service.start(point_count=9, step_deg=STEP_DEG, exposure_s=1.0)
    assert section.start is not None
    assert section.start.arc_span_deg == pytest.approx(150.0)
    assert section.start.step_deg == pytest.approx(18.75)
    assert min(section.start.point_altitudes_deg) >= 15.0
    assert any("reduced it to 150 deg" in warning for warning in section.start.warnings)
    await service.cancel()
    assert mount.slew_calls == []


async def test_recheck_requires_a_completed_measurement(ephemeris: Ephemeris) -> None:
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG)))
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())

    with pytest.raises(PolarAlignmentNoMeasurementError):
        await service.start_recheck(exposure_s=1.0)


@pytest.mark.parametrize("point_count", [3, 5])
async def test_recheck_reuses_point_count_and_preserves_direction(
    ephemeris: Ephemeris, point_count: int
) -> None:
    service, _state, _mount, targets = await _completed_sweep(
        ephemeris, point_count=point_count
    )
    previous = service._sweep_model
    assert previous is not None
    previous_step = service._last_step_deg

    section = await service.start_recheck(exposure_s=1.0)
    assert section.run_kind is PolarAlignmentRunKind.RECHECK
    assert len(section.history) == 1
    assert section.history[0].run_kind is PolarAlignmentRunKind.MEASURE
    assert len(section.points) == point_count
    assert section.start is not None
    assert section.start.mode is PolarAlignmentStartMode.CURRENT
    assert section.start.arc_span_deg == pytest.approx(abs(STEP_DEG) * (point_count - 1))
    assert section.start.step_deg == pytest.approx(STEP_DEG)
    assert [
        (point.target_ra_j2000_h, point.target_dec_j2000_deg) for point in section.points
    ] == targets
    assert service._sweep_model is previous
    assert service._last_step_deg == previous_step
    await asyncio.sleep(0)
    await service.cancel()

    retry = await service.start_recheck(exposure_s=1.0)
    assert [
        (point.target_ra_j2000_h, point.target_dec_j2000_deg) for point in retry.points
    ] == targets
    await service.cancel()
    assert len(service.section.history) == 1
    assert service._sweep_model is previous
    assert service._last_step_deg == previous_step


async def test_recheck_reuses_requested_count_when_one_point_failed(
    ephemeris: Ephemeris,
) -> None:
    service, _state, _mount, _targets = await _completed_sweep(ephemeris, point_count=5)
    previous = service._sweep_model
    assert previous is not None
    service._sweep_model = replace(previous, point_count=4)

    section = await service.start_recheck(exposure_s=1.0)

    assert len(section.points) == 5
    await service.cancel()


async def test_three_point_recheck_can_add_two_verification_points_on_same_arc(
    ephemeris: Ephemeris,
) -> None:
    service, _state, _mount, targets = await _completed_sweep(ephemeris, point_count=3)
    section = await service.start_recheck(exposure_s=1.0, point_count=5)

    assert len(section.points) == 5
    assert section.start is not None
    assert section.start.arc_span_deg == pytest.approx(abs(STEP_DEG) * 2)
    assert section.start.step_deg == pytest.approx(STEP_DEG / 2)
    planned = [(p.target_ra_j2000_h, p.target_dec_j2000_deg) for p in section.points]
    assert planned[0] == targets[0]
    assert planned[2][0] == pytest.approx(targets[1][0])
    assert planned[-1][0] == pytest.approx(targets[-1][0])
    await service.cancel()


@pytest.mark.parametrize(
    ("start_point", "start_ra_h", "start_dec_deg"),
    [
        (PolarAlignmentStartMode.EXPLICIT, 7.25, 42.5),
        (PolarAlignmentStartMode.SEESTAR, None, None),
    ],
)
async def test_recheck_reuses_custom_or_seestar_field(
    ephemeris: Ephemeris,
    start_point: PolarAlignmentStartMode,
    start_ra_h: float | None,
    start_dec_deg: float | None,
) -> None:
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=10.0, dec_deg=DEC_DEG))
    )
    service, _ = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())
    section = await service.start(
        point_count=3,
        step_deg=25.0,
        exposure_s=1.0,
        start_point=start_point,
        start_ra_j2000_h=start_ra_h,
        start_dec_j2000_deg=start_dec_deg,
    )
    targets = [
        (point.target_ra_j2000_h, point.target_dec_j2000_deg) for point in section.points
    ]
    mount.expect_targets(targets)
    solver = ScriptedSolver()
    for ra_h, dec_deg in targets:
        solver.expect_success(ra_h, dec_deg)
    service._solve = solver
    await service._task
    assert service.section.status is PolarAlignmentStatus.COMPLETED

    recheck = await service.start_recheck(exposure_s=1.0)
    assert recheck.start is not None
    assert recheck.start.mode is start_point
    assert recheck.start.arc_span_deg == pytest.approx(50.0)
    assert recheck.start.step_deg == pytest.approx(25.0)
    assert [
        (point.target_ra_j2000_h, point.target_dec_j2000_deg) for point in recheck.points
    ] == targets
    await service.cancel()


async def test_completed_recheck_appends_to_measurement_history(ephemeris: Ephemeris) -> None:
    service, _state, mount, _targets = await _completed_sweep(ephemeris)

    section = await service.start_recheck(exposure_s=1.0)
    recheck_targets = [
        (point.target_ra_j2000_h, point.target_dec_j2000_deg) for point in section.points
    ]
    mount.expect_targets(recheck_targets)
    solver = ScriptedSolver()
    for ra_h, dec_deg in recheck_targets:
        solver.expect_success(ra_h, dec_deg)
    service._solve = solver

    await service._task

    assert service.section.status is PolarAlignmentStatus.COMPLETED
    assert [entry.run_kind for entry in service.section.history] == [
        PolarAlignmentRunKind.MEASURE,
        PolarAlignmentRunKind.RECHECK,
    ]
    assert service.section.result is not None
    assert service.section.history[-1].total_error_deg == service.section.result.total_error_deg


async def test_one_low_sweep_never_confirms_and_disagreeing_low_sweeps_are_inconsistent(
    ephemeris: Ephemeris,
) -> None:
    service, _state, _mount, _targets = await _completed_sweep(ephemeris)
    baseline = service._sweep_model
    assert baseline is not None
    low = replace(
        baseline,
        total_error_deg=Deg(0.01),
        axis_uncertainty_arcsec=30.0,
        point_count=5,
        residual_dof=2,
    )

    first = _assess_sweep(low, None)
    assert first.verdict.value == "recheck"

    shifted = replace(
        low,
        axis_altitude_deg=Deg(float(low.axis_altitude_deg) + 0.1),
    )
    inconsistent = _assess_sweep(shifted, low)
    assert inconsistent.verdict.value == "inconsistent"
    assert inconsistent.axis_separation_arcmin is not None
    assert inconsistent.axis_separation_arcmin > 2.0

    confirmed = _assess_sweep(low, low)
    assert confirmed.verdict.value == "confirmed"


async def test_solve_here_solves_where_the_scope_already_points(ephemeris: Ephemeris) -> None:
    """``solve_here`` moves nothing: no slew is issued, and the solve it
    returns is the one the camera actually produced."""
    start_ra_h, dec_deg, _targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )
    solver = ScriptedSolver()
    solver.expect_success(start_ra_h, dec_deg)
    camera = FakeCamera()
    telescope = FakeDevice()
    service, _state = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=solver,
        camera=camera,
        telescope=telescope,
    )

    result = await service.solve_here(exposure_s=1.0)

    assert solver.calls == 1
    assert float(result.coord.dec_deg) == pytest.approx(dec_deg, abs=1e-9)
    # The whole point: nothing was slewed.
    assert mount.slew_calls == []
    # Solver frames use SSC's known timing and pixels. Optional FITS-header
    # reads used to serialize many 5s driver timeouts before ImageArray.
    assert camera.get_calls == []
    assert telescope.get_calls == []


async def test_solve_here_bounds_a_solver_that_never_returns(ephemeris: Ephemeris) -> None:
    start_ra_h, dec_deg, _targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(
        _state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg))
    )

    async def stuck_solver(_fits_path: Path) -> PlateSolveResult:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service, _state = _build_service(
        ephemeris=ephemeris,
        mount=mount,
        solver=stuck_solver,
        interactive_solve_timeout_s=0.01,
    )

    with pytest.raises(SolveTimedOutError, match="did not finish within"):
        await service.solve_here(exposure_s=1.0)


async def test_solve_here_refuses_while_a_sweep_owns_the_camera(ephemeris: Ephemeris) -> None:
    """A sweep is already exposing; a second frame taken underneath it would
    be attributed to whichever exposure was in flight — a wrong measurement
    rather than a failed one, so it refuses instead."""
    service, _state, _mount, _targets = await _completed_sweep(ephemeris)
    service._section = service._section.model_copy(update={"status": PolarAlignmentStatus.RUNNING})

    with pytest.raises(PolarAlignmentAlreadyRunningError):
        await service.solve_here(exposure_s=1.0)


async def test_service_retains_one_device_declination_per_sweep(ephemeris: Ephemeris) -> None:
    service, _state, mount, _targets = await _completed_sweep(ephemeris, point_count=5)
    assert mount.polar_declination_calls == [None, DEC_DEG, DEC_DEG, DEC_DEG, DEC_DEG]
    assert service._active_mount_dec_deg == DEC_DEG


async def test_large_three_point_rechecks_still_expose_axis_disagreement(
    ephemeris: Ephemeris,
) -> None:
    service, _state, _mount, _targets = await _completed_sweep(ephemeris, point_count=3)
    baseline = service._sweep_model
    assert baseline is not None
    changed = replace(
        baseline,
        axis_altitude_deg=Deg(float(baseline.axis_altitude_deg) + 0.2),
        total_error_deg=Deg(0.25),
    )
    assessment = _assess_sweep(changed, baseline)
    assert assessment.verdict.value == "recheck"
    assert assessment.axis_separation_arcmin == pytest.approx(12.0)
    assert assessment.previous_total_error_arcmin == pytest.approx(
        float(baseline.total_error_deg) * 60
    )


def test_october3_untouched_wedge_solves_expose_the_real_axis_jump(ephemeris: Ephemeris) -> None:
    # Captured production log solves; owner confirmed no wedge adjustment all day.
    # Retain original start epochs to reproduce the then-running implementation.
    from ssc.astro.frames import j2000_coord, mount_site
    from ssc.astro.polar import PolarPoint, solve_polar_alignment
    site = mount_site(Deg(48.709446), Deg(17.848585))
    runs = [
        [("17:57:47.003142", 17.964097535, -.193450374), ("17:58:17.062065", 19.981225942, -.281901138), ("17:58:45.560297", 21.993034629, -.341049298)],
        [("17:59:27.320297", 17.964368255, -.195523271), ("17:59:56.495351", 19.968474562, -.282174639), ("18:00:16.355673", 21.993761510, -.286922629)],
        [("18:01:05.186501", 18.020487467, -.200205084), ("18:01:34.129748", 20.027169643, -.286852751), ("18:01:55.402908", 22.049430060, -.347138896)],
    ]
    previous = None
    totals = []
    for run in runs:
        points = [PolarPoint(coord=j2000_coord(Deg(ra * 15), Deg(dec)), when=datetime.fromisoformat("2026-10-03T" + epoch).replace(tzinfo=UTC)) for epoch, ra, dec in run]
        model = solve_polar_alignment(ephemeris, site, points)
        totals.append(float(model.total_error_deg) * 60)
        assessment = _assess_sweep(model, previous)
        if previous is not None:
            assert assessment.axis_separation_arcmin is not None
            assert assessment.axis_separation_arcmin > 12
            assert assessment.verdict.value != "confirmed"
        previous = model
    assert totals == pytest.approx([3.534303261, 15.433012398, 2.974318018], abs=1e-5)


async def test_solve_here_waits_for_delayed_image_without_restarting_exposure(ephemeris: Ephemeris) -> None:
    from datetime import timedelta

    start_ra_h, dec_deg, _targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg)))
    clock = [fixed_now()()]
    started = clock[0]

    class DelayedCamera(FakeCamera):
        async def image_ready(self) -> bool:
            return (clock[0] - started).total_seconds() >= 20

    async def advance(seconds: float) -> None:
        clock[0] += timedelta(seconds=seconds)

    solver = ScriptedSolver()
    solver.expect_success(start_ra_h, dec_deg)
    camera = DelayedCamera()
    service, _state = _build_service(ephemeris=ephemeris, mount=mount, solver=solver,
                                    camera=camera, now=lambda: clock[0], sleep=advance)
    await service.solve_here(exposure_s=1)
    assert camera.start_calls == 1
    assert solver.calls == 1


@pytest.mark.parametrize("stale", [False, True])
async def test_default_pa_solver_uses_only_fresh_j2000_pointing_hints(
    ephemeris: Ephemeris, monkeypatch: pytest.MonkeyPatch, stale: bool,
) -> None:
    import ssc.api.polar_alignment_service as module

    start_ra_h, dec_deg, _targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg)))
    solver = ScriptedSolver()
    solver.expect_success(start_ra_h, dec_deg)
    service, state = _build_service(ephemeris=ephemeris, mount=mount, solver=solver)
    telemetry = state.snapshot.mount
    assert telemetry is not None
    state.apply(MountChanged(mount=telemetry.model_copy(update={
        "ra_j2000_h": start_ra_h, "dec_j2000_deg": dec_deg,
        "polled_at": service._now(), "stale": stale,
    })))
    hints = []
    async def astap(path: Path, **options) -> PlateSolveResult:
        hints.append(options)
        return await solver(path)
    monkeypatch.setattr(module, "astap_solve", astap)
    service._solve = astap
    await service._expose_and_solve(1, "hint-test")
    assert hints[0]["position_hint"] == (None if stale else (start_ra_h, dec_deg))
    assert hints[0]["field_height_deg"] is None


async def test_cancelled_solver_capture_drains_fits_writer_before_cleanup(
    ephemeris: Ephemeris, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    import ssc.api.polar_alignment_service as module

    start_ra_h, dec_deg, _targets = _targets_for(ephemeris, count=5)
    mount = FakeMountControl(_state_with_mount(_initial_mount_state(ra_h=start_ra_h, dec_deg=dec_deg)))
    service, _state = _build_service(ephemeris=ephemeris, mount=mount, solver=ScriptedSolver())
    entered = asyncio.Event()
    release = threading.Event()
    paths = []
    loop = asyncio.get_running_loop()
    original = module.write_fits_file
    def blocked(path, pixels, metadata) -> None:
        paths.append(path)
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        original(path, pixels, metadata)
    monkeypatch.setattr(module, "write_fits_file", blocked)
    task = asyncio.create_task(service._expose_and_solve(1, "cancel-write"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not paths[0].parent.exists()
