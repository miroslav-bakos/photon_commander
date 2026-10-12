# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Planning tab's data (DESIGN.md 3): profiles, target resolution, framing,
mosaic and night, all pure computation over ``ssc.astro`` — no device required.

``mount`` is a request field on every rotation-aware endpoint, never inferred
from a connected scope: planning happens hours before the mount is touched,
and it must stay possible to plan an alt-az session while the scope is
actually sitting in EQ mode (which the dev unit usually is). When a device
*is* connected, :func:`_device_mount_check` says whether it currently agrees
with the requested mode — a warning surfaced to the UI, never a silent
override of what was asked for.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, cast
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from skyfield.errors import EphemerisRangeError

from ssc.api.astro_service import AstroUnavailableError, FramingVisibility, UnknownProfileError
from ssc.api.deps import ServicesDep, SessionDep, rate_limit, require_location
from ssc.api.schemas import (
    AltitudeSampleModel,
    AnnualAltitudeNightModel,
    AnnualAltitudeRequest,
    AnnualAltitudeResponse,
    AutoNightBlockModel,
    AutoNightGapModel,
    AutoNightRequest,
    AutoNightResponse,
    BestTargetModel,
    BestTargetsRequest,
    BestTargetsResponse,
    BodiesResponse,
    BodyPositionModel,
    BrightStarModel,
    BrightStarsResponse,
    CalibrationSummary,
    CometSuggestionModel,
    CometSuggestionsResponse,
    ConstellationBoundariesResponse,
    ConstellationBoundaryRegionModel,
    ConstellationBoundaryVertexModel,
    ConstellationResponse,
    DeviceMountModeCheck,
    EventTimeModel,
    FieldGeometryModel,
    FieldObjectModel,
    FieldObjectsResponse,
    FramingRequest,
    FramingResponse,
    FramingVisibilityModel,
    HardwareCapabilitiesSummary,
    HorizonIntervalModel,
    LensSummary,
    MoonAltitudeSampleModel,
    MoonAltitudeSeriesResponse,
    MoonInfoModel,
    MoonSeparationSampleModel,
    MosaicPanelModel,
    MosaicRequest,
    MosaicResponse,
    NightRequest,
    NightResponse,
    ObjectInfoAttributionModel,
    ObjectInfoImageModel,
    ObjectInfoResponse,
    OffsetModel,
    PanelCenterModel,
    ProfileDefaultsSummary,
    ProfileLimitsSummary,
    ProfilesResponse,
    ProfileSummary,
    RotationSampleModel,
    SensorSummary,
    SessionEnvelopeModel,
    ShapeModel,
    TargetPositionModel,
    TargetResolutionResponse,
    TargetSuggestionsResponse,
)
from ssc.astro.best_targets import BestTargetCandidate
from ssc.astro.bodies import BodyPosition, bodies_credit
from ssc.astro.catalog import (
    MAX_CONE_LIMIT,
    MAX_CONE_RADIUS_DEG,
    CatalogObject,
    ObjectType,
    UnknownTargetError,
)
from ssc.astro.comets import comet_execution_rejection
from ssc.astro.constellations import (
    boundaries_credit,
    boundaries_j2000,
    constellation_at,
    load_boundaries,
)
from ssc.astro.events import EventTime, Site, TargetPosition, VisibilityInterval
from ssc.astro.framing import (
    Calibration,
    FieldGeometry,
    MountMode,
    Optic,
    Profile,
    SessionEnvelope,
    Shape,
)
from ssc.astro.horizon import HorizonProfile
from ssc.astro.mosaic import MosaicSpec
from ssc.astro.stars import BrightStar, bright_stars_credit, load_bright_stars, stars_within_cone
from ssc.astro.units import Deg, Hours, Metres, deg_to_hours
from ssc.objectinfo import ObjectInfoResult
from ssc.state.models import LocationState

_log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/astro", tags=["astro"])

_Severity = Literal["none", "amber", "red"]


# -- shared helpers ----------------------------------------------------------- #
# The "no location set" 409 guard moved to ``ssc.api.deps.require_location``
# once ``ssc/api/routes/weather.py`` and ``ssc/api/routes/tonight.py`` needed
# it too. Called explicitly inside each handler, not via ``Depends`` — see
# that function's own docstring for why.


def _site_of(location: LocationState) -> Site:
    return Site(
        latitude_deg=Deg(location.latitude_deg),
        longitude_deg=Deg(location.longitude_deg),
        elevation_m=Metres(location.elevation_m or 0.0),
    )


def _horizon_profile_of(services: ServicesDep) -> HorizonProfile | None:
    """The site's recorded skyline mask, or ``None`` when it has none — the
    same distinction the settings store makes (DESIGN.md 5): absent is
    *unknown*, never a silent "no obstructions"."""
    return services.settings.active_horizon_profile()


def _interval_model(interval: VisibilityInterval) -> HorizonIntervalModel:
    return HorizonIntervalModel(
        start=interval.start,
        end=interval.end,
        start_is_window_edge=interval.start_is_window_edge,
        end_is_window_edge=interval.end_is_window_edge,
    )


def _framing_visibility_model(visibility: FramingVisibility) -> FramingVisibilityModel:
    return FramingVisibilityModel(
        horizon_profile_active=visibility.horizon_profile_active,
        start_above_horizon=visibility.start_above_horizon,
        start_horizon_altitude_deg=float(visibility.start_horizon_altitude_deg),
        clear_intervals=[_interval_model(interval) for interval in visibility.clear_intervals],
    )


