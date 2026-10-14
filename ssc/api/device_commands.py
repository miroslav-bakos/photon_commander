# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The executor's ports, made real: Alpaca, the wall clock, the weather service.

``ssc/scheduler/executor.py`` is pure — every side effect goes through a
``Protocol``. This module is where ``backend-core`` supplies the adapters:

* :class:`AlpacaDeviceCommands` — ``DeviceCommands`` over ``ssc.client.alpaca``,
  the same HTTP surface NINA would use (CLAUDE.md invariant 2). Standard ASCOM
  members wherever one exists; an ``ssc.*`` action only where ASCOM has no word
  for it (DESIGN.md 4.3). It is the **one** place that knows about
  :class:`~ssc.client.errors.AlpacaRemoteError`: a driver answer of
  ``NotImplemented``/``ActionNotImplemented`` becomes
  :class:`~ssc.scheduler.executor.DeviceCommandNotImplementedError` carrying the
  driver's own reason text, so the run journal says *why* an item failed in the
  driver's words rather than "not supported". Nothing here is stubbed to look
  like it worked (CLAUDE.md invariant 7).
* :class:`AlpacaDeviceEvents` — ``DeviceEvents``. Alpaca has no push channel and
  firmware 8.46 pushes no completion event of any kind (``docs/M0-FINDINGS.md``:
  zero frames in 110 s of activity), so "the device is done" is observed the way
  ASCOM defines it: an async slew is complete when ``Telescope.Slewing`` reads
  ``False``. Each ``event_kind`` the executor waits for maps to one
  :data:`CompletionSignal`; a kind with no known signal on the recorded firmware
  is refused as not implemented rather than guessed. The device-event-stream
  path (``DeviceEventReceived`` through ``ssc.state``) is wired for the day a
  firmware does push one.
* :class:`WallClock` — the single place ``datetime.now()`` and a real sleep live.
* :class:`ControlQueue` — ``ControlSource`` fed by the REST controls, plus the
  wake-up that lets a control interrupt the executor's idle sleep instead of
  waiting for its next poll tick.
* :class:`ServiceGateInputs` — ``GateInputs`` from the weather service's cached
  series and ``ssc.astro`` for altitude/Moon separation.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.metadata
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Final, cast
from uuid import uuid4, uuid5
from zoneinfo import ZoneInfo

import aiosqlite

from ssc.api.astro_service import AstroService
from ssc.api.capture_orchestrator import (
    CaptureAlreadyRunningError,
    CaptureDeviceUnavailableError,
    CaptureDitherSettleError,
    CaptureDitherWindowEndedError,
)
from ssc.api.capture_service import (
    CameraDevice,
    CaptureMetadataError,
    Device,
    ExposureContext,
    ExposureNotAvailableError,
    ExposureTimeoutError,
    capture_one_exposure,
    exposure_metadata_fallbacks,
)
from ssc.api.dual_capture import DualCaptureError, capture_pair, recover_dual_ownership
from ssc.api.external_tools import ConfiguredAstap
from ssc.api.mount_target import MountFrameUnavailableError, resolve_mount_target
from ssc.api.plans_service import PlanTimezoneUnknownError, resolve_timezone, site_of
from ssc.api.scheduled_focus import ScheduledFocusMonitor
from ssc.api.weather_service import WeatherService, WeatherUnavailableError
from ssc.astro.events import Ephemeris
from ssc.astro.frames import MountFrame, frame_offset_arcsec
from ssc.astro.units import Deg, Hours
from ssc.capture import FitsWriteError, FrameType
from ssc.client.alpaca import AlpacaClient, AlpacaDeviceClient
from ssc.client.errors import AlpacaClientError, AlpacaErrorNumber, AlpacaRemoteError
from ssc.platesolve import PlateSolveResult, SolverAvailability, astap_availability, astap_solve
from ssc.scheduler.executor import (
    ControlCommand,
    DeviceCommandError,
    DeviceCommandNotImplementedError,
    DeviceCommands,
    DeviceEventTimeoutError,
    DeviceStackSaveError,
    DeviceWindowEndedError,
    FrameCountError,
)
from ssc.scheduler.gates import TargetSnapshot
from ssc.settings.store import SettingsStore
from ssc.state.events import DeviceEventReceived, NoticeCleared, NoticeRaised
from ssc.state.models import CaptureStage, LocationState, Notice, NoticeLevel
from ssc.state.ports import DeviceGatewayError
from ssc.state.store import StateStore
from ssc.store.capture import CaptureRepository
from ssc.weather.base import ForecastSeries

_log = logging.getLogger(__name__)

#: The firmware every completion signal below was established against. Restated
#: here rather than imported from ``ssc.alpaca.capabilities`` for the reason
#: ``ssc/client/errors.py`` gives: ``ssc.api`` depends on the protocol, not on
#: the one driver that speaks it.
RECORDED_FIRMWARE: Final = "8.46"

#: How often a ``PollMember`` completion signal re-reads its member.
DEFAULT_DEVICE_POLL_INTERVAL_S: Final = 1.0
MOUNT_FRAME_READY_RETRIES: Final = 30
MOUNT_FRAME_READY_INTERVAL_S: Final = 1.0
PARK_STATE_READY_RETRIES: Final = 15
PARK_STATE_READY_INTERVAL_S: Final = 1.0
SCHEDULED_CAPTURE_RETRIES: Final = 2
SCHEDULED_CAPTURE_RETRY_DELAY_S: Final = 2.0

_NOT_IMPLEMENTED_NUMBERS: Final[frozenset[int]] = frozenset(
    {int(AlpacaErrorNumber.NOT_IMPLEMENTED), int(AlpacaErrorNumber.ACTION_NOT_IMPLEMENTED)}
)

_TRANSIENT_CAPTURE_MARKERS: Final[tuple[str, ...]] = (
    "connecttimeout",
    "readtimeout",
    "no response",
    "connection reset",
    "connection refused",
    "[0x407]",
)


def _retryable_capture_error(exc: DeviceCommandError) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in _TRANSIENT_CAPTURE_MARKERS)


async def _translate[T](call: Awaitable[T]) -> T:
    """Run one client call, converting its failures into the executor's vocabulary."""
    try:
        return await call
    except AlpacaRemoteError as exc:
        if exc.number in _NOT_IMPLEMENTED_NUMBERS:
            raise DeviceCommandNotImplementedError(f"{exc.member}: {exc.message}") from exc
        permanent = (
            exc.number
            in {
                int(AlpacaErrorNumber.INVALID_VALUE),
                int(AlpacaErrorNumber.VALUE_NOT_SET),
                int(AlpacaErrorNumber.SETTINGS_PROVIDER_ERROR),
            }
            or "no device address is configured" in exc.message.casefold()
        )
        raise DeviceCommandError(str(exc), recoverable=not permanent) from exc
    except AlpacaClientError as exc:
        raise DeviceCommandError(str(exc)) from exc


