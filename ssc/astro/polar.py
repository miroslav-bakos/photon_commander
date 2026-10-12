# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Multi-point polar alignment: where the mount's RA axis *actually* points.

Rotate the mount about its RA axis only and plate-solve the field centre at
three or more positions. Whatever the mount believes, those solved directions
lie on a small circle whose axis **is** the mount's rotation axis. The angle
between that axis and the celestial pole is the polar alignment error, and this
module decomposes it into the two quantities a user can physically adjust:
altitude and azimuth.

**The mount's reported RA/Dec is never fitted.** It may be handed in
(:attr:`PolarPoint.reported`) and it is reported back as a per-point
disagreement, because that disagreement is exactly the pointing-model error
``docs/DESIGN.md`` 3.5.1 wants to show. It contributes nothing to the axis. That
is what makes this measurement survive a mount whose pointing model is wrong —
which, before alignment, it always is.

**The frame is the whole game, and it is not J2000.**

A solve arrives as astrometric J2000. The naive move is to fit the circle there
and compare against the J2000 pole; it is wrong twice over.

* The J2000 pole is ~20 arcmin from the pole the sky actually turns about in
  2026 (``ssc.astro.frames``) — the same order as the error being measured, and
  pointing the same way for every point, so it does not look like noise. Getting
  the conversion *backwards* does not cancel it, it doubles it: 44.477' where
  22.238' was right.
* Worse, and less obvious: **in any equatorial frame the mount's axis is not
  fixed.** It is bolted to the ground, so in the sky's frame it sweeps a cone
  about the true pole once per sidereal day. Points taken minutes apart are then
  points on *different* circles, and the fit is quietly meaningless.

So every solved position is carried all the way into the **local horizontal
frame** — J2000 -> apparent place of date (:func:`ssc.astro.frames.to_mount_frame`,
the full chain: bias, precession, nutation, annual *and* diurnal aberration) ->
hour angle against Greenwich *apparent* sidereal time -> altitude and azimuth
(:mod:`ssc.astro.framing`). That frame is Earth-fixed: the mount axis is a
constant there, the celestial pole is a constant there (altitude ``|phi|``,
azimuth 0 north / 180 south), and the comparison is a subtraction rather than an
argument about epochs.

Cartesian convention throughout: ``x`` north, ``y`` east, ``z`` up.

**Refraction is modelled here, and this is the one place in ``ssc.astro`` that
does it.** ``ssc.astro.frames`` deliberately applies none, because the device
reports ``DoesRefraction: false`` and a coordinate sent to the mount must be in
the mount's own unrefracted terms. This measurement is the opposite problem: it
infers a *mechanical* direction from starlight that really did bend. The solver
reports the field centre's catalogue position — unrefracted — while the tube was
physically aimed at the refracted one, higher by ``R(h)``. Since ``R`` runs from
~35 arcsec at 60 deg to ~100 arcsec at 30 deg, points at different altitudes are
displaced differentially and the fitted axis moves with them.

Measured on synthetic runs at latitude 48.15 with a 60-degree arc, ignoring it
costs **1.0 arcmin** across the meridian at declination +5 (altitudes 40-47),
**0.6 arcmin** for the same arc at declination +30 (altitudes 61-72),
**2.3 arcmin** for one starting 30 degrees east, and **5.7 arcmin** for one
running down to 14 degrees altitude
(``tests/astro/test_polar.py::test_unmodelled_refraction_biases_the_axis_by_arcminutes``).
That is larger than everything else in this module put together, and — the part
that makes it dangerous — the residuals barely notice: those same runs sit at
0.03 to 6.2 arcsec RMS, so a fit biased by arcminutes still reads as clean. On a
three-point run there is no residual at all to read.

Saemundsson's formula (Meeus 16.4) is applied by default and can be turned off
with ``apply_refraction=False``; whether it ran is recorded in
:attr:`PolarAlignmentModel.refraction_applied`.
Standard conditions are assumed unless ``pressure_mbar``/``temperature_c`` say
otherwise, which leaves ~10 per cent of the correction — under 0.2 arcmin in a
sane geometry — on the table.

**Three points determine a circle exactly, so a three-point run has no
residual — ever.** A plane through three points fits them all, so its RMS would
be 0.00 whatever went wrong. That is not a clean run, it is an absent check, and
reporting it as zero is the single most misleading thing this module could do to
someone in a field at midnight. So it is not reported:
:attr:`PolarAlignmentModel.rms_residual_arcsec` is **None** when
:attr:`PolarAlignmentModel.residual_dof` is 0 — unknown, never zero (CLAUDE.md
"failure handling") — and the ``SCATTERED_POINTS`` refusal cannot fire there
either. Conditioning (:attr:`PolarAlignmentModel.noise_amplification`) is the
only quality signal a three-point run has. **Capture four or five.** The cost is
one more 5-second iteration and the gain is being able to tell a good
measurement from a broken one at all. Measured on the real hardware sweeps in
``tests/astro/polar_real_solves.json``: the three-point sweep reports ``None``,
while a five-point sweep of the *same physical alignment* reports 1.7 arcsec RMS
and means it.

**Validated against the hardware, not only against itself.** Twenty-two solved
positions from the owner's own S30 Pro on a wedge (firmware 9.31, solved by
ASTAP CLI-2026.09.01), 2026-09-10, in five sweeps
with the tripod physically moved between them
(``tests/astro/polar_real_solves.json``). The check that carries the most weight:
sweeps 2 and 4 are one alignment measured twice, five minutes apart, from
different declinations and hour angles with three points and five. Nothing links
them but the tripod, and their fitted axes land **5.1 arcmin apart — 1.3 sigma
of what the fits themselves claim** (2.8' and 2.7'). Feeding in the mount's own
telemetry, wrong by up to 18.3 degrees on that data, moves the answer by not one
floating-point bit.

**What the numbers must never become.** The correction is an angle. Turning it
into "move the tripod 7 cm" needs a lever arm, and the lever arm is not
reproducible: measured on the owner's own tripod on 2026-09-10, two consecutive
azimuth moves came out at 1.15 and 0.72 cm per degree. A caller may say "turn
east, about a quarter of what you just did" — it must never say a distance.
Re-measure instead. The authoritative workflow in ``docs/IMPLEMENTATION.md`` M10
uses independent five-point sweeps after every physical adjustment.

**Legacy diagnostic refinement: one re-solve, no slewing**
(:func:`refine_polar_alignment`).

This calculation remains available for diagnostics and backwards API
compatibility, but a single field cannot independently re-establish the RA
axis. Its result is always untrusted: a physically moved tripod can make it
report zero while a new sweep measures nine arcminutes, even with less than a
degree of boresight motion. It must never guide or confirm polar alignment.

A sweep is the only way to *find* the axis, but it is a batch — three to five
slews and solves, a minute or more — and its answer is stale before the user's
hands leave the tripod. That is not a precision problem: measured on 2026-09-10,
independent sweeps of different sky repeated to **0.4-0.9 arcmin**, while every
hand correction landed at roughly **3x** the size asked for, and no distance
instruction can close the gap because the lever arm itself is not reproducible
(1.15 cm/deg on one move, 0.72 on the next). What closes the loop is latency.

So: the mount is rigid on the tripod. A tripod adjustment applies **one rigid
rotation to everything bolted to it**, the RA axis included. Leave the mount
mechanically where it is, re-solve the same pointing, and the shift of that one
direction gives the rotation; rotating the swept axis by it gives the new axis,
with no further slewing and no second sweep.

*Why the horizontal frame is what makes this work.* With tracking stopped the
mount is a fixed object in an Earth-fixed frame, so absent a tripod move the
re-solve must land on exactly the same altitude and azimuth as the reference,
whatever the clock says. Every arcsecond of difference is tripod motion. The
same two solves in any equatorial frame differ by 15 arcsec of sidereal
rotation per second of elapsed time, and the signal is buried in it.

