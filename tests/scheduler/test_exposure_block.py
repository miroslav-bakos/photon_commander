"""The exposure block, end to end (DESIGN.md 4.3.0).

The crash-injection half of this feature lives in
``test_crash_resume_exposures.py``; this file covers the two semantics the brief
called out as easy to get subtly wrong — **does a whole cycle fit** and **does
the block set its own state** — plus everything the compiler can say about a
block before the night.

The cycle-time model used throughout, stated once: one cycle is
``exposure + FRAME_OVERHEAD_S`` (4.0 s), and the fake capture port advances the
virtual clock by exactly that on every frame, so "two whole cycles fit" is an
assertion about arithmetic rather than about timing luck.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ssc.astro.events import Ephemeris, Site
from ssc.astro.units import Deg, Metres
from ssc.plan import validate_plan
from ssc.plan.models import (
    AbsoluteTimeSpec,
    DitherSpec,
    DurationEndSpec,
    FixedCountSpec,
    FrameType,
    Plan,
    RelativeTimeSpec,
    Severity,
    TakeExposuresItem,
    UntilBlockEndSpec,
)
from ssc.scheduler.compiler import compile_plan
from ssc.scheduler.executor import EffectName, ItemStatus, reduce, run
from ssc.scheduler.exposure import FRAME_OVERHEAD_S, CameraLimits, cycle_duration_s
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)
from tests.scheduler.schedules import BASE_UTC, build_open_ended_plan, build_schedule

# --- shared fixtures ---------------------------------------------------------

_SITE = Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45.0))
_TZ = ZoneInfo("Europe/London")
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)

#: The S30 Pro's own reported limits, measured 2026-09-10 — as an *input* to the
#: compiler, which is the whole point: nothing in ``ssc/`` hardcodes these.
_S30_LIMITS = CameraLimits(
    exposure_min_s=3e-05,
    exposure_max_s=2000.0,
    gain_min=0,
    gain_max=600,
    filter_names=("Dark", "IR", "LP"),
)


def _local(*args: int) -> datetime:
    return datetime(*args)  # noqa: DTZ001


def _plan_with(item: TakeExposuresItem) -> Plan:
    now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
    return Plan(id="p", name="one block", created_at=now, updated_at=now, items=(item,))


def _block(**overrides: object) -> TakeExposuresItem:
    fields: dict[str, object] = {
        "id": "lights",
        "start": AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 23, 0)),
        "count": FixedCountSpec(frames=60),
        "exposure_sec": 30.0,
        "gain": 200,
        "filter_name": "LP",
        "frame_type": FrameType.LIGHT,
    }
    fields.update(overrides)
    return TakeExposuresItem(**fields)  # type: ignore[arg-type]


def _compile(plan: Plan, ephemeris: Ephemeris, limits: CameraLimits | None = None):  # type: ignore[no-untyped-def]
    return compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=datetime(2026, 8, 28, tzinfo=UTC).date(),
        ephemeris=ephemeris,
        now=_NOW,
        camera_limits=limits,
    )


def _codes(item, severity: Severity | None = None) -> set[str]:  # type: ignore[no-untyped-def]
    return {
        d.code for d in item.diagnostics if severity is None or d.severity is severity
    }


# --- the document ------------------------------------------------------------


class TestTheDocument:
    def test_a_block_carries_every_parameter_one_run_needs(self) -> None:
        block = _block(dither=DitherSpec(pixels=12.0, every_n_frames=3))
        assert block.count == FixedCountSpec(frames=60)
        assert (block.exposure_sec, block.gain, block.filter_name) == (30.0, 200, "LP")
        assert block.frame_type is FrameType.LIGHT
        assert block.dither is not None and block.dither.every_n_frames == 3

    def test_a_hand_scheduled_dark_is_a_block_through_the_shutter(self) -> None:
        """DESIGN.md 4.3.0: "a dark is an exposure block through the Dark
        shutter; it is not a separate item kind"."""
        block = _block(frame_type=FrameType.DARK, filter_name="Dark")
        assert block.kind == "take_exposures"
        assert block.frame_type is FrameType.DARK

    def test_the_plan_frame_type_has_not_drifted_from_the_capture_one(self) -> None:
        """The two enums are deliberately separate (``ssc.plan`` may not import
        ``ssc.capture``, which reaches SQLite) — so the values are pinned here,
        in the one layer allowed to see both."""
        from ssc.capture.frame import FrameType as CaptureFrameType

        assert {t.value for t in FrameType} == {t.value for t in CaptureFrameType}

    @pytest.mark.parametrize(
        "bad",
        [
            {"exposure_sec": 0.0},
            {"gain": -1},
            {"count": {"kind": "fixed", "frames": 0}},
            {"dither": {"every_n_frames": 0}},
            {"dither": {"pixels": 0.0}},
        ],
    )
    def test_nonsense_is_refused_on_the_way_in(self, bad: dict) -> None:
        """A plan document is portable, hand-editable, untrusted input, so a
        zero-frame count or a non-positive exposure is refused at parse time
        rather than discovered at 3 a.m."""
        with pytest.raises(ValueError):
            _block(**bad)


