# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Astro core orchestration for the Planning tab (DESIGN.md 3).

Thin glue, not a second astro core: this module turns a JSON-boundary request
(RA in hours, a UTC instant, a location read from the settings store) into
calls against the pure, device-agnostic ``ssc.astro`` package, and turns the
result back into the astro-typed values ``ssc/api/routes/astro.py`` serialises.
It supplies the two things ``ssc.astro`` deliberately does not fetch for
itself (CLAUDE.md invariant 3): the loaded ephemeris kernel, and *now* — always
a parameter here too, never read from the wall clock in this module either.

The bundled catalogue and every bundled scope profile are loaded once and held
for the process lifetime, exactly as :func:`ssc.astro.events.load_ephemeris`'s
own docstring recommends its caller do ("construct once, share freely"). The
ephemeris kernel is the one input that may legitimately be absent on a fresh
checkout — it is gitignored, fetched once at install time
(``ssc.astro.events.KERNEL_URL``) — so its absence is recorded as a reason
string rather than raised at load time; every endpoint that actually needs it
raises :class:`AstroUnavailableError`, naming the fix, rather than the whole
Planning tab going dark because one file was never fetched (CLAUDE.md
invariant 7).

Target-moon separation and the Moon's own altitude curve (``moon_altitude_series``,
used by the ``/api/tonight`` capture-conditions synthesis) have no home in
``ssc.astro`` (it stops at rise/set/transit, phase, and the rotation envelope),
so they are computed here directly from the primitives
:class:`~ssc.astro.events.Ephemeris` already publishes — the same
``observer(site).at(t).observe(body).apparent()`` shape ``ssc.astro.events``
itself uses, calling skyfield rather than reinventing it. Moon *phase*
(illuminated fraction, phase angle, waxing/waning) does have a home there —
:func:`ssc.astro.events.moon_phase` — because drawing the correct lunar limb
from a single call is a real astro-core primitive, not UI glue.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Final, ParamSpec, TypeVar

from skyfield.errors import EphemerisRangeError

from ssc.astro.auto_night import AutoNightResult, plan_auto_night
from ssc.astro.best_targets import BestTargetsResult, rank_best_targets
from ssc.astro.bodies import BodyPosition, body_positions
from ssc.astro.catalog import CatalogObject, ObjectType, TargetCatalog, load_catalog
from ssc.astro.comets import CometCatalog, CometPosition, comet_position, load_comet_catalog
from ssc.astro.events import (
    DEFAULT_ANNUAL_STEP_MINUTES,
    AnnualAltitudeNight,
    Ephemeris,
    EphemerisCorruptError,
    EphemerisMissingError,
    EventTime,
    MoonPhase,
    Site,
    VisibilityInterval,
    annual_altitude_profile,
    horizon_visibility,
    load_ephemeris,
    lunar_events,
    moon_phase,
    solar_events,
    target,
    target_events,
    target_position,
)
from ssc.astro.framing import (
    DEFAULT_PA_STEP_DEG,
    FieldGeometry,
    MountMode,
    Profile,
    Session,
    SessionEnvelope,
    field_geometry,
    load_profile,
    session_envelope,
)
from ssc.astro.horizon import HorizonProfile, flat_horizon, is_above_horizon
from ssc.astro.mosaic import MosaicPlan, MosaicSpec, mosaic_plan
from ssc.astro.units import Deg, Hours, hours_to_deg

if TYPE_CHECKING:
    from ssc.astro.events import TargetPosition

_log = logging.getLogger(__name__)

#: The geometric (unrefracted) true horizon, 0 deg — the fallback mask used
#: wherever a site has no *recorded* skyline profile, so "no profile set" and
#: "a flat minimum altitude" are the same code path through
#: :func:`ssc.astro.events.horizon_visibility` rather than a parallel one
#: (mirrors ``ssc.astro.events.TRUE_HORIZON_DEG``, a module-private constant
#: there).
_TRUE_HORIZON_DEG: Final = Deg(0.0)