*One solved position is two constraints; a rotation has three degrees of
freedom.* The third is taken from physics rather than from a third measurement.
A tripod offers exactly two adjustments — a turn about the local vertical and a
tilt about the horizontal hinge lying across the axis's meridian — so ``R`` is
restricted to that two-parameter family — which is locally determined but
**globally two-to-one**, see the two-root note below — in
the same basis as the two things the user's hands are actually doing. The
alternative, and the natural third constraint, is the plate solve's own field
rotation angle (ASTAP's ``CROTA2``); it is **rejected here, for now**. An error
in the recovered roll about the boresight moves the fitted axis very nearly
one-for-one — by ``roll * sin(circle_radius)``, and the radius of a usable
sweep is 60 to 90 degrees — so a 0.05 degree rotation angle, entirely plausible
for a plate solve and *unmeasured on this hardware*, is 3 arcmin of axis error,
the size of the whole target. The positional repeatability, by contrast, has
been measured. A 2026-09-29 consistency check against the 22 saved hardware
solves in ``tests/astro/polar_real_solves.json`` found that treating ``CROTA2``
as a full camera pose moved the supposedly fixed RA axis by 1-13 arcmin within
individual RA-only sweeps. That includes solver roll error and any mechanical
flexure; either way it is too large to certify a sub-2-arcmin live correction.

**What that choice cannot detect, stated plainly.** The two modelled generators
span rotations about the vertical and about the east-west hinge. The direction
they do *not* span is a tilt about the horizontal axis lying **in** the RA
axis's own meridian — a tripod settling north-south, which is exactly what soft
ground does, and which on 2026-09-10 coupled into every azimuth push with a
ratio climbing 0.0124 -> 0.0327 -> 0.0501 -> 0.0638 as the legs sank. Such a
tilt does move the reference direction, the fit has no parameter for it, and it
is mis-attributed to the two knobs — by 2.5 arcmin of apparent axis error per
arcmin of settle for a reference an hour east of the meridian at declination 25
(``tests/astro/test_polar.py::test_a_north_south_settle_is_mis_attributed_and_this_is_the_known_limit``,
exactly linear from 1 to 30 arcmin of settle). No method that measures only one direction
can separate it. Two defences, both structural rather than hopeful: every
refinement is differential from **the sweep**, never chained onto the previous
refinement, so the error does not compound with the number of re-solves; and
:attr:`PolarRefinement.axis_shift_deg` reports how far the tripod has travelled
since that sweep, past which
(:data:`DEFAULT_MAX_AXIS_SHIFT_DEG`) the result comes back with
``trusted=False`` and a warning to sweep again.

**Tracking must be stopped, and the caller must say so out loud.**
:class:`MountTracking` is a required keyword argument with no default; anything
but :attr:`MountTracking.STOPPED` raises :class:`TrackingNotStoppedError`
rather than returning a plausible number. With tracking on, the mount is also
turning about its own axis between the two solves at a rate this module would
have to assume and could not verify — 15 arcsec of boresight motion per second,
so a 20-second gap manufactures 5 arcmin of pure artefact, which is the size of
the answer. The usual objection to stopping tracking is that the field drifts
out of frame. It does not matter here: nothing in this measurement cares *which*
stars are in the frame. The mount keeps pointing where it points, a blind solve
of whatever sky has drifted in works exactly as well, and the reference
direction stays valid for as long as the tripod is left alone.

**Two parameters against two constraints is not one answer.** The map from
(tilt, turn) to a landed direction is *globally two-to-one*: two genuinely
different tripod adjustments can carry the reference to **exactly** the same
re-solve, both with zero residual. :func:`_tripod_rotation_roots` enumerates
them in closed form rather than iterating, because an iterative fit converges to
whichever root it fell toward and reports a residual of zero either way — which
is what happened on 2026-09-10: a plain 2-degree azimuth turn, with the
reference 1.1 degrees away from 90 degrees of azimuth off the RA axis, returned
the other root and was wrong by **118 arcmin while reporting itself
trustworthy**, at 0.0000 arcsec residual and a conditioning figure of 76 against
a refusal threshold of 100.

Conditioning is *not* the predictor for this and must not be leaned on: it
measures how solve noise propagates through one root, while the ambiguity is how
far apart two exact roots lie — and the two run in opposite directions, since as
the roots merge the noise blows up. Both gates are needed and the second is not
a threshold on the first. Usually the rival root is a 70- to 177-degree flip of
the whole tripod and is discarded on size against
:data:`DEFAULT_MAX_REFINEMENT_STEP_DEG`; near 90 degrees of azimuth from the RA
axis it shrinks to the order of the real move (4.4 degrees against 2.0, in the
case above) and nothing in the data separates them. Then the refinement
**refuses** (:attr:`DegeneracyReason.AMBIGUOUS_MOVE`), naming both candidate
axes, rather than picking the smaller and hoping.

The same closed form yields a consistency check the iterative fit could not: a
tilt sweeps a fixed range of altitudes and a turn changes none, so a re-solve
outside that range is reachable by **no** tripod adjustment
(:attr:`DegeneracyReason.UNREACHABLE_MOVE`) — a residual in the one direction
the two-parameter model leaves free.

**The one assumption nothing can check: the mount must not slew between the
reference solve and the re-solve.** A slew and a shove look identical in a
single solved direction. The only available guard is size — a move past
:data:`DEFAULT_MAX_REFINEMENT_STEP_DEG` is refused and past half of it warned
about — so a caller that re-slews must throw its reference away and take a new
one at the new pointing.

**Purity.** No I/O beyond the already-open :class:`~ssc.astro.events.Ephemeris`
handle, no ``datetime.now()``: every instant is an argument (CLAUDE.md
invariant 3), timezone-aware UTC (invariant 5). :class:`PolarPoint` is defined
here rather than taking a :class:`ssc.platesolve.base.PlateSolveResult` because
``ssc.platesolve`` already imports ``ssc.astro`` — the adapter belongs on the
caller's side of that arrow.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum
from itertools import pairwise
from typing import Any, Final

import numpy as np

from ssc.astro.events import Ephemeris, Site, _require_utc
from ssc.astro.frames import (
    EquatorialCoord,
    MountFrame,
    frame_offset_arcsec,
    to_j2000,
    to_mount_frame,
)
from ssc.astro.framing import altitude_deg, azimuth_deg
from ssc.astro.units import (
    SIDEREAL_DEG_PER_HOUR,
    ArcMin,
    ArcSec,
    Deg,
    Hours,
    arcmin_to_deg,
    deg_to_arcsec,
    hours_to_deg,
    wrap_deg_180,
    wrap_deg_360,
)

__all__ = [
    "DEFAULT_MAX_AXIS_ERROR_DEG",
    "DEFAULT_MAX_AXIS_SHIFT_DEG",
    "DEFAULT_MAX_BRANCH_SEPARATION_ARCMIN",
    "DEFAULT_MAX_MERIDIAN_ALTITUDE_DEG",
    "DEFAULT_MAX_NOISE_AMPLIFICATION",
    "DEFAULT_MAX_REFINEMENT_ELAPSED_S",
    "DEFAULT_MAX_REFINEMENT_STEP_DEG",
    "DEFAULT_MAX_RESIDUAL_ARCSEC",
    "DEFAULT_MIN_ARC_SPAN_DEG",
    "DEFAULT_MIN_POINT_SEPARATION_DEG",
    "DEFAULT_MIN_SWEEP_ALTITUDE_DEG",
    "DEFAULT_SOLVE_SIGMA_ARCSEC",
    "STANDARD_PRESSURE_MBAR",
    "STANDARD_TEMPERATURE_C",
    "DegeneracyReason",
    "DegenerateGeometryError",
    "MountTracking",
    "PolarAlignmentError",
    "PolarAlignmentModel",
    "PolarPoint",
    "PolarRefinement",
    "SweepField",
    "TrackingNotStoppedError",
    "best_conditioned_field",
    "celestial_pole_altaz",
    "j2000_coord_at_hour_angle",
    "refine_polar_alignment",
    "solve_polar_alignment",
]

#: Assumed 1-sigma uncertainty of one solved field centre, arcsec. It scales
#: :attr:`PolarAlignmentModel.axis_uncertainty_arcsec` and nothing else — the
#: geometry figure :attr:`PolarAlignmentModel.noise_amplification` is
#: independent of it. 5" is a plate solve on the S30 Pro tele (2.4"/px) plus the
#: field-centre determination; a caller holding a real solve should pass its own
#: figure rather than inherit this one.
DEFAULT_SOLVE_SIGMA_ARCSEC: Final = ArcSec(5.0)

#: Atmosphere assumed by the refraction correction when the caller says nothing:
#: 1010 mbar and 10 C, the conditions Meeus 16.4's coefficients are quoted for.
#: Real weather moves the correction by ~10 per cent, i.e. under 0.2 arcmin of
#: axis position in a sane geometry — worth passing if known, not worth
#: inventing. Site elevation is *not* used to guess a pressure: an unknown
#: elevation is stored as 0 m (``ssc.astro.frames.ASSUMED_ELEVATION_M``), so
#: deriving pressure from it would silently assume sea level at a mountain site.
STANDARD_PRESSURE_MBAR: Final = 1010.0
STANDARD_TEMPERATURE_C: Final = 10.0

#: Two solved positions closer than this cannot be distinguished from one
#: position solved twice, and a repeated point adds no information while making
#: the scatter matrix rank-deficient.
DEFAULT_MIN_POINT_SEPARATION_DEG: Final = Deg(1.0)

#: Minimum arc of *mount rotation* the points must span, measured about the
#: fitted axis. Below this the circle's radius — and so its centre — is set by
#: the sagitta of a very shallow arc, and solve noise is amplified by
#: ~``8 / span_rad**2``. 10 deg already amplifies ~150x; 30 deg
#: between points (60 deg total) amplifies ~9x.
DEFAULT_MIN_ARC_SPAN_DEG: Final = Deg(10.0)

#: Refuse a fit whose axis uncertainty exceeds this multiple of the per-point
#: uncertainty. 100x turns even a 1" solve into 100" (1.7') of axis error, which
#: is worse than the alignment anyone is trying to reach. This is the
#: "unusable by any measure" gate, not a quality bar: the quality bar is
#: :attr:`PolarAlignmentModel.axis_uncertainty_arcsec`, reported always so the
#: caller can warn without being refused (architecture policy invariant 7).
DEFAULT_MAX_NOISE_AMPLIFICATION: Final = 100.0

#: Largest tolerated RMS departure from a common small circle. Not a quality
#: bar either — mount flexure and backlash live at the arcminute level and are
#: legitimately present. This catches "these points did not come from an RA-only
#: rotation": a nudged declination axis, a mis-recorded ``date_obs``, a solve of
#: the wrong frame. 1 degree is far outside anything mechanical.
DEFAULT_MAX_RESIDUAL_ARCSEC: Final = ArcSec(3600.0)

#: Beyond this the fitted axis is not a polar alignment error at all — a tripod
#: pointed at the wrong quarter of the sky, or a point set that never came from
#: an RA rotation. Refusing is honest; reporting "your polar error is 61
#: degrees" is not. (Past 90 deg the hemisphere sign is ambiguous outright.)
DEFAULT_MAX_AXIS_ERROR_DEG: Final = Deg(45.0)

#: Largest boresight move :func:`refine_polar_alignment` will read as a tripod
#: adjustment. Bigger than this and the likeliest explanation is that the mount
#: slewed — which looks identical in one solved direction and would be reported
#: as an enormous alignment change. Half of it triggers a warning instead. 20 deg
#: is far past any credible shove: the largest hand correction measured on
#: 2026-09-10, the night this loop was written for, moved the axis 28 deg in
#: azimuth, which is 19 deg of boresight motion at the owner's latitude.
DEFAULT_MAX_REFINEMENT_STEP_DEG: Final = Deg(20.0)

#: How far apart the two candidate answers may be before a refinement refuses
#: rather than choosing between them. Two different tripod moves can carry the
#: reference to exactly the same re-solve (see :func:`_tripod_rotation_roots`);
#: usually one of them is a 100-degree flip and is discarded on size, but near
#: 90 degrees of azimuth from the axis both are small and both fit the data
#: perfectly. 1 arcmin is a fifth of the ~5 arcmin alignment anyone is aiming
#: for: below it, it does not matter which root is taken.
DEFAULT_MAX_BRANCH_SEPARATION_ARCMIN: Final = ArcMin(1.0)

#: How far the axis may travel from the sweep that baselined it before the
#: refinement is returned with ``trusted=False``. Refinement is differential
#: from one sweep and never chained, so noise does not compound with the number
#: of re-solves — but the unmodelled third rotational degree of freedom (module
#: docstring) grows with total travel, and a fresh sweep costs a minute.
DEFAULT_MAX_AXIS_SHIFT_DEG: Final = Deg(5.0)

#: A single re-solve cannot tell a wedge adjustment from a mount slew or an
#: unmodelled tripod tilt. On 2026-09-28 a ~2 deg reference-field displacement
#: produced a 0.54 arcmin live answer, while independent three- and five-point
#: sweeps agreed on ~9.2 arcmin. Require a new sweep once the reference field
#: moves this far, even when the inferred axis shift is small.
DEFAULT_MAX_BORESIGHT_SHIFT_DEG: Final = Deg(1.0)

#: How stale the reference solve may be before the refinement is returned with
#: ``trusted=False``. With tracking stopped nothing physical expires — the guard
#: is against a reference that has outlived the session it was taken in, and so
#: may predate a slew the module cannot see. 30 minutes is many times longer
#: than the loop this exists for, which runs in seconds.
DEFAULT_MAX_REFINEMENT_ELAPSED_S: Final = 1800.0

#: Lowest altitude any point of a *planned* sweep may reach
#: (:func:`best_conditioned_field`). Three reasons, in the order they bind.
#: The module docstring's worst measured refraction case is a sweep "running
#: down to 14 degrees altitude" (5.7 arcmin of axis bias when unmodelled, ~0.6
#: with Saemundsson applied and standard conditions assumed), so 14 deg is the
#: edge of what this module has actually characterised and 15 sits just above
#: it. Airmass at 15 deg is 3.8, which a wide field still solves. And
#: structurally: at declination 0 with the default 5-point, 30-degree sweep the
#: arc's ends sit at ``asin(cos(phi) * cos(60 deg))``, which clears 15 deg for
#: every latitude up to **58.8 deg** — so the textbook answer (the celestial
#: equator) survives unmodified across the whole mid-northern range this is
#: built for, and a lower floor would only buy latitudes where the pole is
#: already too high for a sane arc. Past 58.8 deg the declination is pushed
#: toward the pole and :attr:`SweepField.warnings` says so.
DEFAULT_MIN_SWEEP_ALTITUDE_DEG: Final = Deg(15.0)

#: Highest altitude the *centre* of a planned sweep may reach. **Hard**, not a
#: preference: below latitude 40 the declination is pushed away from the pole to
#: stay under it, and where the minimum altitude cannot be met without passing
#: it, :func:`best_conditioned_field` refuses and names both numbers rather than
#: quietly choosing which of the caller's two requirements to honour. Left soft
#: it walks straight into the degeneracy it exists to avoid: a 9-point, 30-degree
#: sweep at latitude 48 with a 20-degree floor needs declination 48.9, which puts
#: the arc's centre 0.75 deg from the zenith. Straight out of
#: :attr:`PolarRefinement.noise_amplification`'s own text — "1.2 to 1.5 for a
#: reference on or near the meridian below 50 degrees altitude" — because the
#: live refinement loop runs at whatever pointing the sweep parked on and is the
#: more delicate consumer of the two. It binds only below latitude 40, where the
#: celestial equator culminates above 50 deg; at the zenith itself a turn about
#: the tripod's vertical moves the boresight nowhere at all
#: (:func:`_signed_angle_about`), so this is a real degeneracy being kept at
#: arm's length, not a preference.
DEFAULT_MAX_MERIDIAN_ALTITUDE_DEG: Final = Deg(50.0)

#: How many times :func:`best_conditioned_field` may re-measure and re-place the
#: arc before giving up. The closed form that places it works in apparent
#: coordinates, but the sweep the caller will actually run steps **J2000** right
#: ascension at fixed J2000 declination, and the ~20 arcmin between the two
#: frames tilts the circle: measured at latitude -60, the analytic worst point
#: lands 0.20 deg lower than predicted. So the arc is measured through the same
#: frame chain :func:`_horizontal_vector` uses and the declination is corrected
#: by the shortfall over the local slope, which converges in one or two passes.
_SWEEP_FIELD_PASSES: Final = 6

#: Below this the altitude of the arc's lowest point has stopped responding to
#: declination (the field is at or past its own culmination there), so no
#: further push toward the pole can lift it over the floor.
_MIN_ALTITUDE_SLOPE: Final = 1e-3

#: The plane fit spends three parameters: two on the axis direction, one on the
#: circle's radius. Residuals only mean anything once there are more points than
#: that — see the module docstring on why three points report no residual at all.
_PLANE_FIT_PARAMETERS: Final = 3

#: A solved position this far below the horizon cannot be a frame anyone
#: exposed. Half a degree of slack covers dip and a mis-stated site by a hair;
#: more than that is a wrong site, a flipped longitude or a wrong ``date_obs``,
#: and the refraction formula is meaningless there anyway.
_MIN_POINT_ALTITUDE_DEG: Final = -0.5

#: Azimuth has no meaning at the geographic poles: every direction is south (or
#: north) and the pole is at the zenith, so there is no azimuth knob to turn.
_MAX_LATITUDE_DEG: Final = Deg(89.9)

#: How far outside the reachable set a re-solve may fall before the move is
#: declared impossible rather than noisy. A tilt about the hinge can only carry
#: the reference through a fixed range of altitudes, and a turn changes none, so
#: an altitude outside that range cannot be produced by this tripod at all. The
#: bound is in ``sin`` of the shortfall: 1.5e-3 is 5 arcmin, comfortably above
#: what a few arcsec of solve noise pushes it by in a sane geometry (~10 arcsec)
#: and well below a real mechanical surprise.
_MAX_UNREACHABLE_SINE: Final = 1.5e-3

#: Below this ``sin`` of the axis's zenith distance the altitude hinge has no
#: direction. 1e-6 is 0.2 arcsec from the zenith; the latitude gate in
#: :func:`celestial_pole_altaz` already stops 0.1 deg short of it.
_MIN_HINGE_SINE: Final = 1e-6

#: Only used to state a tolerance in the units a human reads it in.
_ARCMIN_PER_RADIAN: Final = 60.0 * 180.0 / math.pi

_Vector = tuple[float, float, float]


class DegeneracyReason(StrEnum):
    """Why a point set cannot support a fit — named so a UI can say which.

    A bare "alignment failed" leaves the user at the tripod guessing; each of
    these has a different remedy, and the remedy is the point.
    """

    #: Fewer than three positions. Three is the minimum that determines a circle.
    TOO_FEW_POINTS = "too_few_points"
    #: Two positions land on top of each other — the mount did not move, or the
    #: same frame was solved twice.
    COINCIDENT_POINTS = "coincident_points"
    #: The rotation covered too little arc. Remedy: rotate further between points.
    SHORT_ARC = "short_arc"
    #: The geometry amplifies solve noise past usefulness even though the arc is
    #: nominally long enough — clustered points, or a circle radius near zero.
    ILL_CONDITIONED = "ill_conditioned"
    #: The points do not lie on any common small circle, so they did not come
    #: from a rotation about one axis. Something moved that should not have.
    #: **Needs four points or more** — three always lie on a circle exactly, so
    #: a three-point run can never be caught this way.
    SCATTERED_POINTS = "scattered_points"
    #: A fitted axis too far from the pole to be a polar alignment error.
    IMPLAUSIBLE_AXIS = "implausible_axis"
    #: Two different tripod adjustments carry the reference to exactly this
    #: re-solve, and they imply materially different axes. Not noise and not a
    #: bad fit — both answers are exact. One solved direction cannot choose
    #: between them. Remedy: re-point the reference nearer the meridian, where
    #: the second root is a 100-degree flip and discards itself.
    AMBIGUOUS_MOVE = "ambiguous_move"
    #: No turn and tilt of this tripod carries the reference to this re-solve.
    #: A tilt about the hinge spans a limited range of altitudes and a turn
    #: changes none, so an altitude outside that range cannot be reached.
    #: Something moved that is not the two adjustments: the tripod settled
    #: north-south, the mount slewed, or a date_obs belongs to another frame.
    UNREACHABLE_MOVE = "unreachable_move"
    #: A refinement re-solve sits too far from its reference to be a tripod
    #: adjustment. Most likely the mount slewed, which one solved direction
    #: cannot distinguish from a shove. Remedy: take a fresh reference.
    STEP_TOO_LARGE = "step_too_large"
    #: A solved position lands below the horizon at the given site and time,
    #: which no telescope can have imaged. The site, the longitude sign or the
    #: ``date_obs`` is wrong — and this is the cheapest place that shows up.
    BELOW_HORIZON = "below_horizon"
    #: A sweep of the requested shape cannot be *planned* above the requested
    #: minimum altitude at this site and time, at any declination. Unlike the
    #: others this refuses before anything moves, and its remedies are the
    #: request's own parameters: fewer points, a smaller separation between
    #: them, a lower floor, or a different hour of the night.
    NO_CLEAR_ARC = "no_clear_arc"


class PolarAlignmentError(ValueError):
    """Input this module refuses: wrong frame, naive datetime, unusable site."""


class MountTracking(StrEnum):
    """What the mount's drive was doing between the two refinement solves.

    Required, with no default, because getting it wrong is silent: a tracking
    mount moves its own boresight 15 arcsec per second, which
    :func:`refine_polar_alignment` would read as tripod motion and convert into
    arcminutes of imaginary alignment change. A caller that does not know must
    say :attr:`UNKNOWN` and be refused, not guess.
    """

    #: Drive halted. The mount is a rigid object in the Earth-fixed frame and
    #: the whole method is exact. The only value that is accepted.
    STOPPED = "stopped"
    #: Drive running at sidereal rate. Refused: modelling it means assuming a
    #: rate and an uninterrupted rotation, neither of which has been measured on
    #: this hardware (module docstring).
    SIDEREAL = "sidereal"
    #: The caller cannot tell. Refused — the honest answer to an unknown.
    UNKNOWN = "unknown"


class TrackingNotStoppedError(PolarAlignmentError):
    """A refinement was asked for while the mount was (or might be) tracking.

    Separate from the other refusals because it has one specific remedy a UI can
    act on: stop tracking, take a fresh reference solve, then refine. See the
    module docstring for why this is required rather than modelled.
    """


class DegenerateGeometryError(PolarAlignmentError):
    """The measured geometry cannot determine an axis, or cannot determine one
    worth reporting. Carries :attr:`reason` so the caller can act on *which*."""

    def __init__(self, reason: DegeneracyReason, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PolarPoint:
    """One plate-solved field centre from the RA-only rotation.

    ``coord`` is the solve, and must be J2000 — which is what
    :class:`ssc.platesolve.base.PlateSolveResult` always carries. ``when`` is
    the exposure midpoint, timezone-aware (``DATE-OBS`` plus half the exposure):
    when the scope was *there*,
    not when the solve returned. ``reported`` is the mount's own belief at the
    same instant, in whatever frame the mount declares; it is recorded and
    reported, never fitted.
    """

    coord: EquatorialCoord
    when: datetime
    reported: EquatorialCoord | None = None


@dataclass(frozen=True, slots=True)
class PolarAlignmentModel:
    """Where the mount's RA axis points, and how far that is from the pole.

    **Sign convention, stated once and used everywhere below: every ``*_error``
    is (where the axis is) minus (where the pole is), in local horizontal
    coordinates.** So an error is what the mount *has*, and the correction is
    what it *needs* — the negation, offered as
    :attr:`altitude_correction_deg` / :attr:`azimuth_correction_deg` so no
    caller has to remember which way round it was.

    * ``altitude_error_deg > 0`` — the axis points **above** the pole; the front
      of the wedge must come **down**.
    * ``azimuth_error_deg > 0`` — the axis lies at **greater azimuth**, i.e.
      **clockwise seen from above** (azimuth runs N -> E -> S -> W); the mount
      must turn **counter-clockwise seen from above**. In the northern
      hemisphere that reads "the axis is east of north, turn it west"; in the
      southern, "east of south, turn it east" — the clockwise phrasing is the
      one that is true in both.

    **Negative azimuth error means the axis points west of true north, and the
    instruction is "rotate east".** That mapping was confirmed physically rather
    than derived: on 2026-09-10 the owner moved the tripod west while the
    reading was -4.92 deg, and it grew to -17.84 deg. Say it that way round in a
    UI; it is the sentence that cannot be got backwards.

    Both are **mount-adjustment angles**: coordinate differences, which is what
    a knob moves. They are not interchangeable with on-sky angles.
    :attr:`azimuth_error_sky_deg` is the same azimuth error projected onto the
    sky (``* cos(pole altitude)``), which is the number that governs drift and
    the one to compare against another tool's "azimuth error". At latitude 48.7
    the two differ by a factor of 1.5 — big enough to send someone the wrong
    distance if the labels are muddled. Altitude needs no such distinction: the
    altitude coordinate difference *is* the on-sky component.
    """

    #: Where the RA axis actually points, local horizontal, degrees.
    axis_altitude_deg: Deg
    axis_azimuth_deg: Deg
    #: Signed altitude error, degrees. Positive = axis too high.
    altitude_error_deg: Deg
    #: Signed azimuth error as a mount rotation, degrees, wrapped (-180, +180].
    #: Positive = axis clockwise of the pole seen from above.
    azimuth_error_deg: Deg
    #: The azimuth error as an angle on the sky, ``azimuth_error * cos(|phi|)``.
    azimuth_error_sky_deg: Deg
    #: True great-circle separation of axis from pole, degrees. Equals
    #: ``hypot(altitude_error, azimuth_error_sky)`` to second order; this is the
    #: exact value, not that approximation.
    total_error_deg: Deg

    #: Angular radius of the fitted small circle — how far the boresight sits
    #: from the RA axis. Near 90 deg (a field near the celestial equator) is the
    #: best-conditioned place to measure from; near 0 is the worst.
    circle_radius_deg: Deg
    #: Arc of mount rotation the points actually span, measured about the axis.
    arc_span_deg: Deg

    #: RMS and worst departure of the points from that common circle, or
    #: **None when there were only three points** — a plane through three points
    #: fits them exactly, so there is nothing left to measure and "0.00" would
    #: be a lie of the most dangerous kind. A good four-plus-point run is
    #: arcseconds; arcminutes means mechanics (flexure, backlash) or a mount
    #: that moved in declination.
    rms_residual_arcsec: ArcSec | None
    max_residual_arcsec: ArcSec | None
    #: Degrees of freedom left in those residuals, ``point_count - 3``: the
    #: plane fit spends two on the axis direction and one on the circle's
    #: radius. Zero means the fit had no freedom to disagree with itself, which
    #: is why the residuals above are None. Show this next to them.
    residual_dof: int
    #: Pure geometry: arcsec of axis error produced per arcsec of solve error.
    #: Independent of any noise assumption, so it is the honest conditioning
    #: number. ~1.2 is ideal, ~9 is a 60-degree arc, >100 is refused.
    noise_amplification: float
    #: :attr:`noise_amplification` times the assumed per-solve sigma. The figure
    #: to show next to the answer, and the one to compare against the error
    #: itself: an error smaller than its own uncertainty means "keep measuring",
    #: not "you are aligned".
    axis_uncertainty_arcsec: ArcSec

    #: How many solved positions went into the fit.
    point_count: int
    #: The newest ``date_obs`` in the fit. A three-point model measures where
    #: the axis is *at a moment* (DESIGN 4.5); staleness is measured from here.
    epoch: datetime
    #: The site the horizontal frame is referred to, kept so a later refinement
    #: cannot silently be evaluated against a different one.
    site: Site
    #: Whether the refraction correction ran. False means the reported axis is
    #: biased by the amount the module docstring measures — arcminutes, not
    #: arcseconds — and the caller asked for that.
    refraction_applied: bool
    #: Per point, the mount's reported position against the solved one, arcsec —
    #: the pointing-model error, recorded only. ``None`` where the caller passed
    #: no reported coordinate. Same order as the input points.
    reported_offsets_arcsec: tuple[ArcSec | None, ...]

    @property
    def altitude_correction_deg(self) -> Deg:
        """How far to *move* the axis in altitude: ``-altitude_error_deg``.
        Positive means raise the front of the wedge."""
        return Deg(-float(self.altitude_error_deg))

    @property
    def azimuth_correction_deg(self) -> Deg:
        """How far to *turn* the mount in azimuth: ``-azimuth_error_deg``.
        Positive means turn clockwise seen from above (toward greater azimuth)."""
        return Deg(-float(self.azimuth_error_deg))


@dataclass(frozen=True, slots=True)
class PolarRefinement:
    """An axis updated from one re-solve, plus everything needed to distrust it.

    :attr:`model` is a full :class:`PolarAlignmentModel` so a consumer that
    already renders a sweep result needs no second code path. Only the axis, the
    four error figures, :attr:`~PolarAlignmentModel.epoch` and
    :attr:`~PolarAlignmentModel.axis_uncertainty_arcsec` are updated; the fields
    that *describe the sweep* — point count, arc span, circle radius, residuals,
    reported offsets — are carried through unchanged, because a rigid rotation
    of the whole mount leaves every one of them true.
    """

    #: The updated model. Feed the **sweep's** model to the next refinement, not
    #: this one: refinement is differential from a fixed baseline, and chaining
    #: would compound the reference solve's own noise once per re-solve.
    model: PolarAlignmentModel

    #: How far the axis moved in altitude since the sweep, degrees, in the same
    #: coordinate sense as :attr:`PolarAlignmentModel.altitude_error_deg`:
    #: positive means the axis rose. This is the number to hold against what the
    #: user was *asked* to do — on 2026-09-10 that ratio was consistently ~3x.
    altitude_step_deg: Deg
    #: How far the axis moved in azimuth since the sweep, degrees, wrapped
    #: (-180, +180]. Positive means clockwise seen from above. A mount-adjustment
    #: angle, not an on-sky one.
    azimuth_step_deg: Deg
    #: Great-circle distance between the refined axis and the swept one. The
    #: staleness figure: refinement error grows with this, not with the number
    #: of re-solves.
    axis_shift_deg: Deg
    #: Great-circle distance between the reference solve and the re-solve — the
    #: raw observable everything else is derived from. Comparable against
    #: :attr:`refinement_uncertainty_arcsec` to see whether anything moved at all.
    boresight_shift_deg: Deg

    #: Seconds between the reference solve's ``date_obs`` and the re-solve's.
    elapsed_seconds: float
    #: Seconds between the sweep's epoch and the re-solve. How old the baseline is.
    sweep_age_seconds: float

    #: What this refinement alone adds to the axis uncertainty, arcsec:
    #: :attr:`noise_amplification` times the per-solve sigma times ``sqrt(2)``,
    #: since reference and re-solve each carry it. This quantifies random solve
    #: noise only; it cannot bound the unobserved third rotation of a tripod.
    refinement_uncertainty_arcsec: ArcSec
    #: Arcsec of axis error per arcsec of solve error for this geometry alone.
    #: 1.2 to 1.5 for a reference on or near the meridian below 50 degrees
    #: altitude, and it diverges as the reference approaches 90 degrees of
    #: azimuth from the axis — near due east or west, where a turn about the
    #: vertical and a tilt about the hinge move the boresight along the same
    #: line and cannot be told apart (0.2 deg away from it is already 330x).
    #: Point near the meridian, and not near the zenith.
    noise_amplification: float

    #: Always false: one field cannot verify the axis after physical movement.
    #: The result is diagnostic and must not guide an adjustment.
    trusted: bool
    #: Human-readable reasons, in the order checked. Empty when trusted.
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SweepField:
    """Where to start a sweep, and every number needed to justify the choice.

    Returned by :func:`best_conditioned_field`. :attr:`coord` is the **first**
    point of the run — the one a caller hands to a slew — not the middle of the
    arc, because the arc is centred on the meridian rather than started there
    (see the function's own docstring).
    """

    #: The sweep's first pointing, **J2000**, matching :attr:`PolarPoint.coord`
    #: and the mount targets built from it. Right ascension is in degrees here
    #: as everywhere inside ``ssc.astro``; convert at the JSON boundary.
    coord: EquatorialCoord

    #: Where :attr:`coord` is in the sky at :attr:`when`, degrees. Azimuth runs
    #: north 0 -> east 90, the same convention as everything else in this module.
    start_altitude_deg: Deg
    start_azimuth_deg: Deg
    #: Apparent hour angle of :attr:`coord` at :attr:`when`, wrapped
    #: (-180, +180]. Half the arc span, signed opposite to ``step_deg``: the
    #: sweep walks from here through the meridian and out the other side.
    start_hour_angle_deg: Deg

    #: How far the chosen declination sits from the celestial equator, degrees,
    #: signed **toward the visible pole** in either hemisphere. **Zero is the
    #: textbook answer** and anything else is a constraint having bound — read
    #: :attr:`warnings` for which one.
    declination_offset_deg: Deg
    #: Altitude of the arc's centre, i.e. of the chosen declination at transit.
    #: Exactly ``90 - |latitude|`` when :attr:`declination_offset_deg` is zero,
    #: which is the identity DESIGN 4.5 asserts.
    meridian_altitude_deg: Deg

    #: ``|step_deg| * (point_count - 1)`` — the rotation the whole run covers.
    arc_span_deg: Deg
    #: Altitude of every planned point, in run order, measured through the same
    #: J2000 -> apparent -> hour angle -> alt/az chain a solved point takes.
    point_altitudes_deg: tuple[Deg, ...]
    #: The lowest of those. The number the minimum-altitude request is enforced
    #: against, and the one to show a user standing in front of a treeline.
    worst_altitude_deg: Deg

    #: Angular radius of the circle the sweep will trace — the mean angle
    #: between its points and the axis, the same quantity
    #: :attr:`PolarAlignmentModel.circle_radius_deg` reports once the fit has
    #: run. 90 deg is the widest and best-conditioned circle, and conditioning
    #: follows ``sin`` of it, so 110 deg costs exactly what 70 deg does.
    circle_radius_deg: Deg
    #: What :attr:`PolarAlignmentModel.noise_amplification` will come out at for
    #: this geometry, computed by the same function the fit uses. ~9 for a
    #: 60-degree arc, ~2 for the 120-degree default.
    sweep_amplification: float
    #: :attr:`PolarRefinement.noise_amplification` for a refinement referenced at
    #: :attr:`coord`, against the celestial pole as the assumed axis — the axis
    #: is not known until the sweep runs, and a few arcminutes of error does not
    #: move this figure.
    refinement_amplification: float
    #: The worst of that figure over every point of the arc. The live loop takes
    #: its reference wherever the sweep parked the mount, which is an *end* of
    #: the arc, so this is the one that bounds the loop rather than the start's.
    worst_refinement_amplification: float

    #: The instant the geometry was computed for. The sky turns 15 deg per hour;
    #: a field chosen an hour ago is an hour off the meridian.
    when: datetime
    #: The site it was computed for, carried so a caller cannot pair it with
    #: another one by accident.
    site: Site

    #: True only when nothing bound: celestial equator, arc centred on the
    #: meridian, every point clear of the floor. False is not a failure — it is
    #: a reduced-quality answer, and :attr:`warnings` says what was given up.
    ideal: bool
    #: Human-readable, in the order checked. Empty when :attr:`ideal`.
    warnings: tuple[str, ...]


def celestial_pole_altaz(site: Site) -> tuple[Deg, Deg]:
    """Altitude and azimuth of the celestial pole a mount at ``site`` aligns to.

    The north pole above the equator, the south pole below: altitude ``|phi|``,
    azimuth 0 or 180. Exact by construction — the pole is the one direction in
    the sky that does not move in this frame, which is why the whole fit happens
    here.
    """
    latitude = float(site.latitude_deg)
    if abs(latitude) > float(_MAX_LATITUDE_DEG):
        raise PolarAlignmentError(
            f"latitude {latitude} deg is inside {_MAX_LATITUDE_DEG} deg of a geographic pole: "
            "the celestial pole is at the zenith there and azimuth has no meaning, so an "
            "azimuth error cannot be reported"
        )
    return Deg(abs(latitude)), Deg(0.0 if latitude >= 0.0 else 180.0)


def best_conditioned_field(
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    *,
    point_count: int,
    step_deg: Deg,
    min_altitude_deg: Deg = DEFAULT_MIN_SWEEP_ALTITUDE_DEG,
    max_meridian_altitude_deg: Deg = DEFAULT_MAX_MERIDIAN_ALTITUDE_DEG,
    sweep_duration_s: float = 0.0,
    max_noise_amplification: float = DEFAULT_MAX_NOISE_AMPLIFICATION,
) -> SweepField:
    """The field to start a sweep from at ``site`` at ``when`` (DESIGN 4.5).

    **Meridian, celestial equator.** The sweep fits a circle about the mount's
    actual polar axis, whose angular radius is ``90 - |declination|``, so the
    widest and best-conditioned circle is the one at declination 0; toward the
    pole the arc barely moves and :func:`_noise_amplification` climbs. The live
    refinement loop that runs afterwards
    (:attr:`PolarRefinement.noise_amplification`) wants the same place for a
    different reason — "point near the meridian, and not near the zenith" — and
    at mid latitudes the two agree exactly: the celestial equator crosses the
    meridian at altitude ``90 - |phi|``, about 41 deg at latitude 48.7, which is
    on the meridian, below 50 deg, and as far as it is possible to get from the
    due-east/west azimuth where a tripod turn and a tripod tilt become the same
    motion. In the southern hemisphere the RA axis points south and this field
    is due **north**; nothing here is special-cased for either.

    **The arc is centred on the meridian, not started there.** The run spans
    ``|step_deg| * (point_count - 1)`` — 120 deg at the usual 5 points of 30 deg
    — so a field starting on the meridian would finish at 8h of hour angle, in
    the ground. The first point is therefore placed half a span *before* the
    meridian, which is why the answer depends on the shape of the sweep and why
    ``point_count`` and ``step_deg`` are required rather than assumed. Their
    signs carry through: a negative ``step_deg`` runs the other way round, so
    the field starts east of the meridian instead of west.

    ``sweep_duration_s`` is how long the whole run will take. The sky turns
    15 deg per hour, so a 4-minute sweep moves the arc 1 deg while it is being
    taken — in the *same* direction as the mount when ``step_deg`` is negative,
    which drops the last point below where a static calculation puts it. Given a
    duration the arc is centred on the meridian across the run rather than at
    its first instant. The default 0.0 evaluates the whole arc at ``when`` and
    is an idealisation, not an unknown.

    **The minimum altitude is honoured for every point, and refused when it
    cannot be.** Where the celestial equator's arc will not clear
    ``min_altitude_deg`` — above latitude ~58.8 at the defaults — the
    declination is pushed toward the pole by the smallest amount that does
    clear it. Where the equator instead culminates above
    ``max_meridian_altitude_deg`` — below latitude 40, and at the equator itself
    it passes through the zenith, where a tripod turn moves the boresight
    nowhere — it is pushed the other way. That ceiling is **hard**: when no
    declination satisfies both it and the floor, this refuses and names both
    numbers rather than deciding for the caller which of the two to give up.
    Either push narrows the fitted circle and is reported: :attr:`SweepField.declination_offset_deg` carries its size,
    :attr:`SweepField.ideal` goes False and :attr:`SweepField.warnings` says
    which constraint bound (CLAUDE.md invariant 7 — a reduced-quality answer is
    never returned silently). When no declination satisfies the floor at all,
    this raises :class:`DegenerateGeometryError` with
    :attr:`DegeneracyReason.NO_CLEAR_ARC` rather than handing back a field whose
    ends are underground.

    The site's own horizon is *not* modelled: SSC does not know where the trees
    are (DESIGN 4.5 records that this default assumes an unobstructed site, and
    that "current position" remains the answer for anyone whose sky is not
    clear). ``min_altitude_deg`` is the only obstruction input there is.

    Pure: no I/O beyond the open ``ephemeris`` handle, and ``when`` is a
    parameter — never ``datetime.now()`` (CLAUDE.md invariant 3), timezone-aware
    UTC (invariant 5).
    """
    instant = _require_utc("when", when)
    if point_count < 3:
        raise DegenerateGeometryError(
            DegeneracyReason.TOO_FEW_POINTS,
            f"a sweep of {point_count} points cannot determine a circle; three is the minimum "
            "and the module docstring argues for four or five, since three report no residual",
        )
    step = float(step_deg)
    if step == 0.0 or abs(step) > 180.0:
        raise PolarAlignmentError(
            f"step_deg must be a non-zero rotation of at most 180 deg, got {step}: zero leaves "
            "every point on top of the last, and more than half a turn is not a separation"
        )
    span = abs(step) * (point_count - 1)
    if span >= 360.0:
        raise DegenerateGeometryError(
            DegeneracyReason.COINCIDENT_POINTS,
            f"{point_count} points {abs(step)} deg apart span {span:.0f} deg, a full turn or "
            "more: the arc closes on itself and later points land on earlier ones",
        )
    floor = float(min_altitude_deg)
    ceiling = float(max_meridian_altitude_deg)
    if not 0.0 <= floor < 90.0:
        raise PolarAlignmentError(
            f"min_altitude_deg must be in [0, 90), got {floor}; it is the lowest altitude any "
            "point of the sweep may reach"
        )
    if not floor < ceiling <= 90.0:
        raise PolarAlignmentError(
            f"max_meridian_altitude_deg must be above min_altitude_deg ({floor}) and at most "
            f"90, got {ceiling}"
        )
    if sweep_duration_s < 0.0:
        raise PolarAlignmentError(
            f"sweep_duration_s must not be negative, got {sweep_duration_s}: it is how long the "
            "run will take, and 0.0 means evaluate the whole arc at `when`"
        )

    # Raises if the site is inside 0.1 deg of a geographic pole, where azimuth
    # — and so the whole idea of an azimuth adjustment — has no meaning.
    pole_altitude, pole_azimuth = celestial_pole_altaz(site)
    latitude = float(site.latitude_deg)
    hemisphere = 1.0 if latitude >= 0.0 else -1.0
    colatitude = 90.0 - abs(latitude)

    # Hour angle of each point. `offset` below is the declination measured
    # toward the *visible* pole, so one set of formulae serves both hemispheres.
    drift = SIDEREAL_DEG_PER_HOUR * sweep_duration_s / 3600.0
    first_hour_angle = math.copysign(0.5 * span, step) - 0.5 * drift
    hour_angles = [
        first_hour_angle - index * step + drift * index / (point_count - 1)
        for index in range(point_count)
    ]
    worst_hour_angle = max(abs(value) for value in hour_angles)

    # sin(altitude) at the worst hour angle is `reach * sin(offset + phase)`, so
    # the whole floor question is one arcsine rather than a search.
    sine_latitude = math.sin(math.radians(abs(latitude)))
    cosine_term = math.cos(math.radians(abs(latitude))) * math.cos(math.radians(worst_hour_angle))
    reach = math.hypot(sine_latitude, cosine_term)
    phase = math.degrees(math.atan2(cosine_term, sine_latitude))
    needed = math.sin(math.radians(floor))
    if reach < needed:
        raise DegenerateGeometryError(
            DegeneracyReason.NO_CLEAR_ARC,
            f"no declination puts a {span:.0f} deg sweep above {floor:.1f} deg altitude at "
            f"latitude {latitude:.2f}: the highest its lowest point can reach is "
            f"{math.degrees(math.asin(reach)):.1f} deg. Use fewer points, a smaller separation, "
            "or a lower minimum altitude",
        )
    offset_floor = math.degrees(math.asin(needed / reach)) - phase
    offset_ceiling = ceiling - colatitude
    if offset_floor > offset_ceiling:
        raise DegenerateGeometryError(
            DegeneracyReason.NO_CLEAR_ARC,
            f"a {span:.0f} deg sweep at latitude {latitude:.2f} cannot clear {floor:.1f} deg "
            f"altitude without a declination {offset_floor:.1f} deg toward the pole, while "
            f"keeping the arc's centre under {ceiling:.1f} deg allows at most "
            f"{offset_ceiling:.1f} deg. Nearer the pole than that is nearer the zenith, where a "
            "turn about the tripod's vertical moves the boresight nowhere. Lower the minimum "
            "altitude, use fewer points or a smaller separation, or raise the ceiling "
            "deliberately",
        )
    # Nearest the celestial equator that satisfies both bounds.
    offset = max(offset_floor, min(0.0, offset_ceiling))

    coord = _sweep_start_coord(ephemeris, site, instant, hemisphere * offset, first_hour_angle)
    altitudes, azimuths, hour_angle = _planned_arc(
        ephemeris, site, coord, instant, point_count, step, sweep_duration_s
    )
    for _ in range(_SWEEP_FIELD_PASSES):
        lowest = min(float(value) for value in altitudes)
        shortfall = floor - lowest
        if shortfall <= 0.0:
            break
        # d(sin alt)/d(offset) over cos(alt): the local gain of pushing the
        # declination poleward, used to take the whole shortfall in one step.
        slope = (
            reach
            * math.cos(math.radians(offset + phase))
            / max(math.cos(math.radians(lowest)), 1e-9)
        )
        if slope < _MIN_ALTITUDE_SLOPE:
            raise DegenerateGeometryError(
                DegeneracyReason.NO_CLEAR_ARC,
                f"the lowest point of this sweep reaches {lowest:.2f} deg, under the requested "
                f"{floor:.1f} deg, and moving the declination no longer raises it: the arc is "
                "already at its own culmination there. Use fewer points or a smaller separation",
            )
        offset += shortfall / slope
        if offset > offset_ceiling:
            raise DegenerateGeometryError(
                DegeneracyReason.NO_CLEAR_ARC,
                f"clearing {floor:.1f} deg altitude on this arc needs declination "
                f"{offset:.1f} deg toward the pole, which carries its centre past the "
                f"{ceiling:.1f} deg ceiling. Lower the minimum altitude, use fewer points or a "
                "smaller separation, or raise the ceiling deliberately",
            )
        coord = _sweep_start_coord(ephemeris, site, instant, hemisphere * offset, first_hour_angle)
        altitudes, azimuths, hour_angle = _planned_arc(
            ephemeris, site, coord, instant, point_count, step, sweep_duration_s
        )
    else:
        stalled = min(float(value) for value in altitudes)
        if stalled < floor:
            raise DegenerateGeometryError(
                DegeneracyReason.NO_CLEAR_ARC,
                f"could not place a {span:.0f} deg sweep above {floor:.1f} deg altitude at "
                f"latitude {latitude:.2f} in {_SWEEP_FIELD_PASSES} passes; its lowest point "
                f"stalled at {stalled:.2f} deg",
            )

    declination = Deg(hemisphere * offset)
    meridian_altitude = altitude_deg(Deg(0.0), declination, site.latitude_deg)
    pole = _altaz_vector(pole_altitude, pole_azimuth)
    vectors = [
        _altaz_vector(altitude, azimuth)
        for altitude, azimuth in zip(altitudes, azimuths, strict=True)
    ]
    radius = Deg(sum(_angle_between_deg(pole, vector) for vector in vectors) / len(vectors))
    sweep_amplification = _noise_amplification(vectors, pole, radius)
    if sweep_amplification > max_noise_amplification:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            f"the best field available for this sweep would still amplify solve error by "
            f"{sweep_amplification:.0f}x in the fit itself (limit "
            f"{max_noise_amplification:.0f}x), so the sweep it planned would be refused when it was "
            f"fitted: the circle is {float(radius):.1f} deg of angular radius about the "
            "axis, which is too narrow to locate a centre. Use a lower minimum altitude, or a "
            "shape that does not need the declination pushed so far",
        )
    amplifications = [_pointing_amplification(pole, vector) for vector in vectors]
    worst_amplification = max(amplifications)
    if worst_amplification > max_noise_amplification:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            f"the best field available for this sweep still multiplies solve error by "
            f"{worst_amplification:.0f}x in the live refinement that follows it (limit "
            f"{max_noise_amplification:.0f}x): part of the arc sits near 90 deg of azimuth from "
            "the RA axis, or near the zenith. Use fewer points or a smaller separation",
        )

    warnings: list[str] = []
    if offset > 0.0:
        warnings.append(
            f"the celestial equator does not clear {floor:.1f} deg at latitude {latitude:.2f} "
            f"with a {span:.0f} deg sweep, so the declination is offset {offset:.2f} deg toward "
            f"the pole, moving the swept circle from 90 to {float(radius):.1f} deg of "
            f"angular radius about the axis"
        )
    elif offset < 0.0:
        warnings.append(
            f"the celestial equator culminates at {colatitude:.1f} deg here, above the "
            f"{ceiling:.1f} deg the live refinement loop is best below, so the declination is "
            f"offset {-offset:.2f} deg away from the pole, moving the swept circle from 90 to "
            f"{float(radius):.1f} deg of angular radius about the axis"
        )

    return SweepField(
        coord=coord,
        start_altitude_deg=altitudes[0],
        start_azimuth_deg=azimuths[0],
        start_hour_angle_deg=hour_angle,
        declination_offset_deg=Deg(offset),
        meridian_altitude_deg=meridian_altitude,
        arc_span_deg=Deg(span),
        point_altitudes_deg=tuple(altitudes),
        worst_altitude_deg=min(altitudes, key=float),
        circle_radius_deg=radius,
        sweep_amplification=sweep_amplification,
        refinement_amplification=amplifications[0],
        worst_refinement_amplification=worst_amplification,
        when=instant,
        site=site,
        ideal=not warnings,
        warnings=tuple(warnings),
    )


