# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Pure gate predicates over a snapshot of sky and forecast conditions (DESIGN.md 4.2).

A gate decides whether the currently-stacking item should be held (parked, stacking
stopped, the operator notified) or is clear to keep running. Every predicate here is a
plain function of data the caller hands in — no clock, no device, no weather fetch — so
the hysteresis it implements (CLAUDE.md: "cloud > 60% sustained 20 min -> pause ...
cloud < 30% sustained 15 min -> resume") is exercised in a test with a synthetic list of
samples and no waiting in real time.

**Sustained-for logic lives here, never in the executor loop.** The loop's only
responsibility is to keep sampling and to keep growing the ``history`` it passes in;
"has it been like this for N minutes" is answered entirely inside :func:`evaluate_gate`.
That split is what makes gate hysteresis testable without a real clock and without the
executor loop knowing anything about cloud percentages.

**Hysteresis, twice over.** Cloud cover gets a genuine two-threshold band (abort above
60%, resume only once below 30%, each sustained for its own duration) because it is noisy
telemetry that would thrash a single-threshold gate. Altitude gets an abort floor plus a
resume margin above it, per DESIGN's own wording, for the same reason — a target sitting
right at the limit should not flap the gate every sample. Moon separation gets a single
threshold and no margin: separation from the Moon changes on the order of arcminutes per
hour, far slower than a sampling interval, so it does not oscillate the way cloud cover
does and a margin would only cost observing time for no benefit.

**A stale forecast is surfaced, never trusted to abort.** architecture policy invariant 7: when
``ForecastSeries.stale_since`` is set, the cloud reading at that sample counts toward
neither the abort streak nor the resume streak — it is treated exactly like a missing
reading (``None``), and the staleness itself is folded into the decision's ``reason`` so
the run log says why the gate did nothing rather than staying quiet about a forecast that
stopped updating.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ssc.weather.base import ForecastSeries


@dataclass(frozen=True, slots=True)
class GateThresholds:
    """The hysteresis band. Defaults match DESIGN.md 4.2's worked example."""

    cloud_abort_pct: float = 60.0
    cloud_abort_sustained_min: float = 20.0
    cloud_resume_pct: float = 30.0
    cloud_resume_sustained_min: float = 15.0
    #: Resume only once the target is this far *above* ``min_alt_deg`` — the abort floor
    #: itself has no margin, since dropping below the user's own limit is unambiguous.
    min_alt_margin_deg: float = 5.0


DEFAULT_THRESHOLDS: GateThresholds = GateThresholds()


@dataclass(frozen=True, slots=True)
class TargetSnapshot:
    """Where the item's target is, and how close the Moon is, at one instant — the
    :class:`~ssc.scheduler.executor.GateInputs` port's astro half. Computed with
    ``ssc.astro`` by whoever implements the port; this module never touches an
    ephemeris."""

    altitude_deg: float
    moon_separation_deg: float


@dataclass(frozen=True, slots=True)
class GateSample:
    """One instant's resolved reading, ready for :func:`evaluate_gate`. ``None`` means
    unknown — a provider gap, a not-yet-sampled altitude — never zero (CLAUDE.md)."""

    at: datetime
    cloud_total_pct: float | None
    forecast_stale_since: datetime | None
    target_alt_deg: float | None
    moon_separation_deg: float | None


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Should the item be held right now, and why — always a reason, even when the
    answer is "conditions are fine" (CLAUDE.md: every decision is logged with its
    reason)."""

    hold: bool
    reason: str


def sample_cloud(series: ForecastSeries, at: datetime) -> tuple[float | None, datetime | None]:
    """The forecast point nearest ``at``, and the series' staleness.

    Nearest-point rather than interpolated: a gate does not need sub-forecast-interval
    precision, and interpolating across a stale/fresh boundary would manufacture a
    reading nobody actually forecast.
    """
    if not series.points:
        return None, series.stale_since
    nearest = min(series.points, key=lambda point: abs((point.t - at).total_seconds()))
    return nearest.cloud_total, series.stale_since


def build_gate_sample(
    *,
    at: datetime,
    forecast: ForecastSeries | None,
    target: TargetSnapshot | None,
) -> GateSample:
    """Fold a raw forecast series and an astro snapshot into one :class:`GateSample`.

    Pure — the I/O (fetching the forecast, computing the target snapshot) is the
    ``GateInputs`` port's job; this just assembles what it returned.
    """
    cloud_total_pct: float | None = None
    stale_since: datetime | None = None
    if forecast is not None:
        cloud_total_pct, stale_since = sample_cloud(forecast, at)
    return GateSample(
        at=at,
        cloud_total_pct=cloud_total_pct,
        forecast_stale_since=stale_since,
        target_alt_deg=target.altitude_deg if target is not None else None,
        moon_separation_deg=target.moon_separation_deg if target is not None else None,
    )


def _sustained(
    history: Sequence[GateSample], *, predicate: Callable[[GateSample], bool], minutes: float
) -> bool:
    """True iff every sample in the trailing ``minutes`` window (ending at the most
    recent sample) satisfies ``predicate``, and the history actually reaches back that
    far. A history that has not been accumulating for ``minutes`` yet cannot honestly
    claim "sustained" — it answers ``False``, which is the safe default on both the
    abort and the resume side."""
    if not history:
        return False
    end = history[-1].at
    window_start = end - timedelta(minutes=minutes)
    if history[0].at > window_start:
        return False
    return all(predicate(sample) for sample in history if sample.at >= window_start)


def _cloud_above(threshold_pct: float) -> Callable[[GateSample], bool]:
    def predicate(sample: GateSample) -> bool:
        return (
            sample.cloud_total_pct is not None
            and sample.forecast_stale_since is None
            and sample.cloud_total_pct > threshold_pct
        )

    return predicate


def _cloud_below(threshold_pct: float) -> Callable[[GateSample], bool]:
    def predicate(sample: GateSample) -> bool:
        return (
            sample.cloud_total_pct is not None
            and sample.forecast_stale_since is None
            and sample.cloud_total_pct < threshold_pct
        )

    return predicate


def evaluate_gate(
    history: Sequence[GateSample],
    *,
    thresholds: GateThresholds = DEFAULT_THRESHOLDS,
    currently_held: bool,
    min_alt_deg: float | None,
    min_moon_sep_deg: float | None,
) -> GateDecision:
    """The gate's whole hysteresis, in one pure fold over ``history``.

    ``history`` is ascending by ``at`` and should span at least
    ``max(cloud_abort_sustained_min, cloud_resume_sustained_min)`` for the cloud gate to
    ever fire — a caller with a shorter history simply gets "not sustained yet", never a
    guess.
    """
    if not history:
        return GateDecision(hold=currently_held, reason="no gate samples yet")

    latest = history[-1]
    notes: list[str] = []
    if latest.forecast_stale_since is not None:
        notes.append(
            f"forecast stale since {latest.forecast_stale_since.isoformat()}; cloud gate "
            "degraded to unknown, not used to abort or resume"
        )

    alt_abort = (
        min_alt_deg is not None
        and latest.target_alt_deg is not None
        and latest.target_alt_deg < min_alt_deg
    )
    alt_resume_clear = (
        min_alt_deg is None
        or latest.target_alt_deg is None
        or latest.target_alt_deg >= min_alt_deg + thresholds.min_alt_margin_deg
    )
    if alt_abort and min_alt_deg is not None and latest.target_alt_deg is not None:
        notes.append(
            f"target altitude {latest.target_alt_deg:.1f} deg is below the "
            f"{min_alt_deg:.1f} deg minimum"
        )

    moon_abort = (
        min_moon_sep_deg is not None
        and latest.moon_separation_deg is not None
        and latest.moon_separation_deg < min_moon_sep_deg
    )
    if moon_abort and min_moon_sep_deg is not None and latest.moon_separation_deg is not None:
        notes.append(
            f"the Moon is {latest.moon_separation_deg:.1f} deg away, closer than the "
            f"{min_moon_sep_deg:.1f} deg minimum"
        )

    cloud_abort = _sustained(
        history,
        predicate=_cloud_above(thresholds.cloud_abort_pct),
        minutes=thresholds.cloud_abort_sustained_min,
    )
    cloud_resume_clear = _sustained(
        history,
        predicate=_cloud_below(thresholds.cloud_resume_pct),
        minutes=thresholds.cloud_resume_sustained_min,
    )
    if cloud_abort:
        notes.append(
            f"cloud cover has been above {thresholds.cloud_abort_pct:.0f}% for at least "
            f"{thresholds.cloud_abort_sustained_min:.0f} min"
        )

    if not currently_held:
        hold = alt_abort or moon_abort or cloud_abort
        reason = "; ".join(notes) if notes else "conditions within limits"
        return GateDecision(hold=hold, reason=reason)

    # Currently held: release only once every condition that could have caused the
    # hold has cleared past its (possibly hysteretic) resume line.
    still_alt = alt_abort or not alt_resume_clear
    still_moon = moon_abort
    still_cloud = cloud_abort or not cloud_resume_clear
    hold = still_alt or still_moon or still_cloud
    if hold:
        reason = (
            "; ".join(notes) if notes else "waiting for conditions to clear the resume threshold"
        )
    else:
        reason = "conditions recovered past the resume threshold" + (
            f" ({'; '.join(notes)})" if notes else ""
        )
    return GateDecision(hold=hold, reason=reason)


__all__ = [
    "DEFAULT_THRESHOLDS",
    "GateDecision",
    "GateSample",
    "GateThresholds",
    "TargetSnapshot",
    "build_gate_sample",
    "evaluate_gate",
    "sample_cloud",
]
