"""Pure unit tests for ``ssc.scheduler.gates`` — no clock, no device, just synthetic
sample sequences, per the module's own docstring.

A note on ``_sustained``: it requires *every* sample in the trailing window to satisfy
the predicate, so a series that genuinely oscillates across the abort threshold itself
(some samples above 60%, some at or below it, within the same 20-minute window) never
sustains an abort at all — which is correct: "sustained above 60% for 20 min" means
just that, not "mostly above". The hysteresis worked example below is therefore built
from three deliberate phases — a genuine sustained-high stretch, a dead-zone plateau
between the two thresholds (proving the gate does not thrash there), and a genuine
sustained-low stretch — which is what actually exercises "exactly one abort, exactly
one resume".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ssc.scheduler.gates import (
    GateSample,
    GateThresholds,
    TargetSnapshot,
    build_gate_sample,
    evaluate_gate,
)

T0 = datetime(2026, 1, 1, 22, 0, tzinfo=UTC)
THRESHOLDS = GateThresholds(
    cloud_abort_pct=60.0,
    cloud_abort_sustained_min=20.0,
    cloud_resume_pct=30.0,
    cloud_resume_sustained_min=15.0,
)


def _samples(
    cloud_pcts: list[float | None],
    *,
    alt_degs: list[float | None] | None = None,
    moon_degs: list[float | None] | None = None,
    stale: bool = False,
    step_min: float = 1.0,
    start: datetime = T0,
) -> list[GateSample]:
    alts = alt_degs if alt_degs is not None else [60.0] * len(cloud_pcts)
    moons = moon_degs if moon_degs is not None else [90.0] * len(cloud_pcts)
    return [
        GateSample(
            at=start + timedelta(minutes=step_min * i),
            cloud_total_pct=pct,
            forecast_stale_since=start if stale else None,
            target_alt_deg=alt,
            moon_separation_deg=moon,
        )
        for i, (pct, alt, moon) in enumerate(zip(cloud_pcts, alts, moons, strict=True))
    ]


def test_no_history_holds_conservatively_but_does_not_change_state() -> None:
    decision = evaluate_gate(
        [], thresholds=THRESHOLDS, currently_held=False, min_alt_deg=None, min_moon_sep_deg=None
    )
    assert decision.hold is False
    held_decision = evaluate_gate(
        [], thresholds=THRESHOLDS, currently_held=True, min_alt_deg=None, min_moon_sep_deg=None
    )
    assert held_decision.hold is True


def test_short_history_never_claims_sustained() -> None:
    # Only 5 minutes of high cloud recorded — nowhere near the 20 min abort window.
    samples = _samples([90.0] * 5)
    decision = evaluate_gate(
        samples,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=None,
        min_moon_sep_deg=None,
    )
    assert decision.hold is False


def test_sustained_high_cloud_triggers_abort() -> None:
    samples = _samples([90.0] * 25)  # 25 min, all above 60%
    decision = evaluate_gate(
        samples,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=None,
        min_moon_sep_deg=None,
    )
    assert decision.hold is True
    assert "cloud cover" in decision.reason


def test_hysteresis_band_produces_exactly_one_abort_and_one_resume() -> None:
    """DESIGN.md 4.2's worked example: sustained-high cloud aborts; a plateau in the
    30-60% dead zone must not resume the gate (proving the resume threshold is a real,
    separate line, not just "no longer aborting"); a genuine sustained-low stretch
    resumes exactly once."""
    abort_phase = [90.0] * 25  # sustained > 60% for >= 20 min -> aborts partway through
    dead_zone = [45.0, 50.0, 40.0, 55.0] * 10  # never < 30%, never sustained > 60% either
    resume_phase = [10.0] * 20  # sustained < 30% for >= 15 min -> resumes partway through
    samples = _samples(abort_phase + dead_zone + resume_phase)

    held = False
    transitions: list[bool] = []
    for i in range(1, len(samples) + 1):
        decision = evaluate_gate(
            samples[:i],
            thresholds=THRESHOLDS,
            currently_held=held,
            min_alt_deg=None,
            min_moon_sep_deg=None,
        )
        if decision.hold != held:
            transitions.append(decision.hold)
        held = decision.hold

    assert transitions == [True, False]


def test_stale_forecast_is_never_used_to_abort() -> None:
    samples = _samples([90.0] * 25, stale=True)
    decision = evaluate_gate(
        samples,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=None,
        min_moon_sep_deg=None,
    )
    assert decision.hold is False
    assert "stale" in decision.reason


def test_stale_forecast_is_never_used_to_resume() -> None:
    samples = _samples([5.0] * 20, stale=True)
    decision = evaluate_gate(
        samples, thresholds=THRESHOLDS, currently_held=True, min_alt_deg=None, min_moon_sep_deg=None
    )
    assert decision.hold is True


def test_missing_cloud_reading_breaks_the_sustained_streak() -> None:
    cloud_pcts: list[float | None] = [90.0] * 15 + [None] + [90.0] * 15
    samples = _samples(cloud_pcts)
    # Only the last 15 minutes (after the gap) are unbroken -> not yet 20 min sustained.
    decision = evaluate_gate(
        samples,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=None,
        min_moon_sep_deg=None,
    )
    assert decision.hold is False


def test_altitude_abort_is_immediate() -> None:
    samples = _samples([5.0] * 20, alt_degs=[10.0] * 20)  # cloud clear throughout
    decision = evaluate_gate(
        samples,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=20.0,
        min_moon_sep_deg=None,
    )
    assert decision.hold is True
    assert "altitude" in decision.reason


def test_altitude_resume_needs_the_margin_not_just_the_floor() -> None:
    clear_cloud = [5.0] * 20  # sustained low cloud throughout: isolates the altitude check
    just_above_floor = _samples(clear_cloud, alt_degs=[22.0] * 20)
    still_held = evaluate_gate(
        just_above_floor,
        thresholds=THRESHOLDS,
        currently_held=True,
        min_alt_deg=20.0,
        min_moon_sep_deg=None,
    )
    assert still_held.hold is True  # above the floor, but not yet past the +5 deg margin

    past_margin = _samples(clear_cloud, alt_degs=[26.0] * 20)
    resumed = evaluate_gate(
        past_margin,
        thresholds=THRESHOLDS,
        currently_held=True,
        min_alt_deg=20.0,
        min_moon_sep_deg=None,
    )
    assert resumed.hold is False


def test_moon_separation_single_threshold_no_margin() -> None:
    clear_cloud = [5.0] * 20
    too_close = _samples(clear_cloud, moon_degs=[10.0] * 20)
    decision = evaluate_gate(
        too_close,
        thresholds=THRESHOLDS,
        currently_held=False,
        min_alt_deg=None,
        min_moon_sep_deg=20.0,
    )
    assert decision.hold is True

    clear = _samples(clear_cloud, moon_degs=[20.0] * 20)
    resumed = evaluate_gate(
        clear, thresholds=THRESHOLDS, currently_held=True, min_alt_deg=None, min_moon_sep_deg=20.0
    )
    assert resumed.hold is False


def test_build_gate_sample_folds_forecast_and_target() -> None:
    from ssc.weather.base import ForecastPoint, ForecastSeries

    point = ForecastPoint(
        t=T0,
        cloud_total=42.0,
        cloud_low=None,
        cloud_mid=None,
        cloud_high=None,
        precip_prob=None,
        humidity=None,
        dew_point_c=None,
        temp_c=None,
        wind_ms=None,
        visibility_m=None,
        seeing_arcsec=None,
        transparency=None,
        source="test",
    )
    series = ForecastSeries(points=(point,), fetched_at=T0)
    target = TargetSnapshot(altitude_deg=45.0, moon_separation_deg=80.0)
    sample = build_gate_sample(at=T0, forecast=series, target=target)
    assert sample.cloud_total_pct == 42.0
    assert sample.target_alt_deg == 45.0
    assert sample.moon_separation_deg == 80.0
    assert sample.forecast_stale_since is None


def test_build_gate_sample_with_no_forecast_or_target_is_all_unknown() -> None:
    sample = build_gate_sample(at=T0, forecast=None, target=None)
    assert sample.cloud_total_pct is None
    assert sample.target_alt_deg is None
    assert sample.moon_separation_deg is None