def solve_polar_alignment(
    ephemeris: Ephemeris,
    site: Site,
    points: Sequence[PolarPoint],
    *,
    apply_refraction: bool = True,
    pressure_mbar: float = STANDARD_PRESSURE_MBAR,
    temperature_c: float = STANDARD_TEMPERATURE_C,
    solve_sigma_arcsec: ArcSec = DEFAULT_SOLVE_SIGMA_ARCSEC,
    min_point_separation_deg: Deg = DEFAULT_MIN_POINT_SEPARATION_DEG,
    min_arc_span_deg: Deg = DEFAULT_MIN_ARC_SPAN_DEG,
    max_noise_amplification: float = DEFAULT_MAX_NOISE_AMPLIFICATION,
    max_residual_arcsec: ArcSec = DEFAULT_MAX_RESIDUAL_ARCSEC,
    max_axis_error_deg: Deg = DEFAULT_MAX_AXIS_ERROR_DEG,
) -> PolarAlignmentModel:
    """Fit the mount's RA axis to three or more solved positions.

    ``points`` are field centres solved after rotating the mount **in RA only**;
    order does not matter, and their ``date_obs`` may differ freely (each is
    projected into the horizontal frame at its own instant). ``site`` is the
    observer the horizontal frame is referred to — the mount's own site if it
    reports one (:func:`ssc.astro.frames.mount_site`), since that is the frame it
    is actually speaking.

    **More than three points improve the answer rather than being discarded.**
    The axis is the total-least-squares plane fit through the solved directions:
    the plane containing a small circle is normal to the circle's axis, so the
    normal of the best-fit plane *is* the best-fit axis, and every point pulls on
    it. (The estimator minimises perpendicular distance to the plane rather than
    angular distance to the circle; the two differ by a factor ``sin(radius)``
    that is common to every point, so the minimiser is the same to first order.
    The residuals reported are the exact angular ones.) **Four or five points is
    the right number**, not three: three fit a circle exactly, so a three-point
    run reports a residual of zero whatever went wrong.

    Refuses rather than guesses — see :class:`DegeneracyReason` for the seven ways
    and the ``min_*``/``max_*`` arguments for where each line is drawn. Every
    default is stated at its constant; pass your own if you know better.

    Raises :class:`PolarAlignmentError` for input that is wrong rather than
    merely difficult (a coordinate that is not J2000, a naive datetime, a site
    at a geographic pole), and :class:`DegenerateGeometryError` — which is one —
    for geometry that cannot support a fit.
    """
    if len(points) < 3:
        raise DegenerateGeometryError(
            DegeneracyReason.TOO_FEW_POINTS,
            f"polar alignment needs at least 3 solved positions, got {len(points)}: "
            "three is the minimum that determines a circle",
        )
    pole_altitude, pole_azimuth = celestial_pole_altaz(site)

    conditions = _Refraction(
        applied=apply_refraction, pressure_mbar=pressure_mbar, temperature_c=temperature_c
    )
    vectors = tuple(_horizontal_vector(ephemeris, site, point, conditions) for point in points)
    _check_distinct(vectors, min_point_separation_deg)

    axis = _fit_axis(vectors, _altaz_vector(pole_altitude, pole_azimuth))
    radii_deg = tuple(_angle_between_deg(axis, vector) for vector in vectors)
    circle_radius = Deg(sum(radii_deg) / len(radii_deg))
    residuals = tuple(deg_to_arcsec(Deg(radius - circle_radius)) for radius in radii_deg)

    arc_span = _arc_span_deg(axis, vectors)
    if arc_span < float(min_arc_span_deg):
        raise DegenerateGeometryError(
            DegeneracyReason.SHORT_ARC,
            f"the points span only {arc_span:.2f} deg of mount rotation about the fitted axis; "
            f"at least {float(min_arc_span_deg)} deg is needed before the circle's centre is "
            "determined by anything but noise. Rotate further in RA between captures",
        )

    amplification = _noise_amplification(vectors, axis, circle_radius)
    if amplification > max_noise_amplification:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            f"this geometry multiplies solve error by {amplification:.0f}x "
            f"(limit {max_noise_amplification:.0f}x): a {float(solve_sigma_arcsec)} arcsec solve "
            f"would place the axis to no better than {amplification * float(solve_sigma_arcsec) / 60.0:.1f} "
            "arcmin. Spread the points over a wider arc, or start from a field nearer the "
            "celestial equator",
        )

    residual_dof = len(points) - _PLANE_FIT_PARAMETERS
    rms_residual = (
        ArcSec(math.sqrt(sum(residual**2 for residual in residuals) / len(residuals)))
        if residual_dof > 0
        else None
    )
    if rms_residual is not None and float(rms_residual) > float(max_residual_arcsec):
        raise DegenerateGeometryError(
            DegeneracyReason.SCATTERED_POINTS,
            f"the solved positions miss a common small circle by "
            f"{float(rms_residual) / 60.0:.1f} arcmin "
            f"RMS (limit {float(max_residual_arcsec) / 60.0:.1f}): they did not come from a "
            "rotation about one axis. Check that only the RA axis moved and that each date_obs "
            "belongs to its own frame",
        )

    axis_altitude, axis_azimuth = _vector_altaz(axis)
    altitude_error = Deg(float(axis_altitude) - float(pole_altitude))
    azimuth_error = wrap_deg_180(float(axis_azimuth) - float(pole_azimuth))
    total_error = _angle_between_deg(axis, _altaz_vector(pole_altitude, pole_azimuth))
    if total_error > float(max_axis_error_deg):
        raise DegenerateGeometryError(
            DegeneracyReason.IMPLAUSIBLE_AXIS,
            f"the fitted axis is {total_error:.1f} deg from the celestial pole, past the "
            f"{float(max_axis_error_deg)} deg this will call a polar alignment error. Either the "
            "mount is not on a polar-aligned wedge at all, or these points did not come from an "
            "RA rotation",
        )

    return PolarAlignmentModel(
        axis_altitude_deg=axis_altitude,
        axis_azimuth_deg=axis_azimuth,
        altitude_error_deg=altitude_error,
        azimuth_error_deg=azimuth_error,
        azimuth_error_sky_deg=Deg(
            float(azimuth_error) * math.cos(math.radians(float(pole_altitude)))
        ),
        total_error_deg=Deg(total_error),
        circle_radius_deg=circle_radius,
        arc_span_deg=Deg(arc_span),
        rms_residual_arcsec=rms_residual,
        max_residual_arcsec=(
            ArcSec(max(abs(residual) for residual in residuals)) if residual_dof > 0 else None
        ),
        residual_dof=residual_dof,
        noise_amplification=amplification,
        axis_uncertainty_arcsec=ArcSec(amplification * float(solve_sigma_arcsec)),
        point_count=len(points),
        epoch=max(_require_utc("date_obs", point.when) for point in points),
        site=site,
        refraction_applied=apply_refraction,
        reported_offsets_arcsec=tuple(_reported_offset(ephemeris, site, point) for point in points),
    )


