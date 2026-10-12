# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Twilight, rise, set and transit solving against a pinned JPL ephemeris.

Everything here is a pure function of its arguments plus one immutable
:class:`Ephemeris` handle. There is no ``datetime.now()`` in this module and
there never will be: the caller supplies the window, so the same inputs always
produce the same output and the solver is testable without a sky.

Definitions used (they match the USNO conventions the fixture table validates
against, ``tests/astro/test_almanac_validation.py``):

* **Sunrise / sunset** — geometric centre of the Sun at
  ``-(34' refraction + 16' semidiameter + dip)``. The geometric horizon dip
  ``1.76' * sqrt(h_m)`` (DESIGN 4.1) applies **here only**.
* **Civil / nautical / astronomical twilight** — geometric centre of the Sun at
  -6, -12 and -18 degrees. No refraction, no semidiameter, **no dip**: these are
  definitions about the Sun's true depression, not about seeing the horizon.
* **Moonrise / moonset** — upper limb plus refraction, with the Moon's varying
  apparent radius and topocentric parallax taken from the ephemeris.
* **Transit** — upper meridian transit (hour angle zero).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo

import numpy as np
from skyfield.almanac import find_risings, find_settings, find_transits
from skyfield.almanac import moon_phase as _skyfield_moon_phase
from skyfield.api import Star, load, wgs84

from ssc.astro.horizon import HorizonProfile, is_above_horizon
from ssc.astro.units import (
    SIDEREAL_DEG_PER_HOUR,
    ArcMin,
    Deg,
    Hours,
    Metres,
    arcmin_to_deg,
    signed_delta_deg,
    wrap_deg_180,
    wrap_deg_360,
)

# --- the pinned kernel -----------------------------------------------------

#: DE440s: the "small" JPL DE440, 1849-2150, sufficient for any observing plan
#: and small enough to cache next to the app. Pinned by name, size and digest so
#: a swapped or truncated file is an error rather than a silent change of answer.
KERNEL_FILENAME: Final = "de440s.bsp"
KERNEL_URL: Final = "https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/de440s.bsp"
KERNEL_SIZE_BYTES: Final = 32_726_016
KERNEL_SHA256: Final = "c1c7feeab882263fc493a9d5a5b2ddd71b54826cdf65d8d17a76126b260a49f2"


class EphemerisMissingError(RuntimeError):
    """The pinned kernel is not on disk. Raised instead of downloading anything."""


class EphemerisCorruptError(RuntimeError):
    """The kernel on disk is not the pinned one."""


def default_kernel_path() -> Path:
    """Where the kernel is cached: ``<repo>/data/ephemeris/de440s.bsp``.

    Gitignored — it is fetched once at install time, never at import time and
    never from inside a solver.
    """
    return Path(__file__).resolve().parents[2] / "data" / "ephemeris" / KERNEL_FILENAME


def _missing_kernel_message(path: Path) -> str:
    return (
        f"Ephemeris kernel not found at {path}.\n"
        f"SSC never downloads it implicitly. Fetch it once:\n"
        f"    mkdir -p {path.parent}\n"
        f"    curl -L -o {path} {KERNEL_URL}\n"
        f"Expected size {KERNEL_SIZE_BYTES} bytes, sha256 {KERNEL_SHA256}."
    )


# --- observing site --------------------------------------------------------

#: Module-level singletons so dataclass and argument defaults are names, not calls.
SEA_LEVEL: Final = Metres(0.0)
TRUE_HORIZON_DEG: Final = Deg(0.0)

# Skyfield's rise/set iteration can degenerate when an event sits within the
# sub-millisecond rounding gap at a search boundary: its first correction is
# zero, so the next hour-angle rate divides 0/0 and feeds NaN into jplephem.
# Search one second wider and retain only results in the caller's closed
# interval. This preserves a real boundary event instead of hiding the warning
# by moving the requested interval inward.
_EVENT_SEARCH_PADDING = timedelta(seconds=1)


@dataclass(frozen=True, slots=True)
class Site:
    """An observing location. Longitude is positive **east**, latitude positive north."""

    latitude_deg: Deg
    longitude_deg: Deg
    elevation_m: Metres = SEA_LEVEL

    def __post_init__(self) -> None:
        if not -90.0 <= self.latitude_deg <= 90.0:
            raise ValueError(f"latitude out of range: {self.latitude_deg}")
        if not -180.0 <= self.longitude_deg <= 360.0:
            raise ValueError(f"longitude out of range: {self.longitude_deg}")
        if self.elevation_m < 0.0:
            raise ValueError(
                f"negative elevation {self.elevation_m} m: the horizon dip is undefined below "
                "the ellipsoid; pass 0.0 if the true elevation is unknown"
            )


def horizon_dip_deg(elevation_m: Metres) -> Deg:
    """Geometric horizon dip ``1.76' * sqrt(h_m)`` (DESIGN 4.1), in degrees.

    Applies to sunrise and sunset only. It is ~0.04 deg at 2 m and ~1.2 deg at
    500 m, which moves sunset by minutes at altitude and moves the twilight
    definitions not at all.
    """
    if elevation_m < 0.0:
        raise ValueError(f"negative elevation: {elevation_m}")
    return arcmin_to_deg(ArcMin(1.76 * math.sqrt(elevation_m)))


#: Mean atmospheric refraction at the horizon, 34 arcmin (USNO convention).
REFRACTION_AT_HORIZON_DEG: Final = Deg(0.5667)
#: Adopted solar semidiameter, 16 arcmin (USNO convention; the true value varies
#: by +/-0.3 arcmin over the year, worth about 1 s of rise time).
SOLAR_SEMIDIAMETER_DEG: Final = Deg(0.2667)

CIVIL_TWILIGHT_DEG: Final = Deg(-6.0)
NAUTICAL_TWILIGHT_DEG: Final = Deg(-12.0)
ASTRONOMICAL_TWILIGHT_DEG: Final = Deg(-18.0)


# --- event vocabulary ------------------------------------------------------


