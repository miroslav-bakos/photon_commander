"""In-memory fakes for the executor's ports (DESIGN.md 4.2's crash-injection gate).

Every fake is deliberately dumb — no timing, no threads, no real sleep — so a test
controls exactly what the driver sees on each loop iteration. ``FakeClock.sleep_until``
jumps the virtual clock straight to the requested instant instead of actually waiting,
which is what lets a whole simulated night run in milliseconds.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ssc.plan.models import BodyTargetSpec
from ssc.scheduler.executor import (
    ControlCommand,
    DeviceEventTimeoutError,
    EffectCall,
    EffectName,
    FrameCountError,
    JournalRecord,
    ResolvedPointing,
    TargetResolutionError,
)
from ssc.scheduler.gates import TargetSnapshot
from ssc.weather.base import ForecastPoint, ForecastSeries


class FakeJournal:
    """One run's worth of records, in memory. ``seed`` pre-loads records the way a
    truncated SQLite table would already contain them before a resumed process starts
    reading."""

    def __init__(self, run_id: str, seed: Sequence[JournalRecord] = ()) -> None:
        self.run_id = run_id
        self._records: list[JournalRecord] = list(seed)

    async def append(self, record: JournalRecord) -> None:
        assert record.run_id == self.run_id
        self._records.append(record)

    async def read_all(self, run_id: str) -> Sequence[JournalRecord]:
        assert run_id == self.run_id
        return list(self._records)

    @property
    def records(self) -> list[JournalRecord]:
        return list(self._records)


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    async def sleep_until(self, when: datetime) -> None:
        if when > self._now:
            self._now = when

    def advance(self, seconds: float) -> None:
        """Move the virtual clock on by ``seconds`` — how a fake makes time
        pass *inside* an operation (an exposure cycle) rather than between them."""
        self._now = self._now + timedelta(seconds=seconds)


def effect_calls(effect: EffectCall) -> list[tuple[EffectName, dict]]:
    """What :func:`ssc.scheduler.executor._dispatch` actually calls on
    :class:`DeviceCommands` for ``effect`` — ``GATE_PARK`` expands to two calls, same
    as the real dispatcher, so a pre-seeded fake device matches a live one call for
    call."""
    if effect.name is EffectName.GATE_PARK:
        return [(EffectName.STOP_STACK, {}), (EffectName.PARK, {})]
    if effect.name is EffectName.DITHER:
        # ``after_frame`` is the cadence bookkeeping the journal needs; the device
        # is asked for the resolved bounded nudge and target identity.
        return [
            (
                EffectName.DITHER,
                {
                    "pixels": effect.args.get("pixels"),
                    "max_pixels": effect.args.get("max_pixels"),
                    "object_name": effect.args.get("object_name"),
                },
            )
        ]
    if effect.name is EffectName.STOP_LIVE_STACK:
        # The reason is durable scheduler context, not an adapter argument.
        return [(EffectName.STOP_LIVE_STACK, {})]
    if effect.name is EffectName.CENTER_TARGET:
        return [(effect.name, {key: value for key, value in effect.args.items()
                              if key not in {"resume_field_verification", "resume_autofocus_during_capture"}})]
    if effect.name is EffectName.EXPOSE_FRAME:
        args = dict(effect.args)
        # The dispatcher only forwards recenter settings when enabled.
        if not args.get("center_after_drift"):
            args.pop("center_after_drift", None)
            args.pop("drift_threshold_pixels", None)
        return [(effect.name, args)]
    return [(effect.name, dict(effect.args))]


class FakeDeviceCommands:
    """Records every call it receives. ``raise_on`` maps an effect name to the
    exception the corresponding method should raise (instead of recording success)."""

    def __init__(self, *, raise_on: dict[EffectName, Exception] | None = None) -> None:
        self.calls: list[tuple[EffectName, dict]] = []
        self.raise_on = raise_on or {}

    async def _record(self, name: EffectName, args: dict) -> None:
        self.calls.append((name, dict(args)))
        if name in self.raise_on:
            raise self.raise_on[name]

    def seed(self, calls: Sequence[tuple[EffectName, dict]]) -> None:
        """Pre-populate the call log without dispatching anything — used to represent
        calls a crashed-and-resumed run's fresh device fake did not itself make, but
        which the journal already durably shows as resolved."""
        self.calls.extend((name, dict(args)) for name, args in calls)

    async def startup(self) -> None:
        await self._record(EffectName.STARTUP, {})

    async def unpark(self) -> None:
        await self._record(EffectName.UNPARK, {})

    async def set_tracking_rate(self, rate: str) -> None:
        await self._record(EffectName.SET_TRACKING_RATE, {"rate": rate})

    async def set_tracking(self, enabled: bool) -> None:
        await self._record(EffectName.SET_TRACKING, {"enabled": enabled})

    async def slew_to(self, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        await self._record(
            EffectName.SLEW_TO, {"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg}
        )

    async def start_stack(
        self, *, target_name: str | None, ra_j2000_h: float, dec_j2000_deg: float
    ) -> None:
        await self._record(
            EffectName.START_STACK,
            {"target_name": target_name, "ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg},
        )

    async def stop_stack(self) -> None:
        await self._record(EffectName.STOP_STACK, {})

    async def autofocus(self) -> None:
        await self._record(EffectName.AUTOFOCUS, {})

    async def set_filter(self, position: str) -> None:
        await self._record(EffectName.SET_FILTER, {"position": position})

    async def set_heater(self, on: bool) -> None:
        await self._record(EffectName.SET_HEATER, {"on": on})

    async def set_exposure(self, exposure_sec: float) -> None:
        await self._record(EffectName.SET_EXPOSURE, {"exposure_sec": exposure_sec})

    async def set_gain(self, gain: int) -> None:
        await self._record(EffectName.SET_GAIN, {"gain": gain})

    async def set_dither(self, dither_pixels: float | None) -> None:
        await self._record(EffectName.SET_DITHER, {"dither_pixels": dither_pixels})

    async def solve(self) -> None:
        await self._record(EffectName.SOLVE, {})

    async def polar_alignment(self, **args: object) -> None:
        await self._record(EffectName.POLAR_ALIGNMENT, args)

    async def sync_to(self, ra_j2000_h: float | None, dec_j2000_deg: float | None) -> None:
        await self._record(
            EffectName.SYNC_TO, {"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg}
        )

    async def park(self) -> None:
        await self._record(EffectName.PARK, {})

    async def find_home(self) -> None:
        await self._record(EffectName.FIND_HOME, {})

    async def capture_calibration(self, **args: object) -> None:
        await self._record(EffectName.CAPTURE_CALIBRATION, args)

    async def shutdown(self) -> None:
        await self._record(EffectName.SHUTDOWN, {})


class FakeCapturePort:
    """``CapturePort`` over a :class:`FakeDeviceCommands`\'s single call log.

    Sharing the log is what makes the crash-injection harness work unchanged: a
    resumed run\'s device is seeded with exactly the calls the journal prefix
    shows as resolved, and because *this* fake counts frames out of that same
    log, the catalogue and the device can never disagree about a frame the test
    did not intend them to disagree about.

    ``freeze_at`` is how a test makes them disagree on purpose: the count stops
    rising there, as though the catalogue had quietly stopped recording, which
    the executor must notice and stop for rather than re-exposing the same frame
    index forever. (The other direction — a frame the catalogue holds that this
    process never shot — is arranged by seeding the device\'s call log, which is
    what the catalogue counts.)
    """

    def __init__(
        self,
        device: FakeDeviceCommands,
        *,
        clock: FakeClock | None = None,
        cycle_s: float = 0.0,
        freeze_at: int | None = None,
        count_error: str | None = None,
    ) -> None:
        self._device = device
        self._clock = clock
        self._cycle_s = cycle_s
        self.freeze_at = freeze_at
        self.count_error = count_error
        self.count_calls = 0

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
        args: dict[str, object] = {
            "block_id": block_id,
            "frame_index": frame_index,
            "exposure_sec": exposure_sec,
            "gain": gain,
            "frame_type": frame_type,
            "filter_name": filter_name,
        }
        if dither_every_n_frames is not None:
            args["dither_every_n_frames"] = dither_every_n_frames
        # Match the dispatcher's sparse argument shape so crash-resume can
        # compare the journal with calls byte-for-byte. Startup's final test
        # exposure is the only current non-retained frame.
        if not retain:
            args["retain"] = False
        if center_after_drift:
            args["center_after_drift"] = True
            args["drift_threshold_pixels"] = drift_threshold_pixels
        if object_name is not None:
            args["object_name"] = object_name
        if live_stack:
            args["live_stack"] = True
        await self._device._record(
            EffectName.EXPOSE_FRAME,
            args,
        )
        # A frame takes time, and for a block bounded by a window that is the
        # whole question. ``cycle_s`` is the test\'s own cycle model, stated
        # explicitly rather than inferred from the exposure.
        if self._clock is not None and self._cycle_s:
            self._clock.advance(self._cycle_s)

    async def focus_required(self) -> bool:
        return False

    async def autofocus(self) -> None:
        await self._device.autofocus()

    async def stop_live_stack(self, *, require_retained: bool = False) -> None:
        await self._device._record(EffectName.STOP_LIVE_STACK, {})

    async def center_target(self, *, ra_j2000_h: float, dec_j2000_deg: float) -> None:
        await self._device._record(
            EffectName.CENTER_TARGET,
            {"ra_j2000_h": ra_j2000_h, "dec_j2000_deg": dec_j2000_deg},
        )

    async def recover_dither(self, *, object_name: str | None) -> None:
        """Independent fake stability proof; deliberately issues no physical move."""
        return None

    async def recover_operation(self, *, level: int, operation: str) -> None:
        """Default healthy reconciliation; fault tests override specific layers."""
        return None

    async def dither(
        self, *, pixels: float | None, max_pixels: float | None, object_name: str | None
    ) -> None:
        await self._device._record(
            EffectName.DITHER,
            {"pixels": pixels, "max_pixels": max_pixels, "object_name": object_name},
        )

    async def frames_captured(self, *, run_id: str, item_id: str) -> int:
        self.count_calls += 1
        if self.count_error is not None:
            raise FrameCountError(self.count_error)
        shot = sum(
            1
            for name, args in self._device.calls
            if name is EffectName.EXPOSE_FRAME and args.get("block_id") == item_id
        )
        return shot if self.freeze_at is None else min(shot, self.freeze_at)


class FakeDeviceEvents:
    """``wait_for`` returns immediately unless ``event_kind`` is in ``timeout_on``, in
    which case it raises :class:`DeviceEventTimeoutError` — no real deadline math, the
    test decides the outcome directly."""

    def __init__(self, *, timeout_on: frozenset[str] = frozenset()) -> None:
        self.waits: list[tuple[str, str]] = []
        self.timeout_on = timeout_on

    async def wait_for(self, event_kind: str, *, item_id: str, deadline: datetime) -> None:
        self.waits.append((event_kind, item_id))
        if event_kind in self.timeout_on:
            raise DeviceEventTimeoutError(f"no {event_kind!r} observed for {item_id!r}")


@dataclass
class FakeControls:
    """A pre-loaded queue of controls, drained one per ``poll()`` — the driver notices
    each at the next decision boundary."""

    queued: list[ControlCommand] = field(default_factory=list)

    def push(self, command: ControlCommand) -> None:
        self.queued.append(command)

    async def poll(self) -> ControlCommand | None:
        if self.queued:
            return self.queued.pop(0)
        return None


class FakeGateInputs:
    """Serves a fixed target snapshot and a caller-supplied sequence of cloud-cover
    forecasts, one per call to :meth:`forecast` (repeating the last value once the
    sequence is exhausted, so a test does not have to predict exactly how many times
    the driver will sample)."""

    def __init__(
        self,
        *,
        cloud_pcts: Sequence[float],
        altitude_deg: float = 80.0,
        moon_separation_deg: float = 90.0,
    ) -> None:
        self._cloud_pcts = list(cloud_pcts)
        self._index = 0
        self._altitude_deg = altitude_deg
        self._moon_separation_deg = moon_separation_deg
        self.forecast_calls = 0

    async def forecast(self) -> ForecastSeries:
        self.forecast_calls += 1
        cloud = self._cloud_pcts[min(self._index, len(self._cloud_pcts) - 1)]
        self._index += 1
        # `t` does not matter for `sample_cloud`'s nearest-point search when there is
        # only one point; any timestamp finds it.
        point = ForecastPoint(
            t=datetime(2026, 1, 1, tzinfo=UTC),
            cloud_total=cloud,
            cloud_low=None,
            cloud_mid=None,
            cloud_high=None,
            precip_prob=None,
            humidity=None,
            dew_point_c=None,
            temp_c=None,
            wind_ms=None,
            visibility_m=None,
            seeing_arcsec=None,
            transparency=None,
            source="fake",
        )
        return ForecastSeries(points=(point,), fetched_at=datetime(2026, 1, 1, tzinfo=UTC))

    async def target_snapshot(
        self, *, ra_j2000_h: float, dec_j2000_deg: float, at: datetime
    ) -> TargetSnapshot:
        return TargetSnapshot(
            altitude_deg=self._altitude_deg, moon_separation_deg=self._moon_separation_deg
        )


class LinearBodyResolver:
    """A body whose position is an exact linear function of time — so a test can
    assert "the executor resolved this at T+6h, not at T" without an ephemeris
    and without tolerances.

    ``rate_h_per_hour`` is RA drift in *hours of RA* per hour of clock time, so
    the Moon's real ~39 arcmin/h of RA is ``39 / 60 / 15``. ``fail`` makes every
    call raise, which is how the "resolution failed" path is exercised.
    """

    def __init__(
        self,
        *,
        epoch: datetime,
        ra_j2000_h: float = 5.0,
        dec_j2000_deg: float = 20.0,
        rate_h_per_hour: float = 0.0,
        fail: str | None = None,
    ) -> None:
        self.epoch = epoch
        self.ra_j2000_h = ra_j2000_h
        self.dec_j2000_deg = dec_j2000_deg
        self.rate_h_per_hour = rate_h_per_hour
        self.fail = fail
        self.calls: list[datetime] = []

    def ra_at(self, at: datetime) -> float:
        hours = (at - self.epoch).total_seconds() / 3600.0
        return self.ra_j2000_h + self.rate_h_per_hour * hours

    async def resolve(self, target: BodyTargetSpec, at: datetime) -> ResolvedPointing:
        self.calls.append(at)
        if self.fail is not None:
            raise TargetResolutionError(self.fail)
        return ResolvedPointing(
            ra_j2000_h=self.ra_at(at),
            dec_j2000_deg=self.dec_j2000_deg,
            tracking_rate=str(target.tracking_rate),
            name=target.name,
            at=at,
        )


__all__ = [
    "FakeCapturePort",
    "FakeClock",
    "FakeControls",
    "FakeDeviceCommands",
    "FakeDeviceEvents",
    "FakeGateInputs",
    "FakeJournal",
    "LinearBodyResolver",
    "effect_calls",
]
