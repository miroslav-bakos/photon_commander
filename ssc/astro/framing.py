# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Field of view, parallactic angle and the alt-az rotation envelope.

Pure geometry. Nothing here touches an ephemeris, the network or a device: a
session is described by a latitude, a declination, a starting hour angle and a
duration, and :func:`ssc.astro.events.target_position` is what turns a real
target and a real clock into those four numbers. That split is what makes the
whole rotation model testable with a calculator.

Coordinate conventions
----------------------
Shapes are returned as **tangent-plane offsets in degrees from the target**,
with ``+y`` toward north and ``+x`` toward east. A direction angle in that plane
is measured from north through east — i.e. it is a position angle. The frame's
short (height) axis lies at the position angle ``PA``, so a corner of the sensor
at ``(u, v)`` in sensor millimetre-space maps to::

    x_east  = +u cos PA + v sin PA
    y_north = -u sin PA + v cos PA

Both the swept envelope and the keep region are star-shaped about the target
(every rotated rectangle contains the centre), so each is stored as a radial
support function ``r(theta)`` as well as a polygon. Union is the pointwise max
of the samples' radii and intersection the pointwise min — exact, given the
sampled position angles, which is why the sampling is adaptive.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any, Final

from ssc.astro.units import (
    ARCSEC_PER_DEG,
    ARCSEC_PER_RADIAN_PER_MM,
    SIDEREAL_DEG_PER_HOUR,
    ArcMin,
    ArcSec,
    Deg,
    signed_delta_deg,
    wrap_deg_180,
    wrap_deg_360,
)

#: DESIGN 3.2: amber past ~15 deg of session rotation, red past ~40 deg.
ROTATION_AMBER_DEG: Final = Deg(15.0)
ROTATION_RED_DEG: Final = Deg(40.0)

#: DESIGN 3.2: flag targets that transit above this altitude — dq/dt diverges at
#: the zenith and the field rotates violently through the meridian.
HIGH_ALTITUDE_TRANSIT_DEG: Final = Deg(70.0)

#: Module-level singletons so argument and dataclass defaults are names, not calls.
NO_PA_OFFSET: Final = Deg(0.0)
DEFAULT_PA_STEP_DEG: Final = Deg(0.5)

#: Within this of the zenith the parallactic angle is not merely large, it is
#: undefined. Reported, never clamped.
ZENITH_SINGULARITY_DEG: Final = Deg(0.05)


# --- profile ---------------------------------------------------------------


class MountMode(StrEnum):
    ALTAZ = "altaz"
    EQ = "eq"


class Provenance(StrEnum):
    """Where a derived optical number came from. Invariant 7: never papered over."""

    DATASHEET = "datasheet"
    MEASURED = "measured"


class DistortionModel(StrEnum):
    """How faithful the rectilinear overlay is for this optic."""

    RECTILINEAR = "rectilinear"
    APPROXIMATE = "approximate"


@dataclass(frozen=True, slots=True)
class Sensor:
    name: str
    w_px: int
    h_px: int
    pixel_um: float


@dataclass(frozen=True, slots=True)
class Optic:
    id: str
    label: str
    focal_length_mm: float
    sensor: Sensor
    aperture_mm: float | None = None
    focal_ratio: float | None = None
    design: str | None = None
    distortion: DistortionModel = DistortionModel.RECTILINEAR


@dataclass(frozen=True, slots=True)
class Calibration:
    """The ``[calibration]`` block of a profile (DESIGN 7).

    ``measured`` is false until the plate-solve flow has written real numbers.
    Every field is ``None`` when unknown — never 0.0, which would be a
    measurement.
    """

    measured: bool = False
    pixel_scale_arcsec: ArcSec | None = None
    pa_offset_deg: Deg | None = None
    boresight_offset_arcmin: ArcMin | None = None
    solved_at: str | None = None
    solve_rms_arcsec: float | None = None


@dataclass(frozen=True, slots=True)
class HardwareCapabilities:
    """Stable hardware facts used to hide controls a model cannot support."""

    tele_camera: bool = True
    wide_camera: bool = False
    wide_camera_autofocus: bool = False
    focuser: bool = True
    filter_wheel: bool = True
    dew_heater: bool = True


@dataclass(frozen=True, slots=True)
class ProfileDefaults:
    gain: int = 200
    exposure_stack_ms: int = 10_000
    exposure_preview_ms: int = 500
    dither_enabled: bool = True
    dither_pixels: int = 50
    dither_every_n_frames: int = 10
    lp_filter: bool = False
    dew_heater_percent: int = 0


