"""Solar-system bodies as plan targets (DESIGN.md 3.6).

The one thing every test here is really checking: **the plan holds a name and
the executor asks where it is when the item runs**. The measurement that makes
that non-negotiable is in DESIGN 3.6 — over 6 h of one night the Moon moves
233.6' in right ascension, Jupiter 3.1' — so a plan resolved at authoring time
images empty sky for the Moon and is merely sloppy for Jupiter. Both magnitudes
are asserted below, because the mechanism has to be the same either way.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from ssc.astro.bodies import BodyId
from ssc.astro.events import Ephemeris, Site
from ssc.astro.framing import Profile
from ssc.astro.targets import TrackingRate, resolve_position
from ssc.astro.units import Deg, Metres
from ssc.plan import (
    CURRENT_PLAN_SCHEMA_VERSION,
    BodyTargetSpec,
    CatalogTargetSpec,
    GotoAndStackItem,
    MosaicGridSpec,
    MosaicItem,
    Plan,
    RelativeTimeSpec,
    SlewItem,
    parse_plan,
)
from ssc.plan.models import AbsoluteTimeSpec, DurationEndSpec
from ssc.scheduler import compile_plan
from ssc.scheduler.executor import (
    EffectName,
    EphemerisTargetResolver,
    ItemStatus,
    RunStatus,
    run,
)
from tests.scheduler.fakes import (
    FakeCapturePort,
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)

_SITE = Site(latitude_deg=Deg(48.1486), longitude_deg=Deg(17.1077), elevation_m=Metres(150.0))
_TZ = ZoneInfo("Europe/Bratislava")
_NIGHT = date(2026, 9, 10)
_NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
#: The item's start. Six hours later is the measurement window DESIGN 3.6 quotes.
_T = datetime(2026, 9, 10, 18, 0, tzinfo=UTC)
_SIX_HOURS = timedelta(hours=6)


def _plan(*items: object) -> Plan:
    return Plan(
        id="body-plan",
        name="bodies",
        items=tuple(items),  # type: ignore[arg-type]
        created_at=_NOW,
        updated_at=_NOW,
    )


def _ra_delta_arcmin(a: float, b: float) -> float:
    """|difference| of two J2000 right ascensions in hours, as arcmin of RA."""
    return abs(a - b) * 15.0 * 60.0


# --- 1. the point of the whole change ----------------------------------------


async def _slew_args_at(
    ephemeris: Ephemeris, body: BodyId, execute_at: datetime
) -> dict[str, float]:
    """Run a one-item plan that starts at ``execute_at`` and return the RA/Dec
    the executor actually slewed to."""
    item = GotoAndStackItem(
        id="goto",
        start=AbsoluteTimeSpec(local_datetime=execute_at.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=20.0),
        target=BodyTargetSpec(body=body),
    )
    schedule = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT,
        ephemeris=ephemeris,
        now=_NOW,
        profile=None,  # no profile -> one segment, so exactly one slew to inspect
    )
    device = FakeDeviceCommands()
    clock = FakeClock(execute_at - timedelta(minutes=1))
    await run(
        run_id="body-run",
        schedule=schedule,
        plan=_plan(item),
        journal=FakeJournal("body-run"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        resolver=EphemerisTargetResolver(ephemeris=ephemeris, site=_SITE),
        capture=FakeCapturePort(device, clock=clock, cycle_s=20 * 60),
        poll_interval_s=3600.0,
    )
    slews = [args for name, args in device.calls if name is EffectName.SLEW_TO]
    assert len(slews) == 1, device.calls
    return slews[0]


@pytest.mark.parametrize(
    ("body", "min_arcmin", "max_arcmin"),
    [
        # DESIGN 3.6's own measurements, with room for a different night and a
        # different site: the Moon's RA motion over 6 h is ~233', Jupiter's ~3'.
        # The bands are wide because the point is the *order of magnitude* — a
        # plan resolved at authoring time is catastrophically wrong for the Moon
        # and quietly wrong for Jupiter, and the mechanism must not care which.
        (BodyId.MOON, 150.0, 320.0),
        (BodyId.JUPITER, 1.0, 8.0),
    ],
)
async def test_a_body_is_resolved_when_the_item_runs_not_when_it_was_written(
    ephemeris: Ephemeris, body: BodyId, min_arcmin: float, max_arcmin: float
) -> None:
    """Compiled at T, executed at T+6h: the scope goes to where the body is at
    T+6h, and that is measurably *not* where it was at T."""
    late = await _slew_args_at(ephemeris, body, _T + _SIX_HOURS)

    at_t = resolve_position(ephemeris, _SITE, BodyTargetSpec(body=body).to_ref(), _T)
    at_t6 = resolve_position(ephemeris, _SITE, BodyTargetSpec(body=body).to_ref(), _T + _SIX_HOURS)

    # It slewed to the T+6h position, to the arcsecond.
    assert late["ra_j2000_h"] == pytest.approx(float(at_t6.ra_j2000_h), abs=1e-6)
    assert late["dec_j2000_deg"] == pytest.approx(float(at_t6.dec_j2000_deg), abs=1e-6)

    # And that is the expected distance from where a plan-time resolve would
    # have frozen it — i.e. the bug this feature exists to prevent is real.
    moved = _ra_delta_arcmin(float(at_t6.ra_j2000_h), float(at_t.ra_j2000_h))
    assert min_arcmin < moved < max_arcmin, f"{body} moved {moved:.1f}' in RA over 6 h"
    assert _ra_delta_arcmin(late["ra_j2000_h"], float(at_t.ra_j2000_h)) == pytest.approx(
        moved, abs=1e-6
    )


async def test_the_same_item_run_at_two_times_points_at_two_places(
    ephemeris: Ephemeris,
) -> None:
    """The same plan document, executed six hours apart, produces two different
    slews — nothing about the *document* changed."""
    early = await _slew_args_at(ephemeris, BodyId.MOON, _T)
    late = await _slew_args_at(ephemeris, BodyId.MOON, _T + _SIX_HOURS)
    assert _ra_delta_arcmin(early["ra_j2000_h"], late["ra_j2000_h"]) > 150.0


# --- the tracking rate travels with the target -------------------------------


async def test_selecting_the_moon_carries_the_lunar_rate(ephemeris: Ephemeris) -> None:
    """DESIGN 3.6: "A body target and its tracking rate are one decision, not
    two." The rate is set before the slew, and it is never in the document."""
    item = GotoAndStackItem(
        id="goto",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=20.0),
        target=BodyTargetSpec(body=BodyId.MOON),
    )
    plan = _plan(item)
    schedule = compile_plan(
        plan, site=_SITE, tz=_TZ, night_date=_NIGHT, ephemeris=ephemeris, now=_NOW, profile=None
    )
    device = FakeDeviceCommands()
    clock = FakeClock(_T - timedelta(minutes=1))
    await run(
        run_id="rate-run",
        schedule=schedule,
        plan=plan,
        journal=FakeJournal("rate-run"),
        device=device,
        events=FakeDeviceEvents(),
        clock=clock,
        controls=FakeControls(),
        resolver=EphemerisTargetResolver(ephemeris=ephemeris, site=_SITE),
        capture=FakeCapturePort(device, clock=clock, cycle_s=20 * 60),
        poll_interval_s=3600.0,
    )
    names = [name for name, _ in device.calls]
    assert names.index(EffectName.SET_TRACKING_RATE) < names.index(EffectName.SLEW_TO)
    rate = next(args for name, args in device.calls if name is EffectName.SET_TRACKING_RATE)
    assert rate == {"rate": "lunar"}
    assert schedule.items[0].target is not None
    assert schedule.items[0].target.tracking_rate == "lunar"