async def _device_mount_check(services: ServicesDep, mount: MountMode) -> DeviceMountModeCheck:
    """Compare the plan's mode against a *live* device reading. Never raises: a
    failed side-channel check must not break the plan it was only cross-checking."""
    try:
        equ_mode = await services.device.mount_alignment()
    except Exception:
        _log.warning("device mount-alignment check failed", exc_info=True)
        equ_mode = None
    if equ_mode is None:
        return DeviceMountModeCheck(
            checked=False, device_equ_mode=None, conflict=False, message=None
        )
    requested_eq = mount is MountMode.EQ
    conflict = equ_mode != requested_eq
    message = None
    if conflict:
        device_word = "EQ" if equ_mode else "alt-az"
        plan_word = "EQ" if requested_eq else "alt-az"
        message = (
            f"the connected device currently reports {device_word} alignment, but this plan "
            f"is for {plan_word} mode; the plan was not changed to match it"
        )
    return DeviceMountModeCheck(
        checked=True, device_equ_mode=equ_mode, conflict=conflict, message=message
    )


def _geometry_model(geometry: FieldGeometry) -> FieldGeometryModel:
    return FieldGeometryModel(
        optic_id=geometry.optic_id,
        label=geometry.label,
        pixel_scale_arcsec=float(geometry.pixel_scale_arcsec),
        width_deg=float(geometry.width_deg),
        height_deg=float(geometry.height_deg),
        diagonal_deg=float(geometry.diagonal_deg),
        inscribed_circle_deg=float(geometry.inscribed_circle_deg),
        provenance=geometry.provenance,
        distortion=geometry.distortion,
        caveats=list(geometry.caveats),
    )


def _shape_model(shape: Shape) -> ShapeModel:
    return ShapeModel(
        points=[OffsetModel(x_deg=float(p.x_deg), y_deg=float(p.y_deg)) for p in shape.points]
    )


def _position_model(position: TargetPosition) -> TargetPositionModel:
    return TargetPositionModel(
        when=position.when,
        hour_angle_deg=float(position.hour_angle_deg),
        declination_deg=float(position.declination_deg),
        altitude_deg=float(position.altitude_deg),
        azimuth_deg=float(position.azimuth_deg),
    )


def _envelope_model(envelope: SessionEnvelope, start_time: datetime) -> SessionEnvelopeModel:
    """``start_time`` is the session's own clock start — every sample's ``when``
    is ``start_time + elapsed_hours``, the timestamp the frame-stack renderer
    needs alongside each sample's position angle (DESIGN.md 3.2, shape 3 of 5)."""
    return SessionEnvelopeModel(
        samples=[
            RotationSampleModel(
                when=start_time + timedelta(hours=sample.elapsed_hours),
                elapsed_hours=sample.elapsed_hours,
                hour_angle_deg=float(sample.hour_angle_deg),
                altitude_deg=float(sample.altitude_deg),
                parallactic_angle_deg=float(sample.parallactic_angle_deg),
                position_angle_deg=float(sample.position_angle_deg),
            )
            for sample in envelope.samples
        ],
        frame_start=_shape_model(envelope.frame_start),
        frame_end=_shape_model(envelope.frame_end),
        swept_envelope=_shape_model(envelope.swept_envelope),
        keep_region=_shape_model(envelope.keep_region),
        boundary_angles_deg=[float(angle) for angle in envelope.boundary_angles_deg],
        swept_radii_deg=[float(radius) for radius in envelope.swept_radii_deg],
        keep_radii_deg=[float(radius) for radius in envelope.keep_radii_deg],
        inscribed_circle_deg=float(envelope.inscribed_circle_deg),
        keep_region_min_diameter_deg=float(envelope.keep_region_min_diameter_deg),
        keep_area_fraction=float(envelope.keep_area_fraction),
        keep_region_area_deg2=float(envelope.keep_region_area_deg2),
        swept_envelope_area_deg2=float(envelope.swept_envelope_area_deg2),
        total_rotation_deg=float(envelope.total_rotation_deg),
        max_abs_rotation_rate_deg_per_hour=envelope.max_abs_rotation_rate_deg_per_hour,
        max_altitude_deg=float(envelope.max_altitude_deg),
        rotation_severity=cast(_Severity, envelope.rotation_severity),
        high_altitude_transit=envelope.high_altitude_transit,
        zenith_singularity=envelope.zenith_singularity,
        warnings=list(envelope.warnings),
    )


def _lens_summary(optic: Optic) -> LensSummary:
    return LensSummary(
        id=optic.id,
        label=optic.label,
        focal_length_mm=optic.focal_length_mm,
        aperture_mm=optic.aperture_mm,
        focal_ratio=optic.focal_ratio,
        design=optic.design,
        distortion=optic.distortion,
        sensor=SensorSummary(
            name=optic.sensor.name,
            w_px=optic.sensor.w_px,
            h_px=optic.sensor.h_px,
            pixel_um=optic.sensor.pixel_um,
        ),
    )


def _calibration_summary(calibration: Calibration) -> CalibrationSummary:
    return CalibrationSummary(
        measured=calibration.measured,
        pixel_scale_arcsec=(
            None
            if calibration.pixel_scale_arcsec is None
            else float(calibration.pixel_scale_arcsec)
        ),
        pa_offset_deg=None
        if calibration.pa_offset_deg is None
        else float(calibration.pa_offset_deg),
        boresight_offset_arcmin=(
            None
            if calibration.boresight_offset_arcmin is None
            else float(calibration.boresight_offset_arcmin)
        ),
        solved_at=calibration.solved_at,
        solve_rms_arcsec=calibration.solve_rms_arcsec,
    )


def _profile_summary(profile: Profile) -> ProfileSummary:
    return ProfileSummary(
        id=profile.id,
        name=profile.name,
        vendor=profile.vendor,
        mount=profile.mount,
        eq_mode_available=profile.eq_mode_available,
        aliases=list(profile.aliases),
        source_urls=list(profile.source_urls),
        capabilities=HardwareCapabilitiesSummary(**asdict(profile.capabilities)),
        defaults=ProfileDefaultsSummary(**asdict(profile.defaults)),
        limits=ProfileLimitsSummary(**asdict(profile.limits)),
        optics=[_lens_summary(optic) for optic in profile.optics],
        calibration=_calibration_summary(profile.calibration),
    )


