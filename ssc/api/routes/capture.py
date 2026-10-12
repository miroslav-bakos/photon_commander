# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Driving one exposure and reading the catalogue back (DESIGN.md §9.5).
Thin: parse, delegate to ``ssc/api/capture_orchestrator.py``, serialise.

``POST /api/capture/exposure`` answers **202** — a capture must never block
the request (dispatch contract) — or one of the errors
``CaptureService.request_exposure`` raises, each mapped to the status the
contract pins: **409** (folder unconfigured / already running / the capture
folder template needs a location timezone it does not have), **503** (no
camera hosted), **422** (duration outside the camera's own limits), **501**
(``StartExposure`` not implemented on this driver).
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ssc.api.capture_orchestrator import (
    CalibrationConfigurationError,
    CalibrationRunNotRunningError,
    CaptureAlreadyRunningError,
    CaptureDeviceUnavailableError,
    CaptureDitherSettleError,
    CaptureDurationOutOfRangeError,
    CaptureFolderNotConfiguredError,
    CaptureFrameNotReadyError,
    CaptureTimezoneUnknownError,
)
from ssc.api.capture_service import ExposureNotAvailableError
from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.schemas import (
    ArchiveDeleteFilesRequest,
    ArchiveDeleteFolderRequest,
    ArchiveDeleteResponse,
    ArchiveEntriesResponse,
    ArchiveEntryModel,
    ArchiveTransferRequest,
    ArchiveTransferResponse,
    ArchiveZipRequest,
    CalibrationCaptureStartRequest,
    CalibrationMasterActionRequest,
    CalibrationMasterBuildRequest,
    CalibrationMasterModel,
    CalibrationMastersResponse,
    CalibrationSourceSetModel,
    CalibrationSourceSetsResponse,
    CaptureExposureRequest,
    CaptureFrameModel,
    CaptureFramePlateSolveModel,
    CaptureFrameQualityModel,
    CaptureFramesResponse,
    CaptureSessionModel,
    CaptureSessionsResponse,
    LiveStackDirectoryResponse,
    LiveStackMasterModel,
    LiveStackMastersResponse,
    LiveStackResetRequest,
    LiveStackResponse,
    LiveStackRtspResponse,
    LiveStackRtspStartRequest,
    YoutubeStreamResponse,
)
from ssc.capture import FrameType, Session, debayer
from ssc.capture.archive import (
    ArchiveDeleteResult,
    ArchiveEntry,
    ArchivePathError,
    ArchiveResourceError,
    ArchiveTransferResult,
)
from ssc.capture.master_catalogue import CalibrationMasterCatalogueError
from ssc.settings.secrets import SecretError
from ssc.skymap.fits import FitsError, read_fits_image
from ssc.skymap.png import (
    balance_rgb_for_display,
    encode_grayscale_png,
    encode_rgb_png,
    nina_autostretch,
    suppress_excess_green_for_display,
)
from ssc.stacking.live import LiveStackError
from ssc.state.models import CaptureState, OperationState
from ssc.store.capture import CaptureFrameNotFoundError, CaptureFrameRecord
from ssc.streaming import YOUTUBE_STREAM_KEY_NAME, RtspConfig, YoutubeStreamError
from ssc.streaming.rtsp import RtspError, RtspStatus

router = APIRouter(prefix="/api/capture", tags=["capture"])

_CONTROL_DEPS = [Depends(rate_limit("capture"))]

#: Mirrors ``GET /api/astro/objects``'s own cap shape: a sane, fixed ceiling
#: on one response rather than an unbounded catalogue read.
_MAX_FRAMES_LIMIT = 500


def _rtsp_model(
    stream: RtspStatus,
    *,
    source: Literal["wide_camera", "live_stack"] | None = None,
) -> LiveStackRtspResponse:
    config = stream.config
    return LiveStackRtspResponse(
        active=stream.active,
        url=stream.url,
        clients=stream.clients,
        encoded_version=stream.encoded_version,
        last_error=stream.last_error,
        encoder=stream.encoder,
        host=None if config is None else config.host,
        port=None if config is None else config.port,
        path=None if config is None else config.path,
        width=None if config is None else config.width,
        height=None if config is None else config.height,
        fps=None if config is None else config.fps,
        bitrate_kbps=None if config is None else config.bitrate_kbps,
        update_interval_s=None if config is None else config.update_interval_s,
        auto_crop=None if config is None else config.auto_crop,
        wide_camera_until_stacked=(None if config is None else config.wide_camera_until_stacked),
        source=source,
    )


@router.get(
    "/live-stack/directories",
    response_model=LiveStackDirectoryResponse,
    dependencies=_CONTROL_DEPS,
)
async def browse_live_stack_directories(
    services: ServicesDep,
    path: str | None = Query(default=None),
) -> LiveStackDirectoryResponse:
    """List folders beneath the configured capture root without exposing files."""
    root = services.settings.capture.folder
    if root is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="capture folder is not configured",
        )
    return _browse_live_stack_directories(root, path)


def _browse_live_stack_directories(root: Path, path: str | None) -> LiveStackDirectoryResponse:
    root = root.resolve()
    selected = root if path is None or path.strip() == "" else Path(path).expanduser().resolve()
    if not selected.is_relative_to(root):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"folder browser is restricted to {root}",
        )
    if not selected.is_dir():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="folder does not exist")
    try:
        directories: list[str] = []
        for child in selected.iterdir():
            if child.name.startswith(".") or child.is_symlink():
                continue
            resolved = child.resolve()
            if resolved.is_relative_to(root) and resolved.is_dir():
                directories.append(str(resolved))
        directories.sort(key=str.casefold)
    except PermissionError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="folder is not readable"
        ) from exc
    parent = selected.parent if selected != root else None
    return LiveStackDirectoryResponse(
        path=str(selected),
        parent=None if parent is None else str(parent),
        directories=directories,
    )


