# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Orchestration for ``POST /api/plans/{id}/compile``: turning the Settings
location into the ``Site``/``ZoneInfo`` pair :func:`ssc.scheduler.compiler.compile_plan`
needs, exactly the way ``ssc/api/routes/astro.py`` builds a ``Site`` from
``ssc.api.deps.require_location`` — duplicated rather than imported from that
module (its ``_site_of`` is private), matching ``ssc/api/routes/tonight.py``'s
own copy of the same four-line conversion.

Kept out of ``ssc/api/routes/plans.py`` for the same reason ``ssc.api.astro_service``
sits below ``ssc/api/routes/astro.py``: the route parses, delegates, serialises.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ssc.api.astro_service import AstroService, AstroUnavailableError
from ssc.astro.comets import load_comet_catalog
from ssc.astro.events import Site
from ssc.astro.framing import Profile
from ssc.astro.units import Deg, Metres
from ssc.plan import Plan
from ssc.scheduler import CompiledSchedule, compile_plan
from ssc.state.models import LocationState

#: Mirrors ``ssc.scheduler.compiler._NIGHT_ANCHOR_LOCAL_TIME``: "tonight" is
#: the night of the local calendar date whose noon-to-noon window contains
#: ``now`` — so in the small hours after local midnight, "tonight" still
#: means *yesterday's* date, not today's (the bug ``ssc/api/tonight_service.py``
#: was fixed for, in the analogous dusk/dawn window rather than this one).
_NIGHT_ANCHOR_LOCAL_TIME: Final = time(12, 0)


class PlanTimezoneUnknownError(ValueError):
    """The active location has no recorded, resolvable IANA timezone.

    Compiling a ``Plan`` needs a real zone to resolve ``absolute`` and
    ``event`` ``TimeSpec``s against (CLAUDE.md invariant 5) — a missing or
    unresolvable zone is refused rather than assumed to be UTC or the host's
    own zone, either of which could silently compile a plan for the wrong
    night.
    """


def site_of(location: LocationState) -> Site:
    return Site(
        latitude_deg=Deg(location.latitude_deg),
        longitude_deg=Deg(location.longitude_deg),
        elevation_m=Metres(location.elevation_m or 0.0),
    )


def resolve_timezone(location: LocationState) -> ZoneInfo:
    """The active location's recorded zone, or a loud, typed refusal."""
    if location.timezone is None:
        raise PlanTimezoneUnknownError(
            "the active observing location has no recorded timezone: add one in "
            "Settings > Location before compiling a plan. A schedule compiled "
            "against the wrong zone is a wrong observing night."
        )
    try:
        return ZoneInfo(location.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise PlanTimezoneUnknownError(
            f"the active location's recorded timezone {location.timezone!r} cannot be "
            "resolved on this host"
        ) from exc


def current_night_date(now_utc: datetime, tz: ZoneInfo) -> date:
    """The local calendar date of "tonight" — the ``night_date`` a
    ``POST /api/plans/{id}/compile`` with no explicit one defaults to.

    Anchored at local noon, exactly like ``ssc.scheduler.compiler``'s own
    night window: before noon local time, "tonight" is the night that has not
    finished yet (yesterday's date); from noon onward it is this evening's.
    """
    local_now = now_utc.astimezone(tz)
    if local_now.time() < _NIGHT_ANCHOR_LOCAL_TIME:
        return (local_now - timedelta(days=1)).date()
    return local_now.date()


def active_profile(astro: AstroService, profile_id: str | None) -> Profile | None:
    """Resolve the explicitly selected hardware profile, never guess among models."""
    if profile_id is not None:
        return astro.profile(profile_id)
    if len(astro.profiles) == 1:  # compatibility for isolated/unit-test services
        return next(iter(astro.profiles.values()))
    return None


def compile_plan_document(
    *,
    astro: AstroService,
    document: Plan,
    location: LocationState,
    night_date: date | None,
    profile_id: str | None = None,
    now: datetime | None = None,
) -> CompiledSchedule:
    """Resolve site/tz/ephemeris/profile and compile ``document`` for one night.

    Raises :class:`PlanTimezoneUnknownError` (→ 409) or
    :class:`~ssc.api.astro_service.AstroUnavailableError` (→ 503); the caller
    maps both, exactly as ``ssc/api/routes/astro.py`` maps the latter already.
    """
    if astro.ephemeris is None:
        raise AstroUnavailableError(astro.ephemeris_error or "the ephemeris kernel is not loaded")
    tz = resolve_timezone(location)
    moment = now or datetime.now(tz=UTC)
    resolved_night_date = night_date or current_night_date(moment, tz)
    return compile_plan(
        document,
        site=site_of(location),
        tz=tz,
        night_date=resolved_night_date,
        ephemeris=astro.ephemeris,
        now=moment,
        profile=active_profile(astro, profile_id),
        comet_catalog=load_comet_catalog(),
    )


__all__ = [
    "PlanTimezoneUnknownError",
    "active_profile",
    "compile_plan_document",
    "current_night_date",
    "resolve_timezone",
    "site_of",
]