def _profiles_dir() -> Path:
    """``<repo>/data/profiles`` — bundled and committed, like the catalogue."""
    return Path(__file__).resolve().parents[2] / "data" / "profiles"


def _dev_profiles_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "data" / "dev-profiles"


class AstroUnavailableError(RuntimeError):
    """The astro core cannot answer right now — the ephemeris kernel is not loaded."""


class UnknownProfileError(LookupError):
    """No bundled scope profile matches the requested id."""


@dataclass(slots=True)
class FramingVisibility:
    """Whether the framed target actually clears the horizon — the mask if
    the site has recorded one, the true horizon otherwise (DESIGN.md 5)."""

    #: ``True`` only when a *recorded* skyline mask was used; ``False`` means
    #: everything below was computed against the flat true horizon instead.
    horizon_profile_active: bool
    start_above_horizon: bool
    start_horizon_altitude_deg: Deg
    #: Every clear stretch within the session window, chronological
    #: (``ssc.astro.events.horizon_visibility`` — a list, never flattened to a
    #: single first-up/last-down pair).
    clear_intervals: tuple[VisibilityInterval, ...]


@dataclass(slots=True)
class NightResult:
    """The un-serialised answer to a ``night`` request; ``ssc/api/routes/astro.py``
    turns this into ``NightResponse``.

    ``target_events``, ``altitude_curve``, ``moon_separation_deg``,
    ``moon_separation_curve`` and
    ``visibility_intervals`` are ``None`` together, if and only if no target
    was given — never a guessed curve for a target the caller never named.
    """

    solar_events: tuple[EventTime, ...]
    lunar_events: tuple[EventTime, ...]
    moon_illuminated_fraction: float
    #: 0-360 deg, 0 New / 90 First Quarter / 180 Full / 270 Last Quarter — the
    #: quantity ``moon_illuminated_fraction`` alone cannot supply: 50%
    #: illuminated is either quarter, and they are mirror images. See
    #: ``ssc.astro.events.MoonPhase``.
    moon_phase_angle_deg: Deg
    #: Derived from ``moon_phase_angle_deg`` (0-180 deg growing towards Full,
    #: 180-360 deg shrinking towards New) — what a client needs to draw the
    #: correct limb without a second call.
    moon_waxing: bool
    target_events: tuple[EventTime, ...] | None
    altitude_curve: tuple[tuple[datetime, Deg], ...] | None
    moon_separation_deg: Deg | None
    moon_separation_curve: tuple[tuple[datetime, Deg], ...] | None
    #: ``True`` only when a *recorded* skyline mask was used to compute
    #: ``visibility_intervals``; ``False`` means the flat ``min_altitude_deg``
    #: limit was used instead — always ``False`` when no target was given.
    horizon_profile_active: bool
    #: Every stretch the target is clear of the horizon (mask or flat limit),
    #: chronological. ``None`` iff no target was given.
    visibility_intervals: tuple[VisibilityInterval, ...] | None


def _load_sync(
    *, include_dev_profiles: bool = False
) -> tuple[TargetCatalog, dict[str, Profile], Ephemeris | None, str | None]:
    """The blocking half of startup: every file read this module needs.

    Called once, off the event loop (CLAUDE.md async discipline), by
    :func:`build_astro_service`.
    """
    catalog = load_catalog()

    profiles_dir = _profiles_dir()
    paths = sorted(profiles_dir.glob("*.toml"))
    if not paths:
        raise RuntimeError(f"no bundled scope profiles found in {profiles_dir}")
    profiles = {profile.id: profile for profile in (load_profile(path) for path in paths)}
    if include_dev_profiles:
        dev_dir = _dev_profiles_dir()
        if not dev_dir.is_dir():
            raise RuntimeError(
                f"SSC_ENABLE_DEV_PROFILES is on, but dev profiles are unavailable in {dev_dir}"
            )
        for path in sorted(dev_dir.glob("*.toml")):
            profile = load_profile(path)
            if profile.id in profiles:
                raise RuntimeError(f"dev profile {profile.id!r} collides with a production profile")
            profiles[profile.id] = profile

    ephemeris: Ephemeris | None = None
    ephemeris_error: str | None = None
    try:
        ephemeris = load_ephemeris()
    except (EphemerisMissingError, EphemerisCorruptError) as exc:
        ephemeris_error = str(exc)
    return catalog, profiles, ephemeris, ephemeris_error