@dataclass(frozen=True, slots=True)
class ProfileLimits:
    min_altitude_deg: float = 20.0
    battery_low_percent: int = 15
    max_exposure_altaz_s: float | None = None
    max_exposure_eq_s: float | None = None


@dataclass(frozen=True, slots=True)
class Profile:
    id: str
    name: str
    mount: MountMode
    eq_mode_available: bool
    optics: tuple[Optic, ...]
    calibration: Calibration
    vendor: str | None = None
    aliases: tuple[str, ...] = ()
    source_urls: tuple[str, ...] = ()
    capabilities: HardwareCapabilities = HardwareCapabilities()
    defaults: ProfileDefaults = ProfileDefaults()
    limits: ProfileLimits = ProfileLimits()

    def optic(self, optic_id: str) -> Optic:
        for optic in self.optics:
            if optic.id == optic_id:
                return optic
        known = ", ".join(o.id for o in self.optics)
        raise KeyError(f"profile {self.id!r} has no optic {optic_id!r}; it has: {known}")


def _optional_float(raw: dict[str, Any], key: str) -> float | None:
    value = raw.get(key)
    return None if value is None else float(value)


def parse_profile(raw: dict[str, Any]) -> Profile:
    """Build a :class:`Profile` from already-parsed TOML. Pure; no file access."""
    optics: list[Optic] = []
    for entry in raw.get("optics", ()):
        sensor_raw = entry["sensor"]
        optics.append(
            Optic(
                id=str(entry["id"]),
                label=str(entry.get("label", entry["id"])),
                focal_length_mm=float(entry["focal_length_mm"]),
                sensor=Sensor(
                    name=str(sensor_raw["name"]),
                    w_px=int(sensor_raw["w_px"]),
                    h_px=int(sensor_raw["h_px"]),
                    pixel_um=float(sensor_raw["pixel_um"]),
                ),
                aperture_mm=_optional_float(entry, "aperture_mm"),
                focal_ratio=_optional_float(entry, "focal_ratio"),
                design=entry.get("design"),
                distortion=DistortionModel(entry.get("distortion", DistortionModel.RECTILINEAR)),
            )
        )
    if not optics:
        raise ValueError(f"profile {raw.get('id')!r} declares no [[optics]]")

    cal_raw = raw.get("calibration", {})
    pixel_scale = _optional_float(cal_raw, "pixel_scale_arcsec")
    pa_offset = _optional_float(cal_raw, "pa_offset_deg")
    boresight = _optional_float(cal_raw, "boresight_offset_arcmin")
    calibration = Calibration(
        measured=bool(cal_raw.get("measured", False)),
        pixel_scale_arcsec=None if pixel_scale is None else ArcSec(pixel_scale),
        pa_offset_deg=None if pa_offset is None else Deg(pa_offset),
        boresight_offset_arcmin=None if boresight is None else ArcMin(boresight),
        solved_at=cal_raw.get("solved_at"),
        solve_rms_arcsec=_optional_float(cal_raw, "solve_rms_arcsec"),
    )
    if calibration.measured and calibration.pixel_scale_arcsec is None:
        raise ValueError(
            f"profile {raw.get('id')!r} claims [calibration] measured = true but has no "
            "pixel_scale_arcsec; a claimed measurement with no measured value is worse "
            "than an honest datasheet figure"
        )

    capability_raw = raw.get("capabilities", {})
    defaults_raw = raw.get("defaults", {})
    limits_raw = raw.get("limits", {})
    return Profile(
        id=str(raw["id"]),
        name=str(raw["name"]),
        vendor=raw.get("vendor"),
        mount=MountMode(raw.get("mount", MountMode.ALTAZ)),
        eq_mode_available=bool(raw.get("eq_mode_available", False)),
        optics=tuple(optics),
        calibration=calibration,
        aliases=tuple(str(alias) for alias in raw.get("aliases", ())),
        source_urls=tuple(str(url) for url in raw.get("source_urls", ())),
        capabilities=HardwareCapabilities(
            tele_camera=bool(capability_raw.get("tele_camera", True)),
            wide_camera=bool(capability_raw.get("wide_camera", False)),
            wide_camera_autofocus=bool(capability_raw.get("wide_camera_autofocus", False)),
            focuser=bool(capability_raw.get("focuser", True)),
            filter_wheel=bool(capability_raw.get("filter_wheel", True)),
            dew_heater=bool(capability_raw.get("dew_heater", True)),
        ),
        defaults=ProfileDefaults(
            gain=int(defaults_raw.get("gain", 200)),
            exposure_stack_ms=int(defaults_raw.get("exposure_stack_ms", 10_000)),
            exposure_preview_ms=int(defaults_raw.get("exposure_preview_ms", 500)),
            dither_enabled=bool(defaults_raw.get("dither_enabled", True)),
            dither_pixels=int(defaults_raw.get("dither_pixels", 50)),
            dither_every_n_frames=int(defaults_raw.get("dither_every_n_frames", 10)),
            lp_filter=bool(defaults_raw.get("lp_filter", False)),
            dew_heater_percent=int(defaults_raw.get("dew_heater_percent", 0)),
        ),
        limits=ProfileLimits(
            min_altitude_deg=float(limits_raw.get("min_altitude_deg", 20.0)),
            battery_low_percent=int(limits_raw.get("battery_low_percent", 15)),
            max_exposure_altaz_s=_optional_float(limits_raw, "max_exposure_altaz_s"),
            max_exposure_eq_s=_optional_float(limits_raw, "max_exposure_eq_s"),
        ),
    )


