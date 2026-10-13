# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Sky-survey tiles, proxied and cached by ssc-core so the map works offline
(``THIRD_PARTY_NOTICES.md`` requirement 5; CLAUDE.md invariant 8). Thin: parse,
delegate to ``ssc.skymap``, serialise.

A tile that cannot be served is a **404 with a JSON body naming the reason**
(``offline``, ``upstream`` with the status, ``missing``, …). Never a blank or
placeholder image: the map must show a hole it can explain, not imagery it
cannot (CLAUDE.md invariant 7).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.reference_image import (
    MAX_REFERENCE_BYTES,
    reference_image_suffix,
    solve_reference_image,
)
from ssc.api.schemas import (
    CacheClearResponse,
    CutoutJobResponse,
    CutoutListResponse,
    CutoutManifestResponse,
    CutoutPieceModel,
    CutoutRequest,
    CutoutSourceModel,
    CutoutWcsModel,
    SkymapImportResponse,
    SurveyCreditModel,
    SurveyModel,
    SurveysResponse,
)
from ssc.skymap import (
    CUTOUT_REGISTRY,
    REGISTRY,
    CutoutNotFoundError,
    CutoutRefusedError,
    TileScheme,
    TileUnavailableError,
    find_cutout_source,
    hips,
    mercator,
    toast,
)
from ssc.skymap.cutouts import CutoutPieceRecord
from ssc.skymap.surveys import client_tile_template
from ssc.state.models import OperationState

router = APIRouter(prefix="/api/skymap", tags=["skymap"])

#: Tiles never change for a given survey release; the browser may keep them
#: for a month and must not revalidate on every pan. ``private``: the
#: response rides on a session cookie.
_TILE_CACHE_CONTROL: Final = "private, max-age=2592000, immutable"
#: A miss is not immutable — the archive may be back in a minute.
_MISS_CACHE_CONTROL: Final = "no-store"


def _unavailable(exc: TileUnavailableError, survey: str) -> JSONResponse:
    body: dict[str, object] = {"reason": exc.reason, "survey": survey, "detail": exc.detail}
    if exc.upstream_status is not None:
        body["upstream_status"] = exc.upstream_status
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=body,
        headers={"Cache-Control": _MISS_CACHE_CONTROL},
    )


@router.get("/surveys", response_model=SurveysResponse)
async def list_surveys(services: ServicesDep, session: SessionDep) -> SurveysResponse:
    del session
    skymap = services.skymap
    surveys: list[SurveyModel] = []
    for survey in REGISTRY:
        state = await skymap.survey_state(survey)
        surveys.append(
            SurveyModel(
                id=survey.id,
                label=survey.label,
                kind=survey.kind,
                scheme=survey.scheme,
                frame=survey.frame,
                tile_format=survey.tile_format,
                max_order=survey.max_order,
                credit=SurveyCreditModel(label=survey.credit_label, url=survey.credit_url),
                upstream_host=survey.upstream_host,
                bundle_allowed=survey.bundle_allowed,
                redistributable_pack_url=survey.redistributable_pack_url,
                blend_only=survey.blend_only,
                bundled=survey.bundled,
                enabled=state.enabled,
                reason=state.reason,
                production_cleared=survey.production_cleared,
                rights_notice=survey.rights_notice,
                tile_url_template=client_tile_template(survey),
                properties_url=(
                    f"/api/skymap/tiles/{survey.id}/properties"
                    if survey.scheme is TileScheme.HIPS
                    else None
                ),
                cached_tiles=state.cached_tiles,
                cached_bytes=state.cached_bytes,
                missing_tiles=state.missing_tiles,
                pack_complete_order=state.pack_complete_order,
            )
        )
    cutout_sources = [
        CutoutSourceModel(
            id=source.id.value,
            label=source.label,
            credit=SurveyCreditModel(label=source.credit_label, url=source.credit_url),
            bands=source.bands,
            default_bands=source.default_bands,
            optional=source.optional,
            max_pixels=source.max_pixels,
        )
        for source in CUTOUT_REGISTRY
    ]
    return SurveysResponse(
        surveys=surveys,
        cache_bytes=services.state.snapshot.skymap.cache_bytes,
        cache_max_bytes=services.config.tiles_max_bytes,
        cutout_sources=cutout_sources,
    )