class SolarEvent(StrEnum):
    """Solar events, named as ``docs/DESIGN.md`` 4.1 names them in a ``TimeSpec``."""

    ASTRONOMICAL_DAWN = "astronomical_dawn"
    NAUTICAL_DAWN = "nautical_dawn"
    CIVIL_DAWN = "civil_dawn"
    SUNRISE = "sunrise"
    SOLAR_TRANSIT = "solar_transit"
    SUNSET = "sunset"
    CIVIL_DUSK = "civil_dusk"
    NAUTICAL_DUSK = "nautical_dusk"
    ASTRONOMICAL_DUSK = "astronomical_dusk"
    ASTRONOMICAL_MIDNIGHT = "astronomical_midnight"


class LunarEvent(StrEnum):
    MOONRISE = "moonrise"
    MOON_TRANSIT = "moon_transit"
    MOONSET = "moonset"


class TargetEvent(StrEnum):
    RISES_ABOVE = "rises_above"
    TRANSITS = "transits"
    SETS_BELOW = "sets_below"


class Outcome(StrEnum):
    """Why an event list is empty. ``None`` never silently means "midnight".

    ``ALWAYS_ABOVE`` / ``ALWAYS_BELOW`` are the polar cases: the body never
    crossed the threshold at all. ``NOT_IN_WINDOW`` is the ordinary case where
    it did cross, but not in this direction inside the window — the Moon that
    rose at 22:00 and sets tomorrow morning.
    """

    OCCURS = "occurs"
    ALWAYS_ABOVE = "always_above"
    ALWAYS_BELOW = "always_below"
    NOT_IN_WINDOW = "not_in_window"


@dataclass(frozen=True, slots=True)
class EventTime:
    """One resolved event, or one explicitly-explained absence.

    ``when`` is ``None`` if and only if ``outcome`` is not ``OCCURS``; in that
    case ``outcome`` says whether the body stayed above or below the threshold
    for the whole window (polar summer / polar night). Callers must not treat a
    missing time as "did not happen for unknown reasons".
    """

    event: str
    when: datetime | None
    outcome: Outcome
    threshold_deg: Deg


# --- the ephemeris handle --------------------------------------------------


@dataclass(frozen=True)
class Ephemeris:
    """A loaded kernel plus its timescale. Construct once, share freely."""

    path: Path
    kernel: Any
    timescale: Any

    @property
    def earth(self) -> Any:
        return self.kernel["earth"]

    @property
    def sun(self) -> Any:
        return self.kernel["sun"]

    @property
    def moon(self) -> Any:
        return self.kernel["moon"]

    def close(self) -> None:
        """Release the kernel's file handle. The handle is a real OS resource, so a
        long-lived process should own one :class:`Ephemeris` and close it on shutdown."""
        self.kernel.close()

    def __enter__(self) -> Ephemeris:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def observer(self, site: Site) -> Any:
        return self.earth + wgs84.latlon(
            latitude_degrees=float(site.latitude_deg),
            longitude_degrees=float(site.longitude_deg),
            elevation_m=float(site.elevation_m),
        )


def load_ephemeris(path: Path | None = None, *, verify_digest: bool = False) -> Ephemeris:
    """Load the pinned ``de440s`` kernel from disk.

    Never downloads. If the file is absent this raises
    :class:`EphemerisMissingError` with the exact URL and command to fetch it —
    a loud failure at the seam beats a silent 32 MB fetch at import time.

    ``verify_digest`` re-hashes the file (~50 ms). The size check is always done
    because it is free and catches the common failure, a truncated download.
    """
    resolved = path if path is not None else default_kernel_path()
    if not resolved.is_file():
        raise EphemerisMissingError(_missing_kernel_message(resolved))

    size = resolved.stat().st_size
    if size != KERNEL_SIZE_BYTES:
        raise EphemerisCorruptError(
            f"{resolved} is {size} bytes, expected {KERNEL_SIZE_BYTES} for the pinned "
            f"{KERNEL_FILENAME}. Re-fetch from {KERNEL_URL}."
        )
    if verify_digest:
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if digest != KERNEL_SHA256:
            raise EphemerisCorruptError(
                f"{resolved} has sha256 {digest}, expected {KERNEL_SHA256}. "
                f"Re-fetch from {KERNEL_URL}."
            )

    return Ephemeris(path=resolved, kernel=load(str(resolved)), timescale=load.timescale())


# --- local time and DST ----------------------------------------------------


class NonexistentLocalTimeError(ValueError):
    """A wall-clock time that the spring-forward DST gap skipped entirely."""


class LocalTimeKind(StrEnum):
    UNIQUE = "unique"
    AMBIGUOUS = "ambiguous"
    NONEXISTENT = "nonexistent"


def local_time_kind(naive_local: datetime, tz: ZoneInfo) -> LocalTimeKind:
    """Classify a wall clock reading against a zone's DST transitions.

    An autumn night contains 01:30 twice and a spring night contains it not at
    all. PEP 495 distinguishes the two by *which* fold carries the larger UTC
    offset: in a fall-back the first reading is still on summer time and so has
    the larger offset, while in a spring-forward gap the first reading is still
    on winter time and has the smaller one. Comparing the offsets for inequality
    alone would conflate the two, which is exactly the bug that silently shifts
    one night a year by an hour.
    """
    if naive_local.tzinfo is not None:
        raise ValueError("local_time_kind takes a naive wall-clock datetime")
    first = naive_local.replace(tzinfo=tz, fold=0).utcoffset()
    second = naive_local.replace(tzinfo=tz, fold=1).utcoffset()
    if first is None or second is None:  # pragma: no cover - ZoneInfo always resolves
        raise ValueError(f"{tz} gave no UTC offset for {naive_local.isoformat()}")
    if first == second:
        return LocalTimeKind.UNIQUE
    return LocalTimeKind.AMBIGUOUS if first > second else LocalTimeKind.NONEXISTENT


def local_to_utc(naive_local: datetime, tz: ZoneInfo, *, fold: int = 0) -> datetime:
    """Convert a wall clock reading to UTC, refusing to guess.

    ``fold=0`` selects the first (pre-transition) reading of an ambiguous time
    and ``fold=1`` the second. A nonexistent time raises rather than being
    silently shifted, because "22:00, except on one night a year when it means
    23:00" is exactly the bug invariant 5 exists to prevent.
    """
    if local_time_kind(naive_local, tz) is LocalTimeKind.NONEXISTENT:
        raise NonexistentLocalTimeError(
            f"{naive_local.isoformat()} does not exist in {tz.key}: the DST gap skips it. "
            "Choose a time either side of the transition."
        )
    return naive_local.replace(tzinfo=tz, fold=fold).astimezone(UTC)