def load_profile(path: Path) -> Profile:
    """Read a scope profile from ``data/profiles/*.toml``. The only I/O in this module."""
    with path.open("rb") as handle:
        return parse_profile(tomllib.load(handle))


# --- derived field geometry ------------------------------------------------


@dataclass(frozen=True, slots=True)
class FieldGeometry:
    """The rectangle an optic projects on the sky, and how much to trust it.

    ``provenance`` and ``distortion`` are part of the value, not documentation:
    they travel to the API boundary so the UI can label a datasheet-derived or
    barrel-distorted overlay rather than presenting it as a measurement
    (invariant 7).

    ``inscribed_circle_deg`` is a **diameter** — the short side, 2.24 deg on the
    S30 Pro tele. It is the guaranteed-keep lower bound: however long the alt-az
    session, the intersection of the rotated frames always contains it.
    """

    optic_id: str
    label: str
    pixel_scale_arcsec: ArcSec
    width_deg: Deg
    height_deg: Deg
    diagonal_deg: Deg
    inscribed_circle_deg: Deg
    provenance: Provenance
    distortion: DistortionModel
    caveats: tuple[str, ...]

    @property
    def half_width_deg(self) -> float:
        return float(self.width_deg) / 2.0

    @property
    def half_height_deg(self) -> float:
        return float(self.height_deg) / 2.0


def pixel_scale_arcsec(pixel_um: float, focal_length_mm: float) -> ArcSec:
    """``206.265 * pixel_um / focal_length_mm`` (DESIGN 3.2), arcsec per pixel."""
    if focal_length_mm <= 0.0:
        raise ValueError(f"focal_length_mm must be positive, got {focal_length_mm}")
    if pixel_um <= 0.0:
        raise ValueError(f"pixel_um must be positive, got {pixel_um}")
    return ArcSec(ARCSEC_PER_RADIAN_PER_MM * pixel_um / focal_length_mm)


def field_geometry(profile: Profile, optic_id: str) -> FieldGeometry:
    """Derive the on-sky rectangle for one optic of a profile.

    A plate-solved ``pixel_scale_arcsec`` from ``[calibration]`` wins over the
    datasheet formula, and only then is the result labelled ``MEASURED``.
    """
    optic = profile.optic(optic_id)
    cal = profile.calibration

    if cal.measured and cal.pixel_scale_arcsec is not None:
        scale = cal.pixel_scale_arcsec
        provenance = Provenance.MEASURED
        caveats: list[str] = []
    else:
        scale = pixel_scale_arcsec(optic.sensor.pixel_um, optic.focal_length_mm)
        provenance = Provenance.DATASHEET
        caveats = [
            "Pixel scale and field of view are derived from published sensor and optical "
            "specifications, not from a plate solve. Run Settings > Calibrate from plate "
            "solve to replace them (DESIGN 7)."
        ]

    if optic.distortion is DistortionModel.APPROXIMATE:
        caveats.append(
            f"The {optic.label} overlay is approximate: its field is back-solved assuming a "
            "rectilinear projection, and a short fast lens has real barrel distortion. "
            "Indicative only, not framing-accurate."
        )

    width = Deg(optic.sensor.w_px * float(scale) / ARCSEC_PER_DEG)
    height = Deg(optic.sensor.h_px * float(scale) / ARCSEC_PER_DEG)
    return FieldGeometry(
        optic_id=optic.id,
        label=optic.label,
        pixel_scale_arcsec=scale,
        width_deg=width,
        height_deg=height,
        diagonal_deg=Deg(math.hypot(float(width), float(height))),
        inscribed_circle_deg=Deg(min(float(width), float(height))),
        provenance=provenance,
        distortion=optic.distortion,
        caveats=tuple(caveats),
    )