def test_a_catalogue_item_puts_the_mount_back_on_sidereal() -> None:
    """The corollary, and the reason the rate is issued for *every* target and
    not only for bodies: a Moon item must not leave the next target drifting."""
    assert CatalogTargetSpec(name="M42", ra_j2000_h=5.5, dec_j2000_deg=-5.4).tracking_rate is (
        TrackingRate.SIDEREAL
    )
    assert BodyTargetSpec(body=BodyId.MOON).tracking_rate is TrackingRate.LUNAR
    assert BodyTargetSpec(body=BodyId.JUPITER).tracking_rate is TrackingRate.SIDEREAL


# --- 2. the document: round-trip, older plans, and the Sun -------------------


def test_a_body_target_round_trips_through_the_plan_document() -> None:
    plan = _plan(
        GotoAndStackItem(
            id="goto",
            start=RelativeTimeSpec(offset_min=0.0),
            end=DurationEndSpec(minutes=30.0),
            target=BodyTargetSpec(body=BodyId.MOON),
        )
    )
    restored = parse_plan(json.loads(plan.model_dump_json()))
    assert restored == plan
    target = restored.items[0].target  # type: ignore[union-attr]
    assert isinstance(target, BodyTargetSpec)
    assert target.body is BodyId.MOON
    # A body target has no coordinates, and asking for them says so rather than
    # answering zero (architecture policy invariant 7).
    assert restored.items[0].ra_j2000_h is None  # type: ignore[union-attr]
    assert restored.items[0].dec_j2000_deg is None  # type: ignore[union-attr]
    assert restored.items[0].target_name == "Moon"  # type: ignore[union-attr]