def _frame_model(record: CaptureFrameRecord) -> CaptureFrameModel:
    return CaptureFrameModel(
        id=record.id,
        path=str(record.path),
        object_name=record.object_name,
        filter_name=record.filter_name,
        frame_type=FrameType(record.frame_type),
        date_obs=record.date_obs,
        exposure_s=record.exposure_s,
        gain=record.gain,
        ccd_temp_c=record.ccd_temp_c,
        width=record.width,
        height=record.height,
        status=record.status,
        created_at=record.created_at,
    )


def _session_model(session: Session) -> CaptureSessionModel:
    return CaptureSessionModel(
        object_name=session.object_name,
        started_at_utc=session.started_at_utc,
        ended_at_utc=session.ended_at_utc,
        frame_count=len(session.frames),
        filters=sorted(session.filters),
    )


def _archive_model(entry: ArchiveEntry) -> ArchiveEntryModel:
    return ArchiveEntryModel(
        key=entry.key,
        name=entry.name,
        directory=entry.directory,
        kind=entry.kind,
        size_bytes=entry.size_bytes,
        modified_at=entry.modified_at,
        date_obs=entry.date_obs,
        date_source=entry.date_source,
        catalogue_id=entry.catalogue_id,
        object_name=entry.object_name,
        filter_name=entry.filter_name,
        frame_type=entry.frame_type,
        exposure_s=entry.exposure_s,
        gain=entry.gain,
        ccd_temp_c=entry.ccd_temp_c,
        width=entry.width,
        height=entry.height,
        bayer_pattern=entry.bayer_pattern,
        preview_available=entry.preview_available,
    )


def _render_fits_png(path: Path) -> bytes:
    image = read_fits_image(path.read_bytes())
    if image.data.ndim == 2:
        pattern = image.header.get("BAYERPAT")
        if isinstance(pattern, str):
            rgb = debayer(image.data.astype(np.float64), pattern)
            return encode_rgb_png(suppress_excess_green_for_display(nina_autostretch(balance_rgb_for_display(rgb))))
        return encode_grayscale_png(nina_autostretch(image.data.astype(np.float64)))
    if image.data.ndim == 3 and image.data.shape[0] == 3:
        rgb = np.moveaxis(image.data.astype(np.float64), 0, -1)
        return encode_rgb_png(suppress_excess_green_for_display(nina_autostretch(balance_rgb_for_display(rgb))))
    return encode_grayscale_png(nina_autostretch(image.data[0].astype(np.float64)))


def _render_live_pixels_png(pixels: np.ndarray, bayer_pattern: str | None) -> bytes:
    if pixels.ndim == 3 and pixels.shape[2] == 3:
        return encode_rgb_png(suppress_excess_green_for_display(nina_autostretch(balance_rgb_for_display(pixels))))
    if bayer_pattern is not None:
        rgb = debayer(pixels.astype(np.float64), bayer_pattern)
        return encode_rgb_png(suppress_excess_green_for_display(nina_autostretch(balance_rgb_for_display(rgb))))
    return encode_grayscale_png(nina_autostretch(pixels.astype(np.float64)))