# --- parallactic angle -----------------------------------------------------


def parallactic_angle_deg(hour_angle_deg: Deg, declination_deg: Deg, latitude_deg: Deg) -> Deg:
    """Meeus 14.1: ``q = atan2(sin H, tan phi cos delta - sin delta cos H)``.

    Wrapped to **(-180, +180]**. Hour angle is negative east of the meridian and
    positive west, so ``q`` increases through transit for a target south of the
    zenith in the northern hemisphere.

    At the zenith (``delta == phi`` and ``H == 0``) both arguments of ``atan2``
    are zero and ``q`` is genuinely undefined; this returns 0 there rather than
    raising, and :func:`is_zenith_singular` is how a caller finds out. The
    geographic poles are rejected outright — ``tan(+-90 deg)`` is not a number
    and an alt-az mount there has no meaningful parallactic angle.
    """
    if abs(float(latitude_deg)) >= 90.0:
        raise ValueError(
            f"parallactic angle is undefined at latitude {latitude_deg}: tan(phi) diverges "
            "at the geographic poles"
        )
    hour_angle = math.radians(float(hour_angle_deg))
    declination = math.radians(float(declination_deg))
    latitude = math.radians(float(latitude_deg))
    numerator = math.sin(hour_angle)
    denominator = math.tan(latitude) * math.cos(declination) - math.sin(declination) * math.cos(
        hour_angle
    )
    return wrap_deg_180(math.degrees(math.atan2(numerator, denominator)))


def position_angle_deg(
    hour_angle_deg: Deg, declination_deg: Deg, latitude_deg: Deg, pa_offset_deg: Deg = NO_PA_OFFSET
) -> Deg:
    """``PA(t) = q(t) + PA_offset`` for an alt-az mount, wrapped to (-180, +180].

    ``pa_offset_deg`` is the sensor's rotation relative to the parallactic
    reference. Until the plate-solve flow measures it (DESIGN 7) it is 0 by
    convention, which is a *choice of origin*, not a measurement.
    """
    return wrap_deg_180(
        float(parallactic_angle_deg(hour_angle_deg, declination_deg, latitude_deg))
        + float(pa_offset_deg)
    )


def altitude_deg(hour_angle_deg: Deg, declination_deg: Deg, latitude_deg: Deg) -> Deg:
    """Geometric altitude: ``sin h = sin phi sin delta + cos phi cos delta cos H``."""
    hour_angle = math.radians(float(hour_angle_deg))
    declination = math.radians(float(declination_deg))
    latitude = math.radians(float(latitude_deg))
    sin_alt = math.sin(latitude) * math.sin(declination) + math.cos(latitude) * math.cos(
        declination
    ) * math.cos(hour_angle)
    return Deg(math.degrees(math.asin(max(-1.0, min(1.0, sin_alt)))))


def azimuth_deg(hour_angle_deg: Deg, declination_deg: Deg, latitude_deg: Deg) -> Deg:
    """Azimuth in **[0, 360)** measured from north through east."""
    hour_angle = math.radians(float(hour_angle_deg))
    declination = math.radians(float(declination_deg))
    latitude = math.radians(float(latitude_deg))
    east = -math.cos(declination) * math.sin(hour_angle)
    north = math.sin(declination) * math.cos(latitude) - math.cos(declination) * math.sin(
        latitude
    ) * math.cos(hour_angle)
    return wrap_deg_360(math.degrees(math.atan2(east, north)))


def is_zenith_singular(
    hour_angle_deg: Deg,
    declination_deg: Deg,
    latitude_deg: Deg,
    tolerance_deg: Deg = ZENITH_SINGULARITY_DEG,
) -> bool:
    """True within ``tolerance_deg`` of the zenith, where ``q`` is undefined."""
    return float(altitude_deg(hour_angle_deg, declination_deg, latitude_deg)) >= 90.0 - float(
        tolerance_deg
    )


def rotation_rate_deg_per_hour(
    hour_angle_deg: Deg, declination_deg: Deg, latitude_deg: Deg
) -> float:
    """Analytic ``dq/dt = omega cos(phi) cos(A) / cos(h)``, degrees per hour, signed.

    Diverges at the zenith by construction — that divergence is the phenomenon
    DESIGN 3.2 asks to be flagged, so it is returned as ``inf`` rather than
    clamped to something comfortable.
    """
    altitude = float(altitude_deg(hour_angle_deg, declination_deg, latitude_deg))
    cos_alt = math.cos(math.radians(altitude))
    numerator = (
        SIDEREAL_DEG_PER_HOUR
        * math.cos(math.radians(float(latitude_deg)))
        * math.cos(math.radians(float(azimuth_deg(hour_angle_deg, declination_deg, latitude_deg))))
    )
    if cos_alt == 0.0:
        return math.inf if numerator >= 0.0 else -math.inf
    return numerator / cos_alt