def refine_polar_alignment(
    ephemeris: Ephemeris,
    model: PolarAlignmentModel,
    reference: PolarPoint,
    resolved: PolarPoint,
    *,
    tracking: MountTracking,
    pressure_mbar: float = STANDARD_PRESSURE_MBAR,
    temperature_c: float = STANDARD_TEMPERATURE_C,
    solve_sigma_arcsec: ArcSec = DEFAULT_SOLVE_SIGMA_ARCSEC,
    max_step_deg: Deg = DEFAULT_MAX_REFINEMENT_STEP_DEG,
    max_axis_shift_deg: Deg = DEFAULT_MAX_AXIS_SHIFT_DEG,
    max_branch_separation_arcmin: ArcMin = DEFAULT_MAX_BRANCH_SEPARATION_ARCMIN,
    max_elapsed_seconds: float = DEFAULT_MAX_REFINEMENT_ELAPSED_S,
    max_noise_amplification: float = DEFAULT_MAX_NOISE_AMPLIFICATION,
    max_axis_error_deg: Deg = DEFAULT_MAX_AXIS_ERROR_DEG,
) -> PolarRefinement:
    """Update a swept axis from **one** new solve at an unchanged pointing.

    The loop this exists for: sweep once, park the mount, stop tracking, solve
    once to fix a reference, then solve again after every tripod nudge and watch
    both errors move — seconds per iteration, no slewing, hands never off the
    tripod. See the module docstring for the physics, for why the rotation is
    restricted to the tripod's own two adjustments, and for the one degree of
    freedom that restriction cannot see.

    ``model`` is the sweep's own result and is **never** replaced by a previous
    refinement's: each refinement is differential from the sweep, so chaining
    would re-add the reference solve's noise at every step. ``reference`` is a
    solve taken while the tripod was in the state ``model`` describes — most
    naturally the last point of the sweep itself, with the mount left where it
    stopped. ``resolved`` is the new solve of that same mechanical pointing.
    The site and the refraction setting are taken from ``model``, so a
    refinement cannot silently be evaluated in a different frame from the sweep
    that baselined it.

    Raises :class:`TrackingNotStoppedError` unless ``tracking`` is
    :attr:`MountTracking.STOPPED`, :class:`PolarAlignmentError` for input that is
    wrong (a non-J2000 coordinate, a naive datetime, solves out of order), and
    :class:`DegenerateGeometryError` for a move too large to be a tripod
    adjustment (:attr:`DegeneracyReason.STEP_TOO_LARGE`), a reference pointing
    that cannot separate a turn from a tilt
    (:attr:`DegeneracyReason.ILL_CONDITIONED`), or a refined axis too far from
    the pole to be an alignment error (:attr:`DegeneracyReason.IMPLAUSIBLE_AXIS`).

    A result that is merely *doubtful* is returned with
    :attr:`PolarRefinement.trusted` False and a populated
    :attr:`PolarRefinement.warnings` rather than refused (CLAUDE.md invariant 7).
    """
    if tracking is not MountTracking.STOPPED:
        raise TrackingNotStoppedError(
            f"refinement needs the drive halted, got tracking={tracking.value}: a tracking mount "
            "turns its own boresight 15 arcsec per second, which this would read as tripod "
            "motion and report as alignment change. Stop tracking, take a fresh reference solve, "
            "then refine"
        )

    site = model.site
    reference_when = _require_utc("reference date_obs", reference.when)
    resolved_when = _require_utc("date_obs", resolved.when)
    if resolved_when < reference_when:
        raise PolarAlignmentError(
            f"the re-solve ({resolved_when.isoformat()}) predates its reference "
            f"({reference_when.isoformat()}): the two are the wrong way round"
        )
    if resolved_when < model.epoch:
        raise PolarAlignmentError(
            f"the re-solve ({resolved_when.isoformat()}) predates the sweep it refines "
            f"({model.epoch.isoformat()}): a refinement measures what changed *after* the sweep"
        )

    conditions = _Refraction(
        applied=model.refraction_applied, pressure_mbar=pressure_mbar, temperature_c=temperature_c
    )
    reference_vector = _horizontal_vector(ephemeris, site, reference, conditions)
    resolved_vector = _horizontal_vector(ephemeris, site, resolved, conditions)
    boresight_shift = _angle_between_deg(reference_vector, resolved_vector)
    if boresight_shift > float(max_step_deg):
        raise DegenerateGeometryError(
            DegeneracyReason.STEP_TOO_LARGE,
            f"the re-solve is {boresight_shift:.2f} deg from its reference, past the "
            f"{float(max_step_deg)} deg this will read as a tripod adjustment. A slew looks "
            "exactly like this and cannot be told apart from one solved direction: if the mount "
            "moved, take a fresh reference; if it really was the tripod, sweep again",
        )

    axis = _altaz_vector(model.axis_altitude_deg, model.axis_azimuth_deg)
    hinge = _cross(_UP, axis)
    if math.sqrt(_dot(hinge, hinge)) < _MIN_HINGE_SINE:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            f"the RA axis is {90.0 - float(model.axis_altitude_deg):.3f} deg from the zenith, so "
            "the altitude hinge has no defined direction and a tilt cannot be separated from a "
            "turn. This is a site at a geographic pole, or an axis that is not an alignment error",
        )
    hinge = _normalise(hinge)
    roots = _tripod_rotation_roots(hinge, reference_vector, resolved_vector)
    if not roots:
        raise DegenerateGeometryError(
            DegeneracyReason.UNREACHABLE_MOVE,
            f"the re-solve sits at an altitude no tilt of this tripod can reach from the "
            f"reference: a tilt about the hinge spans a fixed range of altitudes and a turn "
            f"about the vertical changes none, and this misses that range by more than "
            f"{_MAX_UNREACHABLE_SINE * _ARCMIN_PER_RADIAN:.0f} arcmin. Something moved that is "
            "not the two adjustments — the tripod settled north-south, the mount slewed, or a "
            "date_obs belongs to a different frame",
        )
    plausible = tuple(
        root for root in roots if _tripod_rotation_magnitude_deg(*root) <= float(max_step_deg)
    )
    if not plausible:
        raise DegenerateGeometryError(
            DegeneracyReason.STEP_TOO_LARGE,
            f"the smallest tripod rotation that explains this re-solve is "
            f"{_tripod_rotation_magnitude_deg(*roots[0]):.1f} deg, past the "
            f"{float(max_step_deg)} deg this will read as an adjustment. If the mount slewed, "
            "take a fresh reference; if the tripod really moved that far, sweep again",
        )
    tilt, turn = plausible[0]
    moved_axis, axis_jacobian = _tripod_step(hinge, tilt, turn, axis)
    if len(plausible) > 1:
        rival, _ = _tripod_step(hinge, *plausible[1], axis)
        separation = _angle_between_deg(moved_axis, rival) * 60.0
        if separation > float(max_branch_separation_arcmin):
            first, second = _vector_altaz(moved_axis), _vector_altaz(rival)
            raise DegenerateGeometryError(
                DegeneracyReason.AMBIGUOUS_MOVE,
                f"two different tripod adjustments carry the reference to exactly this re-solve "
                f"— a {_tripod_rotation_magnitude_deg(tilt, turn):.2f} deg one putting the axis "
                f"at altitude {float(first[0]):.3f} azimuth {float(first[1]):.3f}, and a "
                f"{_tripod_rotation_magnitude_deg(*plausible[1]):.2f} deg one putting it at "
                f"{float(second[0]):.3f}/{float(second[1]):.3f}, {separation:.1f} arcmin apart. "
                "Both fit the solve perfectly, so this measurement cannot choose. The reference "
                "is near 90 deg of azimuth from the RA axis, which is where the second root "
                "stops being absurd: re-point nearer the meridian and take a fresh reference",
            )
    _, jacobian = _tripod_step(hinge, tilt, turn, reference_vector)
    amplification = _refinement_amplification(jacobian, axis_jacobian)
    if amplification > max_noise_amplification:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            f"this reference pointing multiplies solve error by {amplification:.0f}x "
            f"(limit {max_noise_amplification:.0f}x): it sits close to 90 deg of azimuth from "
            "the RA axis — near due east or west — where a turn about the vertical and a tilt "
            "about the hinge move the boresight along the same line and cannot be told apart. "
            "Re-point nearer the meridian and take a fresh reference",
        )

    pole_altitude, pole_azimuth = celestial_pole_altaz(site)
    axis_altitude, axis_azimuth = _vector_altaz(moved_axis)
    azimuth_error = wrap_deg_180(float(axis_azimuth) - float(pole_azimuth))
    total_error = _angle_between_deg(moved_axis, _altaz_vector(pole_altitude, pole_azimuth))
    if total_error > float(max_axis_error_deg):
        raise DegenerateGeometryError(
            DegeneracyReason.IMPLAUSIBLE_AXIS,
            f"the refined axis is {total_error:.1f} deg from the celestial pole, past the "
            f"{float(max_axis_error_deg)} deg this will call a polar alignment error. Either the "
            "mount slewed between the reference and this solve, or the tripod was moved far "
            "enough that the sweep no longer describes it",
        )

    refinement_uncertainty = ArcSec(amplification * float(solve_sigma_arcsec) * math.sqrt(2.0))
    axis_shift = _angle_between_deg(axis, moved_axis)
    elapsed = (resolved_when - reference_when).total_seconds()
    sweep_age = (resolved_when - model.epoch).total_seconds()

    warnings: list[str] = [
        "a single solved field cannot verify the RA axis after physical movement; "
        "run a new multi-point sweep before using this correction"
    ]
    if axis_shift > float(max_axis_shift_deg):
        warnings.append(
            f"the axis has travelled {axis_shift:.2f} deg from the sweep that baselined it "
            f"(limit {float(max_axis_shift_deg)} deg); run a fresh sweep to re-baseline"
        )
    if boresight_shift > float(DEFAULT_MAX_BORESIGHT_SHIFT_DEG):
        warnings.append(
            f"the reference field moved {boresight_shift:.2f} deg (limit "
            f"{float(DEFAULT_MAX_BORESIGHT_SHIFT_DEG)} deg); a single solve cannot verify "
            "whether this was wedge motion, tripod settling, or a mount slew. "
            "Run a fresh sweep before using this correction"
        )
    if boresight_shift > 0.5 * float(max_step_deg):
        warnings.append(
            f"the re-solve is {boresight_shift:.2f} deg from its reference, a large move for a "
            "tripod adjustment; if the mount slewed instead, this number is meaningless"
        )
    if elapsed > max_elapsed_seconds:
        warnings.append(
            f"the reference solve is {elapsed / 60.0:.0f} minutes old (limit "
            f"{max_elapsed_seconds / 60.0:.0f}); it may predate a slew this cannot see"
        )

    return PolarRefinement(
        model=replace(
            model,
            axis_altitude_deg=axis_altitude,
            axis_azimuth_deg=axis_azimuth,
            altitude_error_deg=Deg(float(axis_altitude) - float(pole_altitude)),
            azimuth_error_deg=azimuth_error,
            azimuth_error_sky_deg=Deg(
                float(azimuth_error) * math.cos(math.radians(float(pole_altitude)))
            ),
            total_error_deg=Deg(total_error),
            axis_uncertainty_arcsec=ArcSec(
                math.hypot(float(model.axis_uncertainty_arcsec), float(refinement_uncertainty))
            ),
            epoch=resolved_when,
        ),
        altitude_step_deg=Deg(float(axis_altitude) - float(model.axis_altitude_deg)),
        azimuth_step_deg=wrap_deg_180(float(axis_azimuth) - float(model.axis_azimuth_deg)),
        axis_shift_deg=Deg(axis_shift),
        boresight_shift_deg=Deg(boresight_shift),
        elapsed_seconds=elapsed,
        sweep_age_seconds=sweep_age,
        refinement_uncertainty_arcsec=refinement_uncertainty,
        noise_amplification=amplification,
        trusted=not warnings,
        warnings=tuple(warnings),
    )