class TestValidate:
    def test_an_open_ended_block_without_an_end_is_an_error(self) -> None:
        plan = _plan_with(_block(count=UntilBlockEndSpec()))
        codes = {d.code: d.severity for d in validate_plan(plan)}
        assert codes["open_ended_exposure_needs_end"] is Severity.ERROR

    def test_an_open_ended_block_with_an_end_is_fine(self) -> None:
        plan = _plan_with(
            _block(count=UntilBlockEndSpec(), end=DurationEndSpec(minutes=60.0))
        )
        assert "open_ended_exposure_needs_end" not in {d.code for d in validate_plan(plan)}

    def test_a_block_that_names_no_filter_says_so(self) -> None:
        plan = _plan_with(_block(filter_name=None))
        codes = {d.code: d.severity for d in validate_plan(plan)}
        assert codes["exposure_block_sets_no_filter"] is Severity.WARNING


# --- the compiler ------------------------------------------------------------


class TestCompiler:
    def test_a_fixed_count_block_derives_its_own_end(self, ephemeris: Ephemeris) -> None:
        """Sixty 30 s frames is 60 x 34 s = 34 min, and the echo says exactly
        that rather than presenting the number as though it were typed in."""
        schedule = _compile(_plan_with(_block()), ephemeris, _S30_LIMITS)
        item = schedule.items[0]
        assert item.start_utc is not None and item.end_utc is not None
        assert item.end_utc - item.start_utc == timedelta(seconds=60 * (30.0 + FRAME_OVERHEAD_S))
        assert item.end_resolution.startswith("60 x 30 s + 4 s overhead →")

    def test_the_next_item_chains_off_the_derived_end(self, ephemeris: Ephemeris) -> None:
        now = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
        plan = Plan(
            id="p",
            name="two",
            created_at=now,
            updated_at=now,
            items=(
                _block(),
                _block(id="darks", start=RelativeTimeSpec(offset_min=0.0)),
            ),
        )
        schedule = _compile(plan, ephemeris, _S30_LIMITS)
        assert schedule.items[1].start_utc == schedule.items[0].end_utc

    def test_limits_come_from_the_camera_or_nothing_is_checked(
        self, ephemeris: Ephemeris
    ) -> None:
        unchecked = _compile(_plan_with(_block()), ephemeris, None)
        assert "camera_limits_unknown" in _codes(unchecked.items[0], Severity.INFO)
        checked = _compile(_plan_with(_block()), ephemeris, _S30_LIMITS)
        assert _codes(checked.items[0], Severity.ERROR) == set()
        assert "camera_limits_unknown" not in _codes(checked.items[0])

    @pytest.mark.parametrize(
        ("overrides", "code"),
        [
            ({"exposure_sec": 3000.0}, "exposure_out_of_range"),
            ({"exposure_sec": 1e-06}, "exposure_out_of_range"),
            ({"gain": 900}, "gain_out_of_range"),
            ({"filter_name": "Ha"}, "unknown_filter_position"),
        ],
    )
    def test_a_value_the_camera_would_refuse_is_an_error(
        self, ephemeris: Ephemeris, overrides: dict, code: str
    ) -> None:
        schedule = _compile(_plan_with(_block(**overrides)), ephemeris, _S30_LIMITS)
        assert code in _codes(schedule.items[0], Severity.ERROR)

    def test_a_count_that_cannot_fit_its_window_is_warned_about_with_numbers(
        self, ephemeris: Ephemeris
    ) -> None:
        # 60 x 34 s = 34 min asked for, 20 min of window given.
        plan = _plan_with(_block(end=DurationEndSpec(minutes=20.0)))
        schedule = _compile(plan, ephemeris, _S30_LIMITS)
        warning = next(
            d for d in schedule.items[0].diagnostics if d.code == "count_exceeds_window"
        )
        assert "will stop after 35 frames" in warning.message

    def test_a_window_too_short_for_one_whole_cycle_is_warned_about(
        self, ephemeris: Ephemeris
    ) -> None:
        plan = _plan_with(
            _block(count=UntilBlockEndSpec(), exposure_sec=40.0, end=DurationEndSpec(minutes=0.5))
        )
        schedule = _compile(plan, ephemeris, _S30_LIMITS)
        assert "no_whole_cycle_fits" in _codes(schedule.items[0], Severity.WARNING)

    def test_an_open_ended_block_without_an_end_is_refused_by_the_compiler_too(
        self, ephemeris: Ephemeris
    ) -> None:
        plan = _plan_with(_block(count=UntilBlockEndSpec()))
        schedule = _compile(plan, ephemeris, _S30_LIMITS)
        assert "open_ended_exposure_needs_end" in _codes(schedule.items[0], Severity.ERROR)