# --- shapes ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Offset:
    """A tangent-plane offset from the target, degrees. ``+x`` east, ``+y`` north."""

    x_deg: Deg
    y_deg: Deg


@dataclass(frozen=True, slots=True)
class Shape:
    """A closed polygon of tangent-plane offsets, in order. The closing edge is implied."""

    points: tuple[Offset, ...]


def rectangle_radius_deg(geometry: FieldGeometry, angle_from_frame_north_deg: float) -> Deg:
    """Distance from the frame centre to its edge, in a direction relative to the frame.

    The angle is measured from the frame's own short (height) axis toward its
    long (width) axis — i.e. subtract the position angle from a sky direction
    before calling this.
    """
    angle = math.radians(angle_from_frame_north_deg)
    across = abs(math.sin(angle))
    along = abs(math.cos(angle))
    limits = []
    if across > 0.0:
        limits.append(geometry.half_width_deg / across)
    if along > 0.0:
        limits.append(geometry.half_height_deg / along)
    return Deg(min(limits))


def frame_shape(geometry: FieldGeometry, position_angle: Deg) -> Shape:
    """The sensor rectangle on the sky at one position angle: four exact corners."""
    pa = math.radians(float(position_angle))
    cos_pa, sin_pa = math.cos(pa), math.sin(pa)
    half_w, half_h = geometry.half_width_deg, geometry.half_height_deg
    corners = ((half_w, half_h), (-half_w, half_h), (-half_w, -half_h), (half_w, -half_h))
    return Shape(
        tuple(
            Offset(
                x_deg=Deg(u * cos_pa + v * sin_pa),
                y_deg=Deg(-u * sin_pa + v * cos_pa),
            )
            for u, v in corners
        )
    )


def _radial_shape(angles_deg: tuple[Deg, ...], radii_deg: tuple[Deg, ...]) -> Shape:
    return Shape(
        tuple(
            Offset(
                x_deg=Deg(float(r) * math.sin(math.radians(float(a)))),
                y_deg=Deg(float(r) * math.cos(math.radians(float(a)))),
            )
            for a, r in zip(angles_deg, radii_deg, strict=True)
        )
    )


# --- session envelope ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Session:
    """The four numbers an alt-az rotation envelope actually depends on.

    ``hour_angle_start_deg`` and ``declination_deg`` come from
    :func:`ssc.astro.events.target_position`; the hour angle then advances at the
    mean sidereal rate, which is why this module needs no ephemeris.
    """

    latitude_deg: Deg
    declination_deg: Deg
    hour_angle_start_deg: Deg
    duration_hours: float
    pa_offset_deg: Deg = NO_PA_OFFSET

    def __post_init__(self) -> None:
        if self.duration_hours < 0.0:
            raise ValueError(f"duration_hours must not be negative, got {self.duration_hours}")
        if not -90.0 <= self.declination_deg <= 90.0:
            raise ValueError(f"declination out of range: {self.declination_deg}")


@dataclass(frozen=True, slots=True)
class RotationSample:
    elapsed_hours: float
    hour_angle_deg: Deg
    altitude_deg: Deg
    parallactic_angle_deg: Deg
    position_angle_deg: Deg


def radial_area_deg2(angles_deg: Sequence[Deg], radii_deg: Sequence[Deg]) -> float:
    """Area of a star-shaped region given as a radial support function.

    ``0.5 * integral(r^2 dtheta)``, trapezoidal over the shared boundary
    angles and closing the loop from the last sample back to the first.

    This exists because ``keep_region_min_diameter_deg`` cannot answer the
    question a planner actually asks. That minimum width is the frame's short
    side at zero rotation and the inscribed circle's diameter at extreme
    rotation -- and those are the same number, because the inscribed circle is
    inscribed in the short side. So it is provably constant for a given optic
    no matter how long the session runs, which makes it useless as a signal
    that a longer session costs sky. Area does shrink, monotonically.
    """
    n = len(angles_deg)
    if n < 3 or len(radii_deg) != n:
        return 0.0
    total = 0.0
    for i in range(n):
        j = (i + 1) % n
        a0 = math.radians(float(angles_deg[i]))
        a1 = math.radians(float(angles_deg[j]))
        step = a1 - a0
        if step <= 0.0:
            step += 2.0 * math.pi
        r0 = float(radii_deg[i])
        r1 = float(radii_deg[j])
        total += 0.5 * step * 0.5 * (r0 * r0 + r1 * r1)
    return total