@router.post(
    "/exposure",
    response_model=CaptureState,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=_CONTROL_DEPS,
)
async def start_exposure(
    body: CaptureExposureRequest, services: ServicesDep, session: ControlDep
) -> CaptureState:
    del session
    try:
        request = services.capture.start_capture_loop if body.capture_mode == "loop" else services.capture.request_exposure
        return await request(
            duration_s=body.duration_s,
            gain=body.gain,
            object_name=body.object,
            frame_type=body.frame_type,
            optic_id=body.optic_id,
            flat_source=body.flat_source,
            **({"frame_limit": body.frame_limit} if body.capture_mode == "loop" else {}),
        )
    except (
        CaptureFolderNotConfiguredError,
        CaptureAlreadyRunningError,
        CaptureTimezoneUnknownError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except (CaptureDeviceUnavailableError, CaptureDitherSettleError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except CaptureDurationOutOfRangeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    except ExposureNotAvailableError as exc:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail=str(exc)) from exc


@router.post("/loop/stop", response_model=CaptureState, dependencies=_CONTROL_DEPS)
async def stop_capture_loop(services: ServicesDep, session: ControlDep) -> CaptureState:
    del session
    try:
        return services.capture.stop_capture_loop()
    except CaptureAlreadyRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/calibration/start",
    response_model=CaptureState,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=_CONTROL_DEPS,
)
async def start_calibration(
    body: CalibrationCaptureStartRequest, services: ServicesDep, session: ControlDep
) -> CaptureState:
    del session
    try:
        return await services.capture.start_calibration(
            kind=body.kind,
            count=body.count,
            duration_s=body.duration_s,
            gain=body.gain,
            filter_positions=body.filter_positions,
            optic_id=body.optic_id,
            flat_source=body.flat_source,
            generate_master=body.generate_master,
            keep_only_master=body.keep_only_master,
        )
    except (
        CaptureFolderNotConfiguredError,
        CaptureAlreadyRunningError,
        CaptureTimezoneUnknownError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CaptureDeviceUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
        ) from exc
    except (CaptureDurationOutOfRangeError, CalibrationConfigurationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


@router.post("/calibration/cancel", response_model=CaptureState, dependencies=_CONTROL_DEPS)
async def cancel_calibration(
    request: Request, services: ServicesDep, session: ControlDep
) -> CaptureState:
    try:
        result = await services.capture.cancel_calibration()
        logging.getLogger(__name__).warning(
            "Explicit calibration stop requested: client=%s session_label=%r "
            "kind=%s completed=%s requested=%s user_agent=%r",
            request.client.host if request.client else "unknown",
            session.label,
            result.calibration.kind,
            result.calibration.completed_frames,
            result.calibration.requested_count,
            request.headers.get("user-agent", "")[:256],
        )
        return result
    except CalibrationRunNotRunningError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/calibration/source-sets", response_model=CalibrationSourceSetsResponse)
async def list_calibration_source_sets(services: ServicesDep) -> CalibrationSourceSetsResponse:
    groups = await services.capture.list_calibration_source_sets()
    models: list[CalibrationSourceSetModel] = []
    for group in groups:
        bias_group = next(
            (
                candidate
                for candidate in groups
                if candidate.frame_type is FrameType.BIAS
                and candidate.gain == group.gain
                and candidate.width == group.width
                and candidate.height == group.height
                and len(candidate.records) >= 10
            ),
            None,
        )
        blocker: str | None = None
        if len(group.records) < 10:
            blocker = "At least 10 retained frames are required."
        elif group.gain is None:
            blocker = "The retained frames have no gain metadata."
        elif group.frame_type is FrameType.FLAT and bias_group is None:
            blocker = (
                f"Capture at least 10 bias frames at gain {group.gain} and "
                f"{group.width}x{group.height} first."
            )
        models.append(
            CalibrationSourceSetModel(
                frame_ids=[record.id for record in group.records],
                frame_type=group.frame_type,
                frame_count=len(group.records),
                gain=group.gain,
                exposure_s=group.exposure_s,
                filter_name=group.filter_name,
                width=group.width,
                height=group.height,
                started_at=group.started_at,
                ended_at=group.ended_at,
                can_build=blocker is None,
                blocker=blocker,
                compatible_bias_frames=0 if bias_group is None else len(bias_group.records),
            )
        )
    return CalibrationSourceSetsResponse(sets=models)


@router.post(
    "/calibration/masters/build",
    response_model=CaptureState,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=_CONTROL_DEPS,
)
async def build_calibration_master_from_retained(
    body: CalibrationMasterBuildRequest, services: ServicesDep, session: ControlDep
) -> CaptureState:
    del session
    try:
        return await services.capture.start_calibration_master_build(frame_ids=body.frame_ids)
    except (CaptureFolderNotConfiguredError, CaptureAlreadyRunningError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except CalibrationConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc


def _calibration_master_model(record: object) -> CalibrationMasterModel:
    from ssc.capture.master_catalogue import CalibrationMasterRecord

    assert isinstance(record, CalibrationMasterRecord)
    return CalibrationMasterModel(
        name=record.name,
        path=str(record.path),
        frame_type=record.frame_type,
        status=record.status,
        provenance_available=record.provenance_available,
    )


@router.get("/calibration/masters", response_model=CalibrationMastersResponse)
async def list_calibration_masters(services: ServicesDep) -> CalibrationMastersResponse:
    records = await services.capture.list_calibration_masters()
    return CalibrationMastersResponse(
        masters=[_calibration_master_model(record) for record in records]
    )


@router.get("/calibration/masters/image")
async def get_calibration_master_image(
    services: ServicesDep,
    session: SessionDep,
    frame_type: FrameType,
    name: Annotated[str, Query(min_length=1, max_length=255)],
) -> Response:
    """Preview one active generated master without accepting a filesystem path."""
    del session
    try:
        data, media_type = await services.capture.get_calibration_master_image(
            frame_type=frame_type, name=name
        )
    except CalibrationMasterCatalogueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ArchiveResourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except FitsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"calibration master preview is unavailable: {exc}",
        ) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


async def _move_calibration_master(
    body: CalibrationMasterActionRequest,
    services: ServicesDep,
    destination: Literal["active", "archived"],
) -> CalibrationMasterModel:
    try:
        record = await services.capture.move_calibration_master(
            frame_type=body.frame_type, name=body.name, destination=destination
        )
    except CalibrationMasterCatalogueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _calibration_master_model(record)


@router.post(
    "/calibration/masters/archive",
    response_model=CalibrationMasterModel,
    dependencies=_CONTROL_DEPS,
)
async def archive_calibration_master(
    body: CalibrationMasterActionRequest, services: ServicesDep, session: ControlDep
) -> CalibrationMasterModel:
    del session
    return await _move_calibration_master(body, services, "archived")


@router.post(
    "/calibration/masters/restore",
    response_model=CalibrationMasterModel,
    dependencies=_CONTROL_DEPS,
)
async def restore_calibration_master(
    body: CalibrationMasterActionRequest, services: ServicesDep, session: ControlDep
) -> CalibrationMasterModel:
    del session
    return await _move_calibration_master(body, services, "active")


@router.post("/live-stack/reset", response_model=LiveStackResponse, dependencies=_CONTROL_DEPS)
async def reset_live_stack(
    body: LiveStackResetRequest, services: ServicesDep, session: ControlDep
) -> LiveStackResponse:
    del session

    async def master_path(frame_id: int | None, selected_path: str | None) -> Path | None:
        if frame_id is not None and selected_path is not None:
            raise LiveStackError("select either a catalogue master or an uploaded master")
        if frame_id is not None:
            record = await services.capture.frame_record(frame_id)
            return record.path
        if selected_path is None:
            return None
        path = Path(selected_path).resolve()  # noqa: ASYNC240
        root = services.capture.live_stack_master_folder().resolve()
        if path != root and root not in path.parents:
            raise LiveStackError("uploaded calibration master is outside the managed folder")
        if not path.is_file():
            raise CaptureFrameNotFoundError(selected_path)
        return path

    try:
        run = services.capture.reset_live_stack(
            dark_master=await master_path(body.dark_master_id, body.dark_master_path),
            bias_master=await master_path(body.bias_master_id, body.bias_master_path),
            flat_master=await master_path(body.flat_master_id, body.flat_master_path),
            auto_select_masters=body.auto_select_masters,
            watch_folder=body.watch_folder,
            watch_recursive=body.watch_recursive,
            optic_id=body.optic_id,
            binning=body.binning,
        )
        await services.capture.reconcile_stream_automation()
    except CaptureFrameNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LiveStackError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return LiveStackResponse(
        sequence=run.sequence,
        output_path=str(run.output_path),
        parameters_path=str(run.parameters_path),
        frame_count=len(run.frame_paths),
        dark_master=None if run.dark_master is None else str(run.dark_master),
        bias_master=None if run.bias_master is None else str(run.bias_master),
        flat_master=None if run.flat_master is None else str(run.flat_master),
        active=services.capture.live_stack_active,
        error=services.capture.live_stack_error,
        auto_select_masters=services.capture.live_stack_auto_select_masters,
        master_selection_message=services.capture.live_stack_master_selection_message,
        watch_folder=(
            None
            if services.capture.live_stack_watch_folder is None
            else str(services.capture.live_stack_watch_folder)
        ),
        watch_recursive=services.capture.live_stack_watch_recursive,
        processing_file=(
            None
            if services.capture.live_stack_processing_file is None
            else str(services.capture.live_stack_processing_file)
        ),
        pending_files=services.capture.live_stack_pending_count,
        binning=run.binning,
        last_shift_y_px=run.last_shift_y_px,
        last_shift_x_px=run.last_shift_x_px,
        registration_peak=run.registration_peak,
        registration_peak_ratio=run.registration_peak_ratio,
        object_name=run.object_name,
        filter_name=run.filter_name,
        exposure_s=run.exposure_s,
        gain=run.gain,
        ccd_temp_c=run.ccd_temp_c,
        hfr_px=run.hfr_px,
        fwhm_px=run.fwhm_px,
        star_count=run.star_count,
        detected_source_count=run.detected_source_count,
        frame_contrast_snr=run.frame_contrast_snr,
        snr_estimate=run.snr_estimate,
        rejected_count=services.capture.live_stack_rejected_count,
        last_rejection_reason=services.capture.live_stack_last_rejection_reason,
        rejected_folder=(
            None
            if services.capture.live_stack_rejected_folder is None
            else str(services.capture.live_stack_rejected_folder)
        ),
        total_integration_s=(
            None if run.exposure_s is None else run.exposure_s * len(run.frame_paths)
        ),
    )


@router.get("/live-stack/masters", response_model=LiveStackMastersResponse)
async def list_live_stack_masters(services: ServicesDep) -> LiveStackMastersResponse:
    masters: list[LiveStackMasterModel] = []
    try:
        services.capture.live_stack_master_folder()
    except CaptureFolderNotConfiguredError:
        return LiveStackMastersResponse(masters=masters)
    for frame_type in (FrameType.DARK, FrameType.BIAS, FrameType.FLAT):
        for path in services.capture.live_stack_master_files(frame_type):
            masters.append(
                LiveStackMasterModel(name=path.name, path=str(path), frame_type=frame_type)
            )
    return LiveStackMastersResponse(masters=masters)


@router.post(
    "/live-stack/masters",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def upload_live_stack_master(
    services: ServicesDep,
    session: ControlDep,
    frame_type: Annotated[FrameType, Form()],
    file: Annotated[UploadFile, File()],
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    del session
    if frame_type is FrameType.LIGHT:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="live-stack masters must be dark, bias, or flat",
        )
    name = file.filename or "master.fits"
    content = await file.read()

    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message=f"Validating and storing {name}", current=0, total=len(content))
        path = await asyncio.to_thread(
            services.capture.store_live_stack_master,
            name=name,
            frame_type=frame_type,
            content=content,
        )
        context.progress(message=f"Stored {path.name}", current=len(content), total=len(content))
        return LiveStackMasterModel(
            name=path.name, path=str(path), frame_type=frame_type
        ).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="live_stack_master_upload",
            work=work,
            message=f"Accepted {name}",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/live-stack/frame/{frame_id}", response_model=LiveStackResponse, dependencies=_CONTROL_DEPS
)
async def add_live_stack_frame(
    frame_id: int, services: ServicesDep, session: ControlDep
) -> LiveStackResponse:
    del session
    try:
        run = await services.capture.add_live_stack_frame(frame_id)
    except CaptureFrameNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except LiveStackError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return LiveStackResponse(
        sequence=run.sequence,
        output_path=str(run.output_path),
        parameters_path=str(run.parameters_path),
        frame_count=len(run.frame_paths),
        dark_master=None if run.dark_master is None else str(run.dark_master),
        bias_master=None if run.bias_master is None else str(run.bias_master),
        flat_master=None if run.flat_master is None else str(run.flat_master),
        active=services.capture.live_stack_active,
        error=services.capture.live_stack_error,
        auto_select_masters=services.capture.live_stack_auto_select_masters,
        master_selection_message=services.capture.live_stack_master_selection_message,
        watch_folder=(
            None
            if services.capture.live_stack_watch_folder is None
            else str(services.capture.live_stack_watch_folder)
        ),
        watch_recursive=services.capture.live_stack_watch_recursive,
        processing_file=(
            None
            if services.capture.live_stack_processing_file is None
            else str(services.capture.live_stack_processing_file)
        ),
        pending_files=services.capture.live_stack_pending_count,
        binning=run.binning,
        object_name=run.object_name,
        filter_name=run.filter_name,
        exposure_s=run.exposure_s,
        gain=run.gain,
        ccd_temp_c=run.ccd_temp_c,
        hfr_px=run.hfr_px,
        fwhm_px=run.fwhm_px,
        star_count=run.star_count,
        detected_source_count=run.detected_source_count,
        frame_contrast_snr=run.frame_contrast_snr,
        snr_estimate=run.snr_estimate,
        rejected_count=services.capture.live_stack_rejected_count,
        last_rejection_reason=services.capture.live_stack_last_rejection_reason,
        rejected_folder=(
            None
            if services.capture.live_stack_rejected_folder is None
            else str(services.capture.live_stack_rejected_folder)
        ),
        total_integration_s=(
            None if run.exposure_s is None else run.exposure_s * len(run.frame_paths)
        ),
    )