@router.get(
    "/tiles/{survey}/properties",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def hips_properties(survey: str, services: ServicesDep, session: SessionDep) -> Response:
    del session
    try:
        data = await services.skymap.get_properties(survey)
    except TileUnavailableError as exc:
        return _unavailable(exc, survey)
    return Response(
        content=data,
        media_type="text/plain; charset=utf-8",
        headers={"Cache-Control": _TILE_CACHE_CONTROL},
    )


@router.get(
    "/tiles/{survey}/toast/{level}/{x}/{y}.{ext}",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def toast_tile(
    survey: str, level: int, x: int, y: int, ext: str, services: ServicesDep, session: SessionDep
) -> Response:
    """One TOAST tile (``level``, ``x``, ``y``) — the pyramid WWT draws."""
    del session
    try:
        npix = toast.pack(level, x, y)
    except ValueError as exc:
        return _unavailable(TileUnavailableError("out_of_range", str(exc)), survey)
    return await _serve_tile(services, survey, level, npix, ext)


@router.get(
    "/tiles/{survey}/mercator/{z}/{x}/{y}.{ext}",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def mercator_tile(
    survey: str, z: int, x: int, y: int, ext: str, services: ServicesDep, session: SessionDep
) -> Response:
    """One Web Mercator tile (``z``, ``x``, ``y``) — Legacy Surveys' own scheme."""
    del session
    try:
        npix = mercator.pack(z, x, y)
    except ValueError as exc:
        return _unavailable(TileUnavailableError("out_of_range", str(exc)), survey)
    return await _serve_tile(services, survey, z, npix, ext)


@router.get(
    "/tiles/{survey}/Norder{order}/Dir{dir}/Npix{npix}.{ext}",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def hips_tile_standard_layout(
    survey: str,
    order: int,
    dir: int,  # noqa: A002 — the HiPS path component is literally ``Dir``
    npix: int,
    ext: str,
    services: ServicesDep,
    session: SessionDep,
) -> Response:
    """One HiPS tile at its standard path, so ``/tiles/<survey>/`` is a HiPS
    root a stock client can point at (``properties`` sits beside it). A
    ``Dir`` that is not ``npix``'s bucket is refused, not corrected."""
    del session
    if dir != hips.tile_dir(npix):
        return _unavailable(
            TileUnavailableError("out_of_range", f"Dir{dir} is not the bucket of Npix{npix}"),
            survey,
        )
    return await _serve_tile(services, survey, order, npix, ext)


@router.get(
    "/tiles/{survey}/{order}/{npix}.{ext}",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def hips_tile(
    survey: str, order: int, npix: int, ext: str, services: ServicesDep, session: SessionDep
) -> Response:
    """One HiPS tile (``Norder{order}/Dir…/Npix{npix}.{ext}`` upstream)."""
    del session
    return await _serve_tile(services, survey, order, npix, ext)


async def _serve_tile(
    services: ServicesDep, survey: str, order: int, npix: int, ext: str
) -> Response:
    try:
        tile = await services.skymap.get_tile(survey, order, npix, ext)
    except TileUnavailableError as exc:
        return _unavailable(exc, survey)
    return Response(
        content=tile.data,
        media_type=tile.media_type,
        headers={"Cache-Control": _TILE_CACHE_CONTROL, "X-SSC-Tile-Source": tile.source},
    )


@router.delete(
    "/cache/{survey}",
    response_model=CacheClearResponse,
    dependencies=[Depends(rate_limit("skymap"))],
)
async def clear_cache(
    survey: str, services: ServicesDep, session: ControlDep
) -> CacheClearResponse:
    """Delete one survey's cached tiles, packs included. The only way a pack goes."""
    del session
    try:
        freed = await services.skymap.clear(survey)
    except TileUnavailableError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=exc.detail) from exc
    return CacheClearResponse(survey=survey, freed_bytes=freed)


# -- target-field cutouts (ssc/skymap/cutouts.py) ---------------------------- #


def _cutout_piece_model(cutout_id: str, piece: CutoutPieceRecord) -> CutoutPieceModel:
    return CutoutPieceModel(
        idx=piece.idx,
        band=piece.band,
        format=piece.format,
        bytes=piece.bytes,
        width=piece.width,
        height=piece.height,
        wcs=(
            None
            if piece.wcs is None
            else CutoutWcsModel(
                crpix1=piece.wcs.crpix1,
                crpix2=piece.wcs.crpix2,
                crval1=piece.wcs.crval1,
                crval2=piece.wcs.crval2,
                cd1_1=piece.wcs.cd1_1,
                cd1_2=piece.wcs.cd1_2,
                cd2_1=piece.wcs.cd2_1,
                cd2_2=piece.wcs.cd2_2,
                ctype1=piece.wcs.ctype1,
                ctype2=piece.wcs.ctype2,
            )
        ),
        stretch=piece.stretch,
        fits_url=f"/api/skymap/cutouts/{cutout_id}/pieces/{piece.idx}.{piece.format}",
        # A rendered display PNG when one exists (`ssc.skymap.cutouts`'
        # `_render_display`, from a FITS piece); otherwise, for a `jpg`
        # piece, its own raw file -- Legacy Surveys' or SDSS's own
        # quicklook JPEG is already a ready-to-show composite, not a raw
        # science file needing our own stretch, so there is nothing to
        # render and no reason to make the viewer wait on a second copy of
        # bytes it already has. `None` only for a `fits` piece with no
        # display PNG (never expected in practice; `_render_display` covers
        # every plane count `read_fits_image` accepts).
        display_png_url=(
            f"/api/skymap/cutouts/{cutout_id}/pieces/{piece.idx}.png"
            if piece.display_png_path is not None
            else (
                f"/api/skymap/cutouts/{cutout_id}/pieces/{piece.idx}.{piece.format}"
                if piece.format == "jpg"
                else None
            )
        ),
    )


@router.get("/cutouts", response_model=CutoutListResponse)
async def list_cutouts(
    services: ServicesDep,
    session: SessionDep,
    ra_deg: float | None = Query(default=None, ge=0.0, lt=360.0),
    dec_deg: float | None = Query(default=None, ge=-90.0, le=90.0),
    radius_deg: float | None = Query(default=None, gt=0.0, le=180.0),
    source: str | None = Query(default=None, min_length=1),
) -> CutoutListResponse:
    """Cached cutouts covering a point — what the map may already draw
    offline without asking for anything new."""
    del session
    if source is not None:
        if any(value is not None for value in (ra_deg, dec_deg, radius_deg)):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="select either source or a complete ra/dec/radius point query",
            )
        cutouts = await services.cutouts.list_complete_source(source)
    elif ra_deg is not None and dec_deg is not None and radius_deg is not None:
        cutouts = await services.cutouts.list_covering(ra_deg, dec_deg, radius_deg)
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="ra_deg, dec_deg and radius_deg are required together",
        )
    return CutoutListResponse(cutouts=cutouts)


@router.post(
    "/cutouts",
    response_model=CutoutJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("skymap"))],
)
async def request_cutout(
    body: CutoutRequest, services: ServicesDep, session: ControlDep
) -> CutoutJobResponse:
    """Fetch (or, on a cache hit, simply report) one target-field cutout.
    Progress is in the snapshot's ``skymap.cutouts``, exactly like a tile
    prefetch job."""
    del session
    try:
        cutout = await services.cutouts.request(
            body.survey, body.ra_deg, body.dec_deg, body.size_deg, body.bands
        )
    except CutoutRefusedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return CutoutJobResponse(cutout=cutout)