async def build_astro_service(*, include_dev_profiles: bool = False) -> AstroService:
    """Load the catalogue, every bundled profile, and the ephemeris kernel.

    A missing or corrupt kernel is not fatal here — see the module docstring —
    but the caller (``ssc.main.build_services``) is expected to surface it as a
    startup :class:`~ssc.state.models.Notice`, the same way an unavailable
    keyring backend is surfaced today.
    """
    catalog, profiles, ephemeris, ephemeris_error = await asyncio.to_thread(
        _load_sync, include_dev_profiles=include_dev_profiles
    )
    if ephemeris_error is not None:
        _log.warning("astro: %s", ephemeris_error)
    return AstroService(
        catalog=catalog, profiles=profiles, ephemeris=ephemeris, ephemeris_error=ephemeris_error
    )


_ComputeArgs = ParamSpec("_ComputeArgs")
_ComputeResult = TypeVar("_ComputeResult")


class AstroService:
    """Everything the Planning tab's endpoints need, loaded once and shared.

    Numerical methods are synchronous; async routes use ``compute`` to keep
    them off the device/API event loop, with at most two workers per service.
    """

    def __init__(
        self,
        *,
        catalog: TargetCatalog,
        profiles: dict[str, Profile],
        ephemeris: Ephemeris | None,
        ephemeris_error: str | None,
    ) -> None:
        self.catalog = catalog
        self.profiles = profiles
        self.ephemeris = ephemeris
        self.ephemeris_error = ephemeris_error
        # Two numerical workers leave capacity for capture and telemetry on Pi5.
        # Threads start lazily, and are shared by Planning and forecast callers.
        self._compute_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="astro")

    async def compute(
        self,
        operation: Callable[_ComputeArgs, _ComputeResult],
        *args: _ComputeArgs.args,
        **kwargs: _ComputeArgs.kwargs,
    ) -> _ComputeResult:
        """Run numerical work without holding the API/device event loop."""
        return await asyncio.get_running_loop().run_in_executor(
            self._compute_executor, partial(operation, *args, **kwargs)
        )

    def close(self) -> None:
        """Release the kernel's file handle. Safe to call even if never loaded."""
        self._compute_executor.shutdown(wait=True, cancel_futures=True)
        if self.ephemeris is not None:
            self.ephemeris.close()

    # -- profiles ------------------------------------------------------------ #

    def profile(self, profile_id: str) -> Profile:
        found = self.profiles.get(profile_id)
        if found is None:
            known = ", ".join(sorted(self.profiles))
            raise UnknownProfileError(
                f"no bundled scope profile {profile_id!r}; known profiles: {known}"
            )
        return found

    def match_profile(self, model: str | None) -> Profile | None:
        """Match a device-reported model exactly after punctuation normalisation.

        Exact aliases keep ``S30`` from ever being silently mistaken for
        ``S30 Pro``. Unknown firmware strings deliberately return ``None`` so
        Settings can ask the operator to confirm a profile.
        """
        if model is None:
            return None
        key = re.sub(r"[^a-z0-9]", "", model.casefold())
        matches = [
            profile
            for profile in self.profiles.values()
            if key
            in {
                re.sub(r"[^a-z0-9]", "", candidate.casefold())
                for candidate in (profile.id, profile.name, *profile.aliases)
            }
        ]
        return matches[0] if len(matches) == 1 else None

    def geometry(self, profile_id: str, optic_id: str) -> FieldGeometry:
        return field_geometry(self.profile(profile_id), optic_id)

    # -- target resolution ----------------------------------------------------- #

    def resolve(self, query: str) -> CatalogObject:
        """Raises :class:`~ssc.astro.catalog.UnknownTargetError` for no match."""
        return self.catalog.resolve(query)

    def suggest(self, query: str, limit: int = 8) -> tuple[CatalogObject, ...]:
        """Rank offline catalogue matches for the target typeahead."""
        return self.catalog.suggest(query, limit)

    def comet_suggestions(
        self, site: Site, query: str, when: datetime, limit: int = 8
    ) -> tuple[CometCatalog, tuple[CometPosition, ...], tuple[str, ...]]:
        """Search the offline JPL snapshot and resolve results for ``when``.

        A single pathological or too-old orbit must not turn every otherwise
        usable match into a 500. Its name is retained in ``unavailable`` so
        callers can be explicit that a match existed but could not propagate.
        """
        catalog = load_comet_catalog()
        ephemeris = self._require_ephemeris()
        positions: list[CometPosition] = []
        unavailable: list[str] = []
        for comet in catalog.suggest(query, limit):
            try:
                positions.append(comet_position(ephemeris, site, comet, when))
            except EphemerisRangeError:
                raise
            except (ArithmeticError, ValueError):
                unavailable.append(comet.full_name)
        return catalog, tuple(positions), tuple(unavailable)

    # -- ephemeris-backed computation ------------------------------------------- #

    def _require_ephemeris(self) -> Ephemeris:
        if self.ephemeris is None:
            raise AstroUnavailableError(
                self.ephemeris_error or "the ephemeris kernel is not loaded"
            )
        return self.ephemeris

    def position(
        self, site: Site, ra_j2000_h: Hours, dec_j2000_deg: Deg, when: datetime
    ) -> TargetPosition:
        return target_position(self._require_ephemeris(), site, ra_j2000_h, dec_j2000_deg, when)

    def moon_separation_deg(
        self, site: Site, ra_j2000_h: Hours, dec_j2000_deg: Deg, when: datetime
    ) -> Deg:
        """Target-to-Moon separation at one instant — the executor's gate
        samples this per item (``ssc/api/device_commands.py``)."""
        return _moon_separation_deg(
            self._require_ephemeris(), site, ra_j2000_h, dec_j2000_deg, when
        )

    def _session(
        self,
        site: Site,
        ra_j2000_h: Hours,
        dec_j2000_deg: Deg,
        pa_offset_deg: Deg,
        start_time: datetime,
        session_length_hours: float,
    ) -> tuple[Session, TargetPosition]:
        """The bridge :func:`ssc.astro.events.target_position` exists for: turn a
        real target and a real clock into the four numbers
        :class:`~ssc.astro.framing.Session` needs, and nothing else from the
        ephemeris flows any further downstream."""
        start = self.position(site, ra_j2000_h, dec_j2000_deg, start_time)
        session = Session(
            latitude_deg=site.latitude_deg,
            declination_deg=start.declination_deg,
            hour_angle_start_deg=start.hour_angle_deg,
            duration_hours=session_length_hours,
            pa_offset_deg=pa_offset_deg,
        )
        return session, start

    def framing(
        self,
        *,
        site: Site,
        profile_id: str,
        optic_id: str,
        mount: MountMode,
        ra_j2000_h: Hours,
        dec_j2000_deg: Deg,
        pa_offset_deg: Deg,
        start_time: datetime,
        session_length_hours: float,
        horizon_profile: HorizonProfile | None = None,
        max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
        boundary_samples: int = 360,
    ) -> tuple[SessionEnvelope, TargetPosition, FramingVisibility]:
        """The four alt-az shapes (DESIGN 3.2) plus the dense adaptive PA(t) sample
        set a client fetches once per session and interpolates for the time
        scrubber (``SessionEnvelope.samples``, from
        :func:`ssc.astro.framing.sample_session`'s own adaptive refinement).

        ``horizon_profile`` is the site's *recorded* skyline mask, or ``None``
        when it has none — the flat true horizon is used in its place, so "no
        profile" and "a profile" are the same code path through
        :func:`ssc.astro.events.horizon_visibility`, not a parallel one. A
        target the geometry says is framed but the skyline hides is the
        silent-wrong-answer failure DESIGN.md 5 warns about, so the visibility
        answer travels alongside the geometry rather than needing a second
        request."""
        geometry = self.geometry(profile_id, optic_id)
        session, start = self._session(
            site, ra_j2000_h, dec_j2000_deg, pa_offset_deg, start_time, session_length_hours
        )
        envelope = session_envelope(
            geometry,
            mount,
            session,
            max_pa_step_deg=max_pa_step_deg,
            boundary_samples=boundary_samples,
        )
        visibility = self._framing_visibility(
            site,
            ra_j2000_h,
            dec_j2000_deg,
            start,
            start_time,
            session_length_hours,
            horizon_profile,
        )
        return envelope, start, visibility

    def _framing_visibility(
        self,
        site: Site,
        ra_j2000_h: Hours,
        dec_j2000_deg: Deg,
        start: TargetPosition,
        start_time: datetime,
        session_length_hours: float,
        horizon_profile: HorizonProfile | None,
    ) -> FramingVisibility:
        profile = (
            horizon_profile if horizon_profile is not None else flat_horizon(_TRUE_HORIZON_DEG)
        )
        end_time = start_time + timedelta(hours=session_length_hours)
        intervals: tuple[VisibilityInterval, ...] = ()
        if end_time > start_time:
            intervals = horizon_visibility(
                self._require_ephemeris(),
                site,
                ra_j2000_h,
                dec_j2000_deg,
                start_time,
                end_time,
                profile,
            )
        return FramingVisibility(
            horizon_profile_active=horizon_profile is not None,
            start_above_horizon=is_above_horizon(
                profile, altitude_deg=start.altitude_deg, azimuth_deg=start.azimuth_deg
            ),
            start_horizon_altitude_deg=profile.altitude_at(start.azimuth_deg),
            clear_intervals=intervals,
        )

    def mosaic(
        self,
        *,
        site: Site,
        profile_id: str,
        optic_id: str,
        mount: MountMode,
        ra_j2000_h: Hours,
        dec_j2000_deg: Deg,
        pa_offset_deg: Deg,
        start_time: datetime,
        session_length_hours: float,
        spec: MosaicSpec,
        max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
        boundary_samples: int = 360,
    ) -> tuple[MosaicPlan, TargetPosition]:
        geometry = self.geometry(profile_id, optic_id)
        center_session, start = self._session(
            site, ra_j2000_h, dec_j2000_deg, pa_offset_deg, start_time, session_length_hours
        )
        plan = mosaic_plan(
            geometry,
            mount,
            spec,
            hours_to_deg(ra_j2000_h),
            dec_j2000_deg,
            center_session,
            max_pa_step_deg=max_pa_step_deg,
            boundary_samples=boundary_samples,
        )
        return plan, start

    def night(
        self,
        *,
        site: Site,
        start: datetime,
        end: datetime,
        target_ra_dec: tuple[Hours, Deg] | None,
        min_altitude_deg: Deg,
        altitude_samples: int,
        horizon_profile: HorizonProfile | None = None,
    ) -> NightResult:
        """Twilight bands, sun/moon rise-set-transit, moon phase, and — if a
        target is given — its rise/transit/set events, altitude curve and
        separation from the Moon. The existing summary separation is sampled
        at the midpoint; a sparse ephemeris-backed series allows the timeline
        hover to interpolate the separation at its actual time.

        ``horizon_profile`` is the site's recorded skyline mask, or ``None``
        when it has none — ``visibility_intervals`` then falls back to
        ``flat_horizon(min_altitude_deg)``, so "no profile" and "a profile" are
        the same call to :func:`ssc.astro.events.horizon_visibility` rather
        than a parallel code path. ``target_events`` is left as the flat
        rise/transit/set summary the Gantt's axis markers use; the actual
        clear-of-obstruction windows a backyard observer needs are
        ``visibility_intervals`` — a list, never flattened to a single
        first-up/last-down pair (DESIGN.md 5)."""
        ephemeris = self._require_ephemeris()
        solar = solar_events(ephemeris, site, start, end)
        lunar = lunar_events(ephemeris, site, start, end)
        midpoint = start + (end - start) / 2

        target_evts: tuple[EventTime, ...] | None = None
        curve: tuple[tuple[datetime, Deg], ...] | None = None
        separation: Deg | None = None
        separation_curve: tuple[tuple[datetime, Deg], ...] | None = None
        intervals: tuple[VisibilityInterval, ...] | None = None
        horizon_active = False
        if target_ra_dec is not None:
            ra_h, dec_deg = target_ra_dec
            target_evts = target_events(
                ephemeris, site, ra_h, dec_deg, start, end, min_altitude_deg
            )
            curve = self._altitude_curve(
                ephemeris, site, ra_h, dec_deg, start, end, altitude_samples
            )
            separation = _moon_separation_deg(ephemeris, site, ra_h, dec_deg, midpoint)
            sample_count = min(25, max(2, altitude_samples))
            separation_curve = tuple(
                (
                    when,
                    _moon_separation_deg(ephemeris, site, ra_h, dec_deg, when),
                )
                for when in (
                    start + (end - start) * i / (sample_count - 1) for i in range(sample_count)
                )
            )
            profile = (
                horizon_profile if horizon_profile is not None else flat_horizon(min_altitude_deg)
            )
            horizon_active = horizon_profile is not None
            intervals = horizon_visibility(ephemeris, site, ra_h, dec_deg, start, end, profile)

        phase = moon_phase(ephemeris, site, midpoint)
        return NightResult(
            solar_events=solar,
            lunar_events=lunar,
            moon_illuminated_fraction=phase.illuminated_fraction,
            moon_phase_angle_deg=phase.phase_angle_deg,
            moon_waxing=phase.waxing,
            target_events=target_evts,
            altitude_curve=curve,
            moon_separation_deg=separation,
            moon_separation_curve=separation_curve,
            horizon_profile_active=horizon_active,
            visibility_intervals=intervals,
        )

    def moon_phase(self, site: Site, when: datetime) -> MoonPhase:
        """The Moon's phase at one instant — see ``ssc.astro.events.moon_phase``."""
        return moon_phase(self._require_ephemeris(), site, when)

    def bodies(self, site: Site, when: datetime) -> tuple[BodyPosition, ...]:
        """The Sun, Moon and planets at one instant — see ``ssc.astro.bodies``."""
        return body_positions(self._require_ephemeris(), site, when)

    def moon_altitude_series(
        self, site: Site, start: datetime, end: datetime, samples: int
    ) -> tuple[tuple[datetime, Deg], ...]:
        """The Moon's own altitude, sampled uniformly across ``[start, end]`` —
        the per-target ``_altitude_curve`` cannot be reused because it treats
        its subject as a fixed-RA/Dec star, and the Moon moves. Used by the
        ``/api/tonight`` capture-conditions synthesis to weigh moonlight by
        how much of the dark window it is actually up for, not by phase alone.
        """
        if samples < 2:
            raise ValueError(f"samples must be at least 2, got {samples}")
        ephemeris = self._require_ephemeris()
        observer = ephemeris.observer(site)
        step = (end - start) / (samples - 1)
        curve = []
        for i in range(samples):
            when = start + step * i
            instant = ephemeris.timescale.from_datetime(when)
            altitude, _, _ = observer.at(instant).observe(ephemeris.moon).apparent().altaz()
            curve.append((when, Deg(float(altitude.degrees))))
        return tuple(curve)

    def solar_events(self, site: Site, start: datetime, end: datetime) -> tuple[EventTime, ...]:
        return solar_events(self._require_ephemeris(), site, start, end)

    def annual_altitude_profile(
        self,
        *,
        site: Site,
        ra_j2000_h: Hours,
        dec_j2000_deg: Deg,
        start: datetime,
        nights: int,
        min_altitude_deg: Deg,
        step_minutes: float = DEFAULT_ANNUAL_STEP_MINUTES,
    ) -> tuple[AnnualAltitudeNight, ...]:
        """When in the year is this target actually worth imaging — one
        summary sample per night. See
        ``ssc.astro.events.annual_altitude_profile``."""
        return annual_altitude_profile(
            self._require_ephemeris(),
            site,
            ra_j2000_h,
            dec_j2000_deg,
            start,
            nights,
            min_altitude_deg,
            step_minutes=step_minutes,
        )

    def best_targets(
        self,
        *,
        site: Site,
        profile_id: str,
        optic_id: str,
        start: datetime,
        end: datetime,
        min_altitude_deg: Deg,
        top_n: int,
        max_candidate_magnitude: float,
        allowed_object_types: frozenset[ObjectType] | None = None,
    ) -> BestTargetsResult:
        """ "Best targets tonight" (DESIGN.md 3.5): the top ``top_n`` bundled
        catalogue targets for the night in ``[start, end]``, ranked by how
        good *this* night is for each of them — see
        ``ssc.astro.best_targets`` for the scoring model. ``profile_id``/
        ``optic_id`` pick the field of view the size-versus-frame judgement
        is made against.

        ``geometry`` is resolved *before* the ephemeris is required, exactly
        as :meth:`framing` orders it — an unknown profile is a caller error
        the ephemeris kernel's presence has nothing to do with, so a request
        with a bad ``profile_id`` gets 422 even when the kernel is missing."""
        geometry = self.geometry(profile_id, optic_id)
        return rank_best_targets(
            self._require_ephemeris(),
            site,
            self.catalog,
            start,
            end,
            min_altitude_deg,
            top_n=top_n,
            field_geometry=geometry,
            max_candidate_magnitude=max_candidate_magnitude,
            allowed_object_types=allowed_object_types,
        )

    def auto_night(
        self,
        *,
        site: Site,
        profile_id: str,
        optic_id: str,
        start: datetime,
        end: datetime,
        min_altitude_deg: float,
        max_candidate_magnitude: float,
        allowed_object_types: frozenset[ObjectType] | None,
        available_filters: frozenset[str],
        block_minutes: int,
        max_targets: int,
        horizon_profile: HorizonProfile | None,
    ) -> AutoNightResult:
        geometry = self.geometry(profile_id, optic_id)
        return plan_auto_night(
            self._require_ephemeris(),
            site,
            self.catalog,
            start,
            end,
            geometry=geometry,
            min_altitude_deg=min_altitude_deg,
            max_candidate_magnitude=max_candidate_magnitude,
            allowed_object_types=allowed_object_types,
            available_filters=available_filters
            if optic_id == "tele" and self.profile(profile_id).capabilities.filter_wheel
            else available_filters - {"LP"},
            block_minutes=block_minutes,
            max_targets=max_targets,
            horizon_profile=horizon_profile,
        )

    def lunar_events(self, site: Site, start: datetime, end: datetime) -> tuple[EventTime, ...]:
        return lunar_events(self._require_ephemeris(), site, start, end)

    @staticmethod
    def _altitude_curve(
        ephemeris: Ephemeris,
        site: Site,
        ra_h: Hours,
        dec_deg: Deg,
        start: datetime,
        end: datetime,
        samples: int,
    ) -> tuple[tuple[datetime, Deg], ...]:
        if samples < 2:
            raise ValueError(f"altitude_samples must be at least 2, got {samples}")
        step = (end - start) / (samples - 1)
        curve = []
        for i in range(samples):
            position = target_position(ephemeris, site, ra_h, dec_deg, start + step * i)
            curve.append((position.when, position.altitude_deg))
        return tuple(curve)


def _moon_separation_deg(
    ephemeris: Ephemeris, site: Site, ra_h: Hours, dec_deg: Deg, when: datetime
) -> Deg:
    """Angular separation between the Moon and a fixed target, at one instant."""
    instant = ephemeris.timescale.from_datetime(when)
    observer = ephemeris.observer(site)
    apparent_moon = observer.at(instant).observe(ephemeris.moon).apparent()
    apparent_target = observer.at(instant).observe(target(ra_h, dec_deg)).apparent()
    return Deg(float(apparent_moon.separation_from(apparent_target).degrees))


__all__ = [
    "AstroService",
    "AstroUnavailableError",
    "FramingVisibility",
    "NightResult",
    "UnknownProfileError",
    "build_astro_service",
]