def _target_resolution_model(obj: CatalogObject) -> TargetResolutionResponse:
    return TargetResolutionResponse(
        name=obj.name,
        designations=list(obj.designations),
        common_name=obj.common_name,
        object_type=obj.object_type,
        ra_j2000_h=float(deg_to_hours(obj.ra_j2000_deg)),
        dec_j2000_deg=float(obj.dec_j2000_deg),
        magnitude=obj.magnitude,
        size_major_arcmin=None if obj.size_major_arcmin is None else float(obj.size_major_arcmin),
        size_minor_arcmin=None if obj.size_minor_arcmin is None else float(obj.size_minor_arcmin),
        position_angle_deg=None
        if obj.position_angle_deg is None
        else float(obj.position_angle_deg),
        constellation=obj.constellation,
        source=obj.source,
        sources=list(obj.sources),
    )


def _field_object_model(obj: CatalogObject) -> FieldObjectModel:
    return FieldObjectModel(
        designation=obj.name,
        common_name=obj.common_name,
        ra_j2000_h=float(deg_to_hours(obj.ra_j2000_deg)),
        dec_j2000_deg=float(obj.dec_j2000_deg),
        object_type=obj.object_type,
        size_major_arcmin=None if obj.size_major_arcmin is None else float(obj.size_major_arcmin),
        size_minor_arcmin=None if obj.size_minor_arcmin is None else float(obj.size_minor_arcmin),
        position_angle_deg=None
        if obj.position_angle_deg is None
        else float(obj.position_angle_deg),
        source=obj.source,
        sources=list(obj.sources),
    )


_OBJECT_INFO_NOT_CONFIGURED_MESSAGE = "no catalogue source configured"


def _object_info_attributions(result: ObjectInfoResult) -> list[ObjectInfoAttributionModel]:
    """Every credit line, built from what this particular lookup actually
    returned — never a static string (CLAUDE.md invariant 7,
    THIRD_PARTY_NOTICES.md requirement 4)."""
    attributions: list[ObjectInfoAttributionModel] = []
    if result.found:
        attributions.append(
            ObjectInfoAttributionModel(text="Wikipedia contributors", url=result.article_url)
        )
    if result.wikidata_qid is not None:
        # CC0: no attribution legally required, credited anyway per the
        # compliance finding's own recommendation.
        attributions.append(
            ObjectInfoAttributionModel(
                text="Wikidata contributors (CC0)",
                url=f"https://www.wikidata.org/wiki/{result.wikidata_qid}",
            )
        )
    if result.image is not None:
        credit_bits = [result.image.license_short_name]
        if result.image.artist:
            credit_bits.append(result.image.artist)
        attributions.append(
            ObjectInfoAttributionModel(
                text=f"Image via Wikimedia Commons ({', '.join(credit_bits)})",
                url=result.image.source_url,
            )
        )
    return attributions


def _object_info_response(designation: str, result: ObjectInfoResult) -> ObjectInfoResponse:
    image = None
    if result.image is not None:
        image = ObjectInfoImageModel(
            url=f"/api/astro/objects/{quote(designation, safe='')}/info/image",
            width=result.image.width,
            height=result.image.height,
            license_short_name=result.image.license_short_name,
            artist=result.image.artist,
            credit=result.image.credit,
            source_url=result.image.source_url,
        )
    message = None if result.found else f"no Wikipedia article found for {result.designation}"
    return ObjectInfoResponse(
        designation=designation,
        configured=True,
        found=result.found,
        common_name=result.common_name,
        title=result.title,
        article_url=result.article_url,
        extract=result.extract,
        wikidata_qid=result.wikidata_qid,
        instance_of=list(result.instance_of),
        discovery_date=result.discovery_date,
        image=image,
        attributions=_object_info_attributions(result),
        message=message,
        cached=result.cached,
        fetched_at=result.fetched_at,
    )


def _bright_star_model(star: BrightStar) -> BrightStarModel:
    return BrightStarModel(
        name=star.name,
        designation=star.designation,
        ra_j2000_h=float(deg_to_hours(star.ra_j2000_deg)),
        dec_j2000_deg=float(star.dec_j2000_deg),
        magnitude=star.magnitude,
    )


def _body_model(position: BodyPosition) -> BodyPositionModel:
    """Straight field-for-field. The astro core already produces RA in the
    JSON-boundary hours form and the phase fields already carry ``None`` where
    they do not apply, so nothing is decided here."""
    return BodyPositionModel(
        id=position.id,
        name=position.name,
        kind=position.kind,
        ra_j2000_h=float(position.ra_j2000_h),
        dec_j2000_deg=float(position.dec_j2000_deg),
        apparent_diameter_arcmin=float(position.apparent_diameter_arcmin),
        illuminated_fraction=position.illuminated_fraction,
        phase_angle_deg=(
            None if position.phase_angle_deg is None else float(position.phase_angle_deg)
        ),
    )


def _event_model(event: EventTime) -> EventTimeModel:
    return EventTimeModel(
        event=str(event.event),
        when=event.when,
        outcome=event.outcome,
        threshold_deg=float(event.threshold_deg),
    )