# --- into the Earth-fixed frame --------------------------------------------


@dataclass(frozen=True, slots=True)
class _Refraction:
    """Saemundsson's formula (Meeus 16.4), the *true altitude -> apparent* form.

    The direction matters and the two standard formulae differ: Bennett's takes
    an apparent altitude and removes refraction, Saemundsson's takes a true one
    and adds it. Here the solve gives a true (catalogue) altitude and the tube
    pointed at the apparent one, so it is Saemundsson's, added.

    Below the horizon the formula's argument turns over and the value is
    meaningless; points that low are refused before this is reached.
    """

    applied: bool
    pressure_mbar: float
    temperature_c: float

    def at(self, true_altitude: Deg) -> Deg:
        if not self.applied:
            return Deg(0.0)
        altitude = float(true_altitude)
        arcmin = 1.02 / math.tan(math.radians(altitude + 10.3 / (altitude + 5.11)))
        arcmin *= (self.pressure_mbar / STANDARD_PRESSURE_MBAR) * (
            283.0 / (273.0 + self.temperature_c)
        )
        return arcmin_to_deg(ArcMin(arcmin))


def _horizontal_vector(
    ephemeris: Ephemeris, site: Site, point: PolarPoint, refraction: _Refraction
) -> _Vector:
    """One solved J2000 position as the unit vector the tube was aimed along.

    Four steps, each of which is wrong to skip: apparent place of date (so the
    ~20 arcmin of precession is gone), hour angle against Greenwich *apparent*
    sidereal time (apparent RA demands apparent sidereal time; the equation of
    the equinoxes reaches 1.1 s, which is 16 arcsec of hour angle), the textbook
    rotation into altitude and azimuth, and finally refraction — because the
    solve says where the star is and the fit needs to know where the tube was.
    """
    if point.coord.frame is not MountFrame.J2000:
        raise PolarAlignmentError(
            f"a solved position must be J2000, got {point.coord.frame.value}; plate solves "
            "always are (ssc.platesolve.base.PlateSolveResult), so this is a mislabelled input"
        )
    when = _require_utc("date_obs", point.when)
    apparent = to_mount_frame(ephemeris, site, point.coord, when, MountFrame.TOPOCENTRIC)
    time: Any = ephemeris.timescale.from_datetime(when)
    local_sidereal_deg = float(hours_to_deg(Hours(float(time.gast)))) + float(site.longitude_deg)
    hour_angle = wrap_deg_180(local_sidereal_deg - float(apparent.ra_deg))
    altitude = altitude_deg(hour_angle, apparent.dec_deg, site.latitude_deg)
    if float(altitude) < _MIN_POINT_ALTITUDE_DEG:
        raise DegenerateGeometryError(
            DegeneracyReason.BELOW_HORIZON,
            f"a solved position sits {float(altitude):.2f} deg below the horizon at "
            f"{float(site.latitude_deg)}, {float(site.longitude_deg)} on {when.isoformat()}: no "
            "frame was exposed there. Check the site (longitude is positive east) and date_obs",
        )
    return _altaz_vector(
        Deg(float(altitude) + float(refraction.at(altitude))),
        azimuth_deg(hour_angle, apparent.dec_deg, site.latitude_deg),
    )