def test_an_older_plan_without_body_targets_still_loads() -> None:
    """A schema_version 1 document — flat ``target_name``/``ra_j2000_h``/
    ``dec_j2000_deg`` — migrates into a catalogue target unchanged."""
    v1 = {
        "schema_version": 1,
        "id": "old",
        "name": "an older plan",
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
        "items": [
            {
                "kind": "goto_and_stack",
                "id": "goto",
                "start": {"kind": "asap"},
                "duration_min": 45,
                "target_name": "NGC 2244",
                "ra_j2000_h": 6.5317,
                "dec_j2000_deg": 4.9407,
            }
        ],
    }
    plan = parse_plan(v1)
    assert plan.schema_version == CURRENT_PLAN_SCHEMA_VERSION
    item = plan.items[0]
    assert isinstance(item.target, CatalogTargetSpec)  # type: ignore[union-attr]
    assert item.ra_j2000_h == 6.5317  # type: ignore[union-attr]
    assert item.target_name == "NGC 2244"  # type: ignore[union-attr]


def test_the_golden_plan_fixture_is_still_a_v1_document() -> None:
    """Belt and braces on the migration: the checked-in golden plan is v1, so
    ``test_golden_schedule`` is itself a standing test that v1 still compiles."""
    path = Path(__file__).resolve().parents[1] / "fixtures" / "scheduler" / "golden_plan.json"
    assert json.loads(path.read_text())["schema_version"] == 1


def test_a_plan_naming_the_sun_is_refused_with_the_reason() -> None:
    with pytest.raises(ValidationError, match="mechanical shutter"):
        BodyTargetSpec(body=BodyId.SUN)


def test_a_hand_edited_plan_file_naming_the_sun_is_refused_too(tmp_path: Path) -> None:
    """A plan is a portable document and therefore untrusted input: the refusal
    has to happen on the way *in*, not only in the UI that offers the choice."""
    document = {
        "schema_version": 2,
        "id": "sneaky",
        "name": "hand-edited",
        "created_at": "2026-08-01T12:00:00Z",
        "updated_at": "2026-08-01T12:00:00Z",
        "items": [
            {
                "kind": "goto_and_stack",
                "id": "goto",
                "start": {"kind": "asap"},
                "end": {"kind": "duration", "minutes": 30},
                "target": {"kind": "body", "body": "sun"},
            }
        ],
    }
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document))

    with pytest.raises(ValidationError) as excinfo:
        parse_plan(json.loads(path.read_text()))
    message = str(excinfo.value)
    assert "not selectable as a target" in message
    assert "destroy the sensor" in message


def test_an_item_cannot_carry_both_spellings_of_a_target() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        GotoAndStackItem.model_validate(
            {
                "kind": "goto_and_stack",
                "id": "goto",
                "start": {"kind": "asap"},
                "target": {"kind": "body", "body": "moon"},
                "ra_j2000_h": 5.5,
                "dec_j2000_deg": -5.4,
            }
        )


# --- 4. the long-session decision, as compiled numbers -----------------------


def test_a_long_moon_session_is_cut_into_recentring_segments(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    """The explicit decision (compiler.MAX_BODY_DRIFT_FRACTION_OF_FRAME): a body
    item is re-centred whenever the body would otherwise have drifted more than
    a tenth of the frame's short side. For the Moon over six hours that is a
    handful of segments; the drift and the interval are both echoed."""
    item = GotoAndStackItem(
        id="moon",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=360.0),
        target=BodyTargetSpec(body=BodyId.MOON),
    )
    compiled = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    ).items[0]

    assert len(compiled.panels) > 1
    assert all(p.ra_j2000_deg is None and p.dec_j2000_deg is None for p in compiled.panels)
    assert compiled.panels[0].start_utc == compiled.start_utc
    assert compiled.panels[-1].end_utc == compiled.end_utc
    assert compiled.target is not None
    assert compiled.target.recentre_interval_min is not None
    assert compiled.target.recentre_interval_min < 60.0
    # The 6 h drift DESIGN 3.6 measures, now a number the compiler carries.
    assert 150.0 < (compiled.target.drift_arcmin or 0.0) < 320.0
    assert any(d.code == "body_recentred" for d in compiled.diagnostics)


def test_a_planet_needs_no_recentring(ephemeris: Ephemeris, profile: Profile) -> None:
    """Same mechanism, proportional cost: Jupiter's 0.5'/h does not move it out
    of the frame in a night, so a six-hour session is one segment."""
    item = GotoAndStackItem(
        id="jupiter",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=360.0),
        target=BodyTargetSpec(body=BodyId.JUPITER),
    )
    compiled = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    ).items[0]
    assert len(compiled.panels) == 1
    assert not any(d.code == "body_recentred" for d in compiled.diagnostics)