def _area_fraction(keep_area_deg2: float, geometry: FieldGeometry) -> float:
    frame_area = float(geometry.width_deg) * float(geometry.height_deg)
    if frame_area <= 0.0:
        return 0.0
    return min(1.0, keep_area_deg2 / frame_area)


@dataclass(frozen=True, slots=True)
class SessionEnvelope:
    """The four shapes DESIGN 3.2 asks the planner to draw, plus the numbers behind them.

    ``keep_region`` is the one that matters: it is the intersection of the frame
    over the session, so it is what survives stacking. ``inscribed_circle_deg``
    is its guaranteed lower bound (a diameter), reported whatever the session
    length so a caller always has a floor to quote.

    The radial arrays share ``boundary_angles_deg``: ``keep_radii_deg[i] <=
    frame radius <= swept_radii_deg[i]`` at every index, which is the
    ``keep_region`` subset of ``frame`` subset of ``swept_envelope`` invariant in
    computable form.
    """

    mount: MountMode
    geometry: FieldGeometry
    samples: tuple[RotationSample, ...]
    frame_start: Shape
    frame_end: Shape
    swept_envelope: Shape
    keep_region: Shape
    boundary_angles_deg: tuple[Deg, ...]
    swept_radii_deg: tuple[Deg, ...]
    keep_radii_deg: tuple[Deg, ...]
    inscribed_circle_deg: Deg
    keep_region_min_diameter_deg: Deg
    keep_region_area_deg2: float
    swept_envelope_area_deg2: float
    #: Keep-region area as a fraction of the un-rotated frame's area, in
    #: ``0..1``. Unlike the minimum diameter this genuinely falls as rotation
    #: grows, so it is the number worth putting in front of a user.
    keep_area_fraction: float
    total_rotation_deg: Deg
    max_abs_rotation_rate_deg_per_hour: float
    max_altitude_deg: Deg
    rotation_severity: str
    high_altitude_transit: bool
    zenith_singularity: bool
    warnings: tuple[str, ...]

    @property
    def position_angle_start_deg(self) -> Deg:
        return self.samples[0].position_angle_deg

    @property
    def position_angle_end_deg(self) -> Deg:
        return self.samples[-1].position_angle_deg


def _sample(session: Session, elapsed_hours: float) -> RotationSample:
    hour_angle = wrap_deg_180(
        float(session.hour_angle_start_deg) + SIDEREAL_DEG_PER_HOUR * elapsed_hours
    )
    parallactic = parallactic_angle_deg(hour_angle, session.declination_deg, session.latitude_deg)
    return RotationSample(
        elapsed_hours=elapsed_hours,
        hour_angle_deg=hour_angle,
        altitude_deg=altitude_deg(hour_angle, session.declination_deg, session.latitude_deg),
        parallactic_angle_deg=parallactic,
        position_angle_deg=wrap_deg_180(float(parallactic) + float(session.pa_offset_deg)),
    )


def sample_session(
    session: Session,
    *,
    max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
    initial_intervals: int = 8,
    max_depth: int = 14,
) -> tuple[RotationSample, ...]:
    """Sample ``PA(t)`` adaptively, refining where ``|dq/dt|`` is large.

    Uniform sampling is the wrong tool here: a target that crawls for three
    hours and then whips through 60 deg in ten minutes near the meridian needs
    its samples concentrated in those ten minutes. So an interval is bisected
    whenever the position angle changed by more than ``max_pa_step_deg`` across
    it, which is precisely the ``|dq/dt| * dt`` criterion, and left alone
    otherwise. ``max_depth`` bounds the work at the zenith, where no finite
    number of samples resolves the divergence — that case is reported as a
    singularity instead of being sampled to death.
    """
    if session.duration_hours == 0.0:
        return (_sample(session, 0.0),)
    if max_pa_step_deg <= 0.0:
        raise ValueError(f"max_pa_step_deg must be positive, got {max_pa_step_deg}")

    step = session.duration_hours / initial_intervals
    samples = {
        round(i * step, 12): _sample(session, i * step) for i in range(initial_intervals + 1)
    }
    pending = [(i * step, (i + 1) * step, 0) for i in range(initial_intervals)]
    while pending:
        left, right, depth = pending.pop()
        if depth >= max_depth:
            continue
        pa_left = samples[round(left, 12)].position_angle_deg
        pa_right = samples[round(right, 12)].position_angle_deg
        if abs(float(signed_delta_deg(float(pa_left), float(pa_right)))) <= float(max_pa_step_deg):
            continue
        middle = (left + right) / 2.0
        samples.setdefault(round(middle, 12), _sample(session, middle))
        pending.append((left, middle, depth + 1))
        pending.append((middle, right, depth + 1))
    return tuple(samples[key] for key in sorted(samples))


