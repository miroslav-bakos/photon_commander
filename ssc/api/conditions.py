# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SSC's own synthesis of "is tonight worth going out" — cloud cover (total
and the low/mid/high split), the Moon's illumination weighted by how much of
the dark window it actually spends above the horizon, dew-point margin,
humidity, precipitation chance and wind, folded into one score plus the
reasons that produced it.

**This is a judgement, not a measurement.** ``ForecastPoint.seeing_arcsec``
and ``.transparency`` are — and must remain — ``None`` from every shipped
provider: 7Timer!/Meteoblue, the only providers that ever supplied them, are
excluded on commercial-licensing grounds (`THIRD_PARTY_NOTICES.md`). This
module's output must never be confused with either, which is why it is not
called "transparency" or "seeing" anywhere, in code or in the API: it is
*capture conditions*, SSC's own synthesis, and ``reasons`` always says which
inputs drove the score so a bad night explains itself.

**The Moon's illumination alone is misleading.** A full Moon that stays below
the horizon all night costs nothing; a 40% Moon high in the sky all night
ruins faint work. The caller (``ssc.api.tonight_service``) samples the Moon's
altitude across the dark window and hands this module :class:`MoonContribution`
— illumination weighted by *time spent up*, not phase alone.

Pure and deterministic: every input is a number already computed elsewhere
(the forecast rows for tonight, the Moon's illumination and time above the
horizon). No I/O, no clock read — a good night and a bad night are each a
fixed-input unit test (``tests/api/test_conditions.py``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ssc.weather.base import ForecastPoint

#: Score bands, out of 100. Chosen to read naturally on a header widget
#: ("good", "poor", ...) rather than forcing a user to interpret a bare number.
_EXCELLENT_MIN: float = 80.0
_GOOD_MIN: float = 60.0
_FAIR_MIN: float = 40.0
_POOR_MIN: float = 20.0

# A bright Moon above the horizon for most of darkness is a first-order limit
# for broadband deep-sky work, not a small tie-breaker. At 25 points the
# reported 2026-09-22 case (84% illuminated, up for 70% of darkness, with
# 34% cloud) still landed in "good". Forty-five points makes that combination
# honestly "fair" while a full Moon below the horizon still costs nothing.
_MOON_PENALTY_MAX: float = 45.0


class ConditionsCategory(StrEnum):
    EXCELLENT = "excellent"
    GOOD = "good"
    FAIR = "fair"
    POOR = "poor"
    VERY_POOR = "very_poor"


class ConditionFactorName(StrEnum):
    CLOUDS = "clouds"
    MOON = "moon"
    DEW_POINT = "dew_point"
    WIND = "wind"
    PRECIPITATION = "precipitation"
    HUMIDITY = "humidity"


@dataclass(frozen=True, slots=True)
class ConditionFactor:
    """One input and its exact deduction from the 100-point score."""

    name: ConditionFactorName
    measurement: str
    #: None means unavailable and excluded; zero means measured with no impact.
    points_deducted: float | None


@dataclass(frozen=True, slots=True)
class MoonContribution:
    """How much the Moon costs tonight's conditions.

    Illumination alone is not enough (see module docstring): this is
    illumination weighted by ``up_fraction``, the fraction of the dark
    window's sampled instants the Moon was above the horizon.
    """

    illuminated_fraction: float
    #: 0.0-1.0. Time-fraction of the dark window the Moon was above the
    #: horizon, from a uniform altitude sampling — never a guess.
    up_fraction: float
    #: Mean altitude, degrees, across only the samples where it was up.
    #: ``None`` when ``up_fraction`` is 0.0 (never up — nothing to average;
    #: ``None`` means unknown/inapplicable here, not "zero altitude").
    mean_altitude_above_horizon_deg: float | None


@dataclass(frozen=True, slots=True)
class CaptureConditions:
    category: ConditionsCategory
    #: 0 (do not bother) to 100 (go).
    score: float
    #: Human-readable, in the order they were evaluated. Always at least one
    #: entry — a favourable night says so explicitly rather than shipping an
    #: empty list a client has to interpret as "nothing to report".
    reasons: tuple[str, ...]
    factors: tuple[ConditionFactor, ...]


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def assess_capture_conditions(
    rows: Sequence[ForecastPoint], moon: MoonContribution
) -> CaptureConditions:
    """Score tonight from the forecast rows falling inside the dark window
    plus the Moon's contribution.

    ``rows`` must be non-empty, and at least one row must carry
    ``cloud_total`` — cloud cover is the dominant, always-available Met.no
    field this scoring is built on. A caller with no cloud data has nothing
    to score and must not call this; it should surface "conditions
    unavailable" instead (CLAUDE.md invariant 7 — this function never
    guesses a clear sky for a missing reading).
    """
    if not rows:
        raise ValueError("assess_capture_conditions requires at least one forecast row")

    cloud_total = [r.cloud_total for r in rows if r.cloud_total is not None]
    if not cloud_total:
        raise ValueError("assess_capture_conditions requires at least one row with cloud_total")
    cloud_low = [r.cloud_low for r in rows if r.cloud_low is not None]
    cloud_high = [r.cloud_high for r in rows if r.cloud_high is not None]
    dew_margins = [
        r.temp_c - r.dew_point_c for r in rows if r.temp_c is not None and r.dew_point_c is not None
    ]
    humidity = [r.humidity for r in rows if r.humidity is not None]
    precip_prob = [r.precip_prob for r in rows if r.precip_prob is not None]
    wind = [r.wind_ms for r in rows if r.wind_ms is not None]

    reasons: list[str] = []
    factors: list[ConditionFactor] = []
    score = 100.0

    # --- cloud: the dominant factor, up to 65 of the 100 points -------------
    mean_total = _mean(cloud_total)
    assert mean_total is not None  # guaranteed non-empty above
    cloud_penalty = mean_total * 0.5
    mean_low = _mean(cloud_low)
    if mean_low is not None:
        # Low cloud ends the night outright even when the total figure looks
        # moderate (a low deck can sit under a clearer upper sky) — weighted
        # on top of, not instead of, the total-cloud penalty.
        cloud_penalty += mean_low * 0.2
    mean_high = _mean(cloud_high)
    if mean_high is not None:
        # Thin high cloud costs transparency even where it barely moves the
        # total-cover figure.
        cloud_penalty += mean_high * 0.1
    cloud_penalty = _clamp(cloud_penalty, 0.0, 65.0)
    score -= cloud_penalty
    cloud_measurements = [f"avg total {mean_total:.1f}%"]
    cloud_measurements.append(f"low {mean_low:.1f}%" if mean_low is not None else "low unavailable")
    cloud_measurements.append(f"high {mean_high:.1f}%" if mean_high is not None else "high unavailable")
    factors.append(ConditionFactor(ConditionFactorName.CLOUDS, ", ".join(cloud_measurements), cloud_penalty))
    if mean_total >= 20.0:
        reasons.append(f"{mean_total:.0f}% average cloud cover")
    if mean_high is not None and mean_high >= 30.0 and mean_total < 40.0:
        reasons.append(f"{mean_high:.0f}% high cloud degrading transparency")
    if mean_low is not None and mean_low >= 40.0:
        reasons.append(f"{mean_low:.0f}% low cloud")

    # --- moon: illumination weighted by time spent above the horizon --------
    moon_penalty = _clamp(
        moon.illuminated_fraction * moon.up_fraction * _MOON_PENALTY_MAX,
        0.0,
        _MOON_PENALTY_MAX,
    )
    score -= moon_penalty
    moon_altitude = (
        f", mean altitude {moon.mean_altitude_above_horizon_deg:.1f}°"
        if moon.mean_altitude_above_horizon_deg is not None
        else ""
    )
    factors.append(
        ConditionFactor(
            ConditionFactorName.MOON,
            f"{moon.illuminated_fraction * 100:.0f}% illuminated, above horizon "
            f"{moon.up_fraction * 100:.0f}% of dark window{moon_altitude}",
            moon_penalty,
        )
    )
    if moon_penalty >= 3.0:
        reasons.append(
            f"moon {moon.illuminated_fraction * 100:.0f}% illuminated, "
            f"up {moon.up_fraction * 100:.0f}% of the dark window"
        )

    # --- dew-point margin: up to 10 points -----------------------------------
    mean_margin = _mean(dew_margins)
    if mean_margin is not None:
        if mean_margin <= 1.0:
            dew_penalty = 10.0
        else:
            dew_penalty = _clamp(10.0 * (5.0 - mean_margin) / 4.0, 0.0, 10.0)
        score -= dew_penalty
        factors.append(
            ConditionFactor(
                ConditionFactorName.DEW_POINT,
                f"avg temperature/dew-point margin {mean_margin:.1f}°C",
                dew_penalty,
            )
        )
        if mean_margin <= 2.0:
            reasons.append(f"dew-point margin only {mean_margin:.1f}°C — dew/haze risk")
    else:
        factors.append(
            ConditionFactor(ConditionFactorName.DEW_POINT, "temperature/dew-point data unavailable", None)
        )

    # --- wind: up to 8 points (tracking stability on a light tripod) --------
    mean_wind = _mean(wind)
    if mean_wind is not None:
        wind_penalty = _clamp((mean_wind - 3.0) / 9.0 * 8.0, 0.0, 8.0)
        score -= wind_penalty
        factors.append(
            ConditionFactor(ConditionFactorName.WIND, f"avg wind {mean_wind:.1f} m/s", wind_penalty)
        )
        if mean_wind >= 8.0:
            reasons.append(f"{mean_wind:.0f} m/s wind — may affect tracking stability")
    else:
        factors.append(ConditionFactor(ConditionFactorName.WIND, "wind data unavailable", None))

    # --- precipitation chance: up to 12 points -------------------------------
    max_precip = max(precip_prob) if precip_prob else None
    if max_precip is not None:
        precip_penalty = _clamp(max_precip * 0.12, 0.0, 12.0)
        score -= precip_penalty
        factors.append(
            ConditionFactor(
                ConditionFactorName.PRECIPITATION,
                f"max precipitation chance {max_precip:.0f}%",
                precip_penalty,
            )
        )
        if max_precip >= 30.0:
            reasons.append(f"{max_precip:.0f}% chance of precipitation")
    else:
        factors.append(
            ConditionFactor(ConditionFactorName.PRECIPITATION, "precipitation data unavailable", None)
        )

    # --- humidity: up to 5 points, mostly redundant with dew margin ---------
    mean_humidity = _mean(humidity)
    if mean_humidity is not None:
        humidity_penalty = (
            _clamp((mean_humidity - 90.0) / 10.0 * 5.0, 0.0, 5.0)
            if mean_humidity >= 90.0
            else 0.0
        )
        score -= humidity_penalty
        factors.append(
            ConditionFactor(
                ConditionFactorName.HUMIDITY,
                f"avg humidity {mean_humidity:.0f}%",
                humidity_penalty,
            )
        )
    else:
        factors.append(ConditionFactor(ConditionFactorName.HUMIDITY, "humidity data unavailable", None))

    score = _clamp(score, 0.0, 100.0)
    if not reasons:
        reasons.append("clear skies and a favourable moon forecast for tonight")

    if score >= _EXCELLENT_MIN:
        category = ConditionsCategory.EXCELLENT
    elif score >= _GOOD_MIN:
        category = ConditionsCategory.GOOD
    elif score >= _FAIR_MIN:
        category = ConditionsCategory.FAIR
    elif score >= _POOR_MIN:
        category = ConditionsCategory.POOR
    else:
        category = ConditionsCategory.VERY_POOR

    return CaptureConditions(
        category=category,
        score=score,
        reasons=tuple(reasons),
        factors=tuple(factors),
    )


__all__ = [
    "CaptureConditions",
    "ConditionsCategory",
    "ConditionFactor",
    "ConditionFactorName",
    "MoonContribution",
    "assess_capture_conditions",
]