def _local_apparent_sidereal_deg(ephemeris: Ephemeris, site: Site, when: datetime) -> float:
    """Local **apparent** sidereal time in degrees — GAST plus east longitude.

    Apparent, not mean: the whole chain here works in the true equinox of date,
    and the equation of the equinoxes reaches 1.1 s, which is 16 arcsec of hour
    angle. :func:`_horizontal_vector` computes the same quantity inline on the
    measurement path; this is the planning path's copy.
    """
    time: Any = ephemeris.timescale.from_datetime(when)
    return float(hours_to_deg(Hours(float(time.gast)))) + float(site.longitude_deg)


def _sweep_start_coord(
    ephemeris: Ephemeris, site: Site, when: datetime, declination: float, hour_angle: float
) -> EquatorialCoord:
    """The J2000 coordinate of an apparent (hour angle, declination) at ``when``.

    The geometry is chosen in the apparent frame — that is the frame altitude
    and azimuth are defined in — and handed back as J2000, which is what
    :class:`PolarPoint` and every mount target in SSC speak.
    """
    apparent = EquatorialCoord(
        ra_deg=wrap_deg_360(_local_apparent_sidereal_deg(ephemeris, site, when) - hour_angle),
        dec_deg=Deg(declination),
        frame=MountFrame.TOPOCENTRIC,
    )
    return to_j2000(ephemeris, site, apparent, when)