def _unwrapped_rotation_deg(samples: tuple[RotationSample, ...]) -> Deg:
    """Total signed rotation, summed as consecutive shortest deltas so it survives wraps."""
    total = 0.0
    for previous, current in pairwise(samples):
        total += float(
            signed_delta_deg(float(previous.position_angle_deg), float(current.position_angle_deg))
        )
    return Deg(total)


def _severity(total_rotation_deg: Deg) -> str:
    magnitude = abs(float(total_rotation_deg))
    if magnitude >= float(ROTATION_RED_DEG):
        return "red"
    if magnitude >= float(ROTATION_AMBER_DEG):
        return "amber"
    return "none"


def _build(
    mount: MountMode,
    geometry: FieldGeometry,
    samples: tuple[RotationSample, ...],
    boundary_samples: int,
) -> tuple[tuple[Deg, ...], tuple[Deg, ...], tuple[Deg, ...]]:
    """Angle grid plus the swept (max) and keep (min) radii over the sampled frames."""
    angles = tuple(Deg(360.0 * i / boundary_samples) for i in range(boundary_samples))
    position_angles = [float(s.position_angle_deg) for s in samples]
    swept: list[Deg] = []
    keep: list[Deg] = []
    for angle in angles:
        radii = [float(rectangle_radius_deg(geometry, float(angle) - pa)) for pa in position_angles]
        swept.append(Deg(max(radii)))
        keep.append(Deg(min(radii)))
    return angles, tuple(swept), tuple(keep)


def eq_session_envelope(
    geometry: FieldGeometry,
    pa_offset_deg: Deg = NO_PA_OFFSET,
    *,
    boundary_samples: int = 360,
) -> SessionEnvelope:
    """The EQ-mode envelope: one rectangle, constant ``PA = PA_offset``.

    In EQ mode the sensor is fixed with respect to the sky, so the alt-az
    machinery is not merely unnecessary, it is wrong — there is no rotation to
    sweep. All four shapes coincide and the full frame is usable (DESIGN 3.2).
    """
    sample = RotationSample(
        elapsed_hours=0.0,
        hour_angle_deg=Deg(0.0),
        altitude_deg=Deg(0.0),
        parallactic_angle_deg=Deg(0.0),
        position_angle_deg=wrap_deg_180(float(pa_offset_deg)),
    )
    samples = (sample,)
    angles, swept, keep = _build(MountMode.EQ, geometry, samples, boundary_samples)
    frame = frame_shape(geometry, sample.position_angle_deg)
    return SessionEnvelope(
        mount=MountMode.EQ,
        geometry=geometry,
        samples=samples,
        frame_start=frame,
        frame_end=frame,
        swept_envelope=_radial_shape(angles, swept),
        keep_region=_radial_shape(angles, keep),
        boundary_angles_deg=angles,
        swept_radii_deg=swept,
        keep_radii_deg=keep,
        inscribed_circle_deg=geometry.inscribed_circle_deg,
        keep_region_min_diameter_deg=Deg(2.0 * min(float(r) for r in keep)),
        keep_region_area_deg2=radial_area_deg2(angles, keep),
        swept_envelope_area_deg2=radial_area_deg2(angles, swept),
        keep_area_fraction=_area_fraction(radial_area_deg2(angles, keep), geometry),
        total_rotation_deg=Deg(0.0),
        max_abs_rotation_rate_deg_per_hour=0.0,
        max_altitude_deg=Deg(0.0),
        rotation_severity="none",
        high_altitude_transit=False,
        zenith_singularity=False,
        warnings=geometry.caveats,
    )


