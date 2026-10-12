# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

""" "Best targets tonight": rank the bundled catalogue by how good *this*
night actually is for each target (DESIGN.md 3.5) — not by Moon phase alone.

**The domain judgement this module exists to make.** The Moon's illuminated
fraction is not the question a planner needs answered; "does the Moon ruin
tonight for this particular target" is. A full Moon that has already set
costs nothing at all, and a 20%-lit Moon sitting 15 degrees from a faint
galaxy is a real problem — so every score here is built from the Moon's
*actual altitude during this night's dark interval* and its *angular
distance from the target*, weighted by illumination, never from phase in
isolation (see :func:`score_target`).

It also depends on what kind of light the target emits. The Seestar's
built-in dual/multi-narrowband filter passes only the narrow H-alpha / OIII / SII
lines an emission nebula, planetary nebula, HII region or supernova remnant
actually emits, and blocks the broadband continuum moonlit sky glow is made
of — which is why those targets barely notice a bright Moon while a galaxy's
smooth stellar continuum, or a reflection nebula's scattered starlight, is
drowned by the same sky glow the filter cannot separate from it. Dark
nebulae are worse still: they are a silhouette against a brighter
background, so no filter helps and a moonlit sky raises exactly the
background contrast they depend on being low. :func:`moon_sensitivity_of`
is where every :class:`~ssc.astro.catalog.ObjectType` is placed on this
spectrum, each with the reasoning for that placement.

Pure and deterministic per CLAUDE.md invariant 3: every position here comes
from an :class:`~ssc.astro.events.Ephemeris` handle and a ``datetime`` window
the caller supplies — never ``datetime.now()``, never a network call. The
only caller is ``ssc/api/astro_service.py``, which owns loading the
catalogue and the ephemeris kernel and is the only place I/O happens.

**Performance.** The bundled catalogue is >13,000 objects, ~10,000 of them
galaxies. Solving a real topocentric position for every one of them across a
whole night would repeat, catalogue-wide, exactly the naive-grid mistake the
comment above ``ssc.astro.events.DEFAULT_ANNUAL_STEP_MINUTES`` describes for
a single target. Two things keep this fast:

1. **A cheap pool filter first** (:func:`_candidate_pool`), pure arithmetic
   over the catalogue's own static fields, no ephemeris call at all: reject
   an ``ObjectType`` :func:`moon_sensitivity_of` has no basis to classify,
   reject anything whose culmination altitude — ``90 - |latitude -
   declination|``, exact for upper transit — can never clear the caller's
   limit at this site, and reject anything with a *known* magnitude fainter
   than a small telescope's practical reach. A ``None`` magnitude is never
   treated as "too faint": only a known one is ever grounds for exclusion
   (CLAUDE.md invariant 7).
2. **One shared time grid, one skyfield solve, then numpy for the rest.** The
   dark interval is found once with :func:`ssc.astro.events._dark_intervals`
   (reused rather than reimplemented, per that function's own module
   comment), sampled once into a single skyfield ``Time`` array. Only the
   Moon gets a real topocentric skyfield solve on that grid — one body, and
   its parallax genuinely matters at its distance. Every candidate's
   altitude curve instead comes from plain spherical trigonometry against
   the grid's own sidereal time, evaluated for the *entire candidate pool at
   every grid instant in one numpy matrix operation* — see the "why this
   does not call skyfield once per candidate" note on :func:`rank_best_targets`
   for the measurement that motivated this and the precision it trades away.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

import numpy as np

from ssc.astro.catalog import CatalogObject, ObjectType, TargetCatalog
from ssc.astro.events import Ephemeris, Site, _dark_intervals, _require_utc
from ssc.astro.events import moon_phase as _moon_phase
from ssc.astro.framing import FieldGeometry
from ssc.astro.units import ArcMin, Deg

# --- domain judgement: moon sensitivity by object type ----------------------


class MoonSensitivity(StrEnum):
    """How much a bright, high, nearby Moon costs a target of this type —
    the classification :func:`moon_sensitivity_of` assigns every scoreable
    :class:`~ssc.astro.catalog.ObjectType` to. See the module docstring for
    the physical reasoning."""

    #: Narrow emission lines the Seestar's own filter isolates from moonlit
    #: broadband sky glow — a bright Moon barely touches these.
    NARROWBAND_FRIENDLY = "narrowband_friendly"
    #: OpenNGC's ``DIFFUSE_NEBULA``/``CLUSTER_WITH_NEBULA`` cover both
    #: emission and reflection nebulosity without saying which per object —
    #: scored as a genuine midpoint, never guessed either way.
    MIXED = "mixed"
    #: Star clusters are mostly point sources: a brighter sky raises the
    #: background but does not drown a cluster member the way it drowns
    #: diffuse light, so the penalty is real but smaller than a galaxy's.
    CLUSTER = "cluster"
    #: Broadband continuum light (starlight, scattered or direct) that a
    #: narrowband filter cannot separate from moonlit sky glow.
    BROADBAND = "broadband"
    #: Seen only in silhouette against a brighter background; no filter
    #: helps a contrast target, and moonlight raises exactly the background
    #: it depends on being dark. The worst case.
    DARK_SKY_REQUIRED = "dark_sky_required"
    #: Essentially point sources (stars, doubles, novae): a bright Moon
    #: changes their achievable SNR only marginally.
    NEUTRAL = "neutral"


_NARROWBAND_FRIENDLY_TYPES: Final = frozenset(
    {
        ObjectType.EMISSION_NEBULA,
        ObjectType.HII_REGION,
        ObjectType.PLANETARY_NEBULA,
        ObjectType.SUPERNOVA_REMNANT,
    }
)

#: Every ``GALAXY*`` subtype: a galaxy's light is its stars' combined
#: continuum, with no narrow line for a filter to prefer over the sky's own
#: moonlit continuum. ``REFLECTION_NEBULA`` is here for the same reason — it
#: shines by scattering starlight, which is broadband by nature.
_BROADBAND_TYPES: Final = frozenset(
    {
        ObjectType.GALAXY,
        ObjectType.GALAXY_SPIRAL,
        ObjectType.GALAXY_ELLIPTICAL,
        ObjectType.GALAXY_LENTICULAR,
        ObjectType.GALAXY_IRREGULAR,
        ObjectType.GALAXY_PAIR,
        ObjectType.GALAXY_TRIPLET,
        ObjectType.GALAXY_GROUP,
        ObjectType.REFLECTION_NEBULA,
    }
)

_CLUSTER_TYPES: Final = frozenset(
    {
        ObjectType.OPEN_CLUSTER,
        ObjectType.GLOBULAR_CLUSTER,
        ObjectType.STAR_CLOUD,
        ObjectType.STELLAR_ASSOCIATION,
        ObjectType.ASTERISM,
    }
)

_NEUTRAL_TYPES: Final = frozenset({ObjectType.STAR, ObjectType.DOUBLE_STAR, ObjectType.NOVA})

_MIXED_TYPES: Final = frozenset({ObjectType.DIFFUSE_NEBULA, ObjectType.CLUSTER_WITH_NEBULA})

_DARK_SKY_REQUIRED_TYPES: Final = frozenset({ObjectType.DARK_NEBULA})

#: ``OTHER`` is the source's own "no real information" bucket and
#: ``NONEXISTENT`` is a catalogued position with nothing there — neither
#: gives :func:`moon_sensitivity_of` a basis to classify, so both (and an
#: unset ``object_type``) are excluded from the candidate pool entirely
#: rather than guessed at.
_UNCLASSIFIABLE_TYPES: Final = frozenset({ObjectType.OTHER, ObjectType.NONEXISTENT})

#: Physically real (:data:`_NEUTRAL_TYPES` classifies them correctly — a
#: bright Moon barely touches a point source), but excluded from
#: :func:`_candidate_pool` anyway: almost every bundled ``STAR``/
#: ``DOUBLE_STAR`` entry is an OpenNGC historical misidentification — a
#: catalogue number that turned out, on inspection, to be an ordinary field
#: star rather than the nebula or cluster it was first logged as — with no
#: common name and usually no recorded size (spot-checked against
#: ``data/catalog/openngc.csv``: 545 ``STAR`` entries, 3 with a common
#: name). A "best targets tonight" list exists to recommend deep-sky imaging
#: targets; a top ten dominated by anonymous field stars because they
#: happen to be moon-proof would defeat that purpose even though the moon
#: score itself would be correct. ``NOVA`` is excluded for a different
#: reason — a nova is a transient the bundled static catalogue cannot know
#: is currently even visible, so recommending one is never safe here.
_NOT_A_DEEP_SKY_TARGET: Final = frozenset(
    {ObjectType.STAR, ObjectType.DOUBLE_STAR, ObjectType.NOVA}
)


def moon_sensitivity_of(object_type: ObjectType) -> MoonSensitivity | None:
    """Which :class:`MoonSensitivity` bucket an object type falls in, or
    ``None`` when this module has no basis to classify it (``OTHER``,
    ``NONEXISTENT``) — never a guessed default."""
    if object_type in _NARROWBAND_FRIENDLY_TYPES:
        return MoonSensitivity.NARROWBAND_FRIENDLY
    if object_type in _BROADBAND_TYPES:
        return MoonSensitivity.BROADBAND
    if object_type in _CLUSTER_TYPES:
        return MoonSensitivity.CLUSTER
    if object_type in _NEUTRAL_TYPES:
        return MoonSensitivity.NEUTRAL
    if object_type in _MIXED_TYPES:
        return MoonSensitivity.MIXED
    if object_type in _DARK_SKY_REQUIRED_TYPES:
        return MoonSensitivity.DARK_SKY_REQUIRED
    if object_type in _UNCLASSIFIABLE_TYPES:
        return None
    # A new ObjectType was added to ssc.astro.catalog without a place here.
    # Silently defaulting would misclassify a whole new category of object
    # (architecture policy invariant 7); tests/astro/test_best_targets.py enumerates
    # every ObjectType against this function specifically to catch it here,
    # at development time, rather than at request time.
    raise ValueError(f"{object_type!r} has no MoonSensitivity classification")


#: Points of Moon-interference penalty at full pressure (Moon at its most
#: illuminated, highest in the sky, and closest to the target) — the ceiling
#: :func:`score_target` scales down from as any of those three ease off.
#: Ordered by how visibly each type suffers from moonlit sky glow, per the
#: reasoning in the module docstring and :class:`MoonSensitivity`.
MOON_PENALTY_NARROWBAND_MAX: Final = 8.0
MOON_PENALTY_NEUTRAL_MAX: Final = 5.0
MOON_PENALTY_CLUSTER_MAX: Final = 18.0
MOON_PENALTY_MIXED_MAX: Final = 35.0
MOON_PENALTY_BROADBAND_MAX: Final = 55.0
MOON_PENALTY_DARK_SKY_MAX: Final = 70.0

_MOON_PENALTY_MAX: Final[dict[MoonSensitivity, float]] = {
    MoonSensitivity.NARROWBAND_FRIENDLY: MOON_PENALTY_NARROWBAND_MAX,
    MoonSensitivity.NEUTRAL: MOON_PENALTY_NEUTRAL_MAX,
    MoonSensitivity.CLUSTER: MOON_PENALTY_CLUSTER_MAX,
    MoonSensitivity.MIXED: MOON_PENALTY_MIXED_MAX,
    MoonSensitivity.BROADBAND: MOON_PENALTY_BROADBAND_MAX,
    MoonSensitivity.DARK_SKY_REQUIRED: MOON_PENALTY_DARK_SKY_MAX,
}

#: Moon altitude, in degrees, at which its interference is treated as fully
#: "up" for scoring purposes (near the zenith it can get no worse); scales
#: linearly from 0 at the horizon. Below the horizon it is exactly 0 — clean
#: time regardless of phase — never a partial credit for "nearly risen".
MOON_ALTITUDE_FULL_EFFECT_DEG: Final = 60.0

#: Angular separation, in degrees, beyond which the Moon's proximity
#: contribution is treated as fully relieved. Chosen generously (well past
#: the ~30 deg minimum-separation constraint ``docs/DESIGN.md`` 3.4's own
#: ``TargetIntent`` example uses) because moonlit sky glow is a whole-sky
#: effect at high illumination, not purely local to the Moon's own disk —
#: the separation term only ever *scales down* the interference the Moon's
#: altitude and phase already established, it does not zero it out at any
#: separation actually reachable at high illumination.
MOON_SEPARATION_NO_EFFECT_DEG: Final = 120.0

#: Points of Moon-interference penalty at or above which :func:`score_target`
#: calls the effect ``MoonImpact.INTERFERING`` rather than ``FAR_ENOUGH``. A
#: type whose own ceiling is at or below this (``MOON_PENALTY_NARROWBAND_MAX``,
#: ``MOON_PENALTY_NEUTRAL_MAX``) can never reach "interfering" through this
#: path — see the explicit ``NARROWBAND_TOLERANT`` branch in
#: :func:`score_target` instead.
MOON_IMPACT_NOTABLE_THRESHOLD: Final = 5.0


class MoonImpact(StrEnum):
    """Why the Moon did or did not cost this target points tonight — the
    single most useful "why" for a user weighing the ranking against their
    own judgement."""

    #: Below the horizon at the target's peak-altitude instant: clean time,
    #: regardless of phase.
    MOON_DOWN = "moon_down"
    #: Up, but this target's ``MoonSensitivity`` is ``NARROWBAND_FRIENDLY`` —
    #: the filter is doing its job.
    NARROWBAND_TOLERANT = "narrowband_tolerant"
    #: Up and this type is Moon-sensitive, but altitude/phase/separation
    #: combine to a penalty below :data:`MOON_IMPACT_NOTABLE_THRESHOLD`.
    FAR_ENOUGH = "far_enough"
    #: Up, close and/or bright enough to cost real points.
    INTERFERING = "interfering"


class SizeFit(StrEnum):
    """How an object's angular size compares to the field of view — only
    ever set when *both* the size and the field geometry are known
    (CLAUDE.md invariant 7: never guessed from one alone)."""

    FITS_FIELD = "fits_field"
    #: Larger than the frame's own diagonal: no single frame captures it —
    #: a mosaic question, not a "best single target tonight" one.
    EXCEEDS_FIELD = "exceeds_field"
    #: Smaller than :data:`SIZE_TOO_SMALL_MIN_PIXELS` pixels across at this
    #: optic's plate scale — present, but not worth framing as an extended
    #: object.
    TOO_SMALL_TO_RESOLVE = "too_small_to_resolve"


#: A target must span at least this many pixels across its major axis, at
#: the field's own plate scale, to be worth recommending as an extended
#: object rather than treated as effectively stellar. Deliberately small: a
#: compact planetary nebula like NGC 6210 (~20 arcsec, ~5 px on the S30 Pro
#: tele) is a legitimate, popular target, so this only catches objects
#: smaller than that, not planetaries in general.
SIZE_TOO_SMALL_MIN_PIXELS: Final = 5.0

# --- scoring weights ---------------------------------------------------------
#
# Every weight below is a named constant, per architecture policy, with the reasoning
# for its share of the 0-100 scale stated alongside it. The three positive
# components (hours visible, peak altitude, brightness) sum to at most 90;
# the Moon and size components are penalties subtracted from that, and the
# total is clamped to [0, 100] at the end — headroom that is rarely all
# reached at once is intentional, not a bug: a real night almost always
# costs *something* against at least one Moon-sensitive candidate.

#: Up to 50 of the 100 points: the fraction of the night's astronomical
#: darkness the target spends above the caller's altitude limit. The
#: dominant term, because a target visible for the whole night is strictly
#: more useful than one visible for twenty minutes of it, independent of
#: everything else.
ALTITUDE_HOURS_WEIGHT: Final = 50.0

#: Up to 30 points: how far above the limit the target's peak gets, scaled
#: to :data:`ALTITUDE_PEAK_REFERENCE_DEG`. Higher is better (less atmosphere,
#: a smaller horizon-mask risk) but this is a secondary term — a target that
#: only just clears the limit for hours is still worth recommending.
ALTITUDE_PEAK_WEIGHT: Final = 30.0

#: Peak altitude, in degrees, at or above which :data:`ALTITUDE_PEAK_WEIGHT`
#: is awarded in full. Below the zenith deliberately (``ssc.astro.framing``'s
#: own alt-az rotation warning starts at ~70 deg transits) — a target does
#: not need to reach the zenith singularity to be considered well placed.
ALTITUDE_PEAK_REFERENCE_DEG: Final = 70.0

#: Up to 10 points bonus for a known, bright magnitude. Secondary to
#: altitude/duration on purpose: a fainter object well placed tonight is
#: still worth more than a bright one barely clearing the limit.
MAGNITUDE_WEIGHT: Final = 10.0
#: Magnitude at or brighter than this earns the full bonus.
MAGNITUDE_BRIGHT_REFERENCE: Final = 4.0
#: Magnitude at or fainter than this earns none. Matches
#: :data:`DEFAULT_MAX_CANDIDATE_MAGNITUDE` — nothing admitted to the pool at
#: all scores above zero on brightness alone.
MAGNITUDE_FAINT_REFERENCE: Final = 13.0

#: Penalty for :attr:`SizeFit.EXCEEDS_FIELD` — steep but not disqualifying:
#: an oversized target is still a real recommendation (crop it, or plan a
#: mosaic), just not preferred over one that fits the frame outright.
SIZE_PENALTY_EXCEEDS_FIELD: Final = 15.0
#: Penalty for :attr:`SizeFit.TOO_SMALL_TO_RESOLVE` — smaller than
#: :data:`SIZE_PENALTY_EXCEEDS_FIELD`'s because a too-small target is at
#: least framed correctly, just not resolved in detail.
SIZE_PENALTY_TOO_SMALL: Final = 8.0

# --- the candidate pool -------------------------------------------------------

#: A *known* magnitude fainter than this excludes an object from the pool —
#: not a scientific limit, a practical one: nothing this faint is going to
#: produce a usable frame on a 30 mm aperture in a single night regardless of
#: how well placed it is, so it is never worth the ephemeris solve. A
#: ``None`` magnitude never triggers this (invariant 7) — it is common for
#: exactly the emission-line objects this module is built to surface: the
#: bundled Sharpless entries mostly carry no traditional magnitude at all.
DEFAULT_MAX_CANDIDATE_MAGNITUDE: Final = 13.0

#: How many instants to sample the night's dark interval at. Coarse enough
#: to be cheap over hundreds of candidates, fine enough that a typical
#: 6-10h dark window's peak altitude is found within a few minutes and the
#: linear-interpolated ``hours_above_limit`` crossing is not quantised to a
#: visibly wrong sample (mirrors ``ssc.astro.events.DEFAULT_ANNUAL_STEP_MINUTES``'s
#: own reasoning, at a finer step because here it is paid once per night, not
#: once per night times 365).
DEFAULT_STEP_MINUTES: Final = 10.0

#: Mirrors ``ssc.astro.events.MAX_ANNUAL_SAMPLES``: a loud ceiling rather than
#: an unbounded allocation if a caller passes an absurd window.
MAX_NIGHT_SAMPLES: Final = 20_000

DEFAULT_TOP_N: Final = 10


def _candidate_pool(
    catalog: TargetCatalog,
    latitude_deg: Deg,
    min_altitude_deg: Deg,
    max_candidate_magnitude: float,
    allowed_object_types: frozenset[ObjectType] | None = None,
) -> list[CatalogObject]:
    """The cheap, ephemeris-free filter described in the module docstring.

    Every rejection here is based on a fact the catalogue already states for
    every object — no rise/set solve, no per-object skyfield call — which is
    what keeps this an O(catalogue size) Python loop rather than the
    bottleneck.
    """
    latitude = float(latitude_deg)
    limit = float(min_altitude_deg)
    pool: list[CatalogObject] = []
    for obj in catalog.objects:
        if obj.object_type is None:
            continue
        if obj.object_type in _NOT_A_DEEP_SKY_TARGET:
            continue
        if allowed_object_types is not None and obj.object_type not in allowed_object_types:
            continue
        if moon_sensitivity_of(obj.object_type) is None:
            continue
        culmination_altitude_deg = 90.0 - abs(latitude - float(obj.dec_j2000_deg))
        if culmination_altitude_deg < limit:
            continue
        if obj.magnitude is not None and obj.magnitude > max_candidate_magnitude:
            continue
        pool.append(obj)
    return pool


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _sample_plan(
    dark_intervals: list[tuple[datetime, datetime]], step_minutes: float
) -> tuple[list[datetime], np.ndarray]:
    """The night's dark interval(s) sampled at ``step_minutes``, plus each
    sample's rectangle-rule weight in hours (the gap to the *next* sample in
    its own interval; 0 for the last sample of an interval, so a gap between
    two disjoint dark intervals — a rare double-dusk/dawn night — is never
    counted as if it were dark).

    This is a coarser reduction than
    ``ssc.astro.events.annual_altitude_profile``'s linear-interpolated
    crossing: correct to within one sample (``step_minutes``) rather than to
    the instant, which is the trade this module deliberately makes for
    :func:`rank_best_targets`'s much larger fan-out (thousands of candidates
    rather than one target) — see the module docstring's Performance section.
    A ranking heuristic does not need almanac-grade crossing precision; the
    exact solvers in ``ssc.astro.events`` are what ``docs/DESIGN.md``'s
    almanac-validated tests hold to that bar, not this module.
    """
    step_hours = step_minutes / 60.0
    times: list[datetime] = []
    weights: list[float] = []
    for lo, hi in dark_intervals:
        duration_hours = (hi - lo).total_seconds() / 3600.0
        count = max(2, int(duration_hours / step_hours) + 2)
        interval_times = [lo + (hi - lo) * (i / (count - 1)) for i in range(count)]
        times.extend(interval_times)
        weights.extend(
            (interval_times[j + 1] - interval_times[j]).total_seconds() / 3600.0
            for j in range(count - 1)
        )
        weights.append(0.0)
    return times, np.array(weights, dtype=float)


# --- pure scoring -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BestTargetScore:
    """The scoring outputs for one candidate, kept separate from
    :class:`BestTargetCandidate` so :func:`score_target` — the actual domain
    judgement — is unit-testable with no ephemeris at all
    (``tests/astro/test_best_targets.py``)."""

    score: float
    narrowband_friendly: bool
    moon_sensitivity: MoonSensitivity
    moon_impact: MoonImpact
    #: ``None`` iff the object's size or the field geometry (or both) were
    #: not known — never a guessed fit.
    size_fit: SizeFit | None
    altitude_hours_score: float
    altitude_peak_score: float
    magnitude_bonus: float
    moon_penalty: float
    size_penalty: float


def score_target(
    *,
    object_type: ObjectType,
    magnitude: float | None,
    size_major_arcmin: ArcMin | None,
    hours_above_limit: float,
    darkness_hours: float,
    peak_altitude_deg: Deg,
    min_altitude_deg: Deg,
    moon_illuminated_fraction: float,
    moon_altitude_at_peak_deg: Deg,
    moon_separation_deg: Deg,
    field_geometry: FieldGeometry | None,
) -> BestTargetScore:
    """Score one target for one night, from already-computed inputs.

    Pure arithmetic — no ephemeris, no catalogue, no clock — so every rule in
    the module docstring can be exercised directly with hand-picked numbers.
    ``hours_above_limit``/``peak_altitude_deg``/``moon_altitude_at_peak_deg``/
    ``moon_separation_deg`` are all evaluated at (or up to) the target's own
    peak-altitude instant tonight, the moment a session would actually be
    centred on — not a whole-night average, and not the night's midpoint,
    both of which would blur a target that peaks right as the Moon rises (or
    sets) into a number that fits neither case.

    Raises :class:`ValueError` if ``object_type`` has no
    :class:`MoonSensitivity` (a caller error: the candidate pool this module
    builds already excludes such objects) or if ``darkness_hours`` is not
    positive (nothing to score a fraction of hours against).
    """
    sensitivity = moon_sensitivity_of(object_type)
    if sensitivity is None:
        raise ValueError(
            f"{object_type!r} has no MoonSensitivity classification; exclude it from the "
            "candidate pool before scoring rather than scoring it here"
        )
    if darkness_hours <= 0.0:
        raise ValueError("darkness_hours must be positive to score a target against it")

    hours_fraction = _clamp(hours_above_limit / darkness_hours, 0.0, 1.0)
    altitude_hours_score = hours_fraction * ALTITUDE_HOURS_WEIGHT

    peak_span = max(ALTITUDE_PEAK_REFERENCE_DEG - float(min_altitude_deg), 1.0)
    peak_fraction = _clamp(
        (float(peak_altitude_deg) - float(min_altitude_deg)) / peak_span, 0.0, 1.0
    )
    altitude_peak_score = peak_fraction * ALTITUDE_PEAK_WEIGHT

    magnitude_bonus = 0.0
    if magnitude is not None:
        mag_span = MAGNITUDE_FAINT_REFERENCE - MAGNITUDE_BRIGHT_REFERENCE
        mag_fraction = _clamp((MAGNITUDE_FAINT_REFERENCE - magnitude) / mag_span, 0.0, 1.0)
        magnitude_bonus = mag_fraction * MAGNITUDE_WEIGHT

    moon_altitude = float(moon_altitude_at_peak_deg)
    moon_altitude_factor = (
        _clamp(moon_altitude / MOON_ALTITUDE_FULL_EFFECT_DEG, 0.0, 1.0)
        if moon_altitude > 0.0
        else 0.0
    )
    separation_factor = _clamp(
        1.0 - float(moon_separation_deg) / MOON_SEPARATION_NO_EFFECT_DEG, 0.0, 1.0
    )
    moon_pressure = moon_illuminated_fraction * moon_altitude_factor * separation_factor
    moon_penalty = moon_pressure * _MOON_PENALTY_MAX[sensitivity]

    if moon_altitude <= 0.0:
        moon_impact = MoonImpact.MOON_DOWN
    elif sensitivity is MoonSensitivity.NARROWBAND_FRIENDLY:
        moon_impact = MoonImpact.NARROWBAND_TOLERANT
    elif moon_penalty < MOON_IMPACT_NOTABLE_THRESHOLD:
        moon_impact = MoonImpact.FAR_ENOUGH
    else:
        moon_impact = MoonImpact.INTERFERING

    size_fit: SizeFit | None = None
    size_penalty = 0.0
    if size_major_arcmin is not None and field_geometry is not None:
        size_major_deg = float(size_major_arcmin) / 60.0
        min_resolvable_arcmin = (
            SIZE_TOO_SMALL_MIN_PIXELS * float(field_geometry.pixel_scale_arcsec) / 60.0
        )
        if size_major_deg > float(field_geometry.diagonal_deg):
            size_fit = SizeFit.EXCEEDS_FIELD
            size_penalty = SIZE_PENALTY_EXCEEDS_FIELD
        elif float(size_major_arcmin) < min_resolvable_arcmin:
            size_fit = SizeFit.TOO_SMALL_TO_RESOLVE
            size_penalty = SIZE_PENALTY_TOO_SMALL
        else:
            size_fit = SizeFit.FITS_FIELD

    total = (
        altitude_hours_score + altitude_peak_score + magnitude_bonus - moon_penalty - size_penalty
    )
    return BestTargetScore(
        score=_clamp(total, 0.0, 100.0),
        narrowband_friendly=sensitivity is MoonSensitivity.NARROWBAND_FRIENDLY,
        moon_sensitivity=sensitivity,
        moon_impact=moon_impact,
        size_fit=size_fit,
        altitude_hours_score=altitude_hours_score,
        altitude_peak_score=altitude_peak_score,
        magnitude_bonus=magnitude_bonus,
        moon_penalty=moon_penalty,
        size_penalty=size_penalty,
    )


# --- orchestration (needs the ephemeris) --------------------------------------


@dataclass(frozen=True, slots=True)
class BestTargetCandidate:
    """One ranked target plus every component that produced its score — the
    reason, not just the number (CLAUDE.md: results must carry what produced
    them so a caller can disagree with the ranking)."""

    object: CatalogObject
    hours_above_limit: float
    peak_altitude_deg: Deg
    peak_when: datetime
    moon_separation_deg: Deg
    moon_altitude_at_peak_deg: Deg
    moon_illuminated_fraction: float
    score: BestTargetScore


@dataclass(frozen=True, slots=True)
class BestTargetsResult:
    """The un-serialised answer to a "best targets tonight" request."""

    darkness_hours: float
    moon_illuminated_fraction: float
    #: Pool size after the cheap filter, before per-target altitude was
    #: solved — surfaced so a caller can tell "nothing scored" apart from
    #: "nothing was even a candidate".
    candidates_considered: int
    #: Already sorted best-first, already truncated to the caller's ``top_n``.
    results: tuple[BestTargetCandidate, ...]
    #: Set exactly when there is no astronomical darkness in the window at
    #: all (``results`` is then always empty) — never a silent empty list
    #: with no explanation (architecture policy invariant 7).
    note: str | None = None


def rank_best_targets(
    ephemeris: Ephemeris,
    site: Site,
    catalog: TargetCatalog,
    window_start: datetime,
    window_end: datetime,
    min_altitude_deg: Deg,
    *,
    top_n: int = DEFAULT_TOP_N,
    field_geometry: FieldGeometry | None = None,
    max_candidate_magnitude: float = DEFAULT_MAX_CANDIDATE_MAGNITUDE,
    step_minutes: float = DEFAULT_STEP_MINUTES,
    allowed_object_types: frozenset[ObjectType] | None = None,
) -> BestTargetsResult:
    """The top ``top_n`` catalogue targets for the night in ``[window_start,
    window_end]``, ranked by :func:`score_target`.

    ``window_start``/``window_end`` are the same "one 24h span, picked away
    from local midnight" convention ``ssc.astro.events.annual_altitude_profile``
    and the ``/api/astro/night`` endpoint already use — the caller finds
    "tonight"'s window exactly once and this function, like those, takes it
    as given rather than searching for it itself.

    ``field_geometry`` is optional: without it, every result's ``size_fit``
    is ``None`` rather than guessed (CLAUDE.md invariant 7) — pass the
    caller's chosen scope/optic's :class:`~ssc.astro.framing.FieldGeometry`
    to get the size-versus-field judgement.

    **Why this does not call skyfield once per candidate.** An early version
    did — ``observer.at(grid).observe(star).apparent()`` per object, one
    shared ``grid`` — and measured ~1.5 ms per candidate regardless of grid
    size, dominated by skyfield's own per-call Python overhead (aberration
    and deflection corrections, object construction) rather than by the
    numpy work inside it. At the catalogue's real candidate-pool sizes
    (typically 1,000-3,500 objects after :func:`_candidate_pool`) that is
    several seconds — nowhere near "instant". Instead, sidereal time is
    solved once (skyfield, one call, vectorised over the whole night's grid)
    and every candidate's altitude curve is then plain spherical
    trigonometry — ``sin(alt) = sin(dec)sin(lat) + cos(dec)cos(lat)cos(H)`` —
    evaluated for the *entire pool at every grid instant in one numpy matrix
    operation*. The Moon alone still gets a real topocentric skyfield solve
    (it is one body, and its parallax genuinely matters at its distance);
    every candidate's Moon separation then reuses that one solve. This
    trades the sub-arcminute precession/nutation/aberration correction
    skyfield would apply per star for a J2000-vs-apparent-sidereal-time
    approximation good to a few arcminutes — irrelevant at this module's own
    precision budget (whole *hours* of visibility, tens of *degrees* of
    separation) and never used for the second-accurate rise/set/twilight
    solving that is ``ssc.astro.events``'s job and DESIGN.md's almanac
    tolerance applies to, not this one.
    """
    window_start = _require_utc("window_start", window_start)
    window_end = _require_utc("window_end", window_end)
    if window_end <= window_start:
        raise ValueError("window_end must be after window_start")
    if top_n < 1:
        raise ValueError(f"top_n must be at least 1, got {top_n}")
    if step_minutes <= 0.0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes}")

    dark_intervals = _dark_intervals(ephemeris, site, window_start, window_end)
    darkness_hours = sum((hi - lo).total_seconds() for lo, hi in dark_intervals) / 3600.0

    midpoint = window_start + (window_end - window_start) / 2
    phase = _moon_phase(ephemeris, site, midpoint)

    if darkness_hours <= 0.0:
        return BestTargetsResult(
            darkness_hours=0.0,
            moon_illuminated_fraction=phase.illuminated_fraction,
            candidates_considered=0,
            results=(),
            note="no astronomical darkness in this window",
        )

    times, weights_hours = _sample_plan(dark_intervals, step_minutes)
    if len(times) > MAX_NIGHT_SAMPLES:
        raise ValueError(
            f"the dark interval needs {len(times)} samples at {step_minutes} min steps, over "
            f"the {MAX_NIGHT_SAMPLES} ceiling; coarsen step_minutes or shorten the window"
        )

    pool = _candidate_pool(
        catalog,
        site.latitude_deg,
        min_altitude_deg,
        max_candidate_magnitude,
        allowed_object_types,
    )
    limit = float(min_altitude_deg)
    if not pool:
        return BestTargetsResult(
            darkness_hours=darkness_hours,
            moon_illuminated_fraction=phase.illuminated_fraction,
            candidates_considered=0,
            results=(),
        )

    observer = ephemeris.observer(site)
    grid = ephemeris.timescale.from_datetimes(times)

    # One real topocentric skyfield solve, for the Moon only (see the
    # docstring's "why not per candidate" note above).
    moon_apparent = observer.at(grid).observe(ephemeris.moon).apparent()
    moon_altitude_deg = moon_apparent.altaz()[0].degrees  # (T,)
    moon_astrometric = observer.at(grid).observe(ephemeris.moon)
    moon_ra, moon_dec, _ = moon_astrometric.radec()  # ICRF/J2000 frame, matching the catalogue
    moon_ra_deg = moon_ra.degrees  # (T,)
    moon_dec_deg = moon_dec.degrees  # (T,)

    # Local Apparent Sidereal Time, once, from the same grid — this is the
    # one number that turns a static J2000 RA into tonight's hour angle.
    lst_deg = (grid.gast * 15.0 + float(site.longitude_deg)) % 360.0  # (T,)

    ra_deg = np.array([float(obj.ra_j2000_deg) for obj in pool])  # (N,)
    dec_deg = np.array([float(obj.dec_j2000_deg) for obj in pool])  # (N,)
    lat_rad = np.radians(float(site.latitude_deg))
    dec_rad = np.radians(dec_deg)[:, None]  # (N,1)
    hour_angle_rad = np.radians(lst_deg[None, :] - ra_deg[:, None])  # (N,T)
    sin_alt = np.sin(dec_rad) * np.sin(lat_rad) + np.cos(dec_rad) * np.cos(lat_rad) * np.cos(
        hour_angle_rad
    )
    altitude_deg = np.degrees(np.arcsin(np.clip(sin_alt, -1.0, 1.0)))  # (N,T)

    above = altitude_deg >= limit
    hours_above_all = above.astype(float) @ weights_hours  # (N,)
    peak_index_all = altitude_deg.argmax(axis=1)  # (N,)
    rows = np.arange(len(pool))
    peak_altitude_all = altitude_deg[rows, peak_index_all]  # (N,)

    moon_alt_at_peak_all = moon_altitude_deg[peak_index_all]  # (N,)
    moon_ra_at_peak_rad = np.radians(moon_ra_deg[peak_index_all])  # (N,)
    moon_dec_at_peak_rad = np.radians(moon_dec_deg[peak_index_all])  # (N,)
    # ssc.astro.catalog.cos_angular_separation's own formula, vectorised over
    # every candidate's own peak instant at once rather than called per pair.
    cos_sep = np.sin(dec_rad[:, 0]) * np.sin(moon_dec_at_peak_rad) + np.cos(dec_rad[:, 0]) * np.cos(
        moon_dec_at_peak_rad
    ) * np.cos(moon_ra_at_peak_rad - np.radians(ra_deg))
    separation_at_peak_all = np.degrees(np.arccos(np.clip(cos_sep, -1.0, 1.0)))  # (N,)

    candidates: list[BestTargetCandidate] = []
    for i, obj in enumerate(pool):
        hours_above = float(hours_above_all[i])
        if hours_above <= 0.0:
            continue
        # _candidate_pool already excluded object_type is None (and any type
        # moon_sensitivity_of has no classification for) — asserted rather
        # than silently narrowed, so a future change to that filter fails
        # loudly here instead of miscounting candidates_considered.
        object_type = obj.object_type
        assert object_type is not None, f"{obj.name}: pool admitted an unclassified type"

        peak_altitude = Deg(float(peak_altitude_all[i]))
        peak_when = times[int(peak_index_all[i])]
        moon_altitude_at_peak = Deg(float(moon_alt_at_peak_all[i]))
        moon_separation = Deg(float(separation_at_peak_all[i]))

        score = score_target(
            object_type=object_type,
            magnitude=obj.magnitude,
            size_major_arcmin=obj.size_major_arcmin,
            hours_above_limit=hours_above,
            darkness_hours=darkness_hours,
            peak_altitude_deg=peak_altitude,
            min_altitude_deg=min_altitude_deg,
            moon_illuminated_fraction=phase.illuminated_fraction,
            moon_altitude_at_peak_deg=moon_altitude_at_peak,
            moon_separation_deg=moon_separation,
            field_geometry=field_geometry,
        )
        candidates.append(
            BestTargetCandidate(
                object=obj,
                hours_above_limit=hours_above,
                peak_altitude_deg=peak_altitude,
                peak_when=peak_when,
                moon_separation_deg=moon_separation,
                moon_altitude_at_peak_deg=moon_altitude_at_peak,
                moon_illuminated_fraction=phase.illuminated_fraction,
                score=score,
            )
        )

    candidates.sort(key=lambda c: (-c.score.score, c.object.name))
    return BestTargetsResult(
        darkness_hours=darkness_hours,
        moon_illuminated_fraction=phase.illuminated_fraction,
        candidates_considered=len(pool),
        results=tuple(candidates[:top_n]),
    )


__all__ = [
    "ALTITUDE_HOURS_WEIGHT",
    "ALTITUDE_PEAK_REFERENCE_DEG",
    "ALTITUDE_PEAK_WEIGHT",
    "DEFAULT_MAX_CANDIDATE_MAGNITUDE",
    "DEFAULT_STEP_MINUTES",
    "DEFAULT_TOP_N",
    "MAGNITUDE_BRIGHT_REFERENCE",
    "MAGNITUDE_FAINT_REFERENCE",
    "MAGNITUDE_WEIGHT",
    "MAX_NIGHT_SAMPLES",
    "MOON_ALTITUDE_FULL_EFFECT_DEG",
    "MOON_IMPACT_NOTABLE_THRESHOLD",
    "MOON_PENALTY_BROADBAND_MAX",
    "MOON_PENALTY_CLUSTER_MAX",
    "MOON_PENALTY_DARK_SKY_MAX",
    "MOON_PENALTY_MIXED_MAX",
    "MOON_PENALTY_NARROWBAND_MAX",
    "MOON_PENALTY_NEUTRAL_MAX",
    "MOON_SEPARATION_NO_EFFECT_DEG",
    "SIZE_PENALTY_EXCEEDS_FIELD",
    "SIZE_PENALTY_TOO_SMALL",
    "SIZE_TOO_SMALL_MIN_PIXELS",
    "BestTargetCandidate",
    "BestTargetScore",
    "BestTargetsResult",
    "MoonImpact",
    "MoonSensitivity",
    "SizeFit",
    "moon_sensitivity_of",
    "rank_best_targets",
    "score_target",
]