@router.get("/cutouts/{cutout_id}", response_model=CutoutManifestResponse)
async def get_cutout_manifest(
    cutout_id: str, services: ServicesDep, session: SessionDep
) -> CutoutManifestResponse:
    del session
    try:
        cutout_state, pieces = await services.cutouts.get_manifest(cutout_id)
    except CutoutNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    source = find_cutout_source(cutout_state.source)
    credit_label = cutout_state.source if source is None else source.credit_label
    credit_url = "" if source is None else source.credit_url
    return CutoutManifestResponse(
        id=cutout_state.id,
        source=cutout_state.source,
        ra_deg=cutout_state.ra_deg,
        dec_deg=cutout_state.dec_deg,
        size_deg=cutout_state.size_deg,
        status=cutout_state.status,
        bytes=cutout_state.bytes,
        error=cutout_state.error,
        error_detail=cutout_state.error_detail,
        credit_label=credit_label,
        credit_url=credit_url,
        created_at=cutout_state.created_at,
        updated_at=cutout_state.updated_at,
        pieces=[_cutout_piece_model(cutout_id, piece) for piece in pieces],
    )


@router.get(
    "/cutouts/{cutout_id}/pieces/{idx}.{ext}",
    response_class=Response,
    responses={404: {"description": "unavailable; the JSON body says why"}},
)
async def get_cutout_piece(
    cutout_id: str, idx: int, ext: str, services: ServicesDep, session: SessionDep
) -> Response:
    del session
    try:
        data, media_type = await services.cutouts.get_piece_bytes(cutout_id, idx, ext)
    except CutoutNotFoundError as exc:
        # Never blocks to fetch synchronously: a piece not on disk yet (still
        # fetching, or unreachable with the app currently offline) is a 404
        # naming why, immediately.
        reachable = services.state.snapshot.skymap.upstream_reachable
        reason = "offline" if reachable is False else "not_found"
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"reason": reason, "detail": str(exc)},
            headers={"Cache-Control": _MISS_CACHE_CONTROL},
        )
    return Response(
        content=data, media_type=media_type, headers={"Cache-Control": _TILE_CACHE_CONTROL}
    )


