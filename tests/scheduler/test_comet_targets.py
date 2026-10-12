# SPDX-License-Identifier: GPL-3.0-only

"""Comet plans retain identity and resolve a guarded orbit at execution time."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from ssc.astro.comets import comet_execution_rejection, comet_position, load_comet_catalog
from ssc.astro.events import Ephemeris, Site
from ssc.astro.units import Deg, Metres
from ssc.plan import AbsoluteTimeSpec, CometTargetSpec, Plan, RelativeTimeSpec, SlewItem, parse_plan
from ssc.scheduler import compile_plan
from ssc.scheduler.executor import (
    EffectName,
    EphemerisTargetResolver,
    TargetResolutionError,
    run,
)
from tests.scheduler.fakes import (
    FakeClock,
    FakeControls,
    FakeDeviceCommands,
    FakeDeviceEvents,
    FakeJournal,
)

_SITE = Site(latitude_deg=Deg(49.12), longitude_deg=Deg(18.45), elevation_m=Metres(290.0))


def _safe_target(at: datetime) -> CometTargetSpec:
    catalog = load_comet_catalog()
    comet = min(
        (obj for obj in catalog.objects if comet_execution_rejection(catalog, obj, at) is None),
        key=lambda obj: abs(obj.epoch_jd_tdb - (at.timestamp() / 86_400.0 + 2_440_587.5)),
    )
    return CometTargetSpec(
        spk_id=comet.spk_id,
        designation=comet.designation,
        full_name=comet.full_name,
        source_url=catalog.source_url,
        snapshot_fetched_at=catalog.fetched_at,
        solution_date=comet.solution_date,
        element_epoch_jd_tdb=comet.epoch_jd_tdb,
    )


def test_comet_identity_and_provenance_round_trip_without_preview_coordinates() -> None:
    at = load_comet_catalog().fetched_at
    target = _safe_target(at)
    plan = Plan(
        id="comet-plan",
        name="comet",
        items=(SlewItem(id="slew", start=RelativeTimeSpec(offset_min=0), target=target),),
        created_at=at,
        updated_at=at,
    )

    restored = parse_plan(json.loads(plan.model_dump_json()))

    assert restored == plan
    document_target = json.loads(plan.model_dump_json())["items"][0]["target"]
    assert document_target["spk_id"] == target.spk_id
    assert document_target["designation"] == target.designation
    assert document_target["source_url"] == target.source_url
    assert "ra_j2000_h" not in document_target
    assert "dec_j2000_deg" not in document_target


async def test_executor_resolves_the_current_orbit_instant_before_slew(
    ephemeris: Ephemeris,
) -> None:
    catalog = load_comet_catalog()
    at = catalog.fetched_at
    target = _safe_target(at)
    comet = catalog.find(spk_id=target.spk_id, designation=target.designation)
    assert comet is not None

    result = await EphemerisTargetResolver(
        ephemeris=ephemeris, site=_SITE, comet_catalog=catalog
    ).resolve(target, at)
    expected = comet_position(ephemeris, _SITE, comet, at)

    assert result.at == at.astimezone(UTC)
    assert result.ra_j2000_h == pytest.approx(float(expected.ra_j2000_h), abs=1e-9)
    assert result.dec_j2000_deg == pytest.approx(float(expected.dec_j2000_deg), abs=1e-9)
    assert result.tracking_rate == "sidereal"


async def test_run_uses_the_execution_instant_not_an_authored_preview(
    ephemeris: Ephemeris,
) -> None:
    catalog = load_comet_catalog()
    at = catalog.fetched_at.replace(microsecond=0)
    target = _safe_target(at)
    item = SlewItem(
        id="slew",
        start=AbsoluteTimeSpec(local_datetime=at.replace(tzinfo=None)),
        target=target,
    )
    plan = Plan(
        id="comet-run",
        name="comet",
        items=(item,),
        created_at=at,
        updated_at=at,
    )
    schedule = compile_plan(
        plan,
        site=_SITE,
        tz=ZoneInfo("UTC"),
        night_date=at.date(),
        ephemeris=ephemeris,
        now=at,
        comet_catalog=catalog,
    )
    device = FakeDeviceCommands()
    await run(
        run_id="comet-run",
        schedule=schedule,
        plan=plan,
        journal=FakeJournal("comet-run"),
        device=device,
        events=FakeDeviceEvents(),
        clock=FakeClock(at),
        controls=FakeControls(),
        resolver=EphemerisTargetResolver(
            ephemeris=ephemeris, site=_SITE, comet_catalog=catalog
        ),
        poll_interval_s=60,
    )
    slew = next(args for name, args in device.calls if name is EffectName.SLEW_TO)
    comet = catalog.find(spk_id=target.spk_id, designation=target.designation)
    assert comet is not None
    expected = comet_position(ephemeris, _SITE, comet, at)
    assert slew["ra_j2000_h"] == pytest.approx(float(expected.ra_j2000_h), abs=1e-9)
    assert slew["dec_j2000_deg"] == pytest.approx(float(expected.dec_j2000_deg), abs=1e-9)


async def test_executor_refuses_a_recycled_or_mismatched_comet_identity(
    ephemeris: Ephemeris,
) -> None:
    catalog = load_comet_catalog()
    target = _safe_target(catalog.fetched_at).model_copy(
        update={"designation": "definitely-not-the-same-comet"}
    )

    with pytest.raises(TargetResolutionError, match="unavailable or no longer matches"):
        await EphemerisTargetResolver(
            ephemeris=ephemeris, site=_SITE, comet_catalog=catalog
        ).resolve(target, catalog.fetched_at)


def test_execution_guard_refuses_a_stale_snapshot_and_old_elements() -> None:
    catalog = load_comet_catalog()
    at = catalog.fetched_at
    target = _safe_target(at)
    comet = catalog.find(spk_id=target.spk_id, designation=target.designation)
    assert comet is not None

    stale = replace(catalog, fetched_at=at - timedelta(days=46))
    assert "snapshot" in (comet_execution_rejection(stale, comet, at) or "")

    old_elements = replace(comet, epoch_jd_tdb=comet.epoch_jd_tdb - 366)
    assert "orbit elements" in (comet_execution_rejection(catalog, old_elements, at) or "")
