# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The shape every plate-solve backend targets — result, availability, errors.

Mirrors ``ssc.weather.base``'s split on purpose: a solver is a third-party
tool proxied server-side, the same relationship SSC already has with a
weather provider. Today there is exactly one backend
(:mod:`ssc.platesolve.astap`, per the 2026-09-10 gate reading in
``THIRD_PARTY_NOTICES.md``); this module is the part of the shape that is not
ASTAP-specific, so a second backend (astrometry.net, ranked primary by that
same gate but not built here) would slot in without touching callers.

**A solve result's coordinate always carries its frame explicitly**
(:class:`ssc.astro.frames.EquatorialCoord`) and is always J2000 — a plate
solve matches stars in the catalogue's own frame, never the mount's apparent
place of date. See ``ssc/astro/frames.py`` for why mixing J2000 and
topocentric is a ~20 arcmin bug, not a rounding error.

**A solve result's timestamp is the exposure's, not the solve's.** ``DATE-OBS``
travels from the FITS file that was solved; nothing here ever substitutes
``datetime.now()``. A solve describes where the scope *was*.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ssc.astro.frames import EquatorialCoord

__all__ = [
    "InvalidPlateSolveInputError",
    "MalformedSolverOutputError",
    "PlateSolveError",
    "PlateSolveResult",
    "SolveFailedError",
    "SolveIssue",
    "SolveTimedOutError",
    "SolverAvailability",
    "SolverUnavailableError",
]


class SolveIssue(StrEnum):
    """Why a solve is unavailable or did not produce a result — never
    collapsed into one generic "plate solving unavailable" (CLAUDE.md
    invariant 7: unknown/missing capability must be surfaced, not papered
    over). Each member answers a different "what do I do about it" for the
    user; see :mod:`ssc.platesolve.astap` for the exact text each carries.
    """

    #: No solver binary found (not on PATH, not in a usual install location,
    #: not at a configured override). Fix: install it.
    SOLVER_NOT_FOUND = "solver_not_found"
    #: A solver was found and identified, but has no usable star database.
    #: Fix: download one.
    DATABASE_UNAVAILABLE = "database_unavailable"
    #: Something answering to the configured path exists but does not
    #: identify itself as the ASTAP CLI solver we know how to drive (wrong
    #: binary, or a GUI build that does not respond to `-h`). Fix: point at
    #: the right binary.
    UNSUPPORTED_VERSION = "unsupported_version"
    #: The solver ran to completion and reported it could not match the
    #: field (too few stars, wrong FOV guess, cloud-fogged frame, ...).
    NOT_SOLVED = "not_solved"
    #: The subprocess did not finish within the allotted time and was killed.
    TIMED_OUT = "timed_out"
    #: The input FITS is missing something a solve result must carry (e.g.
    #: no DATE-OBS) or the solver could not read it.
    INVALID_INPUT = "invalid_input"
    #: The solver claimed success but its output does not have what a result
    #: needs, or is not parseable — refused rather than guessed at.
    MALFORMED_OUTPUT = "malformed_output"
    #: An exit condition this seam does not have a specific reading for.
    INVOCATION_FAILED = "invocation_failed"


class PlateSolveError(Exception):
    """Base for every error this package raises. Never swallowed silently;
    always carries a :class:`SolveIssue` so a caller can branch on *why*
    without parsing prose."""

    def __init__(self, issue: SolveIssue, message: str) -> None:
        super().__init__(message)
        self.issue = issue


class SolverUnavailableError(PlateSolveError):
    """The solver could not be used at all for this attempt — missing
    binary, missing database, or an exit code astap_cli itself reserves for
    "no database"/"database unreadable" (32/33), hit reactively even though
    :func:`~ssc.platesolve.astap.astap_availability` already checks
    proactively (a database can be deleted between the check and the solve)."""


class SolveFailedError(PlateSolveError):
    """The solver ran, found no match, and said so honestly (astap_cli exit
    1 or 2). Not a crash — a real "no" that a caller may retry with a wider
    search or a different frame."""


class SolveTimedOutError(PlateSolveError):
    """The subprocess did not finish within the caller's timeout and was
    killed. A stuck solver must never hang the caller (CLAUDE.md async
    discipline)."""


class InvalidPlateSolveInputError(PlateSolveError):
    """The FITS handed in cannot be solved as given — missing ``DATE-OBS``,
    unreadable by the solver, or otherwise not what this seam requires
    before it will even try."""


class MalformedSolverOutputError(PlateSolveError):
    """The solver reported success but its output cannot be trusted as one —
    missing keys, non-numeric values, or an out-of-range coordinate. Refused
    rather than parsed optimistically (CLAUDE.md: a subprocess is
    untrusted-adjacent I/O)."""


@dataclass(frozen=True, slots=True)
class SolverAvailability:
    """Whether a plate solver can be used right now, and precisely why not.

    Distinct from a solve failure the same way
    ``ssc.weather.base.ProviderAvailability`` is: this is "should this even
    be tried", checked without touching any particular frame. ``reason`` is
    required (and actionable) whenever ``available`` is ``False`` — "plate
    solving unavailable" with no reason is the failure mode this type exists
    to prevent.
    """

    available: bool
    issue: SolveIssue | None = None
    reason: str | None = None
    #: The solver's own reported version, when one could be identified —
    #: carried even on some failure paths (e.g. database missing) because
    #: the solver itself was still identified successfully.
    version: str | None = None


@dataclass(frozen=True, slots=True)
class PlateSolveResult:
    """One successful solve: a measurement of where the scope was pointing
    when one specific frame was exposed.

    ``coord.frame`` is always :data:`ssc.astro.frames.MountFrame.J2000` —
    constructed via :func:`ssc.astro.frames.j2000_coord`, never assembled by
    hand, so a caller cannot accidentally tag a solved position with the
    mount's frame.
    """

    coord: EquatorialCoord
    #: The frame's own ``DATE-OBS``, timezone-aware UTC — when the scope was
    #: *there*, not when the solve subprocess finished.
    date_obs: datetime
    #: Plate scale along the Y axis, arcsec/pixel (``|CDELT2| * 3600``).
    pixel_scale_arcsec: float
    #: Image rotation, degrees (``CROTA2``).
    rotation_deg: float
    #: Short, stable identifier of the backend that produced this — "astap"
    #: today. Mirrors ``ForecastPoint.source``'s provenance role.
    solver: str
    #: The solver's own reported version, when the caller supplied one from
    #: a prior :class:`SolverAvailability` check — not re-probed per solve
    #: (architecture policy invariant 7: a value is read once, or it is absent).
    solver_version: str | None


@dataclass(frozen=True, slots=True)
class ReferencePlateSolveResult:
    """A reference picture's J2000 center, without claimed capture provenance."""

    coord: EquatorialCoord
    pixel_scale_arcsec: float
    rotation_deg: float
    solver: str
    solver_version: str | None