@router.delete(
    "/cutouts/{cutout_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("skymap"))],
)
async def delete_cutout(cutout_id: str, services: ServicesDep, session: ControlDep) -> None:
    """The only way a cutout goes — mirrors ``DELETE /cache/{survey}``."""
    del session
    try:
        await services.cutouts.delete(cutout_id)
    except CutoutNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc


class CutoutDeleteAllResponse(BaseModel):
    """A local response model, not in ``ssc/api/schemas.py`` — that file was
    mid-edit by a concurrent agent when this route was added; nothing here
    is reused elsewhere, so a shared model would only be more to keep in
    sync for no benefit."""

    deleted: int


@router.delete(
    "/cutouts",
    response_model=CutoutDeleteAllResponse,
    dependencies=[Depends(rate_limit("skymap"))],
)
async def delete_all_cutouts(services: ServicesDep, session: ControlDep) -> CutoutDeleteAllResponse:
    """Every cached target-field image, unconditionally — the Settings
    "Delete all" action (2026-09-07: the per-cutout list grew unwieldy).
    Unlike LRU eviction this is never silent about plan-protected images:
    it deletes them too, because the viewer asked for all of them by name."""
    del session
    deleted = await services.cutouts.delete_all()
    return CutoutDeleteAllResponse(deleted=deleted)


# -- offline cache export/import (ssc/skymap/export_import.py) -------------- #


