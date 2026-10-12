from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from ssc.astro.auto_night import choose_filter, plan_auto_night
from ssc.astro.best_targets import MoonSensitivity, moon_sensitivity_of
from ssc.astro.catalog import ObjectType, load_catalog
from ssc.astro.events import Site
from ssc.astro.framing import field_geometry, load_profile
from ssc.astro.horizon import flat_horizon
from ssc.astro.units import Deg


@pytest.mark.parametrize("fraction", [0, 0.01, 1])
def test_moon_down_is_clean_even_full(fraction):
    assert choose_filter(
        ObjectType.GALAXY, frozenset({"IR"}), np.array([-5]), np.array([5]), np.array([fraction])
    ) == ("IR", "moon_down_ir")


def test_full_moon_global_skyglow_rejects_far_galaxy():
    assert (
        choose_filter(
            ObjectType.GALAXY,
            frozenset({"IR", "LP"}),
            np.array([50]),
            np.array([170]),
            np.array([1]),
        )
        is None
    )


def test_moon_rises_during_block_rejects_even_if_peak_clean():
    assert (
        choose_filter(
            ObjectType.GALAXY,
            frozenset({"IR"}),
            np.array([-5, 20]),
            np.array([50, 50]),
            np.array([0.4, 0.4]),
        )
        is None
    )


def test_full_moon_accepts_only_real_emission_lp():
    for kind in [ObjectType.EMISSION_NEBULA, ObjectType.PLANETARY_NEBULA]:
        assert choose_filter(
            kind, frozenset({"LP"}), np.array([50]), np.array([80]), np.array([1])
        ) == ("LP", "emission_lp_moon")
    for kind in [ObjectType.DIFFUSE_NEBULA, ObjectType.REFLECTION_NEBULA, ObjectType.GALAXY]:
        assert (
            choose_filter(kind, frozenset({"LP"}), np.array([50]), np.array([80]), np.array([1]))
            is None
        )


def test_new_moon_accepts_broadband_close():
    assert choose_filter(
        ObjectType.GALAXY, frozenset({"IR"}), np.array([50]), np.array([5]), np.array([0])
    ) == ("IR", "moon_down_ir")


@pytest.mark.parametrize("allowed,horizon", [(frozenset(), None), (None, flat_horizon(Deg(89)))])
def test_explicit_empty_and_blocked_horizon(ephemeris, allowed, horizon):
    start = datetime(2026, 10, 7, 10, tzinfo=UTC)
    result = plan_auto_night(
        ephemeris,
        Site(Deg(48.71), Deg(17.85)),
        load_catalog(),
        start,
        start + timedelta(hours=24),
        geometry=field_geometry(load_profile(Path("data/profiles/zwo-s30pro.toml")), "tele"),
        allowed_object_types=allowed,
        horizon_profile=horizon,
        max_targets=2,
    )
    assert not result.blocks


def test_real_night_blocks_are_deterministic_nonoverlapping_and_clear(ephemeris):
    start = datetime(2026, 10, 7, 10, tzinfo=UTC)
    args = (
        ephemeris,
        Site(Deg(48.71), Deg(17.85)),
        load_catalog(),
        start,
        start + timedelta(hours=24),
    )
    kwargs = {
        "geometry": field_geometry(load_profile(Path("data/profiles/zwo-s30pro.toml")), "tele"),
        "allowed_object_types": frozenset({ObjectType.GALAXY, ObjectType.EMISSION_NEBULA}),
        "max_targets": 3,
    }
    result = plan_auto_night(*args, **kwargs)
    assert result.blocks
    assert result == plan_auto_night(*args, **kwargs)
    for b in result.blocks:
        assert b.min_altitude_deg >= 31
        assert b.candidate.object.object_type in kwargs["allowed_object_types"]
    assert all(
        a.end_time <= b.start_time for a, b in zip(result.blocks, result.blocks[1:], strict=False)
    )


@pytest.mark.parametrize("day,full", [(3, True), (18, False)])
def test_real_full_and_new_moon_policy(ephemeris, day, full):
    from dataclasses import replace

    catalog = load_catalog()
    catalog = replace(
        catalog,
        objects=tuple(
            obj
            for obj in catalog.objects
            if any(name in {"M31", "M81", "M1"} for name in obj.designations)
        ),
    )
    start = datetime(2026, 1, day, 12, tzinfo=UTC)
    result = plan_auto_night(
        ephemeris,
        Site(Deg(48.71), Deg(17.85)),
        catalog,
        start,
        start + timedelta(hours=24),
        geometry=field_geometry(load_profile(Path("data/profiles/zwo-s30pro.toml")), "tele"),
        block_minutes=30,
        max_targets=3,
        available_filters=frozenset({"IR"}),
    )
    assert (
        result.moon_illuminated_fraction > 0.98 if full else result.moon_illuminated_fraction < 0.02
    )
    for b in result.blocks:
        if (
            full
            and moon_sensitivity_of(b.candidate.object.object_type) == MoonSensitivity.BROADBAND
        ):
            assert b.max_moon_altitude_deg < 15
    if not full:
        assert any(
            moon_sensitivity_of(b.candidate.object.object_type) == MoonSensitivity.BROADBAND
            for b in result.blocks
        )


def test_real_same_field_satellite_not_allocated_twice(ephemeris):
    from dataclasses import replace

    catalog = load_catalog()
    catalog = replace(
        catalog,
        objects=tuple(
            obj
            for obj in catalog.objects
            if any(name in {"M31", "M32", "M110"} for name in obj.designations)
        ),
    )
    start = datetime(2026, 10, 7, 10, tzinfo=UTC)
    result = plan_auto_night(
        ephemeris,
        Site(Deg(48.71), Deg(17.85)),
        catalog,
        start,
        start + timedelta(hours=24),
        geometry=field_geometry(load_profile(Path("data/profiles/zwo-s30pro.toml")), "tele"),
        block_minutes=30,
        max_targets=3,
    )
    assert len(result.blocks) == 1
