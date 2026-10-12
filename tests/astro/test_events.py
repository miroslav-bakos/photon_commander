"""Unit tests for ssc.astro.events: definitions, DST, polar cases and loud failures.

The almanac table in ``test_almanac_validation.py`` proves the numbers are
right. This file proves the *edges* are right — the ones an almanac comparison
at four sites cannot reach.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ssc.astro.events import (
    KERNEL_URL,
    AnnualAltitudeNight,
    Ephemeris,
    EphemerisCorruptError,
    EphemerisMissingError,
    EventTime,
    LocalTimeKind,
    NonexistentLocalTimeError,
    Outcome,
    Site,
    SolarEvent,
    annual_altitude_profile,
    default_kernel_path,
    horizon_dip_deg,
    load_ephemeris,
    local_time_kind,
    local_to_utc,
    moon_phase,
    sidereal_hour_angle_deg,
    solar_events,
    target_events,
    target_position,
)
from ssc.astro.units import Deg, Hours, Metres

BUCHAREST = Site(latitude_deg=Deg(44.4268), longitude_deg=Deg(26.1025), elevation_m=Metres(85.0))
TROMSO = Site(latitude_deg=Deg(69.6492), longitude_deg=Deg(18.9553))


def _day(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _by_event(events: tuple[EventTime, ...]) -> dict[str, EventTime]:
    return {str(event.event): event for event in events}


# --- the pinned kernel -----------------------------------------------------


def test_missing_kernel_fails_loudly_with_the_fetch_command(tmp_path: Path) -> None:
    """Absent kernel raises with the URL, not a 32 MB surprise download."""
    with pytest.raises(EphemerisMissingError) as caught:
        load_ephemeris(tmp_path / "nowhere" / "de440s.bsp")
    message = str(caught.value)
    assert KERNEL_URL in message
    assert "never downloads it implicitly" in message


def test_truncated_kernel_is_rejected(tmp_path: Path) -> None:
    """A short file is a truncated download, not a working ephemeris."""
    stub = tmp_path / "de440s.bsp"
    stub.write_bytes(b"DAF/SPK not really")
    with pytest.raises(EphemerisCorruptError) as caught:
        load_ephemeris(stub)
    assert KERNEL_URL in str(caught.value)


def test_default_kernel_path_is_the_gitignored_cache() -> None:
    path = default_kernel_path()
    assert path.parent.name == "ephemeris"
    assert path.parent.parent.name == "data"
    assert path.name == "de440s.bsp"


def test_pinned_kernel_digest_matches(ephemeris: Ephemeris) -> None:
    """The cached file really is the pinned de440s, not something with the same name."""
    load_ephemeris(ephemeris.path, verify_digest=True).close()


# --- horizon dip -----------------------------------------------------------


@pytest.mark.parametrize(
    ("elevation_m", "expected_arcmin"),
    [(0.0, 0.0), (2.0, 1.76 * 2.0**0.5), (100.0, 17.6), (500.0, 1.76 * 500.0**0.5)],
)
def test_horizon_dip_formula(elevation_m: float, expected_arcmin: float) -> None:
    assert float(horizon_dip_deg(Metres(elevation_m))) == pytest.approx(expected_arcmin / 60.0)


def test_negative_elevation_is_an_error() -> None:
    with pytest.raises(ValueError, match="negative elevation"):
        horizon_dip_deg(Metres(-1.0))


def test_dip_moves_sunset_but_not_twilight(ephemeris: Ephemeris) -> None:
    """DESIGN 4.1: the dip shifts sunrise/sunset meaningfully at altitude and twilight
    not at all. Anything else means the dip has leaked into a twilight definition."""
    start, end = _day(2026, 6, 21)
    sea = Site(latitude_deg=Deg(51.4779), longitude_deg=Deg(-0.0015), elevation_m=Metres(0.0))
    high = Site(latitude_deg=Deg(51.4779), longitude_deg=Deg(-0.0015), elevation_m=Metres(500.0))

    low = _by_event(solar_events(ephemeris, sea, start, end))
    up = _by_event(solar_events(ephemeris, high, start, end))

    high_sunset, low_sunset = up[SolarEvent.SUNSET].when, low[SolarEvent.SUNSET].when
    high_dusk, low_dusk = up[SolarEvent.CIVIL_DUSK].when, low[SolarEvent.CIVIL_DUSK].when
    assert high_sunset and low_sunset and high_dusk and low_dusk
    sunset_shift = (high_sunset - low_sunset).total_seconds()
    dusk_shift = abs((high_dusk - low_dusk).total_seconds())
    assert sunset_shift > 300.0, "500 m of elevation must delay sunset by minutes"
    assert dusk_shift < 1.0, "the dip must not touch the -6 deg twilight definition"


# --- polar cases -----------------------------------------------------------


def test_midnight_sun_is_explained_not_omitted(ephemeris: Ephemeris) -> None:
    start, end = _day(2026, 6, 21)
    events = _by_event(solar_events(ephemeris, TROMSO, start, end))
    for name in (SolarEvent.SUNRISE, SolarEvent.SUNSET, SolarEvent.ASTRONOMICAL_DUSK):
        assert events[name].when is None
        assert events[name].outcome is Outcome.ALWAYS_ABOVE


def test_polar_night_still_has_twilight(ephemeris: Ephemeris) -> None:
    """Tromso on the December solstice: no sunrise, but real astronomical twilight —
    the distinction the scheduler depends on."""
    start, end = _day(2026, 12, 21)
    events = _by_event(solar_events(ephemeris, TROMSO, start, end))
    assert events[SolarEvent.SUNRISE].outcome is Outcome.ALWAYS_BELOW
    assert events[SolarEvent.ASTRONOMICAL_DAWN].outcome is Outcome.OCCURS


# --- UTC discipline --------------------------------------------------------


def test_naive_datetimes_are_rejected(ephemeris: Ephemeris) -> None:
    """Invariant 5: a naive datetime never enters a solver."""
    with pytest.raises(ValueError, match="timezone-aware"):
        solar_events(ephemeris, BUCHAREST, datetime(2026, 6, 21), datetime(2026, 6, 22))  # noqa: DTZ001


def test_backwards_window_is_rejected(ephemeris: Ephemeris) -> None:
    start, end = _day(2026, 6, 21)
    with pytest.raises(ValueError, match="after start"):
        solar_events(ephemeris, BUCHAREST, end, start)


def test_events_are_reproducible(ephemeris: Ephemeris) -> None:
    """Same inputs, same outputs — there is no hidden clock."""
    start, end = _day(2026, 10, 25)
    assert solar_events(ephemeris, BUCHAREST, start, end) == solar_events(
        ephemeris, BUCHAREST, start, end
    )


def test_rise_set_at_millisecond_rounded_window_boundary_is_finite(
    ephemeris: Ephemeris,
) -> None:
    """A browser round-trip must not turn a boundary event into NaN.

    Skyfield resolves beyond milliseconds, while JavaScript ISO timestamps
    return with exactly three fractional digits. Before `_rise_set` padded
    its solve, starting 34 microseconds before the same astronomical-dusk
    crossing divided a zero hour-angle delta by a zero time delta and then
    passed NaN into jplephem. Pytest promotes both RuntimeWarnings to errors.
    """
    night_start, night_end = _day(2026, 9, 13)
    dusk = _by_event(solar_events(ephemeris, BUCHAREST, night_start, night_end))[
        SolarEvent.ASTRONOMICAL_DUSK
    ].when
    assert dusk is not None
    browser_start = dusk.replace(microsecond=(dusk.microsecond // 1_000) * 1_000)

    events = _by_event(
        solar_events(ephemeris, BUCHAREST, browser_start, browser_start + timedelta(hours=4))
    )

    returned = events[SolarEvent.ASTRONOMICAL_DUSK]
    assert returned.outcome is Outcome.OCCURS
    assert returned.when == dusk


# --- DST -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "moment", "kind"),
    [
        ("Europe/Bucharest", (2026, 3, 29, 3, 30), LocalTimeKind.NONEXISTENT),
        ("Europe/Bucharest", (2026, 10, 25, 3, 30), LocalTimeKind.AMBIGUOUS),
        ("Europe/Bucharest", (2026, 7, 1, 12, 0), LocalTimeKind.UNIQUE),
        ("Europe/London", (2026, 3, 29, 1, 30), LocalTimeKind.NONEXISTENT),
        ("Europe/London", (2026, 10, 25, 1, 30), LocalTimeKind.AMBIGUOUS),
        ("Australia/Sydney", (2026, 10, 4, 2, 30), LocalTimeKind.NONEXISTENT),
        ("Australia/Sydney", (2026, 4, 5, 2, 30), LocalTimeKind.AMBIGUOUS),
    ],
)
def test_local_time_kind(zone: str, moment: tuple[int, ...], kind: LocalTimeKind) -> None:
    assert local_time_kind(datetime(*moment), ZoneInfo(zone)) is kind  # noqa: DTZ001


def test_the_autumn_night_that_contains_0330_twice() -> None:
    """CLAUDE.md invariant 5: the ambiguous hour is resolved by an explicit fold,
    never by luck."""
    zone = ZoneInfo("Europe/Bucharest")
    wall = datetime(2026, 10, 25, 3, 30)  # noqa: DTZ001
    first = local_to_utc(wall, zone, fold=0)
    second = local_to_utc(wall, zone, fold=1)
    assert (second - first) == timedelta(hours=1)
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)


def test_the_spring_night_that_contains_0330_not_at_all() -> None:
    zone = ZoneInfo("Europe/Bucharest")
    with pytest.raises(NonexistentLocalTimeError, match="does not exist"):
        local_to_utc(datetime(2026, 3, 29, 3, 30), zone)  # noqa: DTZ001


# --- targets ---------------------------------------------------------------


def test_circumpolar_target_never_sets(ephemeris: Ephemeris) -> None:
    """Polaris from Bucharest: above 20 deg all night, and said so explicitly."""
    start, end = _day(2026, 1, 15)
    events = _by_event(
        target_events(ephemeris, BUCHAREST, Hours(2.5303), Deg(89.2641), start, end, Deg(20.0))
    )
    assert events["rises_above"].outcome is Outcome.ALWAYS_ABOVE
    assert events["sets_below"].outcome is Outcome.ALWAYS_ABOVE


def test_far_southern_target_never_rises(ephemeris: Ephemeris) -> None:
    """The Large Magellanic Cloud is not observable from 44 N, and that is stated."""
    start, end = _day(2026, 1, 15)
    events = _by_event(
        target_events(ephemeris, BUCHAREST, Hours(5.3923), Deg(-69.7561), start, end, Deg(20.0))
    )
    assert events["rises_above"].outcome is Outcome.ALWAYS_BELOW


def test_target_transit_has_zero_hour_angle(ephemeris: Ephemeris) -> None:
    """NGC 2244 (DESIGN 3.4): at the transit the solver reports, the hour angle is zero."""
    start, end = _day(2026, 1, 15)
    events = _by_event(
        target_events(ephemeris, BUCHAREST, Hours(6.5317), Deg(4.9407), start, end, Deg(20.0))
    )
    transit = events["transits"].when
    assert transit is not None
    position = target_position(ephemeris, BUCHAREST, Hours(6.5317), Deg(4.9407), transit)
    assert float(position.hour_angle_deg) == pytest.approx(0.0, abs=0.01)


def test_sidereal_hour_angle_tracks_the_ephemeris(ephemeris: Ephemeris) -> None:
    """The constant-rate hour angle framing.py relies on is good to well under an
    arcminute over a night — four orders of magnitude finer than the geometry needs."""
    moment = datetime(2026, 1, 15, 18, 0, tzinfo=UTC)
    first = target_position(ephemeris, BUCHAREST, Hours(6.5317), Deg(4.9407), moment)
    for hours in (1.0, 4.0, 8.0):
        later = target_position(
            ephemeris, BUCHAREST, Hours(6.5317), Deg(4.9407), moment + timedelta(hours=hours)
        )
        predicted = sidereal_hour_angle_deg(first.hour_angle_deg, hours)
        assert float(later.hour_angle_deg) == pytest.approx(float(predicted), abs=1.0 / 60.0)


# --- moon phase --------------------------------------------------------------


def test_moon_phase_matches_skyfields_own_quarter_finder(ephemeris: Ephemeris) -> None:
    """The mirror-image bug this exists to prevent: First and Last Quarter both
    read ~50% illuminated, but ``moon_phase`` must tell them apart.

    Finds the four phase instants for January 2026 independently, via
    :func:`skyfield.almanac.find_discrete` / :func:`skyfield.almanac.moon_phases`
    (the same primitive ``moon_phase`` itself calls, but driven here from
    scratch rather than reusing ``moon_phase``'s own machinery), then checks
    ``moon_phase`` agrees at each.
    """
    from skyfield import almanac

    ts = ephemeris.timescale
    t0 = ts.from_datetime(datetime(2026, 1, 1, tzinfo=UTC))
    t1 = ts.from_datetime(datetime(2026, 2, 1, tzinfo=UTC))
    times, phase_codes = almanac.find_discrete(t0, t1, almanac.moon_phases(ephemeris.kernel))
    assert set(phase_codes) == {0, 1, 2, 3}, "expected all four quarters in a 31-day window"

    expected = {
        0: (0.0, True),  # New: 0 deg, illumination growing
        1: (90.0, True),  # First Quarter: 90 deg, still growing towards Full
        2: (180.0, False),  # Full: 180 deg, illumination now shrinking
        3: (270.0, False),  # Last Quarter: 270 deg, still shrinking towards New
    }
    for t, code in zip(times, phase_codes, strict=True):
        result = moon_phase(ephemeris, BUCHAREST, t.utc_datetime())
        expected_angle, expected_waxing = expected[int(code)]
        assert float(result.phase_angle_deg) == pytest.approx(expected_angle, abs=0.5)
        assert result.waxing is expected_waxing

    # The money check: First and Last Quarter both read ~50% illuminated, but
    # they are mirror images and must not be told apart by fraction alone.
    first_quarter = moon_phase(
        ephemeris, BUCHAREST, times[list(phase_codes).index(1)].utc_datetime()
    )
    last_quarter = moon_phase(
        ephemeris, BUCHAREST, times[list(phase_codes).index(3)].utc_datetime()
    )
    assert first_quarter.illuminated_fraction == pytest.approx(
        last_quarter.illuminated_fraction, abs=0.02
    )
    assert first_quarter.waxing is True
    assert last_quarter.waxing is False


def test_moon_phase_illuminated_fraction_peaks_at_full(ephemeris: Ephemeris) -> None:
    new = moon_phase(ephemeris, BUCHAREST, datetime(2026, 1, 18, 20, tzinfo=UTC))
    full = moon_phase(ephemeris, BUCHAREST, datetime(2026, 1, 3, 10, tzinfo=UTC))
    assert new.illuminated_fraction < 0.05
    assert full.illuminated_fraction > 0.95


def test_moon_phase_rejects_a_naive_datetime(ephemeris: Ephemeris) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        moon_phase(ephemeris, BUCHAREST, datetime(2026, 1, 1))  # noqa: DTZ001 - deliberately naive


# --- annual altitude profile -------------------------------------------------


def _by_month(
    nights: tuple[AnnualAltitudeNight, ...],
) -> dict[tuple[int, int], AnnualAltitudeNight]:
    return {(n.window_start.year, n.window_start.month): n for n in nights}


def test_annual_profile_mid_latitude_target_peaks_in_its_known_season(
    ephemeris: Ephemeris,
) -> None:
    """M42 (the Orion Nebula) is textbook-known as a Northern-hemisphere winter
    target: well placed in the evening sky roughly Sep-Mar, behind the Sun in
    June. That is an expectation reasoned independently of this function.

    ``hours_above_limit`` at a threshold close to M42's ~40 deg transit
    altitude from Bucharest is the metric that actually carries a seasonal
    signal here: the *peak* altitude alone is close to the transit altitude
    on every night the transit happens to fall inside the dark window at
    all — a wide arc of the year — and arcsecond-level year-round variation
    in that near-constant value is not a meaningful "best night" signal.
    ``hours_above_limit`` at a threshold near the transit altitude, by
    contrast, is only large when the transit lands *deep inside* the dark
    window (near local midnight), which is the real "well placed" season.
    """
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    nights = annual_altitude_profile(
        ephemeris,
        BUCHAREST,
        Hours(5.5883),
        Deg(-5.391),
        start,
        365,
        Deg(30.0),
        step_minutes=30.0,
    )
    assert len(nights) == 365

    scored = [n for n in nights if (n.hours_above_limit or 0.0) > 0.0]
    assert scored, "M42 should clear 30 deg above Bucharest for at least some of the year"
    best = max(scored, key=lambda n: n.hours_above_limit or 0.0)
    assert best.window_start.month in {9, 10, 11, 12, 1, 2, 3}, (
        f"expected M42's best night around Sep-Mar, got {best.window_start.isoformat()}"
    )

    by_month = _by_month(nights)
    summer = by_month[(2026, 6)]
    # Real darkness still exists at 44 N in June, so this is a real
    # zero-vs-a-number comparison, not a None standing in for "didn't check".
    assert summer.darkness_hours > 0.0
    assert summer.hours_above_limit == 0.0


def test_annual_profile_reports_true_nightly_maximum_separately_from_dark_peak(
    ephemeris: Ephemeris,
) -> None:
    """The yearly chart needs an almanac-style maximum altitude, not only the
    target's best altitude during astronomical darkness.

    M31 culminates very high from Bucharest, but in early spring its transit
    happens in daylight. The dark-window peak is then much lower, which is
    useful for imaging-time scoring but must not be labelled as the object's
    nightly maximum.
    """
    start = datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    night = annual_altitude_profile(
        ephemeris,
        BUCHAREST,
        Hours(0.7123),
        Deg(41.269),
        start,
        1,
        Deg(20.0),
        step_minutes=15.0,
    )[0]

    expected_transit_altitude = 90.0 - abs(float(BUCHAREST.latitude_deg) - 41.269)
    assert night.max_altitude_deg == pytest.approx(expected_transit_altitude, abs=0.3)
    assert night.peak_altitude_deg is not None
    assert night.max_altitude_deg - night.peak_altitude_deg > 30.0
    assert night.max_when != night.peak_when


def test_annual_profile_circumpolar_target_clears_the_limit_all_night(
    ephemeris: Ephemeris,
) -> None:
    """Polaris from Bucharest, well above 20 deg all night (``test_circumpolar_
    target_never_sets``): every dark hour counts as an hour above the limit."""
    start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    nights = annual_altitude_profile(
        ephemeris, BUCHAREST, Hours(2.5303), Deg(89.2641), start, 5, Deg(20.0), step_minutes=30.0
    )
    for night in nights:
        assert night.darkness_hours > 0.0
        assert night.peak_altitude_deg is not None
        assert night.hours_above_limit == pytest.approx(night.darkness_hours, abs=0.05)


def test_annual_profile_target_that_never_rises_reports_zero_not_none(
    ephemeris: Ephemeris,
) -> None:
    """The LMC from Bucharest never comes above the horizon
    (``test_far_southern_target_never_rises``). There is still real darkness,
    so ``hours_above_limit`` must be an honest ``0.0`` and the peak altitude a
    real (negative) number — never a ``None`` standing in for "didn't check"."""
    start = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    nights = annual_altitude_profile(
        ephemeris, BUCHAREST, Hours(5.3923), Deg(-69.7561), start, 3, Deg(0.0), step_minutes=30.0
    )
    for night in nights:
        assert night.darkness_hours > 0.0
        assert night.peak_altitude_deg is not None
        assert night.peak_altitude_deg < Deg(0.0)
        assert night.hours_above_limit == 0.0


def test_annual_profile_polar_summer_night_has_no_darkness_at_all(ephemeris: Ephemeris) -> None:
    """Tromso (69.6 N) around the summer solstice: the Sun never reaches -18
    deg, so there is no astronomical darkness to report a peak in. That is
    ``None``, not a guessed zero (CLAUDE.md invariant 7)."""
    start = datetime(2026, 6, 20, 12, 0, tzinfo=UTC)
    nights = annual_altitude_profile(
        ephemeris, TROMSO, Hours(5.5883), Deg(-5.391), start, 3, Deg(20.0), step_minutes=30.0
    )
    for night in nights:
        assert night.darkness_hours == 0.0
        assert night.max_altitude_deg is not None
        assert night.max_when is not None
        assert night.peak_altitude_deg is None
        assert night.peak_when is None
        assert night.hours_above_limit is None


def test_annual_profile_rejects_bad_inputs(ephemeris: Ephemeris) -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="nights must be at least 1"):
        annual_altitude_profile(ephemeris, BUCHAREST, Hours(5.5), Deg(0.0), start, 0, Deg(0.0))
    with pytest.raises(ValueError, match="step_minutes must be positive"):
        annual_altitude_profile(
            ephemeris, BUCHAREST, Hours(5.5), Deg(0.0), start, 1, Deg(0.0), step_minutes=0.0
        )
    with pytest.raises(ValueError, match="min_altitude_deg out of range"):
        annual_altitude_profile(ephemeris, BUCHAREST, Hours(5.5), Deg(0.0), start, 1, Deg(120.0))


def test_annual_profile_365_nights_is_reasonably_fast(ephemeris: Ephemeris) -> None:
    """A regression guard, not a benchmark: a 365-night request must not
    silently regress back to a multi-second, one-ephemeris-call-per-night
    implementation. See the task report for the actual measured latency
    through the live HTTP endpoint."""
    import time

    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    t0 = time.perf_counter()
    annual_altitude_profile(ephemeris, BUCHAREST, Hours(5.5883), Deg(-5.391), start, 365, Deg(20.0))
    elapsed = time.perf_counter() - t0
    assert elapsed < 5.0, f"365-night profile took {elapsed:.2f}s, expected well under 5s"