@router.get("/export")
async def export_cache(services: ServicesDep, session: SessionDep) -> StreamingResponse:
    """Every tile and cutout this instance has already fetched, as one zip
    — a portability/backup download of this session's own cache, never a
    pre-built pack (``ssc.skymap.export_import`` module docstring). A plain
    browser download works: the pairing session cookie already rides on
    this request, same as any other same-origin ``GET``."""
    del session
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    filename = f"ssc-skymap-cache-{stamp}.zip"
    return StreamingResponse(
        services.export_import.export_stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


@router.post(
    "/import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=[Depends(rate_limit("skymap"))],
)
async def import_cache(
    services: ServicesDep,
    session: ControlDep,
    file: Annotated[UploadFile, File()],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Replay an export archive's tiles and cutouts into the local cache. A
    key already cached locally is skipped and kept as-is — imported data
    never overwrites a fresher local fetch. Writes local state, so this
    needs control, exactly like clearing a survey or deleting a cutout."""
    del session
    content = await file.read()

    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Importing sky-map cache", current=0, total=len(content))
        counts = await services.export_import.import_archive(io.BytesIO(content))
        note = (
            f"{counts.tiles_skipped} tile(s) and {counts.cutouts_skipped} cutout(s) were already "
            "cached locally and were kept as-is; "
            f"{counts.tiles_rejected} tile(s) and {counts.cutouts_rejected} cutout(s) were corrupt."
        )
        context.progress(message="Sky-map cache imported", current=len(content), total=len(content))
        return SkymapImportResponse(
            tiles_imported=counts.tiles_imported,
            tiles_skipped=counts.tiles_skipped,
            tiles_rejected=counts.tiles_rejected,
            cutouts_imported=counts.cutouts_imported,
            cutouts_skipped=counts.cutouts_skipped,
            cutouts_rejected=counts.cutouts_rejected,
            note=note,
        ).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="skymap_cache_import",
            work=work,
            message="Accepted sky-map cache archive",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


__all__ = ["router"]


@router.post("/solve-reference", status_code=status.HTTP_202_ACCEPTED,
             response_model=OperationState,
             dependencies=[Depends(rate_limit("skymap"))])
async def solve_reference(request: Request, services: ServicesDep, session: ControlDep,
                          idempotency_key: str | None = Header(default=None, alias="Idempotency-Key")) -> OperationState:
    """Authenticated raw image body; no multipart parsing before authorization.

    The result only supplies planning coordinates. No exposure, calibration,
    camera FOV publication, sync or slew is performed.
    """
    del session
    kind = "skymap_reference_solve"
    if idempotency_key is not None:
        existing = next((item for item in services.operations.items
                         if item.kind == kind and item.idempotency_key == idempotency_key), None)
        if existing is not None:
            return existing
    slot = services.reference_image_slot
    if slot.locked() or services.operations.active(kind) is not None:
        raise HTTPException(status_code=409, detail="A reference image is already being solved.")
    await slot.acquire()
    transferred = False
    try:
        parts: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_REFERENCE_BYTES:
                raise HTTPException(status_code=413, detail="Reference image is larger than 64 MiB.")
            parts.append(chunk)
        content = b"".join(parts)
        try:
            reference_image_suffix(content)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        async def work(context: OperationContext) -> dict[str, object]:
            try:
                return await solve_work(context)
            finally:
                slot.release()

        async def solve_work(context: OperationContext) -> dict[str, object]:
            context.progress(message="Plate solving reference image")
            result = await solve_reference_image(content, services.settings, services.config.data_dir)
            return {"ra_j2000_h": float(result.coord.ra_h),
                    "dec_j2000_deg": float(result.coord.dec_deg),
                    "rotation_deg": result.rotation_deg,
                    "pixel_scale_arcsec": result.pixel_scale_arcsec,
                    "solver": result.solver, "solver_version": result.solver_version}
        try:
            accepted = services.operations.start(kind=kind, work=work,
                                             message="Plate solving reference image",
                                             idempotency_key=idempotency_key)
            transferred = True
            return accepted
        except OperationBusyError as exc:
            raise HTTPException(status_code=409, detail="A reference image is already being solved.") from exc
    finally:
        if not transferred:
            slot.release()
