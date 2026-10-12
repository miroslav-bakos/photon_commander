"""Golden-file test: a fixed Plan + fixed site + fixed date compiles to a
checked-in expected ``CompiledSchedule``, plus focused unit tests for the
diagnostics ``ssc.scheduler.compiler`` can honestly compute.

Regenerate the golden fixture deliberately, never automatically::

    SSC_REGENERATE_GOLDEN=1 uv run pytest tests/scheduler/test_compiler.py -k golden

then re-run without the env var and inspect the diff before committing it.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ssc.astro.events import Ephemeris, Site, SolarEvent, TargetEvent
from ssc.astro.framing import Profile
from ssc.astro.polar import best_conditioned_field
from ssc.astro.units import Deg, Metres
from ssc.plan import (
    AbsoluteTimeSpec,
    CaptureCalibrationItem,
    ConstraintsSpec,
    FixedCountSpec,
    FrameType,
    FramingSpec,
    GotoAndStackItem,
    MosaicGridSpec,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
    ShutdownItem,
    SlewItem,
    StartupItem,
    TakeExposuresItem,
    TargetEventTimeSpec,
    WaitItem,
    parse_plan,
)
from ssc.scheduler import CameraLimits, CompiledSchedule, compile_plan

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "scheduler"
GOLDEN_PLAN_PATH = FIXTURES_DIR / "golden_plan.json"
GOLDEN_SCHEDULE_PATH = FIXTURES_DIR / "golden_schedule.json"
#: A second golden pair for solar-system targets (DESIGN.md 3.6). Kept separate
#: from the catalogue one on purpose: ``golden_plan.json`` is still a
#: ``schema_version`` 1 document, so it also proves the v1 -> v2 migration keeps
#: compiling to the same times, while this one is authored at v2 and exercises
#: the parts only a body can reach — re-centring segments, the tracking rate
#: travelling with the target, and a position echoed as "at this instant".
GOLDEN_BODY_PLAN_PATH = FIXTURES_DIR / "golden_body_plan.json"
GOLDEN_BODY_SCHEDULE_PATH = FIXTURES_DIR / "golden_body_schedule.json"
#: A third golden pair for exposure blocks (DESIGN.md 4.3.0), authored at
#: ``schema_version`` 3. It is where a change to the frame-cycle model, to a
#: fixed-count block's derived end, or to what the compiler will say about a
#: block shows up as a number rather than as an opinion.
GOLDEN_EXPOSURE_PLAN_PATH = FIXTURES_DIR / "golden_exposure_plan.json"
GOLDEN_EXPOSURE_SCHEDULE_PATH = FIXTURES_DIR / "golden_exposure_schedule.json"

#: The S30 Pro's own reported camera limits (measured 2026-09-10), passed in as
#: an *input* so the exposure golden exercises the checked path. Nothing in
#: ``ssc/`` hardcodes these — that is the point of them being a parameter.
_S30_LIMITS = CameraLimits(
    exposure_min_s=3e-05,
    exposure_max_s=2000.0,
    gain_min=0,
    gain_max=600,
    filter_names=("Dark", "IR", "LP"),
)

#: Greenwich, a modest elevation, a fixed August night — chosen to exercise
#: BST (Europe/London is UTC+1 in August) without any DST edge in play; the
#: DST edge itself is ``test_dst.py``'s job.
_SITE = Site(latitude_deg=Deg(51.4934), longitude_deg=Deg(-0.0098), elevation_m=Metres(45.0))
_TZ = ZoneInfo("Europe/London")
_NIGHT_DATE = date(2026, 8, 28)
_NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)


def _local(*args: int) -> datetime:
    """A naive local wall-clock reading for AbsoluteTimeSpec — deliberately
    tz-less (DTZ001 would otherwise flag every one of these)."""
    return datetime(*args)  # noqa: DTZ001


def _compile_golden(
    ephemeris: Ephemeris, profile: Profile, path: Path = GOLDEN_PLAN_PATH
) -> CompiledSchedule:
    plan = parse_plan(json.loads(path.read_text()))
    return compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )


def test_golden_schedule(ephemeris: Ephemeris, profile: Profile) -> None:
    schedule = _compile_golden(ephemeris, profile)

    if os.environ.get("SSC_REGENERATE_GOLDEN") == "1":
        GOLDEN_SCHEDULE_PATH.write_text(schedule.model_dump_json(indent=2) + "\n")
        pytest.skip(
            "regenerated tests/fixtures/scheduler/golden_schedule.json; re-run without "
            "SSC_REGENERATE_GOLDEN=1 to verify it, and inspect the diff before committing"
        )

    expected = json.loads(GOLDEN_SCHEDULE_PATH.read_text())
    actual = json.loads(schedule.model_dump_json())
    assert actual == expected


def test_astronomical_midnight_is_exact_darkness_midpoint(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _compile_golden(ephemeris, profile)
    night = plan.night
    midnight = next(e for e in night.solar_events if e.event == SolarEvent.ASTRONOMICAL_MIDNIGHT)
    assert night.dark_start_utc is not None and night.dark_end_utc is not None
    assert midnight.when_utc == night.dark_start_utc + (
        night.dark_end_utc - night.dark_start_utc
    ) / 2
    assert midnight.outcome == "occurs"

    document = parse_plan(json.loads(GOLDEN_PLAN_PATH.read_text()))
    payload = document.model_dump(mode="json")
    payload["items"][0]["start"] = {
        "kind": "event", "event": "astronomical_midnight", "offset_min": 0,
    }
    scheduled = compile_plan(
        parse_plan(payload), site=_SITE, tz=_TZ, night_date=_NIGHT_DATE,
        ephemeris=ephemeris, now=_NOW, profile=profile,
    )
    assert scheduled.items[0].start_utc == midnight.when_utc


def test_astronomical_midnight_reports_absence_in_polar_summer(ephemeris: Ephemeris) -> None:
    document = parse_plan(json.loads(GOLDEN_PLAN_PATH.read_text()))
    payload = document.model_dump(mode="json")
    payload["items"][0]["start"] = {
        "kind": "event", "event": "astronomical_midnight", "offset_min": 0,
    }
    schedule = compile_plan(
        parse_plan(payload),
        site=Site(latitude_deg=Deg(69.6492), longitude_deg=Deg(18.9553)),
        tz=ZoneInfo("Europe/Oslo"), night_date=date(2026, 6, 21),
        ephemeris=ephemeris, now=_NOW,
    )
    midnight = next(e for e in schedule.night.solar_events if e.event == SolarEvent.ASTRONOMICAL_MIDNIGHT)
    assert midnight.when_utc is None
    assert midnight.outcome == "always_above"
    assert schedule.items[0].start_utc is None
    assert any(d.code == "event_does_not_occur" for d in schedule.items[0].diagnostics)


def test_golden_body_schedule(ephemeris: Ephemeris, profile: Profile) -> None:
    """The Moon, Jupiter and Mars compiled for a fixed night at a fixed site.

    Regenerate with ``SSC_REGENERATE_GOLDEN=1`` exactly as for the catalogue
    fixture, and read the diff: this file is where a change to the re-centring
    interval or to the tracking-rate coupling shows up as a number.
    """
    schedule = _compile_golden(ephemeris, profile, GOLDEN_BODY_PLAN_PATH)

    if os.environ.get("SSC_REGENERATE_GOLDEN") == "1":
        GOLDEN_BODY_SCHEDULE_PATH.write_text(schedule.model_dump_json(indent=2) + "\n")
        pytest.skip(
            "regenerated tests/fixtures/scheduler/golden_body_schedule.json; re-run "
            "without SSC_REGENERATE_GOLDEN=1 to verify it"
        )

    expected = json.loads(GOLDEN_BODY_SCHEDULE_PATH.read_text())
    actual = json.loads(schedule.model_dump_json())
    assert actual == expected


def test_golden_exposure_schedule(ephemeris: Ephemeris, profile: Profile) -> None:
    """Three exposure blocks compiled for the same fixed night and site: a
    fixed count that derives its own end, an open-ended one that runs to
    astronomical dawn minus 30, and matched darks through the shutter.

    Regenerate with ``SSC_REGENERATE_GOLDEN=1`` exactly as for the other two,
    and read the diff.
    """
    plan = parse_plan(json.loads(GOLDEN_EXPOSURE_PLAN_PATH.read_text()))
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
        camera_limits=_S30_LIMITS,
    )

    if os.environ.get("SSC_REGENERATE_GOLDEN") == "1":
        GOLDEN_EXPOSURE_SCHEDULE_PATH.write_text(schedule.model_dump_json(indent=2) + "\n")
        pytest.skip(
            "regenerated tests/fixtures/scheduler/golden_exposure_schedule.json; re-run "
            "without SSC_REGENERATE_GOLDEN=1 to verify it"
        )

    expected = json.loads(GOLDEN_EXPOSURE_SCHEDULE_PATH.read_text())
    actual = json.loads(schedule.model_dump_json())
    assert actual == expected


def test_golden_schedule_is_deterministic(ephemeris: Ephemeris, profile: Profile) -> None:
    """Compiling twice from the same inputs is byte-identical — no clock, no
    device, no hidden state anywhere in the compiler."""
    first = _compile_golden(ephemeris, profile).model_dump_json()
    second = _compile_golden(ephemeris, profile).model_dump_json()
    assert first == second


def _plan(*items: object, plan_id: str = "p") -> Plan:
    return Plan(
        id=plan_id,
        name="test",
        items=tuple(items),  # type: ignore[arg-type]
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.mark.parametrize(
    "item",
    [
        TakeExposuresItem(
            id="dark",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            count=FixedCountSpec(frames=2),
            exposure_sec=30,
            gain=80,
            filter_name="Dark",
            frame_type=FrameType.DARK,
        ),
        TakeExposuresItem(
            id="bias",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            count=FixedCountSpec(frames=2),
            exposure_sec=0.001,
            gain=80,
            filter_name="Dark",
            frame_type=FrameType.BIAS,
        ),
        TakeExposuresItem(
            id="flat",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            count=FixedCountSpec(frames=2),
            exposure_sec=1,
            gain=80,
            filter_name="LP",
            frame_type=FrameType.FLAT,
        ),
        CaptureCalibrationItem(
            id="matched-darks",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
        ),
        WaitItem(
            id="wait",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            duration_min=10,
        ),
    ],
    ids=["dark", "bias", "flat", "matched-calibration", "ordinary-action"],
)
def test_only_light_capture_blocks_warn_past_dark_window(
    item: object, ephemeris: Ephemeris, profile: Profile
) -> None:
    schedule = compile_plan(
        _plan(item),  # type: ignore[arg-type]
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
        camera_limits=_S30_LIMITS,
    )
    assert "exceeds_dark_window" not in {row.code for row in schedule.items[0].diagnostics}


@pytest.mark.parametrize(
    "light",
    [
        TakeExposuresItem(
            id="light",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            count=FixedCountSpec(frames=2),
            exposure_sec=30,
            gain=80,
            filter_name="LP",
            frame_type=FrameType.LIGHT,
        ),
        GotoAndStackItem(
            id="target-stack",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            duration_min=10,
            target_name="NGC 2244",
            ra_j2000_h=6.5317,
            dec_j2000_deg=4.9407,
        ),
        MosaicItem(
            id="mosaic",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 29, 4, 30)),
            duration_min=10,
            target_name="NGC 2244",
            ra_j2000_h=6.5317,
            dec_j2000_deg=4.9407,
        ),
    ],
    ids=["exposure-block", "target-stack", "mosaic"],
)
def test_light_capture_blocks_still_warn_past_dark_window(
    light: object, ephemeris: Ephemeris, profile: Profile
) -> None:
    schedule = compile_plan(
        _plan(light),  # type: ignore[arg-type]
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
        camera_limits=_S30_LIMITS,
    )
    assert "exceeds_dark_window" in {row.code for row in schedule.items[0].diagnostics}


def test_mosaic_expands_into_ordered_panels(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _plan(
        MosaicItem(
            id="m",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 23, 0)),
            duration_min=10,
            ra_j2000_h=6.5317,
            dec_j2000_deg=4.9407,
            mosaic=MosaicGridSpec(ra_num=2, dec_num=2, overlap_pct=20),
        )
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert len(item.panels) == 4
    assert item.start_utc is not None
    assert item.panels[0].start_utc == item.start_utc
    # Every panel is exactly duration_min long and they are laid end to end.
    for a, b in zip(item.panels, item.panels[1:], strict=False):
        assert a.end_utc == b.start_utc
        assert (a.end_utc - a.start_utc) == timedelta(minutes=10)
    assert item.end_utc == item.panels[-1].end_utc


def test_mosaic_without_profile_is_honest_about_not_expanding(ephemeris: Ephemeris) -> None:
    plan = _plan(
        MosaicItem(
            id="m",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 23, 0)),
            duration_min=10,
            ra_j2000_h=6.5317,
            dec_j2000_deg=4.9407,
            mosaic=MosaicGridSpec(ra_num=2, dec_num=2, overlap_pct=20),
        )
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=None,
    )
    item = schedule.items[0]
    assert item.panels == ()
    assert item.start_utc is not None  # still resolved, just not expanded
    codes = [d.code for d in item.diagnostics]
    assert "mosaic_panels_not_expanded" in codes


def test_relative_chains_from_previous_end(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _plan(
        StartupItem(
            id="a",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 0)),
            duration_min=10,
        ),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    a, b = schedule.items
    assert a.end_utc is not None
    assert b.start_utc == a.end_utc + timedelta(minutes=5)


def test_relative_after_unschedulable_item_is_itself_unschedulable(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    plan = _plan(
        # rises_above 89 deg never happens at this site/target: unschedulable.
        StartupItem(
            id="a",
            start=TargetEventTimeSpec(event=TargetEvent.RISES_ABOVE, alt_deg=89.0),
        ),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    # a TargetEventTimeSpec on a startup item (no target) is unschedulable
    # for a different, more basic reason — that is exactly the case this
    # test wants: it proves the *chain* propagates regardless of *why* the
    # predecessor failed.
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    a, b = schedule.items
    assert a.start_utc is None
    assert b.start_utc is None
    assert any(d.code == "relative_no_previous" for d in b.diagnostics)


def test_disabled_item_is_not_chained_from(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _plan(
        StartupItem(
            id="a",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 0)),
            duration_min=10,
        ),
        StartupItem(
            id="disabled",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 30)),
            duration_min=999,
            enabled=False,
        ),
        ShutdownItem(id="b", start=RelativeTimeSpec(offset_min=5)),
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    a, disabled, b = schedule.items
    assert disabled.start_utc is not None  # still compiled, just not chained from
    assert a.end_utc is not None
    assert b.start_utc == a.end_utc + timedelta(minutes=5)


def test_event_that_does_not_occur_is_unschedulable_not_guessed(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    # A far-southern site in southern-hemisphere winter still has astronomical
    # dusk every night, so pick a genuinely absent event instead: moonrise on
    # a night the almanac shows none in the search window is site/date
    # specific and brittle, so this test instead forces the absence via an
    # extreme min-altitude target_event, which is exercised in the
    # high-latitude test module for the real astronomical-twilight case.
    plan = _plan(
        GotoAndStackItem(
            id="g",
            start=TargetEventTimeSpec(event=TargetEvent.RISES_ABOVE, alt_deg=89.9),
            ra_j2000_h=0.0,
            dec_j2000_deg=-80.0,
        )
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert item.start_utc is None
    assert item.end_utc is None
    assert any(d.code == "target_event_does_not_occur" for d in item.diagnostics)


def test_overlap_is_flagged(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _plan(
        StartupItem(
            id="a",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 0)),
            duration_min=30,
        ),
        ShutdownItem(id="b", start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 10))),
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    b = schedule.items[1]
    assert any(d.code == "overlaps_previous" for d in b.diagnostics)


def test_item_moved_before_previous_without_intersection_is_not_flagged(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    plan = _plan(
        StartupItem(
            id="a",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 0)),
            duration_min=30,
        ),
        StartupItem(
            id="b",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 21, 0)),
            duration_min=30,
        ),
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    b = schedule.items[1]
    assert not any(d.code == "overlaps_previous" for d in b.diagnostics)


def test_slew_can_resolve_polar_alignment_best_field(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    start_local = _local(2026, 8, 28, 22, 0)
    item = SlewItem(
        id="pa-field",
        start=AbsoluteTimeSpec(local_datetime=start_local),
        destination="polar_best",
        target=None,
    )
    schedule = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    compiled = schedule.items[0]
    assert compiled.start_utc is not None
    expected = best_conditioned_field(
        ephemeris, _SITE, compiled.start_utc, point_count=5, step_deg=Deg(15.0)
    )
    assert compiled.target is not None
    assert compiled.target.kind == "polar_best"
    assert compiled.target.ra_j2000_deg == pytest.approx(float(expected.coord.ra_deg))
    assert compiled.target.dec_j2000_deg == pytest.approx(float(expected.coord.dec_deg))


def test_device_action_estimate_is_display_only_and_overridable(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    item = SlewItem(
        id="slew",
        start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 22, 0)),
        target={"kind": "catalog", "name": "M31", "ra_j2000_h": 0.712, "dec_j2000_deg": 41.269},
        planning_estimate_min=3.5,
        timeout_min=4.0,
    )
    compiled = compile_plan(
        _plan(item), site=_SITE, tz=_TZ, night_date=_NIGHT_DATE,
        ephemeris=ephemeris, now=_NOW, profile=profile,
    ).items[0]
    assert compiled.start_utc is not None
    assert compiled.end_utc == compiled.start_utc + timedelta(minutes=3.5)
    assert "operator override" in compiled.end_resolution
    assert "actual completion comes from device state" in compiled.end_resolution


def test_moon_too_close_is_flagged(ephemeris: Ephemeris, profile: Profile) -> None:
    plan = _plan(
        GotoAndStackItem(
            id="g",
            start=AbsoluteTimeSpec(local_datetime=_local(2026, 8, 28, 23, 0)),
            duration_min=10,
            ra_j2000_h=6.5317,
            dec_j2000_deg=4.9407,
            constraints=ConstraintsSpec(min_moon_sep_deg=179.0),
        )
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert any(d.code == "moon_too_close" for d in item.diagnostics)


def test_altaz_high_altitude_transit_is_flagged(ephemeris: Ephemeris, profile: Profile) -> None:
    # A target whose declination sits near the site's own latitude transits
    # near the zenith — exactly DESIGN.md 3.2's high-altitude case.
    plan = _plan(
        GotoAndStackItem(
            id="g",
            start=TargetEventTimeSpec(event=TargetEvent.TRANSITS, offset_min=-60),
            duration_min=120,
            ra_j2000_h=12.0,
            dec_j2000_deg=float(_SITE.latitude_deg) - 1.0,
            framing=FramingSpec(),
        )
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT_DATE,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    )
    item = schedule.items[0]
    assert any(d.code == "high_altitude_meridian_crossing" for d in item.diagnostics)
