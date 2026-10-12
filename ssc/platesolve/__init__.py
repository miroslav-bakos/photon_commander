# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SSC's plate-solve seam: find a system-installed solver, run it on a FITS,
hand back a J2000 position or a precise reason it could not.

See ``THIRD_PARTY_NOTICES.md`` "Plate solver — gate reading of 2026-09-10" for
why SSC drives a solver the owner installed rather than shipping one, and
:mod:`ssc.platesolve.astap` for the one backend built so far. Consumed by
M10 (polar alignment, `ssc/astro/polar.py`) and, later, M9's stacking
registration — neither lives here; this package only ever produces a
position, never acts on one.

Does I/O (a subprocess, a filesystem probe) and therefore cannot live under
``ssc/astro/``, which CLAUDE.md invariant 3 keeps pure.
"""

from __future__ import annotations

from ssc.platesolve.astap import (
    ASTAP_DEFAULT_SOLVE_TIMEOUT_S,
    ASTAP_DEFAULT_VERSION_PROBE_TIMEOUT_S,
    astap_availability,
    astap_solve,
)
from ssc.platesolve.base import (
    InvalidPlateSolveInputError,
    MalformedSolverOutputError,
    PlateSolveError,
    PlateSolveResult,
    SolveFailedError,
    SolveIssue,
    SolverAvailability,
    SolverUnavailableError,
    SolveTimedOutError,
)

__all__ = [
    "ASTAP_DEFAULT_SOLVE_TIMEOUT_S",
    "ASTAP_DEFAULT_VERSION_PROBE_TIMEOUT_S",
    "InvalidPlateSolveInputError",
    "MalformedSolverOutputError",
    "PlateSolveError",
    "PlateSolveResult",
    "SolveFailedError",
    "SolveIssue",
    "SolveTimedOutError",
    "SolverAvailability",
    "SolverUnavailableError",
    "astap_availability",
    "astap_solve",
]
