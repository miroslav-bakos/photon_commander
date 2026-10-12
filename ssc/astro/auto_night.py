# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
"""Deterministic, read-only night allocation; no device or clock access.

One-minute apparent-position samples cover every proposed block, including
endpoints. A one-degree altitude and two-degree separation reserve account for
between-sample motion. Moon limits are conservative planning policy, not a
prediction of SNR; weather and measured site brightness remain execution inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import cos, radians

import numpy as np

from ssc.astro.best_targets import (
    BestTargetCandidate,
    MoonSensitivity,
    SizeFit,
    moon_sensitivity_of,
    rank_best_targets,
)
from ssc.astro.catalog import ObjectType, TargetCatalog, cos_angular_separation
from ssc.astro.events import Ephemeris, Site, _dark_intervals, _require_utc, moon_phase, target
from ssc.astro.framing import FieldGeometry
from ssc.astro.horizon import HorizonProfile
from ssc.astro.units import Deg, Hours

SETUP_MINUTES = 10
STARTUP_MINUTES = 20
SHUTDOWN_MINUTES = 10


@dataclass(frozen=True)
class AutoNightBlock:
    candidate: BestTargetCandidate
    start_time: datetime
    end_time: datetime
    filter_name: str
    peak_when: datetime
    peak_altitude_deg: float
    min_altitude_deg: float
    min_moon_separation_deg: float
    max_moon_altitude_deg: float
    max_moon_illuminated_fraction: float
    score: float
    reason_code: str


@dataclass(frozen=True)
class AutoNightGap:
    start_time: datetime
    end_time: datetime
    reason_code: str


@dataclass(frozen=True)
class AutoNightResult:
    blocks: tuple[AutoNightBlock, ...]
    gaps: tuple[AutoNightGap, ...]
    moon_illuminated_fraction: float
    darkness_hours: float
    scheduled_hours: float
    candidates_considered: int
    note: str | None


def choose_filter(
    object_type: ObjectType,
    available: frozenset[str],
    moon_altitudes: np.ndarray,
    separations: np.ndarray,
    illumination: np.ndarray,
) -> tuple[str, str] | None:
    """Reject interference at *any* sample, not only culmination.

    IR: Moon below horizon or almost-new (<=2%) is clean; otherwise separation
    must exceed 30+60*f degrees and high, bright Moon rejects faint continuum
    irrespective of separation. LP is only valid for classified line emission.
    """
    narrow = moon_sensitivity_of(object_type) == MoonSensitivity.NARROWBAND_FRIENDLY
    up = (moon_altitudes > 0) & (illumination > 0.02)
    if narrow and "LP" in available and np.any(up):
        if np.any(up & (separations < 20 + 2)):
            return None
        return "LP", "emission_lp_moon"
    if "IR" not in available:
        return ("LP", "emission_lp") if narrow and "LP" in available and not np.any(up) else None
    sensitive = moon_sensitivity_of(object_type) in {
        MoonSensitivity.BROADBAND,
        MoonSensitivity.DARK_SKY_REQUIRED,
        MoonSensitivity.MIXED,
    }
    if np.any(up & (separations < 30 + 60 * illumination + 2)):
        return None
    if sensitive and np.any(up & (illumination >= 0.65) & (moon_altitudes >= 15)):
        return None
    return "IR", "moon_down_ir" if not np.any(up) else "moon_separated_ir"


def plan_auto_night(
    ephemeris: Ephemeris,
    site: Site,
    catalog: TargetCatalog,
    start: datetime,
    end: datetime,
    *,
    geometry: FieldGeometry,
    min_altitude_deg: float = 30,
    max_candidate_magnitude: float = 13,
    allowed_object_types: frozenset[ObjectType] | None = None,
    available_filters: frozenset[str] = frozenset({"IR", "LP"}),
    block_minutes: int = 90,
    max_targets: int = 5,
    horizon_profile: HorizonProfile | None = None,
) -> AutoNightResult:
    start, end = _require_utc("start", start), _require_utc("end", end)
    if not timedelta(0) < end - start <= timedelta(hours=36):
        raise ValueError("window must be positive and no longer than 36 hours")
    dark = _dark_intervals(ephemeris, site, start, end)
    if allowed_object_types == frozenset() or not available_filters:
        return AutoNightResult(
            (),
            tuple(AutoNightGap(lo, hi, "setup_or_no_safe_target") for lo, hi in dark),
            moon_phase(ephemeris, site, start + (end - start) / 2).illuminated_fraction,
            sum((hi - lo).total_seconds() / 3600 for lo, hi in dark),
            0,
            0,
            "no_safe_targets",
        )
    ranked = rank_best_targets(
        ephemeris,
        site,
        catalog,
        start,
        end,
        Deg(min_altitude_deg),
        top_n=200,
        field_geometry=geometry,
        max_candidate_magnitude=max_candidate_magnitude,
        allowed_object_types=allowed_object_types,
    )
    blocks: list[AutoNightBlock] = []
    observer = ephemeris.observer(site)
    span = timedelta(minutes=block_minutes + SETUP_MINUTES)
    base_windows = []
    for lo, hi in dark:
        lo = max(lo, start) + timedelta(minutes=STARTUP_MINUTES)
        hi = min(hi, end) - timedelta(minutes=SHUTDOWN_MINUTES)
        cursor = lo
        while cursor + span <= hi:
            base_windows.append(cursor)
            cursor += timedelta(minutes=5)
    # One Moon solve per request shared by all candidates.
    if base_windows:
        lo, hi = min(base_windows), max(base_windows) + span
        count = int((hi - lo).total_seconds() / 60) + 1
        times = [lo + timedelta(minutes=i) for i in range(count)]
        grid = ephemeris.timescale.from_datetimes(times)
        moon = observer.at(grid).observe(ephemeris.moon).apparent()
        moon_alt, _, _ = moon.altaz()
        illumination = np.asarray(moon.fraction_illuminated(ephemeris.sun))
        fraction = float(np.max(illumination))
    for candidate in ranked.results:
        if len(blocks) >= max_targets or not base_windows:
            break
        if candidate.score.size_fit in {SizeFit.EXCEEDS_FIELD, SizeFit.TOO_SMALL_TO_RESOLVE}:
            continue
        # Avoid spending another block on the same central single-frame field.
        # This auto-selection heuristic never modifies authored mosaics/plans.
        radius = min(float(geometry.width_deg), float(geometry.height_deg)) / 2
        if any(
            cos_angular_separation(
                float(candidate.object.ra_j2000_deg),
                float(candidate.object.dec_j2000_deg),
                float(b.candidate.object.ra_j2000_deg),
                float(b.candidate.object.dec_j2000_deg),
            )
            >= cos(radians(radius))
            for b in blocks
        ):
            continue
        assert candidate.object.object_type is not None
        sensitivity = moon_sensitivity_of(candidate.object.object_type)
        if "IR" not in available_filters and sensitivity != MoonSensitivity.NARROWBAND_FRIENDLY:
            continue
        if (
            sensitivity
            in {MoonSensitivity.BROADBAND, MoonSensitivity.DARK_SKY_REQUIRED, MoonSensitivity.MIXED}
            and fraction >= 0.65
            and np.all(moon_alt.degrees >= 15)
        ):
            continue
        if horizon_profile is not None:
            lowest_mask = min(float(p.altitude_deg) for p in horizon_profile.points)
            theoretical_peak = 90 - abs(
                float(site.latitude_deg) - float(candidate.object.dec_j2000_deg)
            )
            if theoretical_peak + 1 < lowest_mask + 1:
                continue
        windows = list(base_windows)
        windows.sort(key=lambda t: (abs((t + span / 2 - candidate.peak_when).total_seconds()), t))
        # Vectorise all samples for this candidate once, rather than skyfield per slot.
        if not windows:
            continue
        apparent = (
            observer.at(grid)
            .observe(
                target(Hours(candidate.object.ra_j2000_deg / 15), candidate.object.dec_j2000_deg)
            )
            .apparent()
        )
        alt, az, _ = apparent.altaz()
        separation = moon.separation_from(apparent).degrees
        horizon = np.array(
            [
                float(horizon_profile.altitude_at(Deg(float(a)))) if horizon_profile else 0
                for a in az.degrees
            ]
        )
        for when in windows:
            finish = when + span
            if any(when < b.end_time and finish > b.start_time for b in blocks):
                continue
            left = int((when - lo).total_seconds() / 60)
            right = left + int(span.total_seconds() / 60) + 1
            a, m, s = alt.degrees[left:right], moon_alt.degrees[left:right], separation[left:right]
            if np.any(a < np.maximum(min_altitude_deg, horizon[left:right]) + 1):
                continue
            choice = choose_filter(
                candidate.object.object_type, available_filters, m, s, illumination[left:right]
            )
            if choice is None:
                continue
            # Recheck finalists on their exact endpoints (multiple dark intervals
            # need not share a minute-grid origin). No coarse ranking coordinates.
            exact_times = [
                when + span * i / int(span.total_seconds() / 60)
                for i in range(int(span.total_seconds() / 60) + 1)
            ]
            exact_grid = ephemeris.timescale.from_datetimes(exact_times)
            exact_target = (
                observer.at(exact_grid)
                .observe(
                    target(
                        Hours(candidate.object.ra_j2000_deg / 15), candidate.object.dec_j2000_deg
                    )
                )
                .apparent()
            )
            exact_alt, exact_az, _ = exact_target.altaz()
            exact_moon = observer.at(exact_grid).observe(ephemeris.moon).apparent()
            exact_moon_alt, _, _ = exact_moon.altaz()
            a = exact_alt.degrees
            m = exact_moon_alt.degrees
            s = exact_moon.separation_from(exact_target).degrees
            exact_horizon = np.array(
                [
                    float(horizon_profile.altitude_at(Deg(float(z)))) if horizon_profile else 0
                    for z in exact_az.degrees
                ]
            )
            if np.any(a < np.maximum(min_altitude_deg, exact_horizon) + 1):
                continue
            exact_illumination = np.asarray(exact_moon.fraction_illuminated(ephemeris.sun))
            choice = choose_filter(
                candidate.object.object_type, available_filters, m, s, exact_illumination
            )
            if choice is None:
                continue
            peak = int(np.argmax(a))
            blocks.append(
                AutoNightBlock(
                    candidate,
                    when,
                    finish,
                    choice[0],
                    exact_times[peak],
                    float(max(a)),
                    float(min(a)),
                    float(min(s)),
                    float(max(m)),
                    float(np.max(exact_illumination)),
                    candidate.score.score,
                    choice[1],
                )
            )
            break
    blocks.sort(key=lambda b: b.start_time)
    gaps = []
    for lo, hi in dark:
        cursor = lo
        for block in blocks:
            if not lo <= block.start_time < hi:
                continue
            if cursor < block.start_time:
                gaps.append(AutoNightGap(cursor, block.start_time, "setup_or_no_safe_target"))
            cursor = block.end_time
        if cursor < hi:
            gaps.append(AutoNightGap(cursor, hi, "shutdown_or_no_safe_target"))
    return AutoNightResult(
        tuple(blocks),
        tuple(gaps),
        ranked.moon_illuminated_fraction,
        ranked.darkness_hours,
        len(blocks) * block_minutes / 60,
        ranked.candidates_considered,
        "sampled_policy_weather_unchecked" if blocks else "no_safe_targets",
    )