def _best_target_model(candidate: BestTargetCandidate) -> BestTargetModel:
    obj = candidate.object
    object_type = obj.object_type
    # rank_best_targets' own candidate pool already excludes an unset
    # object_type (ssc.astro.best_targets._candidate_pool) — asserted here
    # too so a future change to that filter fails loudly at serialisation
    # rather than silently emitting a None the response model forbids.
    assert object_type is not None, f"{obj.name}: ranked with no object_type"
    return BestTargetModel(
        designation=obj.name,
        common_name=obj.common_name,
        object_type=object_type,
        ra_j2000_h=float(deg_to_hours(obj.ra_j2000_deg)),
        dec_j2000_deg=float(obj.dec_j2000_deg),
        magnitude=obj.magnitude,
        size_major_arcmin=None if obj.size_major_arcmin is None else float(obj.size_major_arcmin),
        score=candidate.score.score,
        narrowband_friendly=candidate.score.narrowband_friendly,
        moon_sensitivity=candidate.score.moon_sensitivity,
        moon_impact=candidate.score.moon_impact,
        size_fit=candidate.score.size_fit,
        altitude_hours_score=candidate.score.altitude_hours_score,
        altitude_peak_score=candidate.score.altitude_peak_score,
        magnitude_bonus=candidate.score.magnitude_bonus,
        moon_penalty=candidate.score.moon_penalty,
        size_penalty=candidate.score.size_penalty,
        hours_above_limit=candidate.hours_above_limit,
        peak_altitude_deg=float(candidate.peak_altitude_deg),
        peak_when=candidate.peak_when,
        moon_separation_deg=float(candidate.moon_separation_deg),
        moon_altitude_at_peak_deg=float(candidate.moon_altitude_at_peak_deg),
        source=obj.source,
        sources=list(obj.sources),
    )


# -- endpoints ------------------------------------------------------------------ #


@router.get("/profiles", response_model=ProfilesResponse)
async def list_profiles(services: ServicesDep, session: SessionDep) -> ProfilesResponse:
    """Every bundled scope profile — the lens/mount picker the framing and
    mosaic requests draw ``profile_id``/``optic_id`` from."""
    del session
    return ProfilesResponse(
        profiles=[_profile_summary(p) for p in services.astro.profiles.values()]
    )


@router.get("/target", response_model=TargetResolutionResponse)
async def resolve_target(
    query: Annotated[str, Query(min_length=1, max_length=128)],
    services: ServicesDep,
    session: SessionDep,
) -> TargetResolutionResponse:
    """A name (``M31``, ``NGC 2244``, ``Andromeda Galaxy``) to J2000 coordinates plus
    type, magnitude and angular size, from the bundled offline catalogue."""
    del session
    try:
        obj = services.astro.resolve(query)
    except UnknownTargetError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    # Off by default (``ssc.settings.models.SkymapSettings``); read fresh on
    # every resolve so flipping the Settings toggle takes effect without a
    # restart. One cutout for this one resolved target — never a sweep,
    # never bulk prefetch (``CutoutService.schedule_autofetch``'s own
    # docstring).
    if services.settings.skymap.cutout_autofetch_enabled:
        services.cutouts.schedule_autofetch(obj.ra_j2000_deg, obj.dec_j2000_deg)
    return _target_resolution_model(obj)


@router.get("/target-suggestions", response_model=TargetSuggestionsResponse)
async def suggest_targets(
    query: Annotated[str, Query(min_length=1, max_length=128)],
    services: ServicesDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> TargetSuggestionsResponse:
    """Rank unfinished names, aliases, catalogue prefixes and modest typos.

    The lookup is entirely against the same bundled catalogue as ``/target``;
    typing never creates a network dependency at the telescope.
    """
    del session
    return TargetSuggestionsResponse(
        suggestions=[_target_resolution_model(obj) for obj in services.astro.suggest(query, limit)]
    )


@router.get("/comet-suggestions", response_model=CometSuggestionsResponse)
async def suggest_comets(
    query: Annotated[str, Query(min_length=2, max_length=128)],
    instant: Annotated[datetime, Query()],
    services: ServicesDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=20)] = 8,
) -> CometSuggestionsResponse:
    """Search the bundled JPL SBDB comet snapshot and resolve for one instant."""
    del session
    location = require_location(services)
    if instant.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="instant must carry a UTC offset, e.g. 2026-09-09T21:00:00Z",
        )
    try:
        catalog, positions, unavailable = services.astro.comet_suggestions(
            _site_of(location), query, instant, limit
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except EphemerisRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected instant is outside the bundled DE440s ephemeris (1849-2150)",
        ) from exc
    now = datetime.now(tz=UTC)
    return CometSuggestionsResponse(
        comets=[
            CometSuggestionModel(
                spk_id=position.comet.spk_id,
                designation=position.comet.designation,
                name=position.comet.name,
                full_name=position.comet.full_name,
                ra_j2000_h=float(position.ra_j2000_h),
                dec_j2000_deg=float(position.dec_j2000_deg),
                resolved_at=position.when,
                element_epoch_jd_tdb=position.comet.epoch_jd_tdb,
                element_age_days=position.element_age_days,
                solution_date=position.comet.solution_date,
                execution_safe=(
                    comet_execution_rejection(catalog, position.comet, position.when) is None
                ),
                execution_rejection=comet_execution_rejection(
                    catalog, position.comet, position.when
                ),
            )
            for position in positions
        ],
        unavailable=list(unavailable),
        source=catalog.source,
        source_url=catalog.source_url,
        snapshot_fetched_at=catalog.fetched_at,
        snapshot_age_days=catalog.age_days(now),
        stale=catalog.is_stale(now),
        accuracy_note=(
            "Two-body preview from JPL osculating elements; verify a fresh orbit after close "
            "encounters or for precision pointing. Execution refuses stale, uncertain, "
            "non-gravitational, or close-encounter-sensitive solutions."
        ),
    )