def test_without_a_profile_the_frame_is_unknown_and_says_so(ephemeris: Ephemeris) -> None:
    """No profile means no frame size, so the interval cannot be computed. The
    item still runs; it is not silently segmented on a guessed frame."""
    item = GotoAndStackItem(
        id="moon",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=360.0),
        target=BodyTargetSpec(body=BodyId.MOON),
    )
    compiled = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT,
        ephemeris=ephemeris,
        now=_NOW,
        profile=None,
    ).items[0]
    assert compiled.panels == ()
    assert compiled.target is not None
    assert compiled.target.recentre_interval_min is None
    assert any(d.code == "body_not_segmented" for d in compiled.diagnostics)


def test_a_mosaic_on_a_body_is_refused(ephemeris: Ephemeris, profile: Profile) -> None:
    """Panel centres are fixed relative to the target; a body is not fixed. The
    compiler says so rather than laying out panels that are wrong on arrival."""
    item = MosaicItem(
        id="m",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        end=DurationEndSpec(minutes=20.0),
        target=BodyTargetSpec(body=BodyId.MOON),
        mosaic=MosaicGridSpec(ra_num=2, dec_num=2, overlap_pct=20.0),
    )
    compiled = compile_plan(
        _plan(item),
        site=_SITE,
        tz=_TZ,
        night_date=_NIGHT,
        ephemeris=ephemeris,
        now=_NOW,
        profile=profile,
    ).items[0]
    assert compiled.panels == ()
    assert compiled.end_utc is None
    codes = {d.code for d in compiled.diagnostics}
    assert "mosaic_on_body_target" in codes


# --- no resolver, no guess ----------------------------------------------------


async def test_a_body_item_without_a_resolver_waits_through_window_without_guessing(ephemeris: Ephemeris) -> None:
    """An unavailable resolver exhausts the window visibly without guessed motion."""
    item = SlewItem(
        id="slew",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        target=BodyTargetSpec(body=BodyId.MARS),
    )
    plan = _plan(item)
    schedule = compile_plan(
        plan, site=_SITE, tz=_TZ, night_date=_NIGHT, ephemeris=ephemeris, now=_NOW, profile=None
    )
    device = FakeDeviceCommands()
    state = await run(
        run_id="no-resolver",
        schedule=schedule,
        plan=plan,
        journal=FakeJournal("no-resolver"),
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(_T - timedelta(minutes=1)),
        controls=FakeControls(),
        resolver=None,
        poll_interval_s=3600.0,
    )
    assert state.run_status is RunStatus.COMPLETED
    assert state.items["slew"].status is ItemStatus.DONE
    assert "no target resolver" in state.items["slew"].reason
    assert device.calls == []


async def test_slew_to_polar_best_uses_compiled_field_without_target_resolver(
    ephemeris: Ephemeris,
) -> None:
    item = SlewItem(
        id="pa-field",
        start=AbsoluteTimeSpec(local_datetime=_T.astimezone(_TZ).replace(tzinfo=None)),
        destination="polar_best",
        target=None,
    )
    plan = _plan(item)
    schedule = compile_plan(
        plan, site=_SITE, tz=_TZ, night_date=_NIGHT, ephemeris=ephemeris, now=_NOW, profile=None
    )
    compiled_target = schedule.items[0].target
    assert compiled_target is not None
    assert compiled_target.ra_j2000_deg is not None
    assert compiled_target.dec_j2000_deg is not None

    device = FakeDeviceCommands()
    state = await run(
        run_id="pa-best-slew",
        schedule=schedule,
        plan=plan,
        journal=FakeJournal("pa-best-slew"),
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(_T - timedelta(minutes=1)),
        controls=FakeControls(),
        resolver=None,
        poll_interval_s=3600.0,
    )
    assert state.run_status is RunStatus.COMPLETED
    slews = [args for name, args in device.calls if name is EffectName.SLEW_TO]
    assert slews == [
        {
            "ra_j2000_h": pytest.approx(compiled_target.ra_j2000_deg / 15.0),
            "dec_j2000_deg": pytest.approx(compiled_target.dec_j2000_deg),
        }
    ]


def test_utc_everywhere(ephemeris: Ephemeris) -> None:
    """The resolved instant that comes back is UTC-aware, never naive."""
    resolved = resolve_position(ephemeris, _SITE, BodyTargetSpec(body=BodyId.MOON).to_ref(), _T)
    assert resolved.when.tzinfo is not None
    assert resolved.when.astimezone(UTC) == _T