def altaz_session_envelope(
    geometry: FieldGeometry,
    session: Session,
    *,
    max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
    boundary_samples: int = 360,
) -> SessionEnvelope:
    """The four alt-az shapes of DESIGN 3.2 for one session.

    Returns the frame at ``t_start``, the ghost at ``t_end``, the swept union and
    the keep-region intersection, together with the numbers that drive the
    planner's warnings: total rotation, peak rotation rate, peak altitude, and
    whether the target passes close enough to the zenith for ``dq/dt`` to be
    meaningless there.
    """
    samples = sample_session(session, max_pa_step_deg=max_pa_step_deg)
    angles, swept, keep = _build(MountMode.ALTAZ, geometry, samples, boundary_samples)

    total_rotation = _unwrapped_rotation_deg(samples)
    max_altitude = Deg(max(float(s.altitude_deg) for s in samples))
    rates = [
        abs(
            rotation_rate_deg_per_hour(
                s.hour_angle_deg, session.declination_deg, session.latitude_deg
            )
        )
        for s in samples
    ]
    zenith = any(
        is_zenith_singular(s.hour_angle_deg, session.declination_deg, session.latitude_deg)
        for s in samples
    )
    high_altitude = float(max_altitude) >= float(HIGH_ALTITUDE_TRANSIT_DEG)
    severity = _severity(total_rotation)

    warnings = list(geometry.caveats)
    if severity == "amber":
        warnings.append(
            f"Field rotates {abs(float(total_rotation)):.1f} deg over the session; the "
            "guaranteed-keep region is noticeably smaller than the frame."
        )
    elif severity == "red":
        warnings.append(
            f"Field rotates {abs(float(total_rotation)):.1f} deg over the session; the "
            "guaranteed-keep region is collapsing toward the "
            f"{float(geometry.inscribed_circle_deg):.2f} deg inscribed circle."
        )
    if high_altitude:
        warnings.append(
            f"Target transits at {float(max_altitude):.1f} deg altitude. Parallactic angle "
            "changes violently near the zenith; expect a rotation spike through the meridian."
        )
    if zenith:
        warnings.append(
            "Target passes within "
            f"{float(ZENITH_SINGULARITY_DEG):.2f} deg of the zenith, where the parallactic "
            "angle is undefined and its rate of change is unbounded. The sampled position "
            "angles either side of that instant are correct; the value at it is not, and has "
            "not been clamped to hide that."
        )

    return SessionEnvelope(
        mount=MountMode.ALTAZ,
        geometry=geometry,
        samples=samples,
        frame_start=frame_shape(geometry, samples[0].position_angle_deg),
        frame_end=frame_shape(geometry, samples[-1].position_angle_deg),
        swept_envelope=_radial_shape(angles, swept),
        keep_region=_radial_shape(angles, keep),
        boundary_angles_deg=angles,
        swept_radii_deg=swept,
        keep_radii_deg=keep,
        inscribed_circle_deg=geometry.inscribed_circle_deg,
        keep_region_min_diameter_deg=Deg(2.0 * min(float(r) for r in keep)),
        keep_region_area_deg2=radial_area_deg2(angles, keep),
        swept_envelope_area_deg2=radial_area_deg2(angles, swept),
        keep_area_fraction=_area_fraction(radial_area_deg2(angles, keep), geometry),
        total_rotation_deg=total_rotation,
        max_abs_rotation_rate_deg_per_hour=max(rates),
        max_altitude_deg=max_altitude,
        rotation_severity=severity,
        high_altitude_transit=high_altitude,
        zenith_singularity=zenith,
        warnings=tuple(warnings),
    )


def session_envelope(
    geometry: FieldGeometry,
    mount: MountMode,
    session: Session,
    *,
    max_pa_step_deg: Deg = DEFAULT_PA_STEP_DEG,
    boundary_samples: int = 360,
) -> SessionEnvelope:
    """Dispatch on mount mode. EQ takes the constant-PA path, never the alt-az one."""
    if mount is MountMode.EQ:
        return eq_session_envelope(
            geometry, session.pa_offset_deg, boundary_samples=boundary_samples
        )
    return altaz_session_envelope(
        geometry, session, max_pa_step_deg=max_pa_step_deg, boundary_samples=boundary_samples
    )


__all__ = [
    "DEFAULT_PA_STEP_DEG",
    "HIGH_ALTITUDE_TRANSIT_DEG",
    "ROTATION_AMBER_DEG",
    "ROTATION_RED_DEG",
    "ZENITH_SINGULARITY_DEG",
    "Calibration",
    "DistortionModel",
    "FieldGeometry",
    "MountMode",
    "Offset",
    "Optic",
    "Profile",
    "Provenance",
    "RotationSample",
    "Sensor",
    "Session",
    "SessionEnvelope",
    "Shape",
    "altaz_session_envelope",
    "altitude_deg",
    "azimuth_deg",
    "eq_session_envelope",
    "field_geometry",
    "frame_shape",
    "is_zenith_singular",
    "load_profile",
    "parallactic_angle_deg",
    "parse_profile",
    "pixel_scale_arcsec",
    "position_angle_deg",
    "rectangle_radius_deg",
    "rotation_rate_deg_per_hour",
    "sample_session",
    "session_envelope",
]