# --- the executor ------------------------------------------------------------


async def _execute(plan: Plan, *, freeze_at: int | None = None, capture_port: bool = True, waits: list[tuple[datetime, datetime]] | None = None):  # type: ignore[no-untyped-def]
    schedule = build_schedule(plan)
    journal = FakeJournal("run")
    device = FakeDeviceCommands()
    class RecordingClock(FakeClock):
        async def sleep_until(self, until: datetime) -> None:
            if waits is not None:
                waits.append((self.now(), until))
            await super().sleep_until(until)

    clock = RecordingClock(BASE_UTC - timedelta(minutes=1))
    exposure_sec = next(
        item.exposure_sec for item in plan.items if isinstance(item, TakeExposuresItem)
    )
    capture = (
        FakeCapturePort(
            device,
            clock=clock,
            cycle_s=cycle_duration_s(exposure_sec),
            freeze_at=freeze_at,
        )
        if capture_port
        else None
    )
    await run(
        run_id="run",
        schedule=schedule,
        plan=plan,
        journal=journal,
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        capture=capture,
        gate_inputs=None,
        poll_interval_s=3600.0,
    )
    return journal, device


def _frames(device: FakeDeviceCommands) -> list[dict]:
    return [args for name, args in device.calls if name is EffectName.EXPOSE_FRAME]


class TestWholeCycleFits:
    """DESIGN.md 4.3.0: the check is "does a whole cycle fit", not "is there
    time left". A cycle here is 10 s + 4 s = 14 s, and the fake clock advances
    by exactly that per frame."""

    async def test_room_for_two_whole_cycles_takes_exactly_two(self) -> None:
        # 30 s of window: 14 + 14 = 28 s fits, a third would need 42 s.
        plan = build_open_ended_plan(exposure_sec=10.0, window_min=0.5)
        _, device = await _execute(plan)
        assert len(_frames(device)) == 2

    async def test_a_40_s_exposure_with_20_s_of_window_takes_none(self) -> None:
        plan = build_open_ended_plan(exposure_sec=40.0, window_min=20.0 / 60.0)
        journal, device = await _execute(plan)
        assert _frames(device) == []
        state = reduce(journal.records)
        assert state.items["open-block"].status is ItemStatus.DONE
        assert "a whole cycle needs 44 s" in state.items["open-block"].reason

    async def test_the_frame_that_would_not_fit_is_not_started_at_all(self) -> None:
        """The failure this prevents is a *partial file*, so what matters is
        that no exposure was even commanded for the frame that could not
        finish — not merely that it was discarded afterwards."""
        plan = build_open_ended_plan(exposure_sec=10.0, window_min=0.5)
        _, device = await _execute(plan)
        assert [args["frame_index"] for args in _frames(device)] == [0, 1]