@router.post("/live-stack/poll", response_model=LiveStackResponse, dependencies=_CONTROL_DEPS)
async def poll_live_stack(services: ServicesDep, session: ControlDep) -> LiveStackResponse:
    del session
    try:
        run = await services.capture.poll_live_stack()
    except LiveStackError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return LiveStackResponse(
        sequence=run.sequence,
        output_path=str(run.output_path),
        parameters_path=str(run.parameters_path),
        frame_count=len(run.frame_paths),
        dark_master=None if run.dark_master is None else str(run.dark_master),
        bias_master=None if run.bias_master is None else str(run.bias_master),
        flat_master=None if run.flat_master is None else str(run.flat_master),
        active=services.capture.live_stack_active,
        error=services.capture.live_stack_error,
        auto_select_masters=services.capture.live_stack_auto_select_masters,
        master_selection_message=services.capture.live_stack_master_selection_message,
        watch_folder=(
            None
            if services.capture.live_stack_watch_folder is None
            else str(services.capture.live_stack_watch_folder)
        ),
        watch_recursive=services.capture.live_stack_watch_recursive,
        processing_file=(
            None
            if services.capture.live_stack_processing_file is None
            else str(services.capture.live_stack_processing_file)
        ),
        pending_files=services.capture.live_stack_pending_count,
        binning=run.binning,
        object_name=run.object_name,
        filter_name=run.filter_name,
        exposure_s=run.exposure_s,
        gain=run.gain,
        ccd_temp_c=run.ccd_temp_c,
        hfr_px=run.hfr_px,
        fwhm_px=run.fwhm_px,
        star_count=run.star_count,
        detected_source_count=run.detected_source_count,
        frame_contrast_snr=run.frame_contrast_snr,
        snr_estimate=run.snr_estimate,
        rejected_count=services.capture.live_stack_rejected_count,
        last_rejection_reason=services.capture.live_stack_last_rejection_reason,
        rejected_folder=(
            None
            if services.capture.live_stack_rejected_folder is None
            else str(services.capture.live_stack_rejected_folder)
        ),
        total_integration_s=(
            None if run.exposure_s is None else run.exposure_s * len(run.frame_paths)
        ),
    )