# --- internals -------------------------------------------------------------


def _require_utc(name: str, value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are banned (invariant 5)")
    return value.astimezone(UTC)


def _as_time(ephemeris: Ephemeris, when: datetime) -> Any:
    return ephemeris.timescale.from_datetime(when)


def _to_datetimes(times: Any) -> list[datetime]:
    """Normalise skyfield's scalar-or-array ``utc_datetime()`` to a list."""
    if len(times) == 0:
        return []
    value = times.utc_datetime()
    if isinstance(value, datetime):
        return [value]
    return [d.replace(tzinfo=UTC) if d.tzinfo is None else d for d in value]


def _altitude_range_deg(
    observer: Any, target: Any, t0: datetime, t1: datetime, timescale: Any, samples: int = 145
) -> tuple[float, float]:
    """Min and max geometric altitude over the window, sampled uniformly.

    Only used to explain an *absent* event, so a coarse sample (10 min over a
    day) is ample: it has to distinguish "never rose" from "never set".
    """
    step = (t1 - t0) / max(samples - 1, 1)
    grid = timescale.from_datetimes([t0 + step * i for i in range(samples)])
    altitude = observer.at(grid).observe(target).apparent().altaz()[0].degrees
    return float(altitude.min()), float(altitude.max())


def _rise_set(
    ephemeris: Ephemeris,
    site: Site,
    target: Any,
    event: str,
    rising: bool,
    threshold_deg: Deg,
    start: datetime,
    end: datetime,
    *,
    use_body_radius: bool = False,
) -> list[EventTime]:
    observer = ephemeris.observer(site)
    t0 = _as_time(ephemeris, start - _EVENT_SEARCH_PADDING)
    t1 = _as_time(ephemeris, end + _EVENT_SEARCH_PADDING)
    horizon = None if use_body_radius else float(threshold_deg)
    finder = find_risings if rising else find_settings
    times, is_real = finder(observer, target, t0, t1, horizon_degrees=horizon)
    found = [
        EventTime(event=event, when=when, outcome=Outcome.OCCURS, threshold_deg=threshold_deg)
        for when, real in zip(_to_datetimes(times), list(is_real), strict=True)
        if real and start <= when <= end
    ]
    if found:
        return found
    # Nothing found. Say *why*, from the altitude the body actually reached.
    lowest, highest = _altitude_range_deg(observer, target, start, end, ephemeris.timescale)
    limit = float(threshold_deg)
    if lowest > limit:
        outcome = Outcome.ALWAYS_ABOVE
    elif highest < limit:
        outcome = Outcome.ALWAYS_BELOW
    else:
        outcome = Outcome.NOT_IN_WINDOW
    return [EventTime(event=event, when=None, outcome=outcome, threshold_deg=threshold_deg)]


def _transits(
    ephemeris: Ephemeris, site: Site, target: Any, event: str, start: datetime, end: datetime
) -> list[EventTime]:
    observer = ephemeris.observer(site)
    times = find_transits(observer, target, _as_time(ephemeris, start), _as_time(ephemeris, end))
    return [
        EventTime(event=event, when=when, outcome=Outcome.OCCURS, threshold_deg=Deg(0.0))
        for when in _to_datetimes(times)
    ]


def _sorted(events: Iterable[EventTime]) -> tuple[EventTime, ...]:
    """Chronological, with the explained absences kept at the end in a stable order."""
    listed = list(events)
    epoch = datetime.min.replace(tzinfo=UTC)
    timed = sorted(
        (e for e in listed if e.when is not None), key=lambda e: (e.when or epoch, e.event)
    )
    untimed = [e for e in listed if e.when is None]
    return (*timed, *untimed)


# --- public solvers --------------------------------------------------------


def solar_events(
    ephemeris: Ephemeris, site: Site, start: datetime, end: datetime
) -> tuple[EventTime, ...]:
    """Every solar event in ``[start, end]``, plus an explained absence for each kind that
    did not occur.

    ``start`` and ``end`` must be timezone-aware; they are the only clock this
    function has. Sunrise and sunset carry the site's horizon dip; the three
    twilight pairs deliberately do not.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")

    dip = horizon_dip_deg(site.elevation_m)
    sun_horizon = Deg(-(REFRACTION_AT_HORIZON_DEG + SOLAR_SEMIDIAMETER_DEG + dip))
    sun = ephemeris.sun

    spec: Sequence[tuple[str, bool, Deg]] = (
        (SolarEvent.SUNRISE, True, sun_horizon),
        (SolarEvent.SUNSET, False, sun_horizon),
        (SolarEvent.CIVIL_DAWN, True, CIVIL_TWILIGHT_DEG),
        (SolarEvent.CIVIL_DUSK, False, CIVIL_TWILIGHT_DEG),
        (SolarEvent.NAUTICAL_DAWN, True, NAUTICAL_TWILIGHT_DEG),
        (SolarEvent.NAUTICAL_DUSK, False, NAUTICAL_TWILIGHT_DEG),
        (SolarEvent.ASTRONOMICAL_DAWN, True, ASTRONOMICAL_TWILIGHT_DEG),
        (SolarEvent.ASTRONOMICAL_DUSK, False, ASTRONOMICAL_TWILIGHT_DEG),
    )
    out: list[EventTime] = []
    for event, rising, threshold in spec:
        out.extend(_rise_set(ephemeris, site, sun, event, rising, threshold, start, end))
    out.extend(_transits(ephemeris, site, sun, SolarEvent.SOLAR_TRANSIT, start, end))
    return _sorted(out)


def lunar_events(
    ephemeris: Ephemeris, site: Site, start: datetime, end: datetime
) -> tuple[EventTime, ...]:
    """Moonrise, moon transit and moonset in ``[start, end]``.

    Upper limb plus 34' refraction, with the Moon's apparent radius and
    topocentric parallax taken from the ephemeris rather than a fixed constant —
    the Moon's semidiameter varies by 6% over a month, which is worth minutes.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")

    moon = ephemeris.moon
    limb = Deg(-REFRACTION_AT_HORIZON_DEG)
    out = [
        *_rise_set(
            ephemeris, site, moon, LunarEvent.MOONRISE, True, limb, start, end, use_body_radius=True
        ),
        *_rise_set(
            ephemeris, site, moon, LunarEvent.MOONSET, False, limb, start, end, use_body_radius=True
        ),
        *_transits(ephemeris, site, moon, LunarEvent.MOON_TRANSIT, start, end),
    ]
    return _sorted(out)


@dataclass(frozen=True, slots=True)
class MoonPhase:
    """The Moon's phase at one instant — enough for a client to draw the
    correct limb, which the illuminated fraction alone cannot do.

    **Illuminated fraction alone is ambiguous.** 50% illuminated is either
    first quarter or last quarter, and they are mirror images: one grows
    towards full, the other shrinks towards new. ``phase_angle_deg`` (0 deg
    New Moon, 90 deg First Quarter, 180 deg Full Moon, 270 deg Last Quarter,
    the standard geocentric-ecliptic-longitude-difference convention) and the
    derived ``waxing`` flag resolve that: a client can draw the terminator on
    the correct side without a second call.
    """

    illuminated_fraction: float
    #: 0-360 deg; 0 New, 90 First Quarter, 180 Full, 270 Last Quarter.
    phase_angle_deg: Deg
    #: ``True`` for the New-to-Full half of the cycle (0-180 deg, growing),
    #: ``False`` for the Full-to-New half (180-360 deg, shrinking). Exactly
    #: New (0 deg) counts as waxing; exactly Full (180 deg) does not — an
    #: arbitrary but harmless tie-break, since illumination is not changing
    #: measurably at either instant anyway.
    waxing: bool


def moon_phase(ephemeris: Ephemeris, site: Site, when: datetime) -> MoonPhase:
    """The Moon's illuminated fraction plus the phase angle and waxing/waning
    direction needed to draw it correctly (see :class:`MoonPhase`).

    The illuminated fraction is topocentric (from ``site``, matching every
    other apparent-position computation in this module); the phase angle is
    the standard geocentric ecliptic-longitude-difference convention
    (:func:`skyfield.almanac.moon_phase`) — the two differ by at most a few
    arcminutes of parallax, immaterial for either the fraction or the limb a
    client draws from the angle.
    """
    when = _require_utc("when", when)
    instant = _as_time(ephemeris, when)
    fraction = float(
        ephemeris.observer(site)
        .at(instant)
        .observe(ephemeris.moon)
        .apparent()
        .fraction_illuminated(ephemeris.sun)
    )
    angle_deg = float(_skyfield_moon_phase(ephemeris.kernel, instant).degrees) % 360.0
    return MoonPhase(
        illuminated_fraction=fraction,
        phase_angle_deg=Deg(angle_deg),
        waxing=angle_deg < 180.0,
    )


def target(ra_j2000_h: Hours, dec_j2000_deg: Deg) -> Any:
    """A fixed catalogue target. RA in hours here because that is the JSON boundary
    form in DESIGN 3.4; everything downstream of this call is degrees."""
    return Star(ra_hours=float(ra_j2000_h), dec_degrees=float(dec_j2000_deg))


def target_events(
    ephemeris: Ephemeris,
    site: Site,
    ra_j2000_h: Hours,
    dec_j2000_deg: Deg,
    start: datetime,
    end: datetime,
    min_altitude_deg: Deg = TRUE_HORIZON_DEG,
) -> tuple[EventTime, ...]:
    """``rises_above`` / ``transits`` / ``sets_below`` for a fixed target (DESIGN 4.1).

    The threshold is a **geometric** altitude — the user's minimum-altitude
    limit, not a refracted horizon — so no dip and no refraction are applied.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    if not -90.0 <= min_altitude_deg <= 90.0:
        raise ValueError(f"min_altitude_deg out of range: {min_altitude_deg}")

    star = target(ra_j2000_h, dec_j2000_deg)
    out = [
        *_rise_set(
            ephemeris, site, star, TargetEvent.RISES_ABOVE, True, min_altitude_deg, start, end
        ),
        *_rise_set(
            ephemeris, site, star, TargetEvent.SETS_BELOW, False, min_altitude_deg, start, end
        ),
        *_transits(ephemeris, site, star, TargetEvent.TRANSITS, start, end),
    ]
    return _sorted(out)


@dataclass(frozen=True, slots=True)
class TargetPosition:
    """Where a target is, right now-ish, for the framing solver to work from."""

    when: datetime
    hour_angle_deg: Deg
    declination_deg: Deg
    altitude_deg: Deg
    azimuth_deg: Deg


def target_position(
    ephemeris: Ephemeris,
    site: Site,
    ra_j2000_h: Hours,
    dec_j2000_deg: Deg,
    when: datetime,
) -> TargetPosition:
    """Apparent hour angle, declination and horizontal coordinates at one instant.

    This is the bridge to :mod:`ssc.astro.framing`: framing needs a starting
    hour angle and a declination and nothing else from the ephemeris, which is
    what keeps it free of I/O.

    Hour angle is wrapped to (-180, +180]: negative east of the meridian,
    positive west, so it increases through transit. Azimuth is [0, 360) from
    north through east.
    """
    when = _require_utc("when", when)
    star = target(ra_j2000_h, dec_j2000_deg)
    apparent = ephemeris.observer(site).at(_as_time(ephemeris, when)).observe(star).apparent()
    hour_angle, declination, _ = apparent.hadec()
    altitude, azimuth, _ = apparent.altaz()
    return TargetPosition(
        when=when,
        hour_angle_deg=wrap_deg_180(float(hour_angle.degrees)),
        declination_deg=Deg(float(declination.degrees)),
        altitude_deg=Deg(float(altitude.degrees)),
        azimuth_deg=wrap_deg_360(float(azimuth.degrees)),
    )


def sidereal_hour_angle_deg(hour_angle_deg: Deg, elapsed_hours: float) -> Deg:
    """Hour angle after ``elapsed_hours`` of UTC, at the mean sidereal rate.

    Wrapped to (-180, +180]. Good to well under an arcminute over a night, which
    is four orders of magnitude finer than the rotation geometry needs, and it
    is what lets :mod:`ssc.astro.framing` sample a session without an ephemeris.
    """
    return wrap_deg_180(hour_angle_deg + SIDEREAL_DEG_PER_HOUR * elapsed_hours)


# --- horizon-aware visibility ----------------------------------------------
#
# Rise/set against a scalar threshold answers "is it above 20 degrees". It does
# not answer the question a backyard observer has, which is "is it above *the
# tree*". Against a mask the answer is a *list* of intervals, because a target
# can clear the treeline, disappear behind a chimney and re-emerge, and
# collapsing that to a single first-up/last-down would claim an hour of
# observing time that a roof owns.

#: Uniform sampling step for the horizon visibility solver. One minute over a
#: night is 1440 samples, which skyfield evaluates in one vectorised pass.
DEFAULT_VISIBILITY_STEP_MINUTES: Final = 1.0
#: How finely each horizon crossing is bisected. A second is far below the
#: minute-level precision any observing plan can act on.
DEFAULT_VISIBILITY_TOLERANCE_SECONDS: Final = 1.0
#: A loud ceiling on grid size: refuse an absurd window rather than allocate for
#: a minute and then answer. 100k samples is ~69 days at the default step.
MAX_VISIBILITY_SAMPLES: Final = 100_000
#: Bisection rounds are computed from the bracket width, then clamped here so a
#: pathological tolerance cannot spin.
_MAX_BISECTION_ROUNDS: Final = 60


@dataclass(frozen=True, slots=True)
class VisibilityInterval:
    """One continuous stretch during which the target is clear of the horizon mask.

    ``start_is_window_edge`` / ``end_is_window_edge`` distinguish a real horizon
    crossing from the window simply running out. A caller that treats a clipped
    edge as a rise time is wrong by however much window it did not ask for, so
    the two cases are never conflated.
    """

    start: datetime
    end: datetime
    start_is_window_edge: bool
    end_is_window_edge: bool

    @property
    def duration_hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600.0


def _altaz_samples(
    observer: Any, star: Any, timescale: Any, times: Sequence[datetime]
) -> list[tuple[datetime, float, float]]:
    """Apparent (time, altitude, azimuth) for a list of instants, in one vectorised call."""
    if not times:
        return []
    grid = timescale.from_datetimes(list(times))
    altitude, azimuth, _ = observer.at(grid).observe(star).apparent().altaz()
    return [
        (when, float(alt), float(az))
        for when, alt, az in zip(times, altitude.degrees, azimuth.degrees, strict=True)
    ]


def _vertex_crossing_times(
    samples: Sequence[tuple[datetime, float, float]], vertex_azimuths: Sequence[float]
) -> list[datetime]:
    """Instants at which the target's azimuth sweeps past a mask vertex.

    Located by linear interpolation on the sampled azimuth series rather than by
    bisection: over one step the azimuth is very nearly linear, and a knot that
    is a second out of place still does its job, which is to put a sample on the
    kink. Assumes the azimuth moves less than 180 deg per step, which holds
    everywhere except within a whisker of the zenith — and a target at the
    zenith is above every skyline anyway.
    """
    crossings: list[datetime] = []
    for (t0, _, az0), (t1, _, az1) in pairwise(samples):
        step = float(signed_delta_deg(az0, az1))
        if step == 0.0:
            continue
        seconds = (t1 - t0).total_seconds()
        for vertex in vertex_azimuths:
            offset = float(signed_delta_deg(az0, vertex))
            crossed = 0.0 < offset <= step if step > 0.0 else step <= offset < 0.0
            if crossed:
                crossings.append(t0 + timedelta(seconds=seconds * (offset / step)))
    return crossings


def _refine_crossings(
    observer: Any,
    star: Any,
    timescale: Any,
    profile: HorizonProfile,
    brackets: Sequence[tuple[datetime, datetime, bool]],
    tolerance_seconds: float,
) -> list[datetime]:
    """Bisect every bracketed crossing at once, one vectorised call per round.

    Each bracket is ``(low, high, above_at_low)``; the target's state differs at
    the two ends by construction, so bisection cannot fail to converge on a
    crossing. Doing all brackets in lockstep keeps the ephemeris cost at
    ``ceil(log2(step / tolerance))`` calls no matter how ragged the skyline is.
    """
    if not brackets:
        return []
    lows = [bracket[0] for bracket in brackets]
    highs = [bracket[1] for bracket in brackets]
    above_at_low = [bracket[2] for bracket in brackets]

    widest = max((high - low).total_seconds() for low, high in zip(lows, highs, strict=True))
    rounds = 0 if widest <= tolerance_seconds else math.ceil(math.log2(widest / tolerance_seconds))
    for _ in range(min(rounds, _MAX_BISECTION_ROUNDS)):
        middles = [low + (high - low) / 2 for low, high in zip(lows, highs, strict=True)]
        for index, (when, alt, az) in enumerate(_altaz_samples(observer, star, timescale, middles)):
            if (
                is_above_horizon(profile, altitude_deg=Deg(alt), azimuth_deg=Deg(az))
                == (above_at_low[index])
            ):
                lows[index] = when
            else:
                highs[index] = when
    return [low + (high - low) / 2 for low, high in zip(lows, highs, strict=True)]


def horizon_visibility(
    ephemeris: Ephemeris,
    site: Site,
    ra_j2000_h: Hours,
    dec_j2000_deg: Deg,
    start: datetime,
    end: datetime,
    profile: HorizonProfile,
    *,
    step_minutes: float = DEFAULT_VISIBILITY_STEP_MINUTES,
    tolerance_seconds: float = DEFAULT_VISIBILITY_TOLERANCE_SECONDS,
) -> tuple[VisibilityInterval, ...]:
    """When is a fixed target above the *horizon profile* in ``[start, end]``?

    Returns every interval during which the target is clear of the mask, in
    chronological order. An empty tuple means the target was behind the skyline
    at every instant sampled — it is a definite "never", not a "do not know".
    Intervals touching the window edge are flagged as such rather than being
    reported as horizon crossings.

    Altitudes are *apparent* (the same refracted altaz the rise/set solver uses),
    so a flat profile gives the same answer as ``target_events`` with the same
    scalar ``min_altitude_deg``.

    **What the sampling guarantees.** Two sample sets are merged. The first is
    uniform at ``step_minutes``. The second puts a sample at every instant the
    target's azimuth sweeps past a *vertex* of the mask. The second is the one
    that matters: the mask is piecewise linear, so its local minima can only
    occur at vertices, and the floor of any notch between two obstructions is
    therefore always sampled. A narrow gap between two trees cannot be stepped
    over however narrow it is, as long as it is a gap the user actually drew.
    Every sign change found is then bisected to ``tolerance_seconds``.

    **What it can still miss.** A visibility window that opens and closes
    entirely *within one straight segment* of the mask and lasts less than
    ``step_minutes`` — the target grazing a smooth roofline tangentially. Also,
    within a whisker of the zenith the azimuth rate diverges and vertex knots
    may land in the wrong step; the uniform grid is the only cover there, which
    is harmless because nothing at the zenith is behind a tree. Pass a smaller
    ``step_minutes`` if a target is expected to skim an obstruction.

    Pure with respect to time: ``start`` and ``end`` are the only clock.
    """
    start = _require_utc("start", start)
    end = _require_utc("end", end)
    if end <= start:
        raise ValueError("end must be after start")
    if step_minutes <= 0.0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes}")
    if tolerance_seconds <= 0.0:
        raise ValueError(f"tolerance_seconds must be positive, got {tolerance_seconds}")

    span_seconds = (end - start).total_seconds()
    count = math.ceil(span_seconds / (step_minutes * 60.0)) + 1
    if count > MAX_VISIBILITY_SAMPLES:
        raise ValueError(
            f"a {span_seconds / 3600.0:.1f} h window at {step_minutes} min steps needs {count} "
            f"samples, over the {MAX_VISIBILITY_SAMPLES} ceiling; shorten the window or "
            "coarsen step_minutes"
        )

    observer = ephemeris.observer(site)
    star = target(ra_j2000_h, dec_j2000_deg)
    uniform = [start + timedelta(seconds=span_seconds * i / (count - 1)) for i in range(count)]
    samples = _altaz_samples(observer, star, ephemeris.timescale, uniform)

    knots = _vertex_crossing_times(samples, [float(az) for az in profile.azimuths_deg])
    if knots:
        merged = {when: (when, alt, az) for when, alt, az in samples}
        for sample in _altaz_samples(observer, star, ephemeris.timescale, knots):
            merged.setdefault(sample[0], sample)
        samples = [merged[key] for key in sorted(merged)]

    above = [
        is_above_horizon(profile, altitude_deg=Deg(alt), azimuth_deg=Deg(az))
        for _, alt, az in samples
    ]
    brackets = [
        (samples[i][0], samples[i + 1][0], above[i])
        for i in range(len(samples) - 1)
        if above[i] != above[i + 1]
    ]
    crossings = _refine_crossings(
        observer, star, ephemeris.timescale, profile, brackets, tolerance_seconds
    )

    intervals: list[VisibilityInterval] = []
    opened_at: datetime | None = start if above[0] else None
    opened_at_edge = above[0]
    for (_, _, was_above), crossing in zip(brackets, crossings, strict=True):
        if was_above:
            if opened_at is not None:
                intervals.append(
                    VisibilityInterval(
                        start=opened_at,
                        end=crossing,
                        start_is_window_edge=opened_at_edge,
                        end_is_window_edge=False,
                    )
                )
                opened_at = None
        else:
            opened_at, opened_at_edge = crossing, False
    if opened_at is not None:
        intervals.append(
            VisibilityInterval(
                start=opened_at,
                end=end,
                start_is_window_edge=opened_at_edge,
                end_is_window_edge=True,
            )
        )
    return tuple(interval for interval in intervals if interval.end > interval.start)


# --- annual altitude profile ------------------------------------------------
#
# "Is this target worth imaging in March" is a whole-year question that
# ``target_events``/``horizon_visibility`` do not answer: they solve one
# window at a time. Naively sampling the target's apparent altitude on a
# uniform grid across the *whole* 24h of every night (as a first cut of this
# function did) is correct but needlessly slow: skyfield's topocentric
# ``observer.at(t)`` has to build a precession-nutation matrix (IAU2000A,
# ~700 summed terms) for every distinct instant, which costs ~60 microseconds
# a point regardless of what is then observed — 87,600 points (365 nights x
# 240 samples) measured ~5s of that matrix alone, almost all of it spent on
# the roughly 60% of every day that is *daylight* and therefore never
# contributes to the answer.
#
# So instead: find the Sun's astronomical dusk/dawn crossings across the
# whole span first, with :func:`skyfield.almanac.find_settings` /
# ``find_risings`` — the same finder ``solar_events`` already uses, and (being
# a coarse-then-bisect search rather than a fixed grid) fast regardless of
# span length: a full year's worth of both takes tens of milliseconds. Then
# only the target's altitude is sampled finely, and only *inside* the
# resulting dark intervals. This is both faster (typically an order of
# magnitude fewer points than the naive grid) and more accurate (the dark
# interval's boundary is the exact solved twilight instant, not a grid cell
# rounded to the nearest few minutes).

#: Sampling step used *inside* each night's dark interval (not across its
#: whole 24h). Fine enough that a target's altitude is close to linear
#: between two samples — the exact crossing of ``min_altitude_deg`` within a
#: step is then found by linear interpolation, not by rounding to a sample —
#: while keeping a 365-night request at a mid-latitude site comfortably under
#: a second (measured ~0.8s; see the endpoint latency quoted in the PR/task
#: report for the live figure).
DEFAULT_ANNUAL_STEP_MINUTES: Final = 15.0
#: A loud ceiling mirroring ``MAX_VISIBILITY_SAMPLES``: refuse an absurd
#: request rather than allocate for it unboundedly — reachable only by an
#: extreme combination of many nights and permanent (polar-night) darkness,
#: since ordinary nights contribute far fewer than 24h/step_minutes samples
#: each.
MAX_ANNUAL_SAMPLES: Final = 500_000


@dataclass(frozen=True, slots=True)
class AnnualAltitudeNight:
    """One night's reduction from :func:`annual_altitude_profile`.

    ``window_start`` is the UTC instant this night's window begins at: night
    ``i`` is ``[start + i*24h, start + (i+1)*24h)``. Callers should pick
    ``start`` away from local midnight (local noon is the usual choice — the
    same convention ``/night``'s own ``start_time``/``end_time`` already
    assume) so a single true night is not split across two windows.

    ``darkness_hours`` is real information on its own: the number of hours
    within this window the Sun's geometric centre is at or below -18 deg
    (astronomical night). It is legitimately ``0.0`` at high latitude in
    midsummer — that is a fact about the night, not a missing measurement.

    ``max_altitude_deg`` / ``max_when`` are the target's maximum apparent
    altitude in the whole 24h window, normally at upper meridian transit. They
    intentionally are not restricted to darkness: this is the almanac-style
    "how high can this object get tonight" value.

    ``peak_altitude_deg`` / ``peak_when`` are the target's maximum apparent
    altitude, and the instant it occurs, restricted to that dark interval.
    They are ``None`` together, and only when ``darkness_hours`` is ``0.0`` —
    there is no dark interval for a peak to be measured in. When there *is*
    darkness but the target never comes above the horizon during it,
    ``peak_altitude_deg`` is still reported, as a negative number: how close
    it got, never a hidden ``None``.

    ``hours_above_limit`` is the number of hours, within the dark interval,
    the target's apparent altitude is at or above the caller's
    ``min_altitude_deg``. It is a real ``0.0`` when there is darkness but the
    target never clears the limit during it, and ``None`` only when there is
    no darkness at all this night to evaluate against (CLAUDE.md invariant 7:
    ``None`` means unknown or inapplicable, never a disguised zero).
    """

    window_start: datetime
    darkness_hours: float
    max_altitude_deg: Deg
    max_when: datetime
    peak_altitude_deg: Deg | None
    peak_when: datetime | None
    hours_above_limit: float | None


def _dark_intervals(
    ephemeris: Ephemeris, site: Site, search_start: datetime, search_end: datetime
) -> list[tuple[datetime, datetime]]:
    """Every astronomically-dark interval (Sun at or below -18 deg) in
    ``[search_start, search_end]``, merged and chronological.

    Built from the same finders ``solar_events`` uses, called once each over
    the whole span rather than once per night: skyfield's search is a
    coarse-then-bisect algorithm, not a fixed grid, so its cost barely grows
    with the length of the span.
    """
    observer = ephemeris.observer(site)
    threshold = float(ASTRONOMICAL_TWILIGHT_DEG)
    t0, t1 = _as_time(ephemeris, search_start), _as_time(ephemeris, search_end)
    dusk_times, dusk_ok = find_settings(observer, ephemeris.sun, t0, t1, horizon_degrees=threshold)
    dawn_times, dawn_ok = find_risings(observer, ephemeris.sun, t0, t1, horizon_degrees=threshold)
    transitions = sorted(
        [
            (when, "dusk")
            for when, ok in zip(_to_datetimes(dusk_times), list(dusk_ok), strict=True)
            if ok
        ]
        + [
            (when, "dawn")
            for when, ok in zip(_to_datetimes(dawn_times), list(dawn_ok), strict=True)
            if ok
        ]
    )

    dark_at_start = (
        _altaz_samples(observer, ephemeris.sun, ephemeris.timescale, [search_start])[0][1]
        <= threshold
    )

    # Real (``ok``) dusk and dawn crossings of a continuous altitude curve
    # must strictly alternate; a same-direction pair back to back would mean
    # the search returned something other than a clean crossing series. That
    # "cannot happen" case is asserted rather than silently absorbed — architecture policy
    # invariant 7 forbids quietly keeping the old state and fabricating (or
    # dropping) an interval on the strength of an assumption that turned out
    # to be wrong.
    intervals: list[tuple[datetime, datetime]] = []
    is_dark = dark_at_start
    opened_at: datetime | None = search_start if is_dark else None
    for when, kind in transitions:
        if kind == "dusk":
            if is_dark:
                raise AssertionError(f"dusk found while already dark, at {when.isoformat()}")
            opened_at, is_dark = when, True
        else:
            if not is_dark or opened_at is None:
                raise AssertionError(f"dawn found while already light, at {when.isoformat()}")
            intervals.append((opened_at, when))
            opened_at, is_dark = None, False
    if is_dark and opened_at is not None:
        intervals.append((opened_at, search_end))
    return intervals


def annual_altitude_profile(
    ephemeris: Ephemeris,
    site: Site,
    ra_j2000_h: Hours,
    dec_j2000_deg: Deg,
    start: datetime,
    nights: int,
    min_altitude_deg: Deg,
    *,
    step_minutes: float = DEFAULT_ANNUAL_STEP_MINUTES,
) -> tuple[AnnualAltitudeNight, ...]:
    """One summary sample per night over ``nights`` consecutive 24h windows.

    Answers "when in the year is this target actually worth imaging": for
    each night, the target's peak apparent altitude during astronomical
    darkness, when that peak occurred, and how many hours it spent above
    ``min_altitude_deg`` while it was dark. See :class:`AnnualAltitudeNight`
    for how an absent darkness window is distinguished from a real zero.

    Night ``i`` is the 24h window ``[start + i*24h, start + (i+1)*24h)``.
    Pure with respect to time: ``start`` is the only clock, and it must be
    timezone-aware (invariant 5). Pick ``start`` away from local midnight
    (local noon is the usual choice) so a single true night is not split
    across two windows — the same convention ``/night``'s own
    ``start_time``/``end_time`` already assume.

    Implementation note: see the comment above :data:`DEFAULT_ANNUAL_STEP_MINUTES`
    for why this finds the Sun's astronomical dusk/dawn crossings across the
    whole span first (two fast, non-grid skyfield searches), and only then
    finely samples the *target's* altitude, and only inside the resulting
    dark intervals, in one vectorised call — not a Python loop making one
    ephemeris call per night, and not a uniform grid across every daylight
    hour that can never contribute to the answer. Within a dark interval, the
    crossing of ``min_altitude_deg`` is linearly interpolated between the two
    bracketing samples rather than rounded to the nearer one, so
    ``hours_above_limit`` is not quantised to whole samples the way a plain
    rectangle-rule count would be.
    """
    start = _require_utc("start", start)
    if nights < 1:
        raise ValueError(f"nights must be at least 1, got {nights}")
    if step_minutes <= 0.0:
        raise ValueError(f"step_minutes must be positive, got {step_minutes}")
    if not -90.0 <= min_altitude_deg <= 90.0:
        raise ValueError(f"min_altitude_deg out of range: {min_altitude_deg}")

    window_starts = [start + timedelta(hours=24.0 * i) for i in range(nights)]
    span_start, span_end = window_starts[0], window_starts[-1] + timedelta(hours=24.0)

    # Dusk/dawn recur roughly every 24h, so a 36h pad guarantees the interval
    # covering span_start (if any) opened before it, and the one covering
    # span_end (if any) closes after it.
    pad = timedelta(hours=36.0)
    dark_intervals = _dark_intervals(ephemeris, site, span_start - pad, span_end + pad)

    step = timedelta(minutes=step_minutes)
    limit = float(min_altitude_deg)

    # Build one combined, per-night-clipped sample plan before touching the
    # ephemeris at all, so the (potentially expensive) topocentric call below
    # happens exactly once for the whole request.
    night_slices: list[tuple[int, int]] = []  # (start index, end index) into `times`
    night_clipped: list[list[tuple[datetime, datetime]]] = []
    times: list[datetime] = []
    for window_start in window_starts:
        window_end = window_start + timedelta(hours=24.0)
        clipped = [
            (max(lo, window_start), min(hi, window_end))
            for lo, hi in dark_intervals
            if lo < window_end and hi > window_start
        ]
        lo_index = len(times)
        for lo, hi in clipped:
            duration = hi - lo
            count = max(2, math.ceil(duration / step) + 1)
            times.extend(lo + duration * (j / (count - 1)) for j in range(count))
        night_slices.append((lo_index, len(times)))
        night_clipped.append(clipped)

    if len(times) > MAX_ANNUAL_SAMPLES:
        raise ValueError(
            f"{nights} nights of darkness need {len(times)} samples at {step_minutes} min "
            f"steps, over the {MAX_ANNUAL_SAMPLES} ceiling; shorten the span or coarsen "
            "step_minutes"
        )

    observer = ephemeris.observer(site)
    star = target(ra_j2000_h, dec_j2000_deg)
    target_alt_deg: Any = np.empty(0)
    transit_alt_deg: Any = np.empty(0)
    if times:
        grid = ephemeris.timescale.from_datetimes(times)
        target_alt_deg = observer.at(grid).observe(star).apparent().altaz()[0].degrees
    transit_times = find_transits(
        observer, star, _as_time(ephemeris, span_start), _as_time(ephemeris, span_end)
    )
    transit_datetimes = _to_datetimes(transit_times)
    if transit_datetimes:
        transit_alt_deg = observer.at(transit_times).observe(star).apparent().altaz()[0].degrees

    transit_by_night: list[list[tuple[datetime, float]]] = [[] for _ in window_starts]
    for when, altitude in zip(transit_datetimes, transit_alt_deg, strict=True):
        offset_days = int((when - span_start).total_seconds() // 86_400)
        if 0 <= offset_days < len(transit_by_night):
            transit_by_night[offset_days].append((when, float(altitude)))

    nightly: list[AnnualAltitudeNight] = []
    for window_start, clipped, (lo_index, hi_index), transits in zip(
        window_starts, night_clipped, night_slices, transit_by_night, strict=True
    ):
        darkness_hours = sum((hi - lo).total_seconds() for lo, hi in clipped) / 3600.0
        if transits:
            max_when, max_altitude_raw = max(transits, key=lambda item: item[1])
            max_altitude = Deg(max_altitude_raw)
        else:
            # A 24h solar window should contain at least one upper transit of a
            # fixed star, but keep the contract total if Skyfield returns none
            # exactly on a boundary.
            max_when = window_start + timedelta(hours=12.0)
            max_altitude = Deg(
                float(
                    observer.at(_as_time(ephemeris, max_when))
                    .observe(star)
                    .apparent()
                    .altaz()[0]
                    .degrees
                )
            )
        peak_altitude: Deg | None = None
        peak_when: datetime | None = None
        hours_above: float | None = None
        if darkness_hours > 0.0:
            night_times = times[lo_index:hi_index]
            night_alt = target_alt_deg[lo_index:hi_index]
            best = int(np.argmax(night_alt))
            peak_altitude = Deg(float(night_alt[best]))
            peak_when = night_times[best]
            hours_above = 0.0
            for i in range(len(night_times) - 1):
                a0, a1 = float(night_alt[i]), float(night_alt[i + 1])
                dt_hours = (night_times[i + 1] - night_times[i]).total_seconds() / 3600.0
                if a0 >= limit and a1 >= limit:
                    hours_above += dt_hours
                elif a0 >= limit or a1 >= limit:
                    # Exactly one endpoint clears the limit: interpolate the
                    # crossing linearly rather than counting the whole step.
                    frac_above = (
                        (a0 - limit) / (a0 - a1) if a0 >= limit else (a1 - limit) / (a1 - a0)
                    )
                    hours_above += dt_hours * frac_above
        nightly.append(
            AnnualAltitudeNight(
                window_start=window_start,
                darkness_hours=darkness_hours,
                max_altitude_deg=max_altitude,
                max_when=max_when,
                peak_altitude_deg=peak_altitude,
                peak_when=peak_when,
                hours_above_limit=hours_above,
            )
        )
    return tuple(nightly)


__all__ = [
    "ASTRONOMICAL_TWILIGHT_DEG",
    "CIVIL_TWILIGHT_DEG",
    "DEFAULT_ANNUAL_STEP_MINUTES",
    "DEFAULT_VISIBILITY_STEP_MINUTES",
    "DEFAULT_VISIBILITY_TOLERANCE_SECONDS",
    "KERNEL_FILENAME",
    "KERNEL_SHA256",
    "KERNEL_SIZE_BYTES",
    "KERNEL_URL",
    "MAX_ANNUAL_SAMPLES",
    "MAX_VISIBILITY_SAMPLES",
    "NAUTICAL_TWILIGHT_DEG",
    "SEA_LEVEL",
    "AnnualAltitudeNight",
    "Ephemeris",
    "EphemerisCorruptError",
    "EphemerisMissingError",
    "EventTime",
    "LocalTimeKind",
    "LunarEvent",
    "MoonPhase",
    "NonexistentLocalTimeError",
    "Outcome",
    "Site",
    "SolarEvent",
    "TargetEvent",
    "TargetPosition",
    "VisibilityInterval",
    "annual_altitude_profile",
    "default_kernel_path",
    "horizon_dip_deg",
    "horizon_visibility",
    "load_ephemeris",
    "local_time_kind",
    "local_to_utc",
    "lunar_events",
    "moon_phase",
    "sidereal_hour_angle_deg",
    "solar_events",
    "target",
    "target_events",
    "target_position",
]