@router.get("/constellation", response_model=ConstellationResponse)
async def constellation(
    ra_j2000_h: Annotated[float, Query(ge=0.0, lt=24.0)],
    dec_j2000_deg: Annotated[float, Query(ge=-90.0, le=90.0)],
    session: SessionDep,
) -> ConstellationResponse:
    """Which of the 88 IAU regions a J2000 point falls in (``ssc.astro.constellations``,
    offline, Delporte 1930 boundaries via a from-scratch transcription —
    THIRD_PARTY_NOTICES.md, "Constellations — gate reading of 2026-09-07").
    No location or device needed: this is a pure coordinate lookup, the same
    fact WWT's own engine computes internally for its own purposes, computed
    here independently."""
    del session
    abbreviation = constellation_at(Hours(ra_j2000_h), Deg(dec_j2000_deg))
    names = {boundary.abbreviation: boundary.name for boundary in load_boundaries()}
    return ConstellationResponse(abbreviation=abbreviation, name=names[abbreviation])


@router.get("/constellation-boundaries", response_model=ConstellationBoundariesResponse)
async def constellation_boundaries(session: SessionDep) -> ConstellationBoundariesResponse:
    """Every one of the 88 IAU regions' vertex rings, precessed to J2000
    (``ssc.astro.constellations.boundaries_j2000``) so the frontend never
    precesses anything itself (CLAUDE.md, "The engine boundary is
    architecture" / skymap-viz.md). A fixed transform of fixed data — no
    ``epoch``/``now`` query parameter, because B1875-to-J2000 precession
    does not depend on either; the whole response is as static as
    :func:`ssc.astro.constellations.load_boundaries` itself, which is why
    the frontend is told to cache it with a long ``staleTime`` rather than
    refetch per view."""
    del session
    regions = [
        ConstellationBoundaryRegionModel(
            abbreviation=boundary.abbreviation,
            name=boundary.name,
            rings=[
                [
                    ConstellationBoundaryVertexModel(
                        ra_j2000_deg=float(vertex.ra_j2000_deg),
                        dec_j2000_deg=float(vertex.dec_j2000_deg),
                    )
                    for vertex in ring
                ]
                for ring in boundary.rings
            ],
        )
        for boundary in boundaries_j2000()
    ]
    return ConstellationBoundariesResponse(regions=regions, credit=boundaries_credit())


@router.get("/objects", response_model=FieldObjectsResponse)
async def field_objects(
    ra_deg: Annotated[float, Query(ge=0.0, lt=360.0)],
    dec_deg: Annotated[float, Query(ge=-90.0, le=90.0)],
    radius_deg: Annotated[float, Query(gt=0.0, le=MAX_CONE_RADIUS_DEG)],
    services: ServicesDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_CONE_LIMIT)] = 200,
) -> FieldObjectsResponse:
    """The bundled-catalogue objects within a cone — what the sky map outlines
    in the visible field. Largest first, so the cap keeps the things that
    dominate a field. Each object carries its file's ``source`` credit."""
    del session
    objects = services.astro.catalog.within_cone(ra_deg, dec_deg, radius_deg, limit)
    return FieldObjectsResponse(
        objects=[_field_object_model(obj) for obj in objects],
        sources=list(dict.fromkeys(obj.source for obj in objects)),
    )


@router.get(
    "/objects/{designation}/info",
    response_model=ObjectInfoResponse,
    dependencies=[Depends(rate_limit("objectinfo"))],
)
async def object_info(
    designation: str, services: ServicesDep, session: SessionDep
) -> ObjectInfoResponse:
    """A short description and, when available, a per-item-licensed image
    for one catalogue object clicked on the sky map (M11's click-through
    info). ``designation`` is exactly what ``GET /api/astro/objects`` (or
    ``GET /api/astro/target``) already gave the frontend — not a new id
    scheme.

    Off by default (``THIRD_PARTY_NOTICES.md``'s own recommendation): a disabled
    feature answers ``configured=False`` with a real message, never a bare
    404 or an empty body, and makes no network call at all to reach that
    answer — checked before the catalogue lookup below ever asks for one.
    """
    del session
    try:
        obj = services.astro.resolve(designation)
    except UnknownTargetError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    result = await services.object_info.get_info(obj.name, obj.common_name)
    if result is None:
        return ObjectInfoResponse(
            designation=obj.name,
            configured=False,
            found=False,
            message=_OBJECT_INFO_NOT_CONFIGURED_MESSAGE,
        )
    return _object_info_response(obj.name, result)


@router.get(
    "/objects/{designation}/info/image",
    response_class=Response,
    responses={404: {"description": "no cached image for this object"}},
)
async def object_info_image(
    designation: str, services: ServicesDep, session: SessionDep
) -> Response:
    """The cached image's bytes only — never fetched live from this route
    (``GET .../info`` is what triggers a fetch); a 404 here just means no
    image was ever cached for this object, whether because the feature is
    off, no article was found, or the article had no ``P18`` claim."""
    del session
    try:
        obj = services.astro.resolve(designation)
    except UnknownTargetError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    cached = await services.object_info.get_image_bytes(obj.name)
    if cached is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no cached image")
    data, media_type = cached
    return Response(content=data, media_type=media_type)


@router.get("/stars", response_model=BrightStarsResponse)
async def bright_stars(
    ra_deg: Annotated[float, Query(ge=0.0, lt=360.0)],
    dec_deg: Annotated[float, Query(ge=-90.0, le=90.0)],
    radius_deg: Annotated[float, Query(gt=0.0, le=MAX_CONE_RADIUS_DEG)],
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=MAX_CONE_LIMIT)] = 200,
) -> BrightStarsResponse:
    """The bundled named bright stars within a cone (``ssc.astro.stars``,
    offline, IAU Working Group on Star Names, CC BY 4.0), brightest first —
    what the sky map labels in the visible field. No ``services`` dependency:
    like ``/constellation-boundaries``, this is a pure file read with no
    ephemeris or profile involved."""
    del session
    stars = stars_within_cone(load_bright_stars(), ra_deg, dec_deg, radius_deg, limit)
    return BrightStarsResponse(
        stars=[_bright_star_model(star) for star in stars],
        credit=bright_stars_credit(),
    )