@router.get("/live-stack/image", dependencies=_CONTROL_DEPS)
async def live_stack_image(
    services: ServicesDep, auto_crop: bool = Query(default=False)
) -> Response:
    rendered = await services.capture.render_live_stack_preview(
        _render_live_pixels_png, auto_crop=auto_crop
    )
    if rendered is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no live stack frame is available"
        )
    # The browser URL includes stack sequence, accepted-frame count and crop
    # mode, so each rendered bitmap is immutable. Reuse it instantly when a
    # user returns to the tab instead of recomputing a large autostretch.
    return Response(
        content=rendered,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


@router.get("/live-stack/rtsp", response_model=LiveStackRtspResponse)
async def live_stack_rtsp_status(services: ServicesDep) -> LiveStackRtspResponse:
    return _rtsp_model(
        services.capture.live_stack_rtsp_status,
        source=services.capture.live_stack_rtsp_source,
    )


@router.get("/live-stack/rtsp/preview", dependencies=_CONTROL_DEPS)
async def live_stack_rtsp_preview(services: ServicesDep) -> Response:
    try:
        preview = await services.capture.live_stack_rtsp_preview_png()
    except RtspError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return Response(content=preview, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post(
    "/live-stack/rtsp/start",
    response_model=LiveStackRtspResponse,
    dependencies=_CONTROL_DEPS,
)
async def start_live_stack_rtsp(
    body: LiveStackRtspStartRequest, services: ServicesDep, session: ControlDep
) -> LiveStackRtspResponse:
    del session
    try:
        stream = await services.capture.start_live_stack_rtsp(
            RtspConfig(
                host=body.host,
                port=body.port,
                path=body.path,
                width=body.width,
                height=body.height,
                fps=body.fps,
                bitrate_kbps=body.bitrate_kbps,
                update_interval_s=body.update_interval_s,
                auto_crop=body.auto_crop,
                wide_camera_until_stacked=body.wide_camera_until_stacked,
            )
        )
    except (LiveStackError, RtspError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _rtsp_model(stream, source=services.capture.live_stack_rtsp_source)


@router.post(
    "/live-stack/rtsp/stop",
    response_model=LiveStackRtspResponse,
    dependencies=_CONTROL_DEPS,
)
async def stop_live_stack_rtsp(services: ServicesDep, session: ControlDep) -> LiveStackRtspResponse:
    del session
    return _rtsp_model(await services.capture.stop_live_stack_rtsp())


@router.post(
    "/live-stack/rtsp/restart",
    response_model=LiveStackRtspResponse,
    dependencies=_CONTROL_DEPS,
)
async def restart_live_stack_rtsp(
    services: ServicesDep, session: ControlDep
) -> LiveStackRtspResponse:
    """Apply the latest persisted settings by rebuilding the active stream."""
    del session
    try:
        stream = await services.capture.restart_live_stack_rtsp()
    except (LiveStackError, RtspError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _rtsp_model(stream, source=services.capture.live_stack_rtsp_source)


def _youtube_model(services: ServicesDep) -> YoutubeStreamResponse:
    stream = services.capture.youtube_stream_status
    return YoutubeStreamResponse(
        active=stream.active,
        publishing=stream.publishing,
        configured=YOUTUBE_STREAM_KEY_NAME in services.settings.secret_names,
        last_error=stream.last_error,
    )


@router.get("/live-stack/youtube", response_model=YoutubeStreamResponse)
async def youtube_stream_status(services: ServicesDep) -> YoutubeStreamResponse:
    return _youtube_model(services)


@router.post(
    "/live-stack/youtube/start",
    response_model=YoutubeStreamResponse,
    dependencies=_CONTROL_DEPS,
)
async def start_youtube_stream(services: ServicesDep, session: ControlDep) -> YoutubeStreamResponse:
    del session
    try:
        stream_key = await services.secrets.get(YOUTUBE_STREAM_KEY_NAME)
    except SecretError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if stream_key is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="configure the YouTube stream key in Settings before publishing",
        )
    try:
        await services.capture.start_youtube_stream(stream_key)
    except YoutubeStreamError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _youtube_model(services)


@router.post(
    "/live-stack/youtube/stop",
    response_model=YoutubeStreamResponse,
    dependencies=_CONTROL_DEPS,
)
async def stop_youtube_stream(services: ServicesDep, session: ControlDep) -> YoutubeStreamResponse:
    del session
    await services.capture.stop_youtube_stream()
    return _youtube_model(services)


@router.get("/live-stack", response_model=LiveStackResponse)
async def live_stack_status(services: ServicesDep) -> LiveStackResponse:
    run = services.capture.live_stack_run()
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no live stack has been started"
        )
    return LiveStackResponse(
        sequence=run.sequence,
        output_path=str(run.output_path),
        parameters_path=str(run.parameters_path),
        frame_count=len(run.frame_paths),
        dark_master=None if run.dark_master is None else str(run.dark_master),
        bias_master=None if run.bias_master is None else str(run.bias_master),
        flat_master=None if run.flat_master is None else str(run.flat_master),
        active=services.capture.live_stack_active,
        error=services.capture.live_stack_error,
        auto_select_masters=services.capture.live_stack_auto_select_masters,
        master_selection_message=services.capture.live_stack_master_selection_message,
        watch_folder=(
            None
            if services.capture.live_stack_watch_folder is None
            else str(services.capture.live_stack_watch_folder)
        ),
        watch_recursive=services.capture.live_stack_watch_recursive,
        processing_file=(
            None
            if services.capture.live_stack_processing_file is None
            else str(services.capture.live_stack_processing_file)
        ),
        pending_files=services.capture.live_stack_pending_count,
        binning=run.binning,
        last_shift_y_px=run.last_shift_y_px,
        last_shift_x_px=run.last_shift_x_px,
        registration_peak=run.registration_peak,
        registration_peak_ratio=run.registration_peak_ratio,
        object_name=run.object_name,
        filter_name=run.filter_name,
        exposure_s=run.exposure_s,
        gain=run.gain,
        ccd_temp_c=run.ccd_temp_c,
        hfr_px=run.hfr_px,
        fwhm_px=run.fwhm_px,
        star_count=run.star_count,
        detected_source_count=run.detected_source_count,
        frame_contrast_snr=run.frame_contrast_snr,
        snr_estimate=run.snr_estimate,
        rejected_count=services.capture.live_stack_rejected_count,
        last_rejection_reason=services.capture.live_stack_last_rejection_reason,
        rejected_folder=(
            None
            if services.capture.live_stack_rejected_folder is None
            else str(services.capture.live_stack_rejected_folder)
        ),
        total_integration_s=(
            None if run.exposure_s is None else run.exposure_s * len(run.frame_paths)
        ),
        target_header=services.capture.live_stack_target_header_text(),
    )


@router.post("/live-stack/stop", response_model=LiveStackResponse, dependencies=_CONTROL_DEPS)
async def stop_live_stack(services: ServicesDep, session: ControlDep) -> LiveStackResponse:
    del session
    try:
        run = await services.capture.stop_live_stack()
    except LiveStackError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return LiveStackResponse(
        sequence=run.sequence,
        output_path=str(run.output_path),
        parameters_path=str(run.parameters_path),
        frame_count=len(run.frame_paths),
        dark_master=None if run.dark_master is None else str(run.dark_master),
        bias_master=None if run.bias_master is None else str(run.bias_master),
        flat_master=None if run.flat_master is None else str(run.flat_master),
        active=False,
        error=services.capture.live_stack_error,
        auto_select_masters=services.capture.live_stack_auto_select_masters,
        master_selection_message=services.capture.live_stack_master_selection_message,
        watch_folder=(
            None
            if services.capture.live_stack_watch_folder is None
            else str(services.capture.live_stack_watch_folder)
        ),
        watch_recursive=services.capture.live_stack_watch_recursive,
        processing_file=(
            None
            if services.capture.live_stack_processing_file is None
            else str(services.capture.live_stack_processing_file)
        ),
        pending_files=services.capture.live_stack_pending_count,
        binning=run.binning,
        object_name=run.object_name,
        filter_name=run.filter_name,
        exposure_s=run.exposure_s,
        gain=run.gain,
        ccd_temp_c=run.ccd_temp_c,
        hfr_px=run.hfr_px,
        fwhm_px=run.fwhm_px,
        star_count=run.star_count,
        detected_source_count=run.detected_source_count,
        frame_contrast_snr=run.frame_contrast_snr,
        snr_estimate=run.snr_estimate,
        rejected_count=services.capture.live_stack_rejected_count,
        last_rejection_reason=services.capture.live_stack_last_rejection_reason,
        rejected_folder=(
            None
            if services.capture.live_stack_rejected_folder is None
            else str(services.capture.live_stack_rejected_folder)
        ),
        total_integration_s=(
            None if run.exposure_s is None else run.exposure_s * len(run.frame_paths)
        ),
    )


@router.post(
    "/live-stack/save",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def save_live_stack(
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Saving live stack")
        run = await services.capture.save_live_stack()
        return {
            "sequence": run.sequence,
            "output_path": str(run.output_path),
            "parameters_path": str(run.parameters_path),
            "frame_count": len(run.frame_paths),
        }

    try:
        return services.operations.start(
            kind="live_stack_save",
            work=work,
            message="Starting live-stack save",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/archive", response_model=ArchiveEntriesResponse)
async def list_archive(
    services: ServicesDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=5_000)] = 2_000,
) -> ArchiveEntriesResponse:
    """Discover supported images beneath the configured capture root."""
    del session
    try:
        entries, directories, truncated = await services.capture.list_archive_entries(limit=limit)
    except (CaptureFolderNotConfiguredError, ArchivePathError) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    root = services.settings.capture.folder
    assert root is not None
    usage = await asyncio.to_thread(shutil.disk_usage, root)
    return ArchiveEntriesResponse(
        root_name=root.name,
        storage_total_bytes=usage.total,
        storage_free_bytes=usage.free,
        entries=[_archive_model(entry) for entry in entries],
        directories=directories,
        truncated=truncated,
    )


def _archive_delete_model(result: ArchiveDeleteResult) -> ArchiveDeleteResponse:
    return ArchiveDeleteResponse(
        files_deleted=result.files_deleted,
        catalogue_rows_deleted=result.catalogue_rows_deleted,
        bytes_deleted=result.bytes_deleted,
        folder_deleted=result.folder_deleted,
    )


@router.post(
    "/archive/delete-files",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def delete_archive_files(
    body: ArchiveDeleteFilesRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Permanently delete an exact set of validated archive files."""
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Deleting selected archive files", current=0, total=len(body.keys))
        result = await services.capture.delete_archive_files(body.keys)
        context.progress(message="Archive files deleted", current=len(body.keys), total=len(body.keys))
        return _archive_delete_model(result).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="archive_delete",
            work=work,
            message="Starting archive deletion",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/archive/delete-folder",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def delete_archive_folder(
    body: ArchiveDeleteFolderRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Permanently delete one validated non-root folder and all contents."""
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message=f"Deleting archive folder {body.directory}")
        result = await services.capture.delete_archive_folder(body.directory)
        return _archive_delete_model(result).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="archive_delete",
            work=work,
            message="Starting archive folder deletion",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _archive_transfer_model(result: ArchiveTransferResult) -> ArchiveTransferResponse:
    return ArchiveTransferResponse(
        files_transferred=result.files_transferred,
        directories_transferred=result.directories_transferred,
        bytes_transferred=result.bytes_transferred,
        catalogue_rows_updated=result.catalogue_rows_updated,
        destination=result.destination,
        mode=result.mode,
    )


@router.post(
    "/archive/transfer",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def transfer_archive_files(
    body: ArchiveTransferRequest,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Move or copy selected files and folders to an existing archive folder."""
    del session

    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message=f"{body.mode.title()}ing selected archive items")
        result = await services.capture.transfer_archive_files(
            body.keys, body.directories, body.destination, body.mode
        )
        return _archive_transfer_model(result).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="archive_transfer",
            work=work,
            message=f"Starting archive {body.mode}",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/archive/folder-zip", response_class=FileResponse)
async def download_archive_folder_zip(
    services: ServicesDep,
    session: SessionDep,
    directory: Annotated[str, Query(min_length=1, max_length=2_048)],
) -> FileResponse:
    """Download a folder and its complete real-file subtree as a ZIP archive."""
    del session
    try:
        path, filename = await services.capture.create_archive_zip(directory)
    except (CaptureFolderNotConfiguredError, ArchivePathError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.post("/archive/selection-zip", response_class=FileResponse)
async def download_archive_selection_zip(
    body: ArchiveZipRequest,
    services: ServicesDep,
    session: SessionDep,
) -> FileResponse:
    """Download selected files and complete folder subtrees as one temporary ZIP."""
    del session
    try:
        path, filename = await services.capture.create_archive_selection_zip(
            body.keys, body.directories
        )
    except (CaptureFolderNotConfiguredError, ArchivePathError) as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="application/zip",
        filename=filename,
        background=BackgroundTask(path.unlink, missing_ok=True),
    )


@router.get("/archive/image")
async def get_archive_image(
    services: ServicesDep,
    session: SessionDep,
    key: Annotated[str, Query(min_length=1, max_length=2_048)],
    thumbnail: bool = False,
) -> Response:
    """Preview one archive key; originals are never modified or exposed as paths."""
    del session
    try:
        data, media_type = await services.capture.get_archive_image(key, thumbnail=thumbnail)
    except CaptureFolderNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ArchivePathError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ArchiveResourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except FitsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"FITS preview is unavailable: {exc}",
        ) from exc
    return Response(
        content=data,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/archive/quality", response_model=CaptureFrameQualityModel)
async def get_archive_quality(
    services: ServicesDep,
    session: SessionDep,
    key: Annotated[str, Query(min_length=1, max_length=2_048)],
) -> CaptureFrameQualityModel:
    """Measure star quality on demand for one FITS archive entry."""
    del session
    try:
        quality = await services.capture.get_archive_quality(key)
    except CaptureFolderNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ArchivePathError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ArchiveResourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except FitsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"FITS quality analysis is unavailable: {exc}",
        ) from exc
    return CaptureFrameQualityModel(
        star_count=quality.star_count,
        hfr_px=quality.hfr_px,
        fwhm_px=quality.fwhm_px,
        hfr_arcsec=quality.hfr_arcsec,
        fwhm_arcsec=quality.fwhm_arcsec,
        background=quality.background,
        noise=quality.noise,
        detected_source_count=quality.detected_source_count,
        contrast_snr=quality.contrast_snr,
    )


@router.get("/archive/header", response_model=dict[str, str | int | float | bool])
async def get_archive_header(
    services: ServicesDep,
    session: SessionDep,
    key: Annotated[str, Query(min_length=1, max_length=2_048)],
) -> dict[str, str | int | float | bool]:
    """Return sanitized primary-header values for one FITS archive entry."""
    del session
    try:
        return await services.capture.get_archive_header(key)  # type: ignore[return-value]
    except CaptureFolderNotConfiguredError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ArchivePathError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ArchiveResourceError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=str(exc)
        ) from exc
    except FitsError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"FITS header is unavailable: {exc}",
        ) from exc


@router.get("/frames", response_model=CaptureFramesResponse)
async def list_frames(
    services: ServicesDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=_MAX_FRAMES_LIMIT)] = 50,
) -> CaptureFramesResponse:
    """The catalogue, newest first."""
    del session
    frames = await services.capture.list_frames(limit=limit)
    return CaptureFramesResponse(frames=[_frame_model(frame) for frame in frames])


@router.get("/sessions", response_model=CaptureSessionsResponse)
async def list_sessions(services: ServicesDep, session: SessionDep) -> CaptureSessionsResponse:
    """The catalogue grouped into sessions (DESIGN.md §9.5.4)."""
    del session
    sessions = await services.capture.list_sessions()
    return CaptureSessionsResponse(sessions=[_session_model(entry) for entry in sessions])


@router.get("/frames/{frame_id}/image")
async def get_frame_image(
    frame_id: int,
    services: ServicesDep,
    session: SessionDep,
    thumbnail: bool = False,
) -> Response:
    """A server-rendered PNG of a catalogued frame — debayered and
    auto-stretched, never the raw FITS (the browser never parses FITS;
    ``ssc.api.capture_orchestrator.CaptureService.get_frame_image``'s own
    docstring). ``?thumbnail=true`` returns the same rendering downsampled
    for a timeline strip rather than a separate route, mirroring this
    router's own ``limit`` query param on ``GET /frames``."""
    del session
    try:
        data = await services.capture.get_frame_image(frame_id, thumbnail=thumbnail)
    except CaptureFrameNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CaptureFrameNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return Response(content=data, media_type="image/png")


@router.get("/frames/{frame_id}/quality", response_model=CaptureFrameQualityModel)
async def get_frame_quality(
    frame_id: int, services: ServicesDep, session: SessionDep
) -> CaptureFrameQualityModel:
    """A catalogued frame's numpy-only HFR/FWHM star metric
    (``ssc.api.capture_orchestrator.CaptureService.get_frame_quality``'s own
    docstring, ``ssc-bkv.8``) — the bottom-right quality overlay on the big
    preview reads this. Same 404/409 mapping as ``GET .../image``."""
    del session
    try:
        quality = await services.capture.get_frame_quality(frame_id)
    except CaptureFrameNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except CaptureFrameNotReadyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return CaptureFrameQualityModel(
        star_count=quality.star_count,
        hfr_px=quality.hfr_px,
        fwhm_px=quality.fwhm_px,
        hfr_arcsec=quality.hfr_arcsec,
        fwhm_arcsec=quality.fwhm_arcsec,
        background=quality.background,
        noise=quality.noise,
        detected_source_count=quality.detected_source_count,
        contrast_snr=quality.contrast_snr,
    )


@router.post(
    "/frames/{frame_id}/solve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
    dependencies=_CONTROL_DEPS,
)
async def solve_frame(
    frame_id: int,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    """Plate-solve an existing catalogued FITS; no exposure or slew is performed."""
    del session
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Plate solving saved frame")
        result = await services.capture.solve_frame(frame_id)
        response = CaptureFramePlateSolveModel(
            ra_j2000_hours=float(result.coord.ra_h),
            dec_j2000_deg=float(result.coord.dec_deg),
            date_obs=result.date_obs,
            pixel_scale_arcsec=result.pixel_scale_arcsec,
            rotation_deg=result.rotation_deg,
            solver=result.solver,
            solver_version=result.solver_version,
        )
        return response.model_dump(mode="json")

    try:
        return services.operations.start(
            kind=f"capture_frame_solve:{frame_id}",
            work=work,
            message="Starting saved-frame plate solve",
            idempotency_key=idempotency_key,
            exclusive=False,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


__all__ = ["router"]