class TestTheBlockSetsItsOwnState:
    async def test_a_second_block_sets_its_own_filter_gain_and_exposure(self) -> None:
        """The state-leak case the whole item exists to close: block two's
        settings reach the device even though block one set different ones."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        plan = Plan(
            id="two-blocks",
            name="LP then Ha",
            created_at=now,
            updated_at=now,
            items=(
                TakeExposuresItem(
                    id="first",
                    start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                    count=FixedCountSpec(frames=1),
                    exposure_sec=10.0,
                    gain=80,
                    filter_name="LP",
                ),
                TakeExposuresItem(
                    id="second",
                    start=RelativeTimeSpec(offset_min=0.0),
                    count=FixedCountSpec(frames=1),
                    exposure_sec=10.0,
                    gain=300,
                    filter_name="IR",
                    frame_type=FrameType.DARK,
                ),
            ),
        )
        _, device = await _execute(plan)

        first_frame = device.calls.index((EffectName.EXPOSE_FRAME, _frames(device)[0]))
        second = device.calls[first_frame + 1 :]
        assert (EffectName.SET_FILTER, {"position": "IR"}) in second
        assert (EffectName.SET_GAIN, {"gain": 300}) in second
        assert _frames(device)[1]["exposure_sec"] == 10.0
        # ...and the frame itself carries them too, so the run log says what
        # each frame was shot with rather than what was set an hour ago.
        assert _frames(device)[1]["gain"] == 300
        assert _frames(device)[1]["filter_name"] == "IR"
        assert _frames(device)[1]["frame_type"] == "dark"


class TestBlockedAcquisition:
    @staticmethod
    def assert_visible_bounded_recovery(journal, waits, *, reason: str, end: datetime) -> None:
        state = reduce(journal.records)
        blocked = [record for record in journal.records
                   if "Acquisition blocked; retrying until window end:" in record.reason]
        assert len(blocked) == 1  # Unchanged reasons remain durably visible, not journal spam.
        assert reason in blocked[0].reason
        assert blocked[0].status is ItemStatus.RUNNING
        retries = [(start, finish) for start, finish in waits if start >= blocked[0].at]
        assert len(retries) > 1
        assert all(timedelta(0) < finish - start <= timedelta(seconds=30)
                   for start, finish in retries)
        assert retries[-1][1] == end
        assert state.items["open-block"].status is ItemStatus.DONE
        assert state.items["open-block"].reason == "window ended while acquisition blocked; saved frames retained"
        assert journal.records[-1].run_status.value == "completed"
        assert not any(record.status is ItemStatus.FAILED for record in journal.records)

    async def test_without_a_capture_port_the_block_waits_visibly_until_window_end(self) -> None:
        plan = build_open_ended_plan(exposure_sec=10.0, window_min=5.0)
        waits: list[tuple[datetime, datetime]] = []
        journal, device = await _execute(plan, capture_port=False, waits=waits)
        self.assert_visible_bounded_recovery(journal, waits, reason="no capture port",
                                            end=BASE_UTC + timedelta(minutes=5))
        assert _frames(device) == []

    async def test_catalogue_disagreement_blocks_reexposure_until_window_end(self) -> None:
        plan = build_open_ended_plan(exposure_sec=10.0, window_min=60.0)
        waits: list[tuple[datetime, datetime]] = []
        journal, device = await _execute(plan, freeze_at=1, waits=waits)
        self.assert_visible_bounded_recovery(
            journal, waits, reason="refusing to re-expose the same frame index",
            end=BASE_UTC + timedelta(minutes=60))
        assert [frame["frame_index"] for frame in _frames(device)] == [0, 1]


class TestDithering:
    async def test_range_dithering_chooses_durable_amounts_within_bounds(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        plan = Plan(
            id="range-dithered",
            name="range dither",
            created_at=now,
            updated_at=now,
            items=(
                TakeExposuresItem(
                    id="lights",
                    start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                    count=FixedCountSpec(frames=5),
                    exposure_sec=10.0,
                    gain=100,
                    filter_name="LP",
                    dither=DitherSpec(min_pixels=30.0, max_pixels=200.0, every_n_frames=1),
                ),
            ),
        )
        _, device = await _execute(plan)
        amounts = [args["pixels"] for name, args in device.calls if name is EffectName.DITHER]
        assert len(amounts) == 4
        assert all(30.0 <= float(amount) <= 200.0 for amount in amounts)
        assert len(set(amounts)) > 1

    async def test_it_dithers_on_the_block_s_own_cadence(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC)
        plan = Plan(
            id="dithered",
            name="every third frame",
            created_at=now,
            updated_at=now,
            items=(
                TakeExposuresItem(
                    id="lights",
                    start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                    count=FixedCountSpec(frames=7),
                    exposure_sec=10.0,
                    gain=100,
                    filter_name="LP",
                    dither=DitherSpec(pixels=12.0, every_n_frames=3),
                ),
            ),
        )
        _, device = await _execute(plan)
        sequence = [
            (name, args.get("frame_index"))
            for name, args in device.calls
            if name in (EffectName.EXPOSE_FRAME, EffectName.DITHER)
        ]
        assert sequence == [
            (EffectName.EXPOSE_FRAME, 0),
            (EffectName.EXPOSE_FRAME, 1),
            (EffectName.EXPOSE_FRAME, 2),
            (EffectName.DITHER, None),
            (EffectName.EXPOSE_FRAME, 3),
            (EffectName.EXPOSE_FRAME, 4),
            (EffectName.EXPOSE_FRAME, 5),
            (EffectName.DITHER, None),
            (EffectName.EXPOSE_FRAME, 6),
        ]

    async def test_an_unnamed_amount_reaches_the_device_as_unknown(self) -> None:
        """``pixels: None`` means "the optic profile's ``dither_pixels``", which
        only the adapter can read — so it travels as ``None``, never as a
        number this layer invented."""
        now = datetime(2026, 1, 1, tzinfo=UTC)
        plan = Plan(
            id="dithered",
            name="profile amount",
            created_at=now,
            updated_at=now,
            items=(
                TakeExposuresItem(
                    id="lights",
                    start=AbsoluteTimeSpec(local_datetime=BASE_UTC.replace(tzinfo=None)),
                    count=FixedCountSpec(frames=2),
                    exposure_sec=10.0,
                    gain=100,
                    filter_name="LP",
                    dither=DitherSpec(every_n_frames=1),
                ),
            ),
        )
        _, device = await _execute(plan)
        assert (
            EffectName.DITHER,
            {"pixels": None, "max_pixels": None, "object_name": None},
        ) in device.calls