@router.get("/bodies", response_model=BodiesResponse)
async def bodies(
    services: ServicesDep,
    session: SessionDep,
    instant: Annotated[datetime | None, Query()] = None,
) -> BodiesResponse:
    """The Sun, Moon and seven planets at one instant — the sky map's
    solar-system layer (``ssc.astro.bodies``).

    ``instant`` is optional and defaults to now. The wall clock is read *here*,
    at the boundary, and passed down as an argument: ``ssc.astro`` never reads
    it (CLAUDE.md invariant 3). The instant used is echoed back in the
    response so a client that omitted it knows what it got.

    Positions are topocentric on the active location, so this needs one — a
    geocentric Moon is up to 1 deg wrong, twice its own diameter. Hence the
    same 409-if-unset guard the rest of this module uses rather than a
    silent fallback to the geocentre.
    """
    del session
    location = require_location(services)
    when = instant if instant is not None else datetime.now(tz=UTC)
    if when.tzinfo is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="instant must carry a UTC offset, e.g. 2026-09-09T21:00:00Z",
        )
    try:
        positions = services.astro.bodies(_site_of(location), when)
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except EphemerisRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected instant is outside the bundled DE440s ephemeris (1849-2150)",
        ) from exc
    return BodiesResponse(
        instant=when,
        bodies=[_body_model(position) for position in positions],
        credit=bodies_credit(),
    )