# --- DeviceCommands ------------------------------------------------------------ #


#: ASCOM ``DriveRates``. ``3`` (King) exists in the standard and not on this
#: device, so it is deliberately absent — see ``ssc/main.py``'s own copy, which
#: this mirrors rather than imports (that module is the app, not a library).
_ASCOM_DRIVE_RATES: Final[Mapping[str, int]] = {"sidereal": 0, "lunar": 1, "solar": 2}


@dataclass
class _MotionGuardLease:
    # Only same-task nested calls and the explicitly awaited autofocus child
    # share ownership. Arbitrary inherited ContextVars never grant permission.
    owner: asyncio.Task[object] | None
    active: bool = True


def _guard_camera_motion[**P, R](method: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    @wraps(method)
    async def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        commands = cast("AlpacaDeviceCommands", args[0])
        lease = commands._motion_guard_active.get()
        task = asyncio.current_task()
        inherited_owner = (lease is not None and lease.active
                           and (task is lease.owner or task is commands._autofocus_task))
        if inherited_owner or commands._before_motion is None:
            return await method(*args, **kwargs)
        async with commands._before_motion():
            lease = _MotionGuardLease(owner=task)
            token = commands._motion_guard_active.set(lease)
            try:
                return await method(*args, **kwargs)
            finally:
                lease.active = False
                commands._motion_guard_active.reset(token)
    return guarded


class AlpacaDeviceCommands:
    """``DeviceCommands`` over the driver ``ssc-core`` hosts.

    Every ``ssc.*`` action is sent to ``telescope/0`` — the driver answers them
    identically from any device. Standard members go to their own device, and
    that device is set ``Connected = True`` first if it is not already, exactly
    as any ASCOM client does before using a device; the driver refcounts the
    single link behind all four.

    **Slew and sync targets arrive here as J2000** — a compiled plan item, like
    every catalogue entry, resolved body and plate solve in SSC — and this
    mount may declare a different ``EquatorialSystem`` (``ssc/astro/frames.py``'s
    module docstring: ~20' for this scope, and a sync writes that error into
    the mount's pointing model, where it is durable). ``ssc.api.mount_target``
    does the same conversion ``DeviceService`` (``ssc/api/services.py``)
    applies to the Mount tab's own slew/sync — this is a second *caller* of
    that one orchestration, not a second conversion.
    """

    def __init__(
        self,
        client: AlpacaClient,
        *,
        state: StateStore,
        astro: AstroService,
        mount_frame_ready_retries: int = MOUNT_FRAME_READY_RETRIES,
        mount_frame_ready_interval_s: float = MOUNT_FRAME_READY_INTERVAL_S,
        park_state_ready_retries: int = PARK_STATE_READY_RETRIES,
        park_state_ready_interval_s: float = PARK_STATE_READY_INTERVAL_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        before_motion: Callable[[], contextlib.AbstractAsyncContextManager[None]] | None = None,
    ) -> None:
        self._before_motion = before_motion
        self._motion_guard_active = ContextVar[_MotionGuardLease | None]("camera_motion_guard", default=None)
        self._telescope = client.device("telescope")
        self._camera = client.device("camera")
        self._filterwheel = client.device("filterwheel")
        self._state = state
        self._astro = astro
        self._mount_frame_ready_retries = mount_frame_ready_retries
        self._mount_frame_ready_interval_s = mount_frame_ready_interval_s
        self._park_state_ready_retries = park_state_ready_retries
        self._park_state_ready_interval_s = park_state_ready_interval_s
        self._sleep = sleep
        self._autofocus_task: asyncio.Task[None] | None = None
        self._autofocus_runner: Callable[[], Awaitable[None]] | None = None
        self._polar_alignment_runner: Callable[..., Awaitable[None]] | None = None
        self._calibration_runner: Callable[..., Awaitable[None]] | None = None

    def set_polar_alignment_runner(self, runner: Callable[..., Awaitable[None]]) -> None:
        self._polar_alignment_runner = runner

    def set_autofocus_runner(self, runner: Callable[[], Awaitable[None]]) -> None:
        self._autofocus_runner = runner

    def set_calibration_runner(self, runner: Callable[..., Awaitable[None]]) -> None:
        self._calibration_runner = runner

    async def polar_alignment(self, **args: object) -> None:
        if self._polar_alignment_runner is None:
            raise DeviceCommandError("polar-alignment scheduler action is unavailable")
        await self.unpark()
        await self._polar_alignment_runner(**args)

    async def _to_mount_frame(
        self, *, ra_j2000_h: float, dec_j2000_deg: float
    ) -> tuple[float, float]:
        for attempt in range(self._mount_frame_ready_retries + 1):
            mount = self._state.snapshot.mount
            try:
                return resolve_mount_target(
                    mount=mount,
                    ephemeris=self._astro.ephemeris,
                    ephemeris_error=self._astro.ephemeris_error,
                    ra_hours=ra_j2000_h,
                    dec_deg=dec_j2000_deg,
                    when=datetime.now(tz=UTC),
                )
            except MountFrameUnavailableError as exc:
                device = self._state.snapshot.device
                metadata_pending = (
                    device is not None
                    and device.reachable
                    and (
                        mount is None
                        or mount.equatorial_system is None
                        or (
                            mount.equatorial_system is not MountFrame.J2000
                            and (
                                mount.site_latitude_deg is None or mount.site_longitude_deg is None
                            )
                        )
                    )
                )
                if not metadata_pending or attempt >= self._mount_frame_ready_retries:
                    raise DeviceCommandError(str(exc)) from exc
                if attempt == 0:
                    _log.info(
                        "waiting for mount frame metadata after reconnect before issuing target"
                    )
                await self._sleep(self._mount_frame_ready_interval_s)
        raise AssertionError("mount-frame retry loop exhausted without returning or raising")

    async def _ensure_connected(self, device: AlpacaDeviceClient) -> None:
        if not await _translate(device.is_connected()):
            await _translate(device.connect())

    async def _action(self, name: str, parameters: Mapping[str, object] | None = None) -> None:
        await self._ensure_connected(self._telescope)
        await _translate(self._telescope.action(name, parameters))

    async def _put(self, device: AlpacaDeviceClient, member: str, **params: object) -> None:
        await self._ensure_connected(device)
        await _translate(device.put(member, **params))

    @_guard_camera_motion
    async def startup(self) -> None:
        # ``iscope_start_view`` is the verified precondition for a goto (the
        # driver's telescope docstring): it opens the imaging pipeline. Nothing
        # on 8.46 raises the arm — that stays the user's job at the scope.
        await self._action("ssc.start_view", {"mode": "star"})

    async def _unpark_for_imaging(self) -> None:
        mount = self._state.snapshot.mount
        if mount is not None and mount.at_park is True:
            await self._ensure_connected(self._telescope)
        if mount is not None and (
            (mount.at_park is True and await _translate(self._telescope.get("atpark")) is True)
            or (mount.at_home is True and mount.capabilities is not None
                and mount.capabilities.can_unpark is True)
        ):
            await self.unpark()

    @_guard_camera_motion
    async def set_tracking_rate(self, rate: str) -> None:
        """ASCOM ``Telescope.TrackingRate``, the standard member for exactly this
        (invariant 2). The device offers sidereal/lunar/solar and nothing else, so
        an unknown name is refused rather than silently left on the current rate:
        a target tracked at the wrong rate drifts for a reason nothing explains.
        """
        code = _ASCOM_DRIVE_RATES.get(rate)
        if code is None:
            raise DeviceCommandError(
                f"tracking rate {rate!r} is not one the device offers "
                f"({', '.join(sorted(_ASCOM_DRIVE_RATES))})"
            )
        await self._unpark_for_imaging()
        await self._put(self._telescope, "trackingrate", TrackingRate=code)

    @_guard_camera_motion
    async def set_tracking(self, enabled: bool) -> None:
        await self._put(self._telescope, "tracking", Tracking=enabled)

    @_guard_camera_motion
    async def slew_to(self, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        await self._unpark_for_imaging()
        device_ra_h, device_dec_deg = await self._to_mount_frame(
            ra_j2000_h=ra_j2000_h, dec_j2000_deg=dec_j2000_deg
        )
        await self._put(
            self._telescope,
            "slewtocoordinatesasync",
            RightAscension=device_ra_h,
            Declination=device_dec_deg,
        )

    async def start_stack(
        self, *, target_name: str | None, ra_j2000_h: float, dec_j2000_deg: float
    ) -> None:
        # ``start_stack`` is unverified on 8.46 and its parameter shape has never
        # been observed, so none is sent: the driver refuses the verb before it
        # would look at parameters. When the verb is verified and gains a
        # schema, pass the target here — do not let it stay an empty call.
        del target_name, ra_j2000_h, dec_j2000_deg
        await self._action("ssc.start_stack")

    async def stop_stack(self) -> None:
        await self._action("ssc.stop_stack")

    @_guard_camera_motion
    async def autofocus(self) -> None:
        runner = self._autofocus_runner
        if runner is None:
            await self._action("ssc.start_auto_focus")
            return
        async def run_autofocus() -> None:
            await runner()

        task = asyncio.create_task(run_autofocus())
        self._autofocus_task = task
        try:
            await task
        except asyncio.CancelledError as exc:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            raise DeviceCommandError("autofocus interrupted by Stop", recoverable=False) from exc
        finally:
            self._autofocus_task = None

    @property
    def autofocus_running(self) -> bool:
        return self._autofocus_task is not None and not self._autofocus_task.done()

    async def stop_autofocus(self) -> None:
        task = self._autofocus_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    @_guard_camera_motion
    async def set_filter(self, position: str) -> None:
        await self._ensure_connected(self._filterwheel)
        index = await self._filter_index(position)
        await _translate(self._filterwheel.put("position", Position=index))
        deadline = asyncio.get_running_loop().time() + 120.0
        while True:
            actual = await _translate(self._filterwheel.get("position"))
            if type(actual) is not int:
                raise DeviceCommandError(f"FilterWheel.Position returned {actual!r}, not an integer; {position!r} unverified")
            if actual == index:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise DeviceCommandError(
                    f"filter wheel did not report slot {position!r} before the 120 s deadline"
                )
            await asyncio.sleep(DEFAULT_DEVICE_POLL_INTERVAL_S)

    async def _filter_index(self, position: str) -> int:
        """ASCOM ``FilterWheel.Position`` is an index; a plan names the slot
        (``"Dark"``) or gives the index as text — both are resolved against the
        wheel's own ``Names``."""
        names = await _translate(self._filterwheel.get("names"))
        if not isinstance(names, list):
            raise DeviceCommandError("FilterWheel.Names did not return a list")
        labels = [str(name) for name in names]
        folded = [label.casefold() for label in labels]
        if position.casefold() in folded:
            return folded.index(position.casefold())
        if position.isdigit() and int(position) < len(labels):
            return int(position)
        raise DeviceCommandError(
            f"filter position {position!r} is not one of the wheel's slots {labels}"
        )

    async def set_heater(self, on: bool) -> None:
        await self._action("ssc.set_heater", {"on": on})

    async def set_exposure(self, exposure_sec: float) -> None:
        await self._action("ssc.set_exposure", {"exposure_sec": exposure_sec})

    async def set_gain(self, gain: int) -> None:
        await self._put(self._camera, "gain", Gain=gain)

    async def set_dither(self, dither_pixels: float | None) -> None:
        await self._action("ssc.set_dither", {"dither_pixels": dither_pixels})

    async def solve(self) -> None:
        await self._action("ssc.start_solve")

    async def sync_to(self, ra_j2000_h: float | None, dec_j2000_deg: float | None) -> None:
        if ra_j2000_h is None or dec_j2000_deg is None:
            raise DeviceCommandNotImplementedError(
                "sync to the last plate solve: the solve-result payload has not been "
                f"observed on firmware {RECORDED_FIRMWARE}, so SSC cannot read coordinates "
                "out of it; give the sync item explicit coordinates"
            )
        # A sync writes straight into the mount's pointing model, so an
        # unconverted target here is not recoverable the way a wrong slew is
        # (class docstring) — this goes through the same conversion as slew
        # rather than a trusted-caller shortcut.
        device_ra_h, device_dec_deg = await self._to_mount_frame(
            ra_j2000_h=ra_j2000_h, dec_j2000_deg=dec_j2000_deg
        )
        await self._put(
            self._telescope,
            "synctocoordinates",
            RightAscension=device_ra_h,
            Declination=device_dec_deg,
        )

    async def ensure_safety_parked(self) -> None:
        """Dawn interlock: stop activity and independently verify physical Park.

        Each action remains bounded and failures do not suppress later stop/park
        attempts. Cached Home/AtPark and command acknowledgements are not proof.
        Filter protection is independently attempted by the safety supervisor.
        """
        errors: list[str] = []

        async def attempt(stage: str, action: Callable[[], Awaitable[object]], timeout_s: float) -> None:
            _log.info("Dawn safety stage=%s", stage)
            try:
                async with asyncio.timeout(timeout_s):
                    await action()
            except Exception as exc:
                error = f"{stage}: {type(exc).__name__}: {exc}"
                errors.append(error)
                _log.warning("Dawn safety %s", error)

        await attempt("autofocus_stop", self.stop_autofocus, 5)
        await attempt("tracking_stop", lambda: self._put(self._telescope, "tracking", Tracking=False), 5)
        await attempt("slew_abort", lambda: self._put(self._telescope, "abortslew"), 5)
        await attempt("camera_abort", lambda: _translate(self._camera.put("abortexposure")), 5)
        await attempt("park", self.park, 15)
        latest = "fresh mount readback unavailable"
        try:
            async with asyncio.timeout(20):
                for index in range(self._park_state_ready_retries + 1):
                    try:
                        values = []
                        for member in ("atpark", "slewing", "tracking"):
                            async with asyncio.timeout(5):
                                values.append(await _translate(self._telescope.get(member)))
                        atpark, slewing, tracking = values
                        latest = f"AtPark={atpark!r}; Slewing={slewing!r}; Tracking={tracking!r}"
                        if atpark is True and slewing is False and tracking is False:
                            _log.info("Dawn safety physical Park freshly verified: %s", latest)
                            return
                    except Exception as exc:
                        latest = f"fresh readback failed: {type(exc).__name__}: {exc}"
                    if index < self._park_state_ready_retries:
                        await self._sleep(self._park_state_ready_interval_s)
        except TimeoutError:
            latest += "; fresh verification exceeded 20 seconds"
        details = "; ".join([*errors, latest])
        raise DeviceCommandError(f"Dawn park unverified: {details}")

    @_guard_camera_motion
    async def park(self) -> str | None:
        try:
            await self._put(self._telescope, "park")
        except DeviceCommandError as exc:
            # Seestar firmware refuses Park with 0x4ff/1279 once FindHome has
            # already put the arm below the horizon.  That refusal is safe and
            # idempotent only when fresh telemetry independently proves the
            # final shutdown state; the same message anywhere else remains a
            # real failure.
            message = str(exc).casefold()
            below_horizon_refusal = "below the horizon" in message and (
                "0x4ff" in message or "1279" in message
            )
            if not below_horizon_refusal:
                raise
            for attempt in range(self._park_state_ready_retries + 1):
                mount = self._state.snapshot.mount
                safe = (
                    mount is not None
                    and not mount.stale
                    and (mount.at_home is True or mount.at_park is True)
                    and mount.slewing is False
                    and mount.tracking is False
                )
                if safe:
                    assert mount is not None
                    if mount.at_park is True:
                        self._state.apply(NoticeCleared(notice_id="park-safe-home"))
                        return None
                    message = "safe Home confirmed; physical Park was not confirmed"
                    _log.warning("%s", message)
                    self._state.apply(NoticeRaised(notice=Notice(
                        id="park-safe-home", level=NoticeLevel.WARNING,
                        code="park_safe_home", message=message,
                        raised_at=datetime.now(UTC),
                    )))
                    return message
                if attempt >= self._park_state_ready_retries:
                    raise
                if attempt == 0:
                    _log.info(
                        "waiting for fresh safe mount telemetry after Seestar's "
                        "below-horizon Park refusal"
                    )
                await self._sleep(self._park_state_ready_interval_s)
        self._state.apply(NoticeCleared(notice_id="park-safe-home"))
        return None

    @_guard_camera_motion
    async def unpark(self) -> None:
        await self._put(self._telescope, "unpark")

    @_guard_camera_motion
    async def find_home(self) -> None:
        await self._put(self._telescope, "findhome")

    async def capture_calibration(self, **args: object) -> None:
        if self._calibration_runner is None:
            raise DeviceCommandNotImplementedError(
                "not an SSC action until the shared calibration service is attached"
            )
        await self._calibration_runner(**args)

    @_guard_camera_motion
    async def shutdown(self) -> None:
        await self._action("ssc.shutdown")


# --- DeviceEvents ----------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class OnReturn:
    """The command's own return *is* the completion: the verb acknowledges
    synchronously and pushes nothing afterwards (``view_verbs.jsonl``)."""


@dataclass(frozen=True, slots=True)
class PollMember:
    """Complete once ``device_type.member`` satisfies ``done_when`` — the ASCOM
    contract for an async operation (``SlewToCoordinatesAsync`` -> ``Slewing``)."""

    device_type: str
    member: str
    done_when: Callable[[object], bool]


@dataclass(frozen=True, slots=True)
class DeviceEventNamed:
    """Complete when the device pushes an event whose name is in ``names``,
    observed through ``ssc.state``'s ``DeviceEventReceived``."""

    names: frozenset[str]


CompletionSignal = OnReturn | PollMember | DeviceEventNamed


def _not_slewing(value: object) -> bool:
    return value is False


#: What "done" means for each ``event_kind`` the executor waits on, as
#: established on the recorded firmware. Kinds absent here (``autofocus_complete``,
#: ``solve_complete``, ``calibration_complete``) have no observed completion
#: signal and their commands are themselves unverified; ``wait_for`` refuses
#: them rather than inventing one.
#:
#: ``frame_saved`` is ``OnReturn`` rather than a pushed event, and that is a
#: deliberate reading of firmware 8.46's silence, not an oversight: unlike a
#: slew or a solve, nothing here is commanded and then polled at arm's length
#: — ``AlpacaCapturePort.expose_frame`` (below) *is* the exposure pipeline,
#: and it does not return until ``ssc.capture.pipeline.capture_frame`` has
#: flipped the catalogue row to ``'complete'`` (fsynced and renamed into
#: place). By the time ``_dispatch`` gets a return to move past, the frame is
#: already exactly as durable as it will ever be — there is nothing left to
#: wait for, and firmware 8.46 pushes nothing to wait for anyway.
FIRMWARE_8_46_SIGNALS: Final[Mapping[str, CompletionSignal]] = {
    "startup_complete": OnReturn(),
    "slew_complete": PollMember("telescope", "slewing", _not_slewing),
    "home_complete": PollMember("telescope", "athome", lambda value: value is True),
    "park_complete": PollMember("telescope", "atpark", lambda value: value is True),
    "frame_saved": OnReturn(),
}


class AlpacaDeviceEvents:
    """``DeviceEvents`` — see the module docstring for why this polls."""

    def __init__(
        self,
        client: AlpacaClient,
        state: StateStore,
        *,
        signals: Mapping[str, CompletionSignal] = FIRMWARE_8_46_SIGNALS,
        poll_interval_s: float = DEFAULT_DEVICE_POLL_INTERVAL_S,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._state = state
        self._signals = signals
        self._poll_interval_s = poll_interval_s
        self._now = now or (lambda: datetime.now(tz=UTC))

    async def wait_for(self, event_kind: str, *, item_id: str, deadline: datetime) -> None:
        signal = self._signals.get(event_kind)
        if signal is None:
            raise DeviceCommandNotImplementedError(
                f"no completion signal for {event_kind!r} is known on firmware "
                f"{RECORDED_FIRMWARE}; SSC will not guess that the operation finished"
            )
        if isinstance(signal, OnReturn):
            return
        if isinstance(signal, PollMember):
            await self._poll(signal, event_kind, item_id, deadline)
            return
        await self._await_event(signal, event_kind, item_id, deadline)

    async def _poll(
        self, signal: PollMember, event_kind: str, item_id: str, deadline: datetime
    ) -> None:
        device = self._client.device(signal.device_type)
        while True:
            value = await _translate(device.get(signal.member))
            if signal.done_when(value):
                return
            if event_kind == "park_complete":
                mount = self._state.snapshot.mount
                # Park() already accepts this native below-horizon final state.
                # Use the same independent proof for completion, without
                # fabricating an AtPark flag that the device did not report.
                if (mount is not None and mount.polled_at is not None and not mount.stale and mount.at_home is True
                    and mount.tracking is False and mount.slewing is False
                    and 0 <= (self._now() - mount.polled_at).total_seconds() <= 5):
                    return
            remaining = (deadline - self._now()).total_seconds()
            if remaining <= 0:
                raise DeviceEventTimeoutError(
                    f"{event_kind} for item {item_id!r}: {signal.device_type}.{signal.member} "
                    f"still {value!r} at the deadline"
                )
            await asyncio.sleep(min(self._poll_interval_s, remaining))

    async def _await_event(
        self, signal: DeviceEventNamed, event_kind: str, item_id: str, deadline: datetime
    ) -> None:
        subscription = self._state.subscribe()
        try:
            while True:
                remaining = (deadline - self._now()).total_seconds()
                if remaining <= 0:
                    raise DeviceEventTimeoutError(
                        f"{event_kind} for item {item_id!r}: none of {sorted(signal.names)} "
                        "arrived before the deadline"
                    )
                try:
                    item = await asyncio.wait_for(subscription.queue.get(), timeout=remaining)
                except TimeoutError:
                    continue
                if subscription.overflowed:
                    raise DeviceCommandError(
                        f"{event_kind} for item {item_id!r}: the device event feed overflowed "
                        "while waiting, so a completion may have been missed"
                    )
                event = item.event
                if isinstance(event, DeviceEventReceived) and event.event.name in signal.names:
                    return
        finally:
            self._state.unsubscribe(subscription)


# --- CapturePort -------------------------------------------------------------- #


#: Passed to ``capture_one_exposure`` when the configured capture folder
#: template has no ``%DATEOFCAPTURE%`` tag — mirrors
#: ``ssc.api.capture_orchestrator``'s own placeholder for the same reason: a
#: real ``ZoneInfo`` is required by the call's own type, but nothing renders
#: through it in that case.
_UNUSED_CAPTURE_TZ: Final = ZoneInfo("UTC")


def _ssc_version() -> str:
    try:
        return importlib.metadata.version("ssc")
    except importlib.metadata.PackageNotFoundError:
        return "dev"


def _resolve_capture_timezone(settings: SettingsStore, template: str) -> ZoneInfo:
    """Mirrors ``ssc.api.capture_orchestrator.CaptureService``'s own
    ``_resolve_template_timezone`` — restated rather than imported, since that
    method is private to a class built around the *manual* capture lock this
    adapter deliberately does not share (see :class:`AlpacaCapturePort`)."""
    if "%DATEOFCAPTURE%" not in template:
        return _UNUSED_CAPTURE_TZ
    location = settings.active_location()
    if location is None:
        raise DeviceCommandError(
            "the capture folder template uses %DATEOFCAPTURE%, which needs the active "
            "observing location's timezone: set one in Settings > Location before "
            "scheduling a take_exposures block, or edit the template to remove that tag"
        )
    try:
        return resolve_timezone(location)
    except PlanTimezoneUnknownError as exc:
        raise DeviceCommandError(str(exc)) from exc


class AlpacaCapturePort:
    """``CapturePort`` (``ssc.scheduler.executor``) over the same exposure
    pipeline ``POST /api/capture/exposure`` drives
    (``ssc.api.capture_service.capture_one_exposure``) — adapted, not
    reimplemented, for the scheduler's own seam.

    **One instance per run**, built by ``RunService._launch`` and carrying
    that run's own ``run_id`` as its own state: :meth:`expose_frame`'s
    signature (``ssc.scheduler.executor.CapturePort``, owned by
    ``scheduler-engine``) has no ``run_id`` parameter, only ``block_id`` — the
    plan item's own id — so the run id has to come from somewhere else. This
    mirrors ``EphemerisTargetResolver``/``PublishingJournal``, both also built
    fresh per launch rather than threaded through every call.

    **Where "durably catalogued" is decided.** ``ssc.capture.pipeline.capture_frame``
    inserts a ``'writing'`` row — via ``CaptureRepository.begin_frame``, tagged
    with this run/block right there — *before* any FITS byte reaches disk, and
    flips it to ``'complete'`` only once the file is fsynced and renamed into
    place (CLAUDE.md invariant 6). :meth:`expose_frame` does not return until
    that flip has happened, so by the time it returns the frame is exactly as
    durable as :meth:`frames_captured` will ever see it. That commit boundary
    is also why ``"frame_saved"`` is mapped to ``OnReturn`` in
    :data:`FIRMWARE_8_46_SIGNALS` above rather than a pushed event.

    ``gain``/``filter_name`` arrive in :meth:`expose_frame`'s args for the
    journal's own record of intent, but are not re-applied to the device here:
    the exposure block's compiled phases (``SET_GAIN``/``SET_FILTER``,
    DESIGN.md 4.3.0) already put the device in that state before the block's
    first frame, every time, and the FITS header is authored from what the
    device actually reports at exposure time
    (``ssc.api.capture_service``'s own "never guessed" rule), not from what
    the plan asked for.
    """

    def __init__(
        self,
        *,
        run_id: str,
        device_commands: DeviceCommands,
        camera: CameraDevice | None,
        wide_camera: CameraDevice | None = None,
        companion_enabled: bool = False,
        telescope: Device | None,
        filterwheel: Device | None,
        focuser: Device | None = None,
        repo: CaptureRepository,
        settings: SettingsStore,
        state: StateStore,
        recenter: Callable[[float, float], Awaitable[None]] | None = None,
        solve: Callable[[Path], Awaitable[PlateSolveResult]] = astap_solve,
        availability: Callable[[], Awaitable[SolverAvailability]] = astap_availability,
        reset_live_stack: Callable[[Path], object] | None = None,
        add_live_stack_frame: Callable[[int], Awaitable[object]] | None = None,
        stop_live_stack: Callable[..., Awaitable[object]] | None = None,
        restore_live_stack: Callable[..., Awaitable[bool]] | None = None,
        dither: Callable[[float, float, str | None], Awaitable[object]] | None = None,
        recover_dither: Callable[[str | None], Awaitable[object]] | None = None,
        recover_operation: Callable[[int, str], Awaitable[object]] | None = None,
        before_capture: Callable[[], Awaitable[None]] | None = None,
        begin_capture: Callable[..., object] | None = None,
        update_capture_stage: Callable[[CaptureStage], object] | None = None,
        complete_capture: Callable[[int], object] | None = None,
        fail_capture: Callable[[Exception], object] | None = None,
        focus_quality: Callable[[], tuple[Path, float | None, int] | None] | None = None,
        ephemeris: Ephemeris | None = None,
        metadata_capabilities: Callable[[], set[str]] | None = None,
        set_dither_center: Callable[[float, float], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._device_commands = device_commands
        self._camera = camera
        self._wide_camera = wide_camera
        self._companion_enabled = companion_enabled
        self._telescope = telescope
        self._filterwheel = filterwheel
        self._focuser = focuser
        self._repo = repo
        self._settings = settings
        self._state = state
        self._recenter = recenter
        configured_solver = ConfiguredAstap(settings)
        self._solve = configured_solver.solve if solve is astap_solve else solve
        self._availability = (configured_solver.availability
                              if availability is astap_availability else availability)
        self._reset_live_stack = reset_live_stack
        self._add_live_stack_frame = add_live_stack_frame
        self._stop_live_stack = stop_live_stack
        self._restore_live_stack = restore_live_stack
        self._stack_panel_context: tuple[int | None, int | None] = (None, None)
        self._dither = dither
        self._recover_dither = recover_dither
        self._recover_operation = recover_operation
        self._unsupported_metadata_members: set[str] = set()
        self._metadata_capabilities = metadata_capabilities
        self._set_dither_center = set_dither_center
        self._before_capture = before_capture
        self._begin_capture = begin_capture
        self._update_capture_stage = update_capture_stage
        self._complete_capture = complete_capture
        self._fail_capture = fail_capture
        self._focus_quality = focus_quality
        self._focus_monitor = ScheduledFocusMonitor()
        self._ephemeris = ephemeris
        self._scheduled_live_stack_active = False
        self._center_reference: PlateSolveResult | None = None
        self._dithered_since_reference = False
        self._dither_group_namespace = uuid4()
        self._dither_group_index = 0

    def _publish_capture_stage(self, stage: CaptureStage) -> None:
        assert self._update_capture_stage is not None
        self._update_capture_stage(stage)

    async def expose_frame(
        self,
        *,
        exposure_sec: float,
        gain: int,
        frame_type: str,
        filter_name: str | None,
        block_id: str,
        frame_index: int,
        retain: bool = True,
        center_after_drift: bool = False,
        drift_threshold_pixels: float = 400.0,
        object_name: str | None = None,
        live_stack: bool = False,
        dither_every_n_frames: int | None = None,
    ) -> None:
        # Already applied by this block's own SET_GAIN/SET_FILTER phases —
        # see the class docstring.
        del filter_name
        if live_stack and self._restore_live_stack is not None:
            await self._restore_live_stack(self._run_id, block_id, *self._stack_panel_context,
                                           prepare_only=True)
        if self._camera is None:
            raise DeviceCommandNotImplementedError(
                "this ssc-core hosts no camera, so a scheduled exposure cannot be shot"
            )
        folder = self._settings.capture.folder
        if folder is None:
            raise DeviceCommandNotImplementedError(
                "no capture folder is configured; set one in Settings > Capture before "
                "scheduling a take_exposures block"
            )
        template = self._settings.capture.folder_template
        filename_template = self._settings.capture.filename_template
        tz = _resolve_capture_timezone(self._settings, f"{template}/{filename_template}")
        try:
            resolved_frame_type = FrameType(frame_type)
        except ValueError as exc:
            raise DeviceCommandError(f"unknown frame type {frame_type!r}: {exc}") from exc
        companion = await self._repo.freeze_run_companion(self._run_id, self._companion_enabled)
        paired = companion and resolved_frame_type is FrameType.LIGHT
        if paired and self._wide_camera is None:
            raise DeviceCommandNotImplementedError("Wide companion camera is unavailable")
        if paired:
            tz = _resolve_capture_timezone(self._settings, "%TARGET%/%DATEOFCAPTURE%")
        device = self._state.snapshot.device
        context = ExposureContext(
            object_name=object_name,
            dither_group_id=(
                f"{uuid5(self._dither_group_namespace, block_id).hex}:{self._dither_group_index}"
                if resolved_frame_type is FrameType.LIGHT and dither_every_n_frames is not None
                else None
            ),
            # No persisted setting carries this yet (architecture policy invariant 7:
            # absent, never fabricated) — mirrors
            # ``ssc.api.capture_orchestrator.CaptureService._context``.
            focal_length_mm=None,
            telescope_name=self._settings.device.label,
            ssc_version=_ssc_version(),
            device_firmware=None if device is None else device.firmware,
            ephemeris=self._ephemeris,
            **exposure_metadata_fallbacks(
                self._state.snapshot.mount,
                self._settings.active_location(),
                now=datetime.now(tz=UTC),
            ),
        )
        owned = False
        try:
            await self.recover_dual_capture()
            if self._before_capture is not None:
                await _translate(self._before_capture())
            owned = True
            if self._begin_capture is not None:
                self._begin_capture(
                    duration_s=exposure_sec,
                    gain=gain,
                    object_name=object_name,
                    frame_type=resolved_frame_type,
                )
            for capture_attempt in range(SCHEDULED_CAPTURE_RETRIES + 1):
                try:
                    if paired:
                        assert self._wide_camera is not None
                        try:
                            frame_id = await capture_pair(
                                tele=self._camera, wide=self._wide_camera, telescope=self._telescope,
                                filterwheel=self._filterwheel, focuser=self._focuser, repo=self._repo,
                                run_id=self._run_id, block_id=block_id, frame_index=frame_index,
                                duration_s=exposure_sec, gain=gain, context=context, folder=folder,
                                filename_template=filename_template, tz=tz,
                            )
                        except DualCaptureError as exc:
                            raise DeviceCommandError(str(exc)) from exc
                    else:
                        frame_id = await _translate(
                            capture_one_exposure(
                                camera=self._camera,
                                telescope=self._telescope,
                                filterwheel=self._filterwheel,
                                focuser=self._focuser,
                                repo=self._repo,
                                folder=folder,
                                duration_s=exposure_sec,
                                context=context,
                                frame_type=resolved_frame_type,
                                folder_template=template,
                                filename_template=filename_template,
                                tz=tz,
                                # 0-based and stable across a resume (unlike a
                                # process-local counter): frame 37 of a resumed block is
                                # still asked for as ``frame_index=37``.
                                sequence=frame_index,
                                run_id=self._run_id,
                                block_id=block_id,
                                on_stage=(
                                    None
                                    if self._update_capture_stage is None
                                    else self._publish_capture_stage
                                ),
                                unsupported_members=(self._metadata_capabilities() if self._metadata_capabilities is not None
                                                     else self._unsupported_metadata_members),
                            )
                        )
                    break
                except DeviceCommandError as exc:
                    if capture_attempt >= SCHEDULED_CAPTURE_RETRIES or not _retryable_capture_error(
                        exc
                    ):
                        raise
                    # Leave retries to the durable executor. Its admission gate
                    # must first recover the ambiguously dispatched camera.
                    raise
            if self._complete_capture is not None:
                self._complete_capture(frame_id)
            owned = False  # Saved exposure completed; later consumer errors own no camera.
            if live_stack:
                try:
                    if self._reset_live_stack is None or self._add_live_stack_frame is None:
                        raise DeviceCommandNotImplementedError(
                            "scheduled live stacking is unavailable from this capture adapter"
                        )
                    if not self._scheduled_live_stack_active:
                        record = await self._repo.get_frame(frame_id)
                        restored = await self.restore_live_stack(block_id)
                        if not restored:
                            self._reset_live_stack(record.path)
                        self._scheduled_live_stack_active = True
                    await self._add_live_stack_frame(frame_id)
                except Exception as exc:
                    # Stacking is an optional consumer of an already saved frame.
                    # Its lifecycle or processing failure must not fail capture.
                    _log.warning("live stack skipped saved frame %d: %s", frame_id, exc)
            if not retain:
                record = await self._repo.get_frame(frame_id)
                await self._repo.delete_complete_frame(frame_id)
                with contextlib.suppress(FileNotFoundError):
                    await asyncio.to_thread(record.path.unlink)
            if center_after_drift and resolved_frame_type is FrameType.LIGHT:
                availability = await self._availability()
                if availability.available:
                    record = await self._repo.get_frame(frame_id)
                    solved = await self._solve(record.path)
                    if self._center_reference is None or self._dithered_since_reference:
                        self._center_reference = solved
                        self._dithered_since_reference = False
                    else:
                        drift_pixels = (
                            frame_offset_arcsec(self._center_reference.coord, solved.coord)
                            / solved.pixel_scale_arcsec
                        )
                        if drift_pixels > drift_threshold_pixels:
                            if self._recenter is None:
                                raise DeviceCommandNotImplementedError(
                                    "closed-loop centering is unavailable for this capture adapter"
                                )
                            await self._recenter(
                                self._center_reference.coord.ra_h,
                                self._center_reference.coord.dec_deg,
                            )
        except ExposureNotAvailableError as exc:
            if owned and self._fail_capture is not None:
                self._fail_capture(exc)
            raise DeviceCommandNotImplementedError(str(exc)) from exc
        except (CaptureAlreadyRunningError, ExposureTimeoutError, CaptureMetadataError, FitsWriteError) as exc:
            if owned and self._fail_capture is not None:
                self._fail_capture(exc)
            raise DeviceCommandError(str(exc)) from exc
        except Exception as exc:
            if owned and self._fail_capture is not None:
                self._fail_capture(exc)
            raise

    def set_stack_panel_context(self, panel_index: int | None, start_index: int | None) -> None:
        if self._stack_panel_context[0] != panel_index or not self._scheduled_live_stack_active:
            self._stack_panel_context = (panel_index, start_index)

    async def restore_live_stack(self, block_id: str, panel_index: int | None = None) -> bool:
        if self._restore_live_stack is None:
            return False
        if self._stack_panel_context == (None, None):
            self._stack_panel_context = (panel_index, None)
        restored = await self._restore_live_stack(self._run_id, block_id, *self._stack_panel_context)
        self._scheduled_live_stack_active = restored
        return restored

    async def stop_live_stack(self, *, require_retained: bool = False) -> None:
        # Stop/park is deliberately safe even when the run is waiting or the
        # process resumed without an in-memory stack to save.
        if self._stop_live_stack is None:
            if require_retained:
                raise DeviceStackSaveError("retained integration cannot be saved: stack adapter unavailable")
            if not self._scheduled_live_stack_active:
                return
            raise DeviceCommandNotImplementedError(
                "scheduled live stacking is unavailable from this capture adapter"
            )
        try:
            if require_retained:
                await self._stop_live_stack(require_retained=True)
            else:
                await self._stop_live_stack()
        except Exception as exc:
            raise DeviceStackSaveError(f"live-stack save failed; integration retained: {exc}") from exc
        self._scheduled_live_stack_active = False

    async def center_target(self, *, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        await self.recover_dual_capture()
        if self._recenter is None:
            raise DeviceCommandNotImplementedError(
                "closed-loop centering is unavailable from this capture adapter"
            )
        try:
            await self._recenter(ra_j2000_h, dec_j2000_deg)
        except DeviceCommandNotImplementedError:
            raise
        except Exception as exc:
            raise DeviceCommandError(f"closed-loop target centering failed: {exc}") from exc
        if self._set_dither_center is not None:
            self._set_dither_center(ra_j2000_h, dec_j2000_deg)
        self._dither_group_index += 1
        self._focus_monitor = ScheduledFocusMonitor()
        previous_quality = None if self._focus_quality is None else self._focus_quality()
        if previous_quality is not None:
            self._focus_monitor.last_frame = previous_quality[0]

    async def focus_required(self) -> bool:
        quality = None if self._focus_quality is None else self._focus_quality()
        if quality is not None:
            was_pending = self._focus_monitor.pending
            pending = self._focus_monitor.observe(*quality)
            if pending and not was_pending:
                self._state.apply(
                    NoticeRaised(
                        notice=Notice(
                            id="scheduled-focus-recovery",
                            level=NoticeLevel.WARNING,
                            code="scheduled_focus_recovery",
                            message="Three consecutive frames have weak stars or degraded HFR. The plan is trying autofocus and will wait if clouds prevent a reliable measurement.",
                            raised_at=datetime.now(UTC),
                        )
                    )
                )
            return pending
        return self._focus_monitor.pending

    async def autofocus(self) -> None:
        await self.recover_dual_capture()
        await self._device_commands.autofocus()
        self._focus_monitor.focused()

    async def recover_operation(self, *, level: int, operation: str) -> None:
        await self.recover_dual_capture()
        if self._recover_operation is None:
            raise DeviceCommandNotImplementedError("scheduled operation recovery unavailable")
        try:
            await _translate(self._recover_operation(level, operation))
        except CaptureDitherWindowEndedError as exc:
            raise DeviceWindowEndedError(str(exc), recoverable=False) from exc
        except (CaptureAlreadyRunningError, CaptureDitherSettleError,
                CaptureDeviceUnavailableError, DeviceGatewayError, TimeoutError) as exc:
            raise DeviceCommandError(str(exc)) from exc

    async def recover_dither(self, *, object_name: str | None) -> None:
        await self.recover_dual_capture()
        if self._recover_dither is None:
            raise DeviceCommandNotImplementedError("ambiguous dither image recovery unavailable")
        try:
            await self._recover_dither(object_name)
        except CaptureDitherWindowEndedError as exc:
            raise DeviceWindowEndedError(str(exc), recoverable=False) from exc
        except CaptureDitherSettleError as exc:
            raise DeviceCommandError(str(exc)) from exc
        self._dithered_since_reference = True
        self._dither_group_index += 1

    async def dither(
        self, *, pixels: float | None, max_pixels: float | None, object_name: str | None
    ) -> None:
        await self.recover_dual_capture()
        if pixels is None or max_pixels is None:
            raise DeviceCommandNotImplementedError(
                "scheduled dithering requires an explicit pixel amount and maximum"
            )
        if self._dither is None:
            raise DeviceCommandNotImplementedError(
                "verified absolute-slew dithering is unavailable from this capture adapter"
            )
        try:
            await self._dither(pixels, max_pixels, object_name)
        except CaptureDitherWindowEndedError as exc:
            raise DeviceWindowEndedError(str(exc), recoverable=False) from exc
        except CaptureDitherSettleError as exc:
            raise DeviceCommandError(str(exc)) from exc
        self._dithered_since_reference = True
        self._dither_group_index += 1

    async def recover_dual_capture(self) -> None:
        try:
            await recover_dual_ownership(repo=self._repo, run_id=self._run_id,
                                         tele=self._camera, wide=self._wide_camera)
        except Exception as exc:
            raise DeviceCommandError(f"companion cleanup must complete before motion/acquisition: {exc}") from exc

    async def frames_captured(self, *, run_id: str, item_id: str) -> int:
        try:
            await self.recover_dual_capture()
            return await self._repo.count_committed(run_id=run_id, block_id=item_id)
        except (aiosqlite.Error, DeviceCommandError) as exc:
            raise FrameCountError(str(exc)) from exc


# --- Clock and controls ------------------------------------------------------------- #


class ControlQueue:
    """``ControlSource`` fed by the REST controls, drained one per ``poll()``.

    Also the executor's alarm clock: :meth:`push` wakes a :class:`WallClock`
    sleeping in ``sleep_until`` so a pause or stop is acted on at once rather
    than at the next poll tick. The wake flag is cleared only by a ``poll()``
    that found the queue empty, so a push can never be lost between a poll and
    the sleep that follows it.
    """

    def __init__(self) -> None:
        self._queue: deque[ControlCommand] = deque()
        self._wake = asyncio.Event()

    def push(self, command: ControlCommand) -> None:
        self._queue.append(command)
        self._wake.set()

    async def poll(self) -> ControlCommand | None:
        if self._queue:
            return self._queue.popleft()
        self._wake.clear()
        return None

    async def wait(self, delay_s: float) -> None:
        """Sleep ``delay_s``, or less if a control arrives first."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=delay_s)


class WallClock:
    """``Clock`` on the real world. The one place wall-clock time lives."""

    def __init__(self, wake: Callable[[float], Awaitable[None]] | None = None) -> None:
        self._wake = wake

    def now(self) -> datetime:
        return datetime.now(tz=UTC)

    async def sleep_until(self, when: datetime) -> None:
        delay = (when - self.now()).total_seconds()
        if delay <= 0:
            return
        if self._wake is None:
            await asyncio.sleep(delay)
        else:
            await self._wake(delay)


# --- GateInputs --------------------------------------------------------------------- #


class ServiceGateInputs:
    """``GateInputs`` from the weather service and ``ssc.astro``.

    A forecast nobody can serve is returned as an *empty* series: the gate then
    reads cloud cover as ``None`` — unknown, never clear — and its decision's
    reason says so (CLAUDE.md invariant 7). It is logged here too, because the
    executor loop does not otherwise get to say why it is not gating.
    """

    def __init__(
        self,
        *,
        weather: WeatherService,
        astro: AstroService,
        location: LocationState,
        now: Callable[[], datetime],
    ) -> None:
        self._weather = weather
        self._astro = astro
        self._location = location
        self._site = site_of(location)
        self._now = now

    async def forecast(self) -> ForecastSeries:
        try:
            return await self._weather.forecast(
                self._location.latitude_deg, self._location.longitude_deg
            )
        except WeatherUnavailableError as exc:
            _log.warning("gate: no forecast to sample; cloud cover is unknown (%s)", exc)
            return ForecastSeries(points=(), fetched_at=self._now())

    async def target_snapshot(
        self, *, ra_j2000_h: float, dec_j2000_deg: float, at: datetime
    ) -> TargetSnapshot:
        ra, dec = Hours(ra_j2000_h), Deg(dec_j2000_deg)
        position = self._astro.position(self._site, ra, dec, at)
        separation = self._astro.moon_separation_deg(self._site, ra, dec, at)
        return TargetSnapshot(
            altitude_deg=float(position.altitude_deg), moon_separation_deg=float(separation)
        )


__all__ = [
    "DEFAULT_DEVICE_POLL_INTERVAL_S",
    "FIRMWARE_8_46_SIGNALS",
    "RECORDED_FIRMWARE",
    "AlpacaCapturePort",
    "AlpacaDeviceCommands",
    "AlpacaDeviceEvents",
    "CompletionSignal",
    "ControlQueue",
    "DeviceEventNamed",
    "OnReturn",
    "PollMember",
    "ServiceGateInputs",
    "WallClock",
]
