"""Unit tests for ssc.astro.best_targets: the moon-sensitivity domain
judgement, the pure scoring math, and the end-to-end ranking against a real
ephemeris.

The acceptance test this feature actually lives or dies by — pasting the
real top-10 for a new-Moon and a full-Moon night and reading whether the
ranking is sensible — is not a pytest assertion; see the task report for
those two lists. What follows are the mechanical guarantees: a galaxy must
rank below an emission nebula under a bright, high, close Moon; that gap
must narrow (or vanish) once the Moon is down; a target that never clears
the altitude limit tonight must be excluded; unknown magnitude/size must
never be scored as if it were good or bad; and ties must break
deterministically.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ssc.astro.best_targets import (
    MAGNITUDE_BRIGHT_REFERENCE,
    MAGNITUDE_FAINT_REFERENCE,
    MoonImpact,
    MoonSensitivity,
    SizeFit,
    moon_sensitivity_of,
    rank_best_targets,
    score_target,
)
from ssc.astro.catalog import CatalogObject, ObjectType, TargetCatalog
from ssc.astro.events import Ephemeris, Site
from ssc.astro.framing import Profile, field_geometry
from ssc.astro.units import ArcMin, Deg

BUCHAREST = Site(latitude_deg=Deg(44.4268), longitude_deg=Deg(26.1025))

# A 24h window picked away from local midnight, the convention every other
# ssc.astro.events window-taking function assumes — see annual_altitude_profile.
_WINDOW_START = datetime(2026, 1, 3, 12, 0, tzinfo=UTC)  # full Moon, per moon_phase
_WINDOW_END = _WINDOW_START + timedelta(hours=24)
_NEW_MOON_START = datetime(2026, 1, 18, 12, 0, tzinfo=UTC)
_NEW_MOON_END = _NEW_MOON_START + timedelta(hours=24)


def _object(
    name: str,
    object_type: ObjectType | None,
    *,
    ra_deg: float = 90.0,
    dec_deg: float = 30.0,
    magnitude: float | None = None,
    size_major_arcmin: float | None = None,
) -> CatalogObject:
    return CatalogObject(
        designations=(name,),
        common_name=None,
        object_type=object_type,
        ra_j2000_deg=Deg(ra_deg),
        dec_j2000_deg=Deg(dec_deg),
        magnitude=magnitude,
        magnitude_band=None if magnitude is None else "V",
        size_major_arcmin=None if size_major_arcmin is None else ArcMin(size_major_arcmin),
        size_minor_arcmin=None,
        position_angle_deg=None,
        constellation=None,
        source="test",
    )


def _catalog(*objects: CatalogObject) -> TargetCatalog:
    return TargetCatalog(
        objects=tuple(objects),
        sources=("test",),
        _by_designation={},
        _by_common_name={},
    )


# -- moon_sensitivity_of: total over every ObjectType ------------------------- #


def test_moon_sensitivity_of_is_total_and_never_guesses_a_new_type() -> None:
    """Every current ObjectType is classified or explicitly excluded — this
    is the test the module docstring promises will catch a new ObjectType
    added without a place in the classification."""
    unclassified = {ObjectType.OTHER, ObjectType.NONEXISTENT}
    for object_type in ObjectType:
        result = moon_sensitivity_of(object_type)
        if object_type in unclassified:
            assert result is None
        else:
            assert isinstance(result, MoonSensitivity)


@pytest.mark.parametrize(
    ("object_type", "expected"),
    [
        (ObjectType.EMISSION_NEBULA, MoonSensitivity.NARROWBAND_FRIENDLY),
        (ObjectType.HII_REGION, MoonSensitivity.NARROWBAND_FRIENDLY),
        (ObjectType.PLANETARY_NEBULA, MoonSensitivity.NARROWBAND_FRIENDLY),
        (ObjectType.SUPERNOVA_REMNANT, MoonSensitivity.NARROWBAND_FRIENDLY),
        (ObjectType.GALAXY, MoonSensitivity.BROADBAND),
        (ObjectType.GALAXY_SPIRAL, MoonSensitivity.BROADBAND),
        (ObjectType.REFLECTION_NEBULA, MoonSensitivity.BROADBAND),
        (ObjectType.DARK_NEBULA, MoonSensitivity.DARK_SKY_REQUIRED),
        (ObjectType.OPEN_CLUSTER, MoonSensitivity.CLUSTER),
        (ObjectType.DIFFUSE_NEBULA, MoonSensitivity.MIXED),
        (ObjectType.CLUSTER_WITH_NEBULA, MoonSensitivity.MIXED),
        (ObjectType.STAR, MoonSensitivity.NEUTRAL),
    ],
)
def test_moon_sensitivity_of_known_buckets(
    object_type: ObjectType, expected: MoonSensitivity
) -> None:
    assert moon_sensitivity_of(object_type) is expected


# -- score_target: the pure domain judgement ---------------------------------- #

_COMMON_SCORE_KWARGS = {
    "hours_above_limit": 6.0,
    "darkness_hours": 8.0,
    "peak_altitude_deg": Deg(60.0),
    "min_altitude_deg": Deg(20.0),
}


def test_galaxy_ranks_below_emission_nebula_under_a_bright_high_close_moon() -> None:
    galaxy = score_target(
        object_type=ObjectType.GALAXY_SPIRAL,
        magnitude=9.0,
        size_major_arcmin=None,
        moon_illuminated_fraction=0.95,
        moon_altitude_at_peak_deg=Deg(70.0),
        moon_separation_deg=Deg(20.0),
        field_geometry=None,
        **_COMMON_SCORE_KWARGS,
    )
    nebula = score_target(
        object_type=ObjectType.EMISSION_NEBULA,
        magnitude=9.0,
        size_major_arcmin=None,
        moon_illuminated_fraction=0.95,
        moon_altitude_at_peak_deg=Deg(70.0),
        moon_separation_deg=Deg(20.0),
        field_geometry=None,
        **_COMMON_SCORE_KWARGS,
    )
    assert galaxy.score < nebula.score
    assert galaxy.moon_impact is MoonImpact.INTERFERING
    assert nebula.moon_impact is MoonImpact.NARROWBAND_TOLERANT
    assert nebula.narrowband_friendly
    assert not galaxy.narrowband_friendly


def test_the_moon_being_down_narrows_or_reverses_the_gap() -> None:
    """Same two targets as above; only the Moon's altitude changes. With the
    Moon below the horizon, phase is irrelevant (module docstring) and both
    targets are scored identically — the starkest possible demonstration of
    "narrows", since the gap goes to exactly zero."""

    def scored(object_type: ObjectType, moon_altitude_deg: float) -> float:
        return score_target(
            object_type=object_type,
            magnitude=9.0,
            size_major_arcmin=None,
            moon_illuminated_fraction=0.95,
            moon_altitude_at_peak_deg=Deg(moon_altitude_deg),
            moon_separation_deg=Deg(20.0),
            field_geometry=None,
            **_COMMON_SCORE_KWARGS,
        ).score

    gap_moon_up = scored(ObjectType.EMISSION_NEBULA, 70.0) - scored(ObjectType.GALAXY_SPIRAL, 70.0)
    gap_moon_down = scored(ObjectType.EMISSION_NEBULA, -10.0) - scored(
        ObjectType.GALAXY_SPIRAL, -10.0
    )
    assert gap_moon_up > 0.0
    assert gap_moon_down == pytest.approx(0.0)
    assert gap_moon_down < gap_moon_up


def test_moon_down_is_flagged_regardless_of_sensitivity() -> None:
    result = score_target(
        object_type=ObjectType.GALAXY,
        magnitude=None,
        size_major_arcmin=None,
        moon_illuminated_fraction=0.99,
        moon_altitude_at_peak_deg=Deg(-0.5),
        moon_separation_deg=Deg(5.0),
        field_geometry=None,
        **_COMMON_SCORE_KWARGS,
    )
    assert result.moon_impact is MoonImpact.MOON_DOWN


def test_score_target_rejects_an_unclassifiable_object_type() -> None:
    with pytest.raises(ValueError, match="MoonSensitivity"):
        score_target(
            object_type=ObjectType.OTHER,
            magnitude=None,
            size_major_arcmin=None,
            moon_illuminated_fraction=0.0,
            moon_altitude_at_peak_deg=Deg(-10.0),
            moon_separation_deg=Deg(90.0),
            field_geometry=None,
            **_COMMON_SCORE_KWARGS,
        )


# -- unknown magnitude/size: never guessed as good or bad --------------------- #


def test_unknown_magnitude_is_scored_without_the_brightness_bonus() -> None:
    def scored(magnitude: float | None) -> float:
        return score_target(
            object_type=ObjectType.OPEN_CLUSTER,
            magnitude=magnitude,
            size_major_arcmin=None,
            moon_illuminated_fraction=0.0,
            moon_altitude_at_peak_deg=Deg(-10.0),
            moon_separation_deg=Deg(90.0),
            field_geometry=None,
            **_COMMON_SCORE_KWARGS,
        ).score

    unknown = scored(None)
    bright = scored(MAGNITUDE_BRIGHT_REFERENCE)
    faint = scored(MAGNITUDE_FAINT_REFERENCE)
    # Unknown must not be treated as if it were the good (bright) value...
    assert unknown < bright
    # ...nor scored any worse than the worst *known* value the pool would
    # ever admit — "scored without that component" (invariant 7), not
    # "assumed faintest".
    assert unknown == pytest.approx(faint)


def test_unknown_size_gives_no_size_fit_and_no_penalty(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")

    def scored(size_major_arcmin: float | None) -> tuple[float, SizeFit | None]:
        result = score_target(
            object_type=ObjectType.PLANETARY_NEBULA,
            magnitude=None,
            size_major_arcmin=None if size_major_arcmin is None else ArcMin(size_major_arcmin),
            moon_illuminated_fraction=0.0,
            moon_altitude_at_peak_deg=Deg(-10.0),
            moon_separation_deg=Deg(90.0),
            field_geometry=geometry,
            **_COMMON_SCORE_KWARGS,
        )
        return result.score, result.size_fit

    unknown_score, unknown_fit = scored(None)
    assert unknown_fit is None
    # A size that fits the field cleanly scores the same as an unknown one —
    # unknown is never penalised as if it were oversized or under-resolved.
    fits_score, fits_fit = scored(60.0)  # 1 arcmin, well inside the tele frame
    assert fits_fit is SizeFit.FITS_FIELD
    assert unknown_score == pytest.approx(fits_score)


def test_oversized_object_is_penalised_relative_to_one_that_fits(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    diagonal_arcmin = float(geometry.diagonal_deg) * 60.0

    def scored(size_major_arcmin: float) -> tuple[float, SizeFit | None]:
        result = score_target(
            object_type=ObjectType.GALAXY,
            magnitude=None,
            size_major_arcmin=ArcMin(size_major_arcmin),
            moon_illuminated_fraction=0.0,
            moon_altitude_at_peak_deg=Deg(-10.0),
            moon_separation_deg=Deg(90.0),
            field_geometry=geometry,
            **_COMMON_SCORE_KWARGS,
        )
        return result.score, result.size_fit

    fits_score, fits_fit = scored(30.0)
    oversized_score, oversized_fit = scored(diagonal_arcmin * 2.0)
    assert fits_fit is SizeFit.FITS_FIELD
    assert oversized_fit is SizeFit.EXCEEDS_FIELD
    assert oversized_score < fits_score


def test_tiny_object_is_flagged_too_small_to_resolve(profile: Profile) -> None:
    geometry = field_geometry(profile, "tele")
    # Comfortably under SIZE_TOO_SMALL_MIN_PIXELS at the tele's own plate
    # scale (a fraction of an arcsecond) — nothing a real bundled object
    # would ever have, chosen purely to exercise the branch.
    result = score_target(
        object_type=ObjectType.PLANETARY_NEBULA,
        magnitude=None,
        size_major_arcmin=ArcMin(0.01),
        moon_illuminated_fraction=0.0,
        moon_altitude_at_peak_deg=Deg(-10.0),
        moon_separation_deg=Deg(90.0),
        field_geometry=geometry,
        **_COMMON_SCORE_KWARGS,
    )
    assert result.size_fit is SizeFit.TOO_SMALL_TO_RESOLVE


# -- rank_best_targets: needs the real ephemeris ------------------------------ #


def test_a_target_that_never_clears_the_limit_tonight_is_excluded(ephemeris: Ephemeris) -> None:
    """A far-southern HII region can never reach 30 deg altitude from
    Bucharest (culmination = 90 - |44.4 - (-80)| < 0) — excluded by the
    cheap pool filter before any position is ever solved."""
    hopeless = _object("TEST HOPELESS", ObjectType.HII_REGION, dec_deg=-80.0)
    catalog = _catalog(hopeless)
    result = rank_best_targets(
        ephemeris, BUCHAREST, catalog, _NEW_MOON_START, _NEW_MOON_END, Deg(30.0)
    )
    assert result.candidates_considered == 0
    assert result.results == ()


def test_a_reachable_target_is_included_with_positive_hours_above_limit(
    ephemeris: Ephemeris,
) -> None:
    reachable = _object("TEST REACHABLE", ObjectType.HII_REGION, ra_deg=90.0, dec_deg=44.4)
    catalog = _catalog(reachable)
    result = rank_best_targets(
        ephemeris, BUCHAREST, catalog, _NEW_MOON_START, _NEW_MOON_END, Deg(20.0)
    )
    assert result.candidates_considered == 1
    assert len(result.results) == 1
    candidate = result.results[0]
    assert candidate.hours_above_limit > 0.0
    assert float(candidate.peak_altitude_deg) >= 20.0


def test_no_astronomical_darkness_gives_an_explained_empty_result(ephemeris: Ephemeris) -> None:
    """Tromso (69.6 N) around the summer solstice never reaches -18 deg —
    the same polar-summer case ``test_events.py`` proves for
    ``annual_altitude_profile`` — so there is no dark window to rank
    anything against at all, and that must be a stated reason, never a
    silently empty list (CLAUDE.md invariant 7)."""
    tromso = Site(latitude_deg=Deg(69.6492), longitude_deg=Deg(18.9553))
    start = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    catalog = _catalog(_object("TEST", ObjectType.HII_REGION, dec_deg=60.0))
    result = rank_best_targets(
        ephemeris, tromso, catalog, start, start + timedelta(hours=24), Deg(20.0)
    )
    assert result.darkness_hours == 0.0
    assert result.results == ()
    assert result.note is not None


def test_determinism_ties_break_on_designation_not_insertion_order(ephemeris: Ephemeris) -> None:
    """Two targets with identical RA/Dec/type/magnitude/size score identically
    (same real ephemeris computation) — a genuine tie, not a contrived one —
    so the sort key's designation tiebreak is what is actually exercised, not
    incidental dict/set ordering."""
    first = _object("TEST TIE B", ObjectType.OPEN_CLUSTER, ra_deg=90.0, dec_deg=44.4, magnitude=6.0)
    second = _object(
        "TEST TIE A", ObjectType.OPEN_CLUSTER, ra_deg=90.0, dec_deg=44.4, magnitude=6.0
    )

    forward = rank_best_targets(
        ephemeris, BUCHAREST, _catalog(first, second), _NEW_MOON_START, _NEW_MOON_END, Deg(20.0)
    )
    backward = rank_best_targets(
        ephemeris, BUCHAREST, _catalog(second, first), _NEW_MOON_START, _NEW_MOON_END, Deg(20.0)
    )
    assert [c.object.name for c in forward.results] == ["TEST TIE A", "TEST TIE B"]
    assert [c.object.name for c in backward.results] == ["TEST TIE A", "TEST TIE B"]


def test_top_n_is_honoured(ephemeris: Ephemeris) -> None:
    objects = [
        _object(f"TEST N{i}", ObjectType.OPEN_CLUSTER, ra_deg=float(i) * 10.0, dec_deg=44.4)
        for i in range(5)
    ]
    result = rank_best_targets(
        ephemeris,
        BUCHAREST,
        _catalog(*objects),
        _NEW_MOON_START,
        _NEW_MOON_END,
        Deg(20.0),
        top_n=2,
    )
    assert len(result.results) <= 2


# -- full-catalog sanity: real catalogue, real ephemeris ---------------------- #


def test_full_catalog_results_all_clear_the_altitude_limit(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    from ssc.astro.catalog import load_catalog

    geometry = field_geometry(profile, "tele")
    result = rank_best_targets(
        ephemeris,
        BUCHAREST,
        load_catalog(),
        _NEW_MOON_START,
        _NEW_MOON_END,
        Deg(25.0),
        top_n=10,
        field_geometry=geometry,
    )
    assert result.candidates_considered > 0
    assert len(result.results) == 10
    for candidate in result.results:
        assert candidate.hours_above_limit > 0.0
        assert float(candidate.peak_altitude_deg) >= 25.0
        # Every result carries the reason, not just the score.
        assert candidate.score.moon_sensitivity is not None
        assert candidate.score.moon_impact is not None
    scores = [c.score.score for c in result.results]
    assert scores == sorted(scores, reverse=True)


def test_full_moon_night_excludes_galaxies_from_the_top_ten(
    ephemeris: Ephemeris, profile: Profile
) -> None:
    """The real acceptance test for this feature is reading the actual
    top-10 lists (see the task report); this pins the one claim that must
    always hold given a near-full Moon up for most of the night at a
    mid-latitude site: no broadband-sensitive galaxy displaces the
    narrowband-friendly and cluster targets that tolerate it."""
    from ssc.astro.catalog import load_catalog

    geometry = field_geometry(profile, "tele")
    result = rank_best_targets(
        ephemeris,
        BUCHAREST,
        load_catalog(),
        _WINDOW_START,
        _WINDOW_END,
        Deg(25.0),
        top_n=10,
        field_geometry=geometry,
    )
    assert result.moon_illuminated_fraction > 0.9
    galaxy_types = {
        ObjectType.GALAXY,
        ObjectType.GALAXY_SPIRAL,
        ObjectType.GALAXY_ELLIPTICAL,
        ObjectType.GALAXY_LENTICULAR,
        ObjectType.GALAXY_IRREGULAR,
        ObjectType.GALAXY_PAIR,
        ObjectType.GALAXY_TRIPLET,
        ObjectType.GALAXY_GROUP,
    }
    assert all(c.object.object_type not in galaxy_types for c in result.results)