def j2000_coord_at_hour_angle(
    ephemeris: Ephemeris,
    site: Site,
    when: datetime,
    *,
    declination_deg: Deg,
    hour_angle_deg: Deg,
) -> EquatorialCoord:
    """Return the J2000 target for an apparent local hour angle/declination.

    Field presets are naturally specified in the local apparent frame, where
    their altitude and azimuth stay meaningful at every observing time. Mount
    slews use J2000, so this is the public planning boundary between them.
    """
    return _sweep_start_coord(
        ephemeris,
        site,
        when,
        float(declination_deg),
        float(hour_angle_deg),
    )


def _planned_arc(
    ephemeris: Ephemeris,
    site: Site,
    start: EquatorialCoord,
    when: datetime,
    point_count: int,
    step: float,
    duration_s: float,
) -> tuple[list[Deg], list[Deg], Deg]:
    """Altitude and azimuth of every point a sweep from ``start`` will visit.

    The targets are built the way the caller will build them — **J2000** right
    ascension stepped by ``step``, J2000 declination held — and then carried
    into the horizontal frame by the same chain :func:`_horizontal_vector`
    takes, minus refraction (this is where the tube will be aimed, not where the
    light came from). Stepping J2000 rather than apparent right ascension is not
    the same circle: the two poles are ~20 arcmin apart, which tilts the arc and
    moves its lowest point by up to ~0.2 deg. Measuring it here rather than
    assuming it is what lets the floor be enforced on the real geometry.
    """
    altitudes: list[Deg] = []
    azimuths: list[Deg] = []
    first_hour_angle = Deg(0.0)
    for index in range(point_count):
        moment = when + timedelta(seconds=duration_s * index / (point_count - 1))
        target = EquatorialCoord(
            ra_deg=wrap_deg_360(float(start.ra_deg) + index * step),
            dec_deg=start.dec_deg,
            frame=MountFrame.J2000,
        )
        apparent = to_mount_frame(ephemeris, site, target, moment, MountFrame.TOPOCENTRIC)
        hour_angle = wrap_deg_180(
            _local_apparent_sidereal_deg(ephemeris, site, moment) - float(apparent.ra_deg)
        )
        altitudes.append(altitude_deg(hour_angle, apparent.dec_deg, site.latitude_deg))
        azimuths.append(azimuth_deg(hour_angle, apparent.dec_deg, site.latitude_deg))
        if index == 0:
            first_hour_angle = hour_angle
    return altitudes, azimuths, first_hour_angle


def _pointing_amplification(axis: _Vector, boresight: _Vector) -> float:
    """:attr:`PolarRefinement.noise_amplification` for a reference at ``boresight``.

    The same two Jacobians :func:`refine_polar_alignment` builds, evaluated at
    the *unmoved* tripod (tilt and turn both zero) because this asks what the
    geometry would cost, not what a particular adjustment did. ``inf`` when the
    axis is at the zenith, where the altitude hinge has no direction.
    """
    hinge = _cross(_UP, axis)
    if math.sqrt(_dot(hinge, hinge)) < _MIN_HINGE_SINE:
        return math.inf
    hinge = _normalise(hinge)
    _, jacobian = _tripod_step(hinge, 0.0, 0.0, boresight)
    _, axis_jacobian = _tripod_step(hinge, 0.0, 0.0, axis)
    return _refinement_amplification(jacobian, axis_jacobian)


def _reported_offset(ephemeris: Ephemeris, site: Site, point: PolarPoint) -> ArcSec | None:
    """The mount's belief against the solve, arcsec. Recorded, never fitted.

    Compared in J2000, so a mount declaring ``equTopocentric`` is converted
    first: comparing across frames here would report the frame gap (~20') as
    pointing error, which is the exact confusion ``frames.py`` exists to end.
    """
    if point.reported is None:
        return None
    when = _require_utc("date_obs", point.when)
    return frame_offset_arcsec(to_j2000(ephemeris, site, point.reported, when), point.coord)


# --- the fit ---------------------------------------------------------------


def _fit_axis(vectors: Sequence[_Vector], pole: _Vector) -> _Vector:
    """Total-least-squares plane through the points; its normal is the axis.

    The normal is the eigenvector of the scatter matrix with the smallest
    eigenvalue — the direction in which the points vary least, which for points
    on a small circle is the circle's own axis. With exactly three points the
    plane is exact and this agrees with the analytic circumcentre to machine
    precision (asserted in the tests, against an independent implementation).

    An eigenvector has no sign, so it is oriented toward the celestial pole:
    the mount aims at the pole of its own hemisphere, and past 90 degrees of
    error the choice would be genuinely ambiguous — which is why
    :data:`DEFAULT_MAX_AXIS_ERROR_DEG` stops well short of it.
    """
    points = np.array(vectors, dtype=float)
    centred = points - points.mean(axis=0)
    _, eigenvectors = np.linalg.eigh(centred.T @ centred)
    normal = eigenvectors[:, 0]
    if float(np.dot(normal, np.array(pole, dtype=float))) < 0.0:
        normal = -normal
    return _normalise((float(normal[0]), float(normal[1]), float(normal[2])))