@router.post("/framing", response_model=FramingResponse)
async def framing(
    body: FramingRequest, services: ServicesDep, session: SessionDep
) -> FramingResponse:
    """The field geometry, the five-shape alt-az overlay (or the single EQ
    rectangle) and the dense adaptive ``PA(t)`` sample set the time scrubber
    and frame stack both draw from (DESIGN.md 3.2)."""
    del session
    location = require_location(services)
    site = _site_of(location)
    horizon_profile = _horizon_profile_of(services)
    try:
        envelope, start, visibility = await services.astro.compute(
            services.astro.framing,
            site=site,
            profile_id=body.profile_id,
            optic_id=body.optic_id,
            mount=body.mount,
            ra_j2000_h=Hours(body.ra_j2000_h),
            dec_j2000_deg=Deg(body.dec_j2000_deg),
            pa_offset_deg=Deg(body.pa_offset_deg),
            start_time=body.start_time,
            session_length_hours=body.session_length_hours,
            horizon_profile=horizon_profile,
            max_pa_step_deg=Deg(body.max_pa_step_deg),
            boundary_samples=body.boundary_samples,
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except EphemerisRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected night is outside the bundled DE440s ephemeris (1849-2150)",
        ) from exc
    except (UnknownProfileError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return FramingResponse(
        mount=envelope.mount,
        geometry=_geometry_model(envelope.geometry),
        location=location,
        start_position=_position_model(start),
        envelope=_envelope_model(envelope, body.start_time),
        device_mount_check=await _device_mount_check(services, body.mount),
        visibility=_framing_visibility_model(visibility),
    )


@router.post("/mosaic", response_model=MosaicResponse)
async def mosaic(body: MosaicRequest, services: ServicesDep, session: SessionDep) -> MosaicResponse:
    """``ra_num x dec_num`` panel grid, RA-convergence corrected, with every
    panel's own rotation envelope (DESIGN.md 3.3) — field rotation compounds
    across a mosaic, so each panel gets its own keep-region, not the centre's."""
    del session
    location = require_location(services)
    site = _site_of(location)
    spec = MosaicSpec(
        ra_num=body.mosaic.ra_num, dec_num=body.mosaic.dec_num, overlap_pct=body.mosaic.overlap_pct
    )
    try:
        plan, start = await services.astro.compute(
            services.astro.mosaic,
            site=site,
            profile_id=body.profile_id,
            optic_id=body.optic_id,
            mount=body.mount,
            ra_j2000_h=Hours(body.ra_j2000_h),
            dec_j2000_deg=Deg(body.dec_j2000_deg),
            pa_offset_deg=Deg(body.pa_offset_deg),
            start_time=body.start_time,
            session_length_hours=body.session_length_hours,
            spec=spec,
            max_pa_step_deg=Deg(body.max_pa_step_deg),
            boundary_samples=body.boundary_samples,
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (UnknownProfileError, KeyError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    panels: list[MosaicPanelModel] = []
    for panel in plan.panels:
        # AstroService.mosaic always supplies a center_session, so mosaic_plan()
        # always computes a per-panel envelope; see ssc.astro.mosaic.mosaic_plan.
        assert panel.envelope is not None
        panels.append(
            MosaicPanelModel(
                center=PanelCenterModel(
                    row=panel.center.row,
                    col=panel.center.col,
                    ra_j2000_h=float(deg_to_hours(panel.center.ra_j2000_deg)),
                    dec_j2000_deg=float(panel.center.dec_j2000_deg),
                ),
                envelope=_envelope_model(panel.envelope, body.start_time),
            )
        )

    return MosaicResponse(
        mount=plan.mount,
        geometry=_geometry_model(plan.geometry),
        location=location,
        start_position=_position_model(start),
        panel_count=plan.panel_count,
        panels=panels,
        worst_rotation_severity=cast(_Severity, plan.worst_rotation_severity),
        warnings=list(plan.warnings),
        device_mount_check=await _device_mount_check(services, body.mount),
    )


@router.post("/night", response_model=NightResponse)
async def night(body: NightRequest, services: ServicesDep, session: SessionDep) -> NightResponse:
    """Twilight bands, sun/moon rise-set-transit, moon phase, and — if a target
    is given — its rise/transit/set events, altitude curve and separation from
    the Moon. The Gantt's x-axis and each target block's altitude curve both
    render from this (DESIGN.md 4.4)."""
    del session
    location = require_location(services)
    site = _site_of(location)
    horizon_profile = _horizon_profile_of(services)
    target_ra_dec: tuple[Hours, Deg] | None = None
    if body.ra_j2000_h is not None and body.dec_j2000_deg is not None:
        target_ra_dec = (Hours(body.ra_j2000_h), Deg(body.dec_j2000_deg))
    try:
        result = await services.astro.compute(
            services.astro.night,
            site=site,
            start=body.start_time,
            end=body.end_time,
            target_ra_dec=target_ra_dec,
            min_altitude_deg=Deg(body.min_altitude_deg),
            altitude_samples=body.altitude_samples,
            horizon_profile=horizon_profile,
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except EphemerisRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="the selected night is outside the bundled DE440s ephemeris (1849-2150)",
        ) from exc

    return NightResponse(
        location=location,
        window_start=body.start_time,
        window_end=body.end_time,
        solar_events=[_event_model(e) for e in result.solar_events],
        lunar_events=[_event_model(e) for e in result.lunar_events],
        moon=MoonInfoModel(
            illuminated_fraction=result.moon_illuminated_fraction,
            phase_angle_deg=float(result.moon_phase_angle_deg),
            waxing=result.moon_waxing,
            separation_deg=(
                None if result.moon_separation_deg is None else float(result.moon_separation_deg)
            ),
        ),
        target_events=(
            None
            if result.target_events is None
            else [_event_model(e) for e in result.target_events]
        ),
        altitude_curve=(
            None
            if result.altitude_curve is None
            else [
                AltitudeSampleModel(when=when, altitude_deg=float(altitude))
                for when, altitude in result.altitude_curve
            ]
        ),
        moon_separation_curve=(
            None
            if result.moon_separation_curve is None
            else [
                MoonSeparationSampleModel(when=when, separation_deg=float(separation))
                for when, separation in result.moon_separation_curve
            ]
        ),
        horizon_profile_active=result.horizon_profile_active,
        visibility_intervals=(
            None
            if result.visibility_intervals is None
            else [_interval_model(interval) for interval in result.visibility_intervals]
        ),
    )


@router.get("/moon-altitude", response_model=MoonAltitudeSeriesResponse)
async def moon_altitude(
    start: Annotated[datetime, Query()],
    end: Annotated[datetime, Query()],
    services: ServicesDep,
    session: SessionDep,
    step_min: Annotated[float, Query(gt=0.0, le=180.0)] = 10.0,
) -> MoonAltitudeSeriesResponse:
    """The Moon's own altitude across ``[start, end]`` — the Scheduling tab's
    grey moon curve (DESIGN.md 4.4). Distinct from ``/night``'s
    ``altitude_curve``, which follows a fixed-RA/Dec target, not the Moon."""
    del session
    location = require_location(services)
    site = _site_of(location)
    if end <= start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="end must be after start",
        )
    span_min = (end - start).total_seconds() / 60.0
    samples = max(2, round(span_min / step_min) + 1)
    try:
        series = await services.astro.compute(
            services.astro.moon_altitude_series, site, start, end, samples
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    return MoonAltitudeSeriesResponse(
        samples=[MoonAltitudeSampleModel(at=when, alt_deg=float(alt)) for when, alt in series]
    )


@router.post("/annual-altitude", response_model=AnnualAltitudeResponse)
async def annual_altitude(
    body: AnnualAltitudeRequest, services: ServicesDep, session: SessionDep
) -> AnnualAltitudeResponse:
    """When in the year is this target actually worth imaging — one summary
    sample per night over ``body.nights`` consecutive nights starting at
    ``body.start_time`` (DESIGN.md 4.4): the target's peak apparent altitude
    during astronomical darkness, when that peak occurred, and how many hours
    it spent above ``body.min_altitude_deg`` while it was dark. This is the
    yearly counterpart to ``/night``'s per-night altitude curve — a POST, like
    ``/night`` and unlike ``/moon-altitude``, because the body carries a
    target's RA/Dec rather than just a time window.

    A night with no astronomical darkness at all (high latitude, midsummer)
    or where the target never clears the limit is reported honestly rather
    than as a guessed zero — see ``ssc.astro.events.AnnualAltitudeNight``."""
    del session
    location = require_location(services)
    site = _site_of(location)
    try:
        nights = await services.astro.compute(
            services.astro.annual_altitude_profile,
            site=site,
            ra_j2000_h=Hours(body.ra_j2000_h),
            dec_j2000_deg=Deg(body.dec_j2000_deg),
            start=body.start_time,
            nights=body.nights,
            min_altitude_deg=Deg(body.min_altitude_deg),
            step_minutes=body.step_minutes,
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return AnnualAltitudeResponse(
        location=location,
        min_altitude_deg=body.min_altitude_deg,
        nights=[
            AnnualAltitudeNightModel(
                window_start=night.window_start,
                darkness_hours=night.darkness_hours,
                max_altitude_deg=float(night.max_altitude_deg),
                max_when=night.max_when,
                peak_altitude_deg=(
                    None if night.peak_altitude_deg is None else float(night.peak_altitude_deg)
                ),
                peak_when=night.peak_when,
                hours_above_limit=night.hours_above_limit,
            )
            for night in nights
        ],
    )


@router.post("/best-targets", response_model=BestTargetsResponse)
async def best_targets(
    body: BestTargetsRequest, services: ServicesDep, session: SessionDep
) -> BestTargetsResponse:
    """ "Best targets tonight" (DESIGN.md 3.5): the top ``body.top_n`` bundled
    catalogue targets for the night in ``[body.start_time, body.end_time]``,
    ranked by how good *this* night actually is for each of them — Moon
    altitude, illumination and separation weighed against whether the
    target's light is the narrow emission lines the Seestar's own
    dual/multi-narrowband filter passes, not Moon phase alone. See
    ``ssc.astro.best_targets`` for the full scoring model and the reasoning
    behind it; every result carries the component values that produced its
    score, never a bare number."""
    del session
    location = require_location(services)
    site = _site_of(location)
    try:
        result = await services.astro.compute(
            services.astro.best_targets,
            site=site,
            profile_id=body.profile_id,
            optic_id=body.optic_id,
            start=body.start_time,
            end=body.end_time,
            min_altitude_deg=Deg(body.min_altitude_deg),
            top_n=body.top_n,
            max_candidate_magnitude=body.max_candidate_magnitude,
            allowed_object_types=(
                None if body.allowed_object_types is None else frozenset(body.allowed_object_types)
            ),
        )
    except AstroUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (UnknownProfileError, KeyError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc

    return BestTargetsResponse(
        location=location,
        window_start=body.start_time,
        window_end=body.end_time,
        min_altitude_deg=body.min_altitude_deg,
        darkness_hours=result.darkness_hours,
        moon_illuminated_fraction=result.moon_illuminated_fraction,
        candidates_considered=result.candidates_considered,
        targets=[_best_target_model(candidate) for candidate in result.results],
        note=result.note,
    )


@router.post("/auto-night", response_model=AutoNightResponse)
async def auto_night(
    body: AutoNightRequest, services: ServicesDep, session: SessionDep
) -> AutoNightResponse:
    """Generate a preview document only; saving and execution are separate actions."""
    from ssc.astro.auto_night import SETUP_MINUTES, SHUTDOWN_MINUTES, STARTUP_MINUTES
    from ssc.plan.models import (
        AbsoluteTimeSpec,
        CatalogTargetSpec,
        ConstraintsSpec,
        DurationEndSpec,
        FramingSpec,
        GotoAndStackItem,
        Plan,
        PlanItem,
        ShutdownItem,
        StartupItem,
    )

    del session
    location = require_location(services)
    if not location.timezone:
        raise HTTPException(status_code=422, detail="location_timezone_required")
    try:
        zone = ZoneInfo(location.timezone)
    except ZoneInfoNotFoundError as exc:
        raise HTTPException(status_code=422, detail="location_timezone_invalid") from exc
    try:
        result = await services.astro.compute(
            services.astro.auto_night,
            site=_site_of(location),
            profile_id=body.profile_id,
            optic_id=body.optic_id,
            start=body.start_time,
            end=body.end_time,
            min_altitude_deg=body.min_altitude_deg,
            max_candidate_magnitude=body.max_candidate_magnitude,
            allowed_object_types=None
            if body.allowed_object_types is None
            else frozenset(body.allowed_object_types),
            available_filters=frozenset(body.available_filters),
            block_minutes=body.block_minutes,
            max_targets=body.max_targets,
            horizon_profile=_horizon_profile_of(services),
        )
    except AstroUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except (UnknownProfileError, KeyError, ValueError, EphemerisRangeError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def absolute(when: datetime) -> AbsoluteTimeSpec:
        local = when.astimezone(zone)
        return AbsoluteTimeSpec(
            local_datetime=local.replace(tzinfo=None), fold=cast(Literal[0, 1], local.fold)
        )

    items: list[PlanItem] = []
    if result.blocks:
        items.append(
            StartupItem(
                id=uuid4().hex,
                start=absolute(result.blocks[0].start_time - timedelta(minutes=STARTUP_MINUTES)),
                end=DurationEndSpec(minutes=STARTUP_MINUTES),
            )
        )
        for block in result.blocks:
            obj = block.candidate.object
            items.append(
                GotoAndStackItem(
                    id=uuid4().hex,
                    start=absolute(block.start_time),
                    end=DurationEndSpec(minutes=body.block_minutes + SETUP_MINUTES),
                    target=CatalogTargetSpec(
                        name=obj.designations[0],
                        ra_j2000_h=float(obj.ra_j2000_deg) / 15,
                        dec_j2000_deg=float(obj.dec_j2000_deg),
                    ),
                    framing=FramingSpec(mode=body.mount_mode, lens=body.optic_id),
                    constraints=ConstraintsSpec(min_alt_deg=body.min_altitude_deg),
                    exposure_sec=body.exposure_sec,
                    gain=body.gain,
                    filter_name=block.filter_name,
                    autofocus_before=True,
                )
            )
        items.append(
            ShutdownItem(
                id=uuid4().hex,
                start=absolute(result.blocks[-1].end_time),
                end=DurationEndSpec(minutes=SHUTDOWN_MINUTES),
            )
        )
    now = datetime.now(UTC)
    document = Plan(
        id=uuid4().hex,
        name="Auto night " + body.start_time.astimezone(zone).date().isoformat(),
        items=tuple(items),
        created_at=now,
        updated_at=now,
    )
    return AutoNightResponse(
        document=document,
        blocks=[
            AutoNightBlockModel(
                designation=b.candidate.object.designations[0],
                common_name=b.candidate.object.common_name,
                object_type=cast(ObjectType, b.candidate.object.object_type),
                **{k: v for k, v in asdict(b).items() if k != "candidate"},
            )
            for b in result.blocks
        ],
        gaps=[AutoNightGapModel(**asdict(g)) for g in result.gaps],
        moon_illuminated_fraction=result.moon_illuminated_fraction,
        darkness_hours=result.darkness_hours,
        scheduled_hours=result.scheduled_hours,
        candidates_considered=result.candidates_considered,
        note="wide_ir_only_weather_unchecked" if body.optic_id != "tele" else result.note,
    )
