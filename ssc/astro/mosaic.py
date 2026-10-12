# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Panel tiling for a mosaic session (DESIGN.md 3.3, the ``mosaic`` field of a
``TargetIntent`` in DESIGN.md 3.4).

A mosaic is a ``ra_num x dec_num`` grid of panels centred on one target, each
panel the size of one frame (from :func:`ssc.astro.framing.field_geometry`,
reused rather than recomputing plate scale) with a requested overlap between
neighbours.

**RA convergence.** Panels are laid out in equatorial coordinates, the way the
planner drags a grid over the map. A degree of RA is a shorter arc on the sky
the closer a row is to the pole — circles of constant declination shrink by
``cos(delta)`` — so holding the RA *step in degrees* constant would leave
gaps at the equator and overlapping, wasted panels near the pole. Each row's
RA step is therefore ``frame_width / cos(row_declination)``, using that row's
own declination (not the mosaic centre's), so convergence is correct panel by
panel as the grid climbs toward a pole.

**Alt-az compounding.** DESIGN.md 3.3 warns that each panel rotates too, and
mosaics plus field rotation compound badly: this module never assumes a
panel's full frame survives an alt-az session. Pass a ``center_session`` and
it derives every panel's own :class:`~ssc.astro.framing.Session` and reuses
:func:`ssc.astro.framing.session_envelope` to compute that panel's real
keep-region; without one, every panel's ``envelope`` is ``None`` and the
returned plan says so, rather than staying silent about the omission.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ssc.astro.framing import (
    DEFAULT_PA_STEP_DEG,
    FieldGeometry,
    MountMode,
    Session,
    SessionEnvelope,
    session_envelope,
)
from ssc.astro.units import Deg, signed_delta_deg, wrap_deg_180, wrap_deg_360

__all__ = [
    "MosaicPanel",
    "MosaicPlan",
    "MosaicSpec",
    "PanelCenter",
    "mosaic_panel_centers",
    "mosaic_plan",
    "sky_tile_centers_deg",
]

#: Rotation severities in worst-first order, matching
#: :func:`ssc.astro.framing._severity`'s vocabulary ("none" / "amber" / "red").
_SEVERITY_RANK = {"none": 0, "amber": 1, "red": 2}


@dataclass(frozen=True, slots=True)
class MosaicSpec:
    """The ``mosaic`` object of a ``TargetIntent`` (DESIGN.md 3.4)."""

    ra_num: int
    dec_num: int
    overlap_pct: float

    def __post_init__(self) -> None:
        if self.ra_num < 1:
            raise ValueError(f"ra_num must be at least 1, got {self.ra_num}")
        if self.dec_num < 1:
            raise ValueError(f"dec_num must be at least 1, got {self.dec_num}")
        if not 0.0 <= self.overlap_pct < 100.0:
            raise ValueError(f"overlap_pct must be in [0, 100), got {self.overlap_pct}")

    @property
    def panel_count(self) -> int:
        return self.ra_num * self.dec_num


@dataclass(frozen=True, slots=True)
class PanelCenter:
    """One panel's grid position and sky centre."""

    row: int  # dec index, 0 at the south edge of the grid
    col: int  # ra index, 0 at the east edge of the grid (see mosaic_panel_centers)
    ra_j2000_deg: Deg
    dec_j2000_deg: Deg


def _offsets(count: int, step: float) -> list[float]:
    """Symmetric offsets ``(i - (count - 1) / 2) * step`` for ``i in range(count)``.

    Centred on 0 regardless of parity, so the requested centre is the mosaic
    centre whether the grid is 1x1, 2x2 or 3x4.
    """
    midpoint = (count - 1) / 2.0
    return [(i - midpoint) * step for i in range(count)]


def mosaic_panel_centers(
    ra_center_deg: Deg,
    dec_center_deg: Deg,
    geometry: FieldGeometry,
    spec: MosaicSpec,
) -> tuple[PanelCenter, ...]:
    """The ``ra_num x dec_num`` grid of panel centres, RA-convergence corrected.

    Pure geometry: no ephemeris, no rotation. RA is wrapped to [0, 360) so a
    mosaic that straddles 0h/24h (RA wrap) still tiles as one contiguous grid
    instead of jumping across the sky. Raises if a row's declination would put
    it at or beyond a geographic-style pole (|dec| >= 90 - epsilon), where the
    RA circle degenerates and a step size in RA is not a finite angle.
    """
    if not -90.0 <= float(dec_center_deg) <= 90.0:
        raise ValueError(f"dec_center_deg out of range: {dec_center_deg}")

    step_fraction = 1.0 - spec.overlap_pct / 100.0
    dec_step = float(geometry.height_deg) * step_fraction
    ra_step_base = float(geometry.width_deg) * step_fraction

    dec_offsets = _offsets(spec.dec_num, dec_step)
    ra_offsets = list(reversed(_offsets(spec.ra_num, ra_step_base)))

    panels: list[PanelCenter] = []
    for row, dec_offset in enumerate(dec_offsets):
        panel_dec = float(dec_center_deg) + dec_offset
        if not -90.0 <= panel_dec <= 90.0:
            raise ValueError(
                f"mosaic row {row} declination {panel_dec:.3f} deg is out of range; this grid "
                "is too tall for its declination centre"
            )
        cos_dec = math.cos(math.radians(panel_dec))
        if abs(cos_dec) < 1e-9:
            raise ValueError(
                f"mosaic row {row} sits at declination {panel_dec:.3f} deg, within a hair of "
                "the celestial pole, where the RA circle degenerates and a panel's RA step is "
                "not a finite angle"
            )
        for col, ra_offset in enumerate(ra_offsets):
            panel_ra = wrap_deg_360(float(ra_center_deg) + ra_offset / cos_dec)
            panels.append(
                PanelCenter(
                    row=row,
                    col=col,
                    ra_j2000_deg=panel_ra,
                    dec_j2000_deg=Deg(panel_dec),
                )
            )
    return tuple(panels)


def sky_tile_centers_deg(
    ra_center_deg: float, dec_center_deg: float, n: int, tile_size_deg: float
) -> tuple[tuple[Deg, Deg], ...]:
    """An ``n x n`` grid of tile centres, each ``tile_size_deg`` square,
    tiling an ``n*tile_size_deg`` square footprint centred on
    ``(ra_center_deg, dec_center_deg)`` edge to edge — no gap, no overlap.
    Row-major order: index ``row * n + col``, ``row`` 0 at the south edge,
    ``col`` 0 at the west edge. This cutout grid retains its spatial tile order;
    telescope mosaic panels are numbered east to west (left to right on the sky map).

    Same RA-convergence geometry as :func:`mosaic_panel_centers` (a degree of
    RA is a shorter arc the closer a row is to the pole, so each row's RA
    step is corrected by that row's own declination via ``cos(dec)``), reused
    here for a plain spatial grid rather than telescope-frame panels — this
    is why it takes a size directly instead of a :class:`FieldGeometry`
    (CLAUDE.md invariant 3: ``ssc.skymap``'s unWISE cutout tiling calls this
    rather than re-deriving the ``cos(dec)`` correction itself).

    Unlike :func:`mosaic_panel_centers`, a row too close to a pole is not
    refused: a mosaic panel grid is a deliberate telescope framing choice
    that should fail loudly if it cannot be honoured, but a cutout tile grid
    is generated automatically for whatever declination a target happens to
    sit at, including one within a tile's width of a pole (rare, but real,
    and a job should not be refused outright for it — CLAUDE.md invariant 7
    is about surfacing failure, not manufacturing one). So near a pole this
    clamps rather than raises: the declination used for the ``cos(dec)`` RA
    correction is clamped to +/-89.9 deg (an RA circle a few arcmin from the
    pole is still a well-defined, if tiny, circle, so this keeps the RA
    offset a finite, sane angle instead of diverging near +/-90), and each
    tile's own declination is clamped to [-90, 90] so it is never a value no
    WCS could represent. The tiles nearest the pole end up overlapping more
    than the rest of the grid as a result — the honest outcome of a square
    grid meeting a point, not hidden by refusing the request.
    """
    if n < 1:
        raise ValueError(f"n must be at least 1, got {n}")
    offsets = _offsets(n, tile_size_deg)
    centers: list[tuple[Deg, Deg]] = []
    for dec_offset in offsets:
        panel_dec = max(-90.0, min(90.0, float(dec_center_deg) + dec_offset))
        cos_dec = math.cos(math.radians(max(-89.9, min(89.9, panel_dec))))
        for ra_offset in offsets:
            panel_ra = wrap_deg_360(float(ra_center_deg) + ra_offset / cos_dec)
            centers.append((panel_ra, Deg(panel_dec)))
    return tuple(centers)


def _panel_session(center_session: Session, ra_center_deg: Deg, panel: PanelCenter) -> Session:
    """The panel's own alt-az session, derived from the centre's without an ephemeris.

    At a fixed instant, hour angle is ``local sidereal time - RA``, so two
    targets observed at the same instant differ in hour angle by exactly the
    (shortest-path, wrap-safe) difference between their right ascensions. That
    is the same primitive :mod:`ssc.astro.framing` already uses to unwrap a
    sampled position-angle series, reused here instead of re-deriving it.
    """
    delta_ra = signed_delta_deg(float(panel.ra_j2000_deg), float(ra_center_deg))
    hour_angle_start = wrap_deg_180(float(center_session.hour_angle_start_deg) + float(delta_ra))
    return Session(
        latitude_deg=center_session.latitude_deg,
        declination_deg=panel.dec_j2000_deg,
        hour_angle_start_deg=hour_angle_start,
        duration_hours=center_session.duration_hours,
        pa_offset_deg=center_session.pa_offset_deg,
    )


@dataclass(frozen=True, slots=True)
class MosaicPanel:
    """One panel: its sky centre, and its rotation envelope if one was requested."""

    center: PanelCenter
    envelope: SessionEnvelope | None


@dataclass(frozen=True, slots=True)
class MosaicPlan:
    """The full mosaic: every panel, the total count, and the compounded-rotation
    picture across the panels (DESIGN.md 3.3), if a session was supplied.

    ``panel_count`` is what the UI multiplies by a per-panel capture time to
    show the session estimate; that multiplication is the caller's (the
    per-panel time is a scheduling input, not something this module knows).
    """

    spec: MosaicSpec
    geometry: FieldGeometry
    mount: MountMode
    panels: tuple[MosaicPanel, ...]
    worst_rotation_severity: str
    warnings: tuple[str, ...]

    @property
    def panel_count(self) -> int:
        return len(self.panels)

    def session_time_estimate_seconds(self, seconds_per_panel: float) -> float:
        """``panel_count * seconds_per_panel`` (DESIGN.md 3.3): the session-time
        estimate input the UI shows. ``seconds_per_panel`` is the caller's own
        capture-time budget per panel; this module has no opinion on it."""
        if seconds_per_panel < 0.0:
            raise ValueError(f"seconds_per_panel must not be negative, got {seconds_per_panel}")
        return self.panel_count * seconds_per_panel


_NO_SESSION_WARNING = (
    "No rotation session was supplied to mosaic_plan(): per-panel keep-regions were not "
    "computed. In alt-az mode, do not assume a panel's full frame survives the session — "
    "call mosaic_plan() with a center_session to get real per-panel warnings (DESIGN 3.3)."
)


def mosaic_plan(
    geometry: FieldGeometry,
    mount: MountMode,
    spec: MosaicSpec,
    ra_center_deg: Deg,
    dec_center_deg: Deg,
    center_session: Session | None = None,
    *,
    max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
    boundary_samples: int = 360,
) -> MosaicPlan:
    """Build the mosaic grid and, if ``center_session`` is given, every panel's
    rotation envelope by re-anchoring the session at each panel's own RA/Dec.

    ``center_session``'s ``declination_deg`` and ``hour_angle_start_deg`` are
    expected to describe the *centre* target at the session's start time — the
    same values :func:`ssc.astro.events.target_position` would give for
    ``(ra_center_deg, dec_center_deg)`` at ``t_start``. Passing a session
    describing a different target silently produces a wrong-but-plausible
    plan, which is why the parameter is documented rather than merely typed.
    """
    panel_centers = mosaic_panel_centers(ra_center_deg, dec_center_deg, geometry, spec)

    panels: list[MosaicPanel] = []
    seen_warnings: dict[str, None] = {}  # insertion-ordered de-dup, no set() reordering
    worst = "none"
    for center in panel_centers:
        envelope: SessionEnvelope | None = None
        if center_session is not None:
            panel_session = _panel_session(center_session, ra_center_deg, center)
            envelope = session_envelope(
                geometry,
                mount,
                panel_session,
                max_pa_step_deg=max_pa_step_deg,
                boundary_samples=boundary_samples,
            )
            if _SEVERITY_RANK[envelope.rotation_severity] > _SEVERITY_RANK[worst]:
                worst = envelope.rotation_severity
            for warning in envelope.warnings:
                seen_warnings[warning] = None
        panels.append(MosaicPanel(center=center, envelope=envelope))

    warnings: list[str] = list(seen_warnings)
    if center_session is None:
        warnings.append(_NO_SESSION_WARNING)
    elif mount is MountMode.ALTAZ and spec.panel_count > 1:
        warnings.append(
            f"{spec.panel_count} panels each rotate independently over the alt-az session; "
            "field rotation compounds across the mosaic. Check each panel's own keep-region "
            "rather than the mosaic's as a whole (DESIGN 3.3)."
        )

    return MosaicPlan(
        spec=spec,
        geometry=geometry,
        mount=mount,
        panels=tuple(panels),
        worst_rotation_severity=worst,
        warnings=tuple(warnings),
    )