def _noise_amplification(vectors: Sequence[_Vector], axis: _Vector, radius: Deg) -> float:
    """Arcsec of axis error per arcsec of independent per-point solve error.

    Linearised least squares, exactly the fit above: with the axis written as
    ``n + d1*t1 + d2*t2`` for a tangent basis ``(t1, t2)``, the residual of point
    ``i`` is ``u_i . n - c`` and its derivatives are ``(u_i . t1, u_i . t2, -1)``.
    The parameter covariance is then ``sigma_r**2 (J^T J)^-1``, and a point
    displaced by ``sigma`` on the sky moves its residual by ``sigma * sin(radius)``
    — the component of the axis lying in that point's tangent plane. So the
    axis's own 1-sigma is ``sigma * sin(radius) * sqrt(A11 + A22)``, and the
    factor multiplying ``sigma`` is pure geometry.

    Sanity check that fixes the scale: three points 120 deg apart on a great
    circle give 1.15 — the axis is known slightly *worse* than one point, since
    three noisy points must agree on a plane. A 60 deg total arc gives ~9, a
    30 deg arc ~46. That steepness is why short arcs are refused.
    """
    first, second = _tangent_basis(axis)
    jacobian = np.array(
        [[_dot(vector, first), _dot(vector, second), -1.0] for vector in vectors], dtype=float
    )
    normal_matrix = jacobian.T @ jacobian
    try:
        covariance = np.linalg.inv(normal_matrix)
    except np.linalg.LinAlgError:
        return math.inf
    variance = float(covariance[0, 0] + covariance[1, 1])
    if not math.isfinite(variance) or variance < 0.0:
        return math.inf
    return math.sin(math.radians(float(radius))) * math.sqrt(variance)


def _arc_span_deg(axis: _Vector, vectors: Sequence[_Vector]) -> float:
    """How much mount rotation the points cover, degrees in [0, 360).

    The smallest arc containing every point: sort the rotation angles about the
    axis, and take 360 minus the widest empty gap. Using max-minus-min instead
    would report 350 deg for two points 10 deg apart across the wrap.
    """
    first, second = _tangent_basis(axis)
    angles = sorted(
        wrap_deg_360(math.degrees(math.atan2(_dot(vector, second), _dot(vector, first))))
        for vector in vectors
    )
    gaps = [after - before for before, after in pairwise(angles)]
    gaps.append(360.0 - angles[-1] + angles[0])
    return 360.0 - max(gaps)


def _check_distinct(vectors: Sequence[_Vector], minimum_deg: Deg) -> None:
    for index, vector in enumerate(vectors):
        for other_index, other in enumerate(vectors[index + 1 :], start=index + 1):
            separation = _angle_between_deg(vector, other)
            if separation < float(minimum_deg):
                raise DegenerateGeometryError(
                    DegeneracyReason.COINCIDENT_POINTS,
                    f"solved positions {index} and {other_index} are {separation * 60.0:.1f} "
                    f"arcmin apart, under the {float(minimum_deg) * 60.0:.0f} arcmin minimum: "
                    "the mount did not move between them, or the same frame was solved twice",
                )


# --- the tripod rotation ---------------------------------------------------


def _tripod_step(
    hinge: _Vector, tilt_rad: float, turn_rad: float, vector: _Vector
) -> tuple[_Vector, np.ndarray]:
    """Where ``vector`` lands after one tripod adjustment, and its derivatives.

    Two rotations, in the tripod's own order: tilt about the altitude ``hinge``
    (horizontal, across the axis's meridian), then turn about the local
    vertical. The order is not cosmetic — the azimuth stage sits *under* the
    altitude hinge, so turning it carries the hinge along, which is why ``hinge``
    is the pre-adjustment one and the tilt is applied first.

    Returns the landing direction and the 3x2 Jacobian
    ``d(landing)/d(tilt, turn)``: the tilt column is the turn applied to
    ``hinge x tilted``, the turn column is ``up x landed``. Both are exact, so
    the Gauss-Newton below converges quadratically and the same call serves to
    move the axis and to size the noise.
    """
    tilted = _rotate(hinge, tilt_rad, vector)
    landed = _rotate(_UP, turn_rad, tilted)
    return landed, np.array(
        [_rotate(_UP, turn_rad, _cross(hinge, tilted)), _cross(_UP, landed)], dtype=float
    ).T


def _signed_angle_about(axis: _Vector, start: _Vector, end: _Vector) -> float:
    """Signed rotation about a unit ``axis`` carrying ``start`` onto ``end``, radians.

    Both are projected onto the plane normal to ``axis`` first. Undefined when
    either projection collapses — a direction lying along the axis is not
    carried anywhere by a rotation about it, so the angle would be arbitrary and
    is refused rather than defaulted to zero.
    """

    def perpendicular(vector: _Vector) -> _Vector:
        along = _dot(axis, vector)
        return (
            vector[0] - along * axis[0],
            vector[1] - along * axis[1],
            vector[2] - along * axis[2],
        )

    first, second = perpendicular(start), perpendicular(end)
    if min(_dot(first, first), _dot(second, second)) < _MIN_HINGE_SINE**2:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED,
            "the reference or the re-solve lies along one of the tripod's own rotation axes, "
            "so that rotation moves it nowhere and cannot be measured from it. Re-point away "
            "from the zenith and from due east/west, and take a fresh reference",
        )
    return math.atan2(_dot(_cross(first, second), axis), _dot(first, second))


def _tripod_rotation_magnitude_deg(tilt_rad: float, turn_rad: float) -> float:
    """Total rotation angle of ``R_z(turn) R_hinge(tilt)``, degrees in [0, 180].

    The two axes are perpendicular (the hinge is horizontal, the turn is about
    the vertical), so the quaternion product's scalar part collapses to
    ``cos(tilt/2) cos(turn/2)`` and the angle follows exactly. This is the size
    of the tripod move, which is *not* the same as how far the boresight went:
    a rotation can move any given direction by anything from nothing up to its
    own angle, and telling a 2-degree adjustment from a 177-degree one is what
    resolves the two-root ambiguity below.
    """
    scalar = math.cos(0.5 * tilt_rad) * math.cos(0.5 * turn_rad)
    return math.degrees(2.0 * math.acos(max(-1.0, min(1.0, abs(scalar)))))


def _tripod_rotation_roots(
    hinge: _Vector, reference: _Vector, resolved: _Vector
) -> tuple[tuple[float, float], ...]:
    """**Every** (tilt, turn) that carries ``reference`` exactly onto ``resolved``.

    Closed form, and there is more than one — which is the whole point, and what
    an iterative fit hides. Write ``v = R_hinge(tilt) reference`` for the
    intermediate direction. A tilt about the hinge preserves the hinge
    component, so ``v . hinge = reference . hinge``; a turn about the vertical
    preserves altitude, so ``v . up = resolved . up``. Those are two planes, and
    ``v`` is where their line of intersection meets the unit sphere: **zero, one
    or two points**, exactly like two circles drawn on a globe.

    The hinge is horizontal, so ``hinge . up`` is zero and ``{hinge, up,
    hinge x up}`` is orthonormal; in it ``v = c1 hinge + c2 up + s (hinge x
    up)`` with ``s**2 = 1 - c1**2 - c2**2``. Each root gives its tilt as the
    signed angle from ``reference`` to ``v`` about the hinge, and its turn as
    the signed angle from ``v`` to ``resolved`` about the vertical.

    Returns them in ascending order of rotation magnitude. Empty when the
    discriminant is negative by more than solve noise could explain: no tilt of
    this tripod can reach the observed altitude at all, so whatever moved was
    not the two knobs.
    """
    across = _cross(hinge, _UP)
    hinge_component = _dot(reference, hinge)
    altitude_component = _dot(resolved, _UP)
    discriminant = 1.0 - hinge_component**2 - altitude_component**2
    if discriminant < -(_MAX_UNREACHABLE_SINE**2):
        return ()
    offset = math.sqrt(max(discriminant, 0.0))
    roots: list[tuple[float, float]] = []
    for sign in (1.0, -1.0):
        intermediate: _Vector = (
            hinge_component * hinge[0] + altitude_component * _UP[0] + sign * offset * across[0],
            hinge_component * hinge[1] + altitude_component * _UP[1] + sign * offset * across[1],
            hinge_component * hinge[2] + altitude_component * _UP[2] + sign * offset * across[2],
        )
        root = (
            _signed_angle_about(hinge, reference, intermediate),
            _signed_angle_about(_UP, intermediate, resolved),
        )
        if offset == 0.0 and roots:
            break
        roots.append(root)
    return tuple(sorted(roots, key=lambda root: _tripod_rotation_magnitude_deg(*root)))


def _refinement_amplification(jacobian: np.ndarray, axis_jacobian: np.ndarray) -> float:
    """Arcsec of refined-axis error per arcsec of one solve's error.

    Same linearised least squares as :func:`_noise_amplification`, one step
    longer: ``jacobian`` maps (tilt, turn) onto the observed boresight, so the
    parameter covariance is ``sigma**2 (J^T J)^-1``; ``axis_jacobian`` maps the
    same two parameters onto the *axis*, so the axis covariance is
    ``G (J^T J)^-1 G^T sigma**2`` and its trace is the squared position error.
    Pure geometry — the sigma cancels out of what is returned.

    1.2 for a reference on the meridian, which is *better* than the ~9 of a
    60-degree sweep: the whole rotation is seen in one direction rather than
    inferred from the curvature of a short arc. It reaches 65x at due east or
    west and diverges at 90 degrees of azimuth from the axis itself.
    """
    try:
        covariance = np.linalg.inv(jacobian.T @ jacobian)
    except np.linalg.LinAlgError:
        return math.inf
    variance = float(np.trace(axis_jacobian @ covariance @ axis_jacobian.T))
    if not math.isfinite(variance) or variance < 0.0:
        return math.inf
    return math.sqrt(variance)


# --- vectors ---------------------------------------------------------------
#
# x north, y east, z up. Stated here and true everywhere in this module.


#: Local vertical. The azimuth adjustment's rotation axis, and half of the
#: two-parameter tripod family in :func:`refine_polar_alignment`.
_UP: Final[_Vector] = (0.0, 0.0, 1.0)


def _rotate(axis: _Vector, angle_rad: float, vector: _Vector) -> _Vector:
    """Rodrigues, right-handed about a **unit** ``axis``."""
    cos, sin = math.cos(angle_rad), math.sin(angle_rad)
    cross = _cross(axis, vector)
    projection = _dot(axis, vector) * (1.0 - cos)
    return (
        vector[0] * cos + cross[0] * sin + axis[0] * projection,
        vector[1] * cos + cross[1] * sin + axis[1] * projection,
        vector[2] * cos + cross[2] * sin + axis[2] * projection,
    )


def _altaz_vector(altitude: Deg, azimuth: Deg) -> _Vector:
    altitude_rad = math.radians(float(altitude))
    azimuth_rad = math.radians(float(azimuth))
    return (
        math.cos(altitude_rad) * math.cos(azimuth_rad),
        math.cos(altitude_rad) * math.sin(azimuth_rad),
        math.sin(altitude_rad),
    )


def _vector_altaz(vector: _Vector) -> tuple[Deg, Deg]:
    north, east, up = vector
    return (
        Deg(math.degrees(math.atan2(up, math.hypot(north, east)))),
        wrap_deg_360(math.degrees(math.atan2(east, north))),
    )


def _tangent_basis(vector: _Vector) -> tuple[_Vector, _Vector]:
    """Two unit vectors spanning the plane normal to ``vector``.

    The helper axis is the one least aligned with ``vector``, so the cross
    product is never near zero — the orientation of the pair is arbitrary and
    every use here is invariant to it.
    """
    helper: _Vector = (0.0, 0.0, 1.0) if abs(vector[2]) < 0.9 else (1.0, 0.0, 0.0)
    first = _normalise(_cross(helper, vector))
    return first, _cross(vector, first)


def _dot(first: _Vector, second: _Vector) -> float:
    return first[0] * second[0] + first[1] * second[1] + first[2] * second[2]


def _cross(first: _Vector, second: _Vector) -> _Vector:
    return (
        first[1] * second[2] - first[2] * second[1],
        first[2] * second[0] - first[0] * second[2],
        first[0] * second[1] - first[1] * second[0],
    )


def _normalise(vector: _Vector) -> _Vector:
    norm = math.sqrt(_dot(vector, vector))
    if norm == 0.0:
        raise DegenerateGeometryError(
            DegeneracyReason.ILL_CONDITIONED, "a direction collapsed to the zero vector"
        )
    return (vector[0] / norm, vector[1] / norm, vector[2] / norm)


def _angle_between_deg(first: _Vector, second: _Vector) -> float:
    """``atan2(|a x b|, a . b)`` — keeps its digits at the arcsecond separations
    the residuals are measured at, where ``acos`` of a dot product has none left."""
    return math.degrees(
        math.atan2(
            math.sqrt(_dot(_cross(first, second), _cross(first, second))), _dot(first, second)
        )
    )
