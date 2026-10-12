# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Location provenance, appearance, and the names of keyring secrets.

Two things this router will not do:

* Return a secret value. ``GET`` gives names; ``PUT`` accepts a value and echoes
  back the name list (CLAUDE.md invariant 8).
* Guess a location. Every fix arrives labelled with the source that produced it,
  and :func:`ssc.settings.location.resolve` decides which one is active. The
  active source is always in the response, because a silently wrong location is
  the worst failure this app has (DESIGN.md §5).
"""

from __future__ import annotations

import asyncio
import os
import platform
import shutil
import signal
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response, status

from ssc import host_reboot
from ssc.api.deps import ControlDep, ServicesDep, SessionDep, rate_limit
from ssc.api.external_tools import HostPathListing, list_host_paths, solver_environment
from ssc.api.operation_service import OperationBusyError, OperationContext
from ssc.api.schemas import (
    AppearanceRequest,
    ApplicationRestartRequest,
    ApplicationRestartResponse,
    AuthRequiredRequest,
    AutoLpFilterRequest,
    AutoLpFilterResponse,
    CalibrationPathTemplates,
    CaptureCenterAfterDriftRequest,
    CaptureCenterAfterDriftResponse,
    CaptureDitherRequest,
    CaptureDitherResponse,
    CaptureFilenameTemplateRequest,
    CaptureFilenameTemplateResponse,
    CaptureFolderRequest,
    CaptureFolderResponse,
    CaptureFolderTemplateRequest,
    CaptureFolderTemplateResponse,
    CapturePathTagModel,
    CapturePathTagsResponse,
    CaptureRejectedFramesSettings,
    ConfigurationBundle,
    ConfigurationImportResponse,
    ExternalToolModel,
    ExternalToolsResponse,
    HorizonPointModel,
    HorizonRequest,
    HorizonResponse,
    HostRebootRequest,
    HostRebootResponse,
    HostRebootStatusResponse,
    LocationFixRequest,
    LocationResponse,
    MetNoContactRequest,
    ObjectInfoEnabledRequest,
    ObjectInfoSettingsResponse,
    PreferredSourceRequest,
    RemoteAccessResponse,
    SecretNamesResponse,
    SecretUpsert,
    SecurityResponse,
    SettingsResponse,
    SkymapAutofetchEnabledRequest,
    SkymapAutofetchSettingsResponse,
    StreamingSettingsRequest,
    StreamingSettingsResponse,
    WeatherProviderRequest,
    WeatherProviderStatus,
    WeatherSettingsResponse,
)
from ssc.api.security import SESSION_COOKIE
from ssc.api.weather_service import resolve_metno_contact
from ssc.capture.pathtemplate import PathTemplateError, path_tags
from ssc.platesolve import astap_availability
from ssc.platesolve.astap import resolve_astap_cli
from ssc.remote_access import inspect_tailscale
from ssc.settings.capture_folder import (
    CaptureFolderError,
    default_capture_root,
    user_pictures_folder,
    validate_capture_folder,
)
from ssc.settings.location import resolve as resolve_location
from ssc.settings.models import (
    AppearanceSettings,
    ClientConfigurationSettings,
    ExternalToolsSettings,
    HorizonPointFix,
    HorizonSettings,
    LocationFix,
    LocationSettings,
)
from ssc.settings.secrets import CONTROL_PIN_NAME, SecretError, validate_name
from ssc.state.events import LocationChanged, SettingsChanged
from ssc.state.models import LocationSource, LocationState, OperationState, refresh_mount_staleness
from ssc.streaming import YOUTUBE_STREAM_KEY_NAME
from ssc.weather.providers.openweathermap import DEFAULT_SECRET_NAME as OWM_SECRET_NAME
from ssc.weather.providers.visualcrossing import DEFAULT_SECRET_NAME as VC_SECRET_NAME
from ssc.weather.providers.weatherapi import DEFAULT_SECRET_NAME as WEATHERAPI_SECRET_NAME

router = APIRouter(prefix="/api/settings", tags=["settings"])


def _terminate_for_supervisor_restart() -> None:
    """Exit gracefully after the response; systemd's Restart=always relaunches us."""
    os.kill(os.getpid(), signal.SIGTERM)


@router.post(
    "/restart",
    response_model=ApplicationRestartResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def restart_application(
    body: ApplicationRestartRequest,
    background_tasks: BackgroundTasks,
    services: ServicesDep,
    session: ControlDep,
) -> ApplicationRestartResponse:
    """Restart a supervised installation after returning an acknowledgement."""
    del body, services, session
    if not os.environ.get("INVOCATION_ID"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="application restart is available only when Photon Commander runs as a supervised service",
        )
    background_tasks.add_task(_terminate_for_supervisor_restart)
    return ApplicationRestartResponse(accepted=True)


def _host_reboot_safety_reason(services: ServicesDep) -> str | None:
    snapshot = refresh_mount_staleness(services.state.read().snapshot, now=datetime.now(tz=UTC))
    if snapshot.run is not None and snapshot.run.status in {"running", "paused"}:
        return "Stop the active plan before rebooting the Raspberry Pi."
    capture = snapshot.capture
    if capture is not None and (
        capture.in_progress or capture.loop_active or capture.calibration.status in {"running", "cancelling"}
    ):
        return "Stop capture and calibration before rebooting the Raspberry Pi."
    if services.capture.live_stack_active or services.capture.live_stack_rtsp_status.active:
        return "Stop live stacking and streaming before rebooting the Raspberry Pi."
    if any(operation.status in {"queued", "running"} for operation in snapshot.operations):
        return "Wait for active equipment commands to finish before rebooting."
    if snapshot.mount is not None and (
        snapshot.mount.stale or snapshot.mount.slewing is not False or snapshot.mount.at_park is not True
    ):
        return "Park the mount and wait for a fresh stationary read-back before rebooting."
    return None


@router.get("/host-reboot", response_model=HostRebootStatusResponse)
async def host_reboot_status(services: ServicesDep, session: SessionDep) -> HostRebootStatusResponse:
    del session
    available, reason = await asyncio.to_thread(
        host_reboot.availability, auth_required=services.settings.effective_auth_required
    )
    if available:
        safety_reason = _host_reboot_safety_reason(services)
        if safety_reason is not None:
            return HostRebootStatusResponse(available=False, reason=safety_reason)
    return HostRebootStatusResponse(available=available, reason=reason)


@router.post(
    "/host-reboot",
    response_model=HostRebootResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reboot_host(
    body: HostRebootRequest,
    background_tasks: BackgroundTasks,
    services: ServicesDep,
    session: ControlDep,
) -> HostRebootResponse:
    """Reboot the Pi only after explicit provisioning, control-session authorization, and idle checks."""
    del body, session
    available, reason = await asyncio.to_thread(
        host_reboot.availability, auth_required=services.settings.effective_auth_required
    )
    if not available:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    safety_reason = _host_reboot_safety_reason(services)
    if safety_reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=safety_reason)
    background_tasks.add_task(host_reboot.reboot)
    return HostRebootResponse(accepted=True, previous_epoch=services.state.epoch)


@router.get("/host-shutdown", response_model=HostRebootStatusResponse)
async def host_shutdown_status(services: ServicesDep, session: SessionDep) -> HostRebootStatusResponse:
    del session
    available, reason = await asyncio.to_thread(
        host_reboot.availability, auth_required=services.settings.effective_auth_required, action="poweroff"
    )
    if available:
        safety_reason = _host_reboot_safety_reason(services)
        if safety_reason is not None:
            return HostRebootStatusResponse(available=False, reason=safety_reason.replace("rebooting", "shutting down"))
    return HostRebootStatusResponse(available=available, reason=reason.replace("reboot", "shut down"))


@router.post(
    "/host-shutdown",
    response_model=HostRebootResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def shutdown_host(
    body: HostRebootRequest,
    background_tasks: BackgroundTasks,
    services: ServicesDep,
    session: ControlDep,
) -> HostRebootResponse:
    """Reboot the Pi only after explicit provisioning, control-session authorization, and idle checks."""
    del body, session
    available, reason = await asyncio.to_thread(
        host_reboot.availability, auth_required=services.settings.effective_auth_required, action="poweroff"
    )
    if not available:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)
    safety_reason = _host_reboot_safety_reason(services)
    if safety_reason is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=safety_reason.replace("rebooting", "shutting down"))
    background_tasks.add_task(host_reboot.shutdown)
    return HostRebootResponse(accepted=True, previous_epoch=services.state.epoch)


@router.get("/remote-access", response_model=RemoteAccessResponse)
async def read_remote_access(session: SessionDep, services: ServicesDep) -> RemoteAccessResponse:
    """Inspect local Tailscale state without installing or configuring it."""
    del session
    inspected = await asyncio.to_thread(
        inspect_tailscale, services.config.port, host=services.config.host
    )
    return RemoteAccessResponse(
        **{field: getattr(inspected, field) for field in RemoteAccessResponse.model_fields}
    )


@router.get("/external-tools/paths", response_model=ExternalToolsSettings)
async def read_tool_paths(services: ServicesDep, session: SessionDep) -> ExternalToolsSettings:
    del session
    return services.settings.external_tools


@router.put("/external-tools/paths", response_model=ExternalToolsSettings)
async def write_tool_paths(body: ExternalToolsSettings, services: ServicesDep,
                           session: ControlDep) -> ExternalToolsSettings:
    del session
    tools = await services.settings.set_external_tools(body)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return tools


@router.get("/host-paths", response_model=HostPathListing)
async def browse_host_paths(services: ServicesDep, session: ControlDep,
                            path: str | None = None) -> HostPathListing:
    del session
    try:
        return await asyncio.to_thread(list_host_paths, None if path is None else Path(path), services.config.data_dir)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=f"Cannot browse this server folder: {exc}") from exc


@router.get("/external-tools", response_model=ExternalToolsResponse)
async def read_external_tools(session: SessionDep, services: ServicesDep) -> ExternalToolsResponse:
    """Report optional executables without downloading or running installers.

    Installation remains a deliberate, platform-owned operation.  In
    particular, GPL bridges stay separate processes with their own source and
    notices instead of becoming linked code in a commercial SSC build.
    """
    del session
    ffmpeg_path = shutil.which("ffmpeg")
    dwarf_path = shutil.which("dwarf-alpaca")
    astap = await astap_availability(env=solver_environment(services.settings))
    resolved_cli = await asyncio.to_thread(resolve_astap_cli, solver_environment(services.settings))
    astap_path = str(resolved_cli) if resolved_cli is not None else None

    astap_status: Literal["installed", "incomplete", "not_installed"] = (
        "installed"
        if astap.available
        else ("incomplete" if astap_path is not None else "not_installed")
    )
    return ExternalToolsResponse(
        platform=platform.system().lower(),
        architecture=platform.machine().lower(),
        tools=[
            ExternalToolModel(
                id="ffmpeg",
                name="FFmpeg",
                purpose="RTSP and YouTube video encoding",
                status="installed" if ffmpeg_path else "not_installed",
                detail=(
                    "Ready for broadcasting."
                    if ffmpeg_path
                    else "Required only for RTSP or YouTube broadcasting."
                ),
                detected_path=ffmpeg_path,
                license="LGPL-2.1-or-later / GPL build options",
                distribution="system_package",
                source_url="https://ffmpeg.org/download.html",
                guide_article_id="external-tools",
                supported_platforms=["Linux", "Raspberry Pi OS", "macOS", "Windows"],
            ),
            ExternalToolModel(
                id="astap",
                name="ASTAP command-line solver",
                purpose="Local plate solving for centering and polar alignment",
                status=astap_status,
                detail="Ready for plate solving."
                if astap.available
                else (astap.reason or "Unavailable."),
                version=astap.version,
                detected_path=astap_path,
                license="MPL-2.0 (separate executable; database installed separately)",
                distribution="separate_process",
                source_url="https://www.hnsky.org/astap.htm",
                guide_article_id="external-tools",
                supported_platforms=["Linux", "Raspberry Pi OS", "macOS", "Windows"],
            ),
            ExternalToolModel(
                id="dwarf-alpaca",
                name="dwarfAlp",
                purpose="Experimental Alpaca bridge for DWARFLAB telescopes",
                status="installed" if dwarf_path else "not_installed",
                detail=(
                    "Bridge executable found; configure and run it separately."
                    if dwarf_path
                    else "Optional unofficial bridge; hardware support varies by model."
                ),
                detected_path=dwarf_path,
                license="GPL-3.0-only (separate process)",
                distribution="separate_process",
                source_url="https://github.com/acocalypso/dwarfAlp",
                guide_article_id="external-tools",
                supported_platforms=["Linux", "Raspberry Pi OS", "macOS", "Windows"],
            ),
        ],
    )


def _streaming_response(services: ServicesDep) -> StreamingSettingsResponse:
    stream = services.capture.live_stack_rtsp_status
    config = stream.config
    settings = services.settings.streaming
    port = settings.rtsp_port if config is None else config.port
    path = settings.rtsp_path if config is None else config.path
    return StreamingSettingsResponse(
        broadcast_when_unparked=settings.broadcast_when_unparked,
        wide_camera_picture_in_picture=settings.wide_camera_picture_in_picture,
        rtsp_host=settings.rtsp_host,
        rtsp_port=settings.rtsp_port,
        rtsp_path=settings.rtsp_path,
        rtsp_width=settings.rtsp_width,
        rtsp_height=settings.rtsp_height,
        rtsp_fps=settings.rtsp_fps,
        rtsp_bitrate_kbps=settings.rtsp_bitrate_kbps,
        rtsp_update_interval_s=settings.rtsp_update_interval_s,
        rtsp_auto_crop=settings.rtsp_auto_crop,
        youtube_resolution=settings.youtube_resolution,
        youtube_stream_key_set=YOUTUBE_STREAM_KEY_NAME in services.settings.secret_names,
        active=stream.active,
        url=f"rtsp://HOST:{port}/{path}",
        source=services.capture.live_stack_rtsp_source,
        last_error=stream.last_error,
    )


def _location_response(location: LocationSettings) -> LocationResponse:
    return LocationResponse(
        active=resolve_location(location),
        preferred_source=location.preferred_source,
        sources={
            source: LocationState(
                latitude_deg=fix.latitude_deg,
                longitude_deg=fix.longitude_deg,
                elevation_m=fix.elevation_m,
                timezone=fix.timezone,
                source=source,
                accuracy_m=fix.accuracy_m,
                acquired_at=fix.acquired_at,
            )
            for source, fix in location.fixes.items()
        },
    )


def _publish(services: ServicesDep) -> None:
    services.state.apply(LocationChanged(location=services.settings.active_location()))
    services.state.apply(SettingsChanged(settings=services.settings.summary()))


def _horizon_response(horizon: HorizonSettings) -> HorizonResponse:
    if horizon.points is None:
        return HorizonResponse(active=False, points=None)
    return HorizonResponse(
        active=True,
        points=[
            HorizonPointModel(azimuth_deg=point.azimuth_deg, altitude_deg=point.altitude_deg)
            for point in horizon.points
        ],
    )


@router.get("", response_model=SettingsResponse)
async def read_settings(services: ServicesDep, session: SessionDep) -> SettingsResponse:
    del session
    return SettingsResponse(
        location=_location_response(services.settings.location),
        horizon=_horizon_response(services.settings.horizon),
        appearance=services.settings.appearance,
        device=services.settings.device,
        secret_names=services.settings.secret_names,
        control_pin_set=services.settings.security.control_pin_set,
    )


@router.get("/configuration/export", response_model=ConfigurationBundle)
async def export_configuration(services: ServicesDep, session: SessionDep) -> ConfigurationBundle:
    del session
    return ConfigurationBundle(
        exported_at=datetime.now(tz=UTC),
        settings=services.settings.configuration(),
    )


@router.get("/client-configuration", response_model=ClientConfigurationSettings)
async def read_client_configuration(
    services: ServicesDep, session: SessionDep
) -> ClientConfigurationSettings:
    del session
    return services.settings.client


@router.put("/client-configuration", response_model=ClientConfigurationSettings)
async def write_client_configuration(
    body: ClientConfigurationSettings, services: ServicesDep, session: ControlDep
) -> ClientConfigurationSettings:
    del session
    return await services.settings.set_client_configuration(body)


@router.put(
    "/configuration/import",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=OperationState,
)
async def import_configuration(
    body: ConfigurationBundle,
    services: ServicesDep,
    session: ControlDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> OperationState:
    del session
    if "client" not in body.settings.model_fields_set:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="configuration import must include the complete client UI configuration",
        )
    async def work(context: OperationContext) -> dict[str, object]:
        context.progress(message="Applying complete configuration", current=0, total=1)
        configuration = body.settings
        warnings: list[str] = []
        imported_folder = configuration.capture.folder
        if imported_folder is not None and not await asyncio.to_thread(imported_folder.is_dir):
            replacement = await asyncio.to_thread(user_pictures_folder)
            if replacement is None:
                raise CaptureFolderError("Cannot find your Pictures folder. Choose a capture folder in Settings.")
            await asyncio.to_thread(replacement.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(validate_capture_folder, replacement)
            configuration = configuration.model_copy(update={
                "capture": configuration.capture.model_copy(update={"folder": replacement})
            })
            warnings.append(
                f"The imported capture folder is unavailable on this computer. "
                f"Captures will be saved in your Pictures folder: {replacement}. "
                "You can change this in Settings → Capture."
            )
        await services.settings.replace_configuration(configuration)
        services.capture.set_folder_configured(configuration.capture.folder is not None)
        _publish(services)
        await services.capture.reconcile_stream_automation()
        context.progress(message="Configuration applied", current=1, total=1)
        return ConfigurationImportResponse(
            applied_sections=list(type(body.settings).model_fields), warnings=warnings
        ).model_dump(mode="json")

    try:
        return services.operations.start(
            kind="configuration_import",
            work=work,
            message="Starting configuration import",
            idempotency_key=idempotency_key,
        )
    except OperationBusyError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/streaming", response_model=StreamingSettingsResponse)
async def read_streaming_settings(
    services: ServicesDep, session: SessionDep
) -> StreamingSettingsResponse:
    del session
    return _streaming_response(services)


@router.put("/streaming", response_model=StreamingSettingsResponse)
async def set_streaming_settings(
    body: StreamingSettingsRequest, services: ServicesDep, session: ControlDep
) -> StreamingSettingsResponse:
    del session
    await services.settings.set_streaming(
        broadcast_when_unparked=body.broadcast_when_unparked,
        wide_camera_picture_in_picture=body.wide_camera_picture_in_picture,
        rtsp_host=body.rtsp_host,
        rtsp_port=body.rtsp_port,
        rtsp_path=body.rtsp_path,
        rtsp_width=body.rtsp_width,
        rtsp_height=body.rtsp_height,
        rtsp_fps=body.rtsp_fps,
        rtsp_bitrate_kbps=body.rtsp_bitrate_kbps,
        rtsp_update_interval_s=body.rtsp_update_interval_s,
        rtsp_auto_crop=body.rtsp_auto_crop,
        youtube_resolution=body.youtube_resolution,
    )
    await services.capture.reconcile_stream_automation()
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return _streaming_response(services)


@router.put("/location/fix", response_model=LocationResponse)
async def record_fix(
    body: LocationFixRequest, services: ServicesDep, session: ControlDep
) -> LocationResponse:
    """Record one source's answer. The active location is then re-resolved."""
    del session
    fix = LocationFix(
        latitude_deg=body.latitude_deg,
        longitude_deg=body.longitude_deg,
        elevation_m=body.elevation_m,
        timezone=body.timezone,
        accuracy_m=body.accuracy_m,
        acquired_at=datetime.now(tz=UTC),
    )
    location = await services.settings.record_fix(body.source, fix)
    _publish(services)
    return _location_response(location)


@router.delete("/location/fix/{source}", response_model=LocationResponse)
async def clear_fix(
    source: LocationSource, services: ServicesDep, session: ControlDep
) -> LocationResponse:
    del session
    location = await services.settings.clear_fix(source)
    _publish(services)
    return _location_response(location)


@router.put("/location/preferred-source", response_model=LocationResponse)
async def set_preferred_source(
    body: PreferredSourceRequest, services: ServicesDep, session: ControlDep
) -> LocationResponse:
    del session
    try:
        location = await services.settings.set_preferred_source(body.source)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    _publish(services)
    return _location_response(location)


@router.get("/horizon", response_model=HorizonResponse)
async def read_horizon(services: ServicesDep, session: SessionDep) -> HorizonResponse:
    del session
    return _horizon_response(services.settings.horizon)


@router.put("/horizon", response_model=HorizonResponse)
async def set_horizon(
    body: HorizonRequest, services: ServicesDep, session: ControlDep
) -> HorizonResponse:
    """Replace the site's skyline mask. Validation (at least 2 points, azimuths
    in ``[0, 360)``, altitudes in ``[-10, 90]``, no duplicate azimuth) happens
    on ``HorizonRequest`` itself, so a rejection names the fix."""
    del session
    points = [
        HorizonPointFix(azimuth_deg=point.azimuth_deg, altitude_deg=point.altitude_deg)
        for point in body.points
    ]
    horizon = await services.settings.set_horizon(points)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return _horizon_response(horizon)


@router.delete("/horizon", response_model=HorizonResponse)
async def clear_horizon(services: ServicesDep, session: ControlDep) -> HorizonResponse:
    """Clear the profile back to *unknown* — never an empty-list "no
    obstructions" (CLAUDE.md invariant 7)."""
    del session
    horizon = await services.settings.set_horizon(None)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return _horizon_response(horizon)


@router.put("/appearance", response_model=AppearanceSettings)
async def set_appearance(
    body: AppearanceRequest, services: ServicesDep, session: SessionDep
) -> AppearanceSettings:
    del session
    appearance = await services.settings.set_appearance(
        AppearanceSettings(
            theme=body.theme,
            font_scale=body.font_scale,
            accent=body.accent,
            background_enabled=body.background_enabled,
            background_opacity_percent=body.background_opacity_percent,
        )
    )
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return appearance


async def _weather_response(services: ServicesDep) -> WeatherSettingsResponse:
    """One builder for both the GET and the two mutations, so the three can
    never disagree about what "configured" means."""
    contact = resolve_metno_contact(
        stored=services.settings.weather.metno_contact,
        environment=services.config.weather_contact,
    )
    capability = services.state.snapshot.secrets
    providers = [
        WeatherProviderStatus(name=name, available=available, reason=reason)
        for name, available, reason in await services.weather.availability()
    ]
    return WeatherSettingsResponse(
        metno_contact=contact.value,
        metno_contact_source=contact.source,
        openweathermap_key_set=OWM_SECRET_NAME in services.settings.secret_names,
        weatherapi_key_set=WEATHERAPI_SECRET_NAME in services.settings.secret_names,
        visualcrossing_key_set=VC_SECRET_NAME in services.settings.secret_names,
        secrets_backend_available=capability.available if capability else False,
        secrets_backend_reason=capability.reason if capability else None,
        provider=services.settings.weather.provider,
        providers=providers,
    )


@router.get("/weather", response_model=WeatherSettingsResponse)
async def read_weather(services: ServicesDep, session: SessionDep) -> WeatherSettingsResponse:
    """What is configured and what actually works right now.

    The OpenWeatherMap key is reported as a boolean and never returned; it is
    written through ``PUT /api/settings/secrets`` like any other secret
    (invariant 8).
    """
    del session
    return await _weather_response(services)


@router.put("/weather/metno-contact", response_model=WeatherSettingsResponse)
async def set_metno_contact(
    body: MetNoContactRequest, services: ServicesDep, session: ControlDep
) -> WeatherSettingsResponse:
    """Store Met.no's identifying contact and apply it immediately.

    Applied to the live service rather than only persisted, because a setting
    that needs a restart to take effect is one a user will conclude is broken.
    A contact Met.no would reject is refused here, at the moment it is typed,
    rather than silently failing at the next forecast.
    """
    del session
    await services.settings.set_metno_contact(body.contact)
    reason = await services.weather.set_metno_contact(
        resolve_metno_contact(
            stored=body.contact, environment=services.config.weather_contact
        ).value
    )
    if reason is not None and (body.contact or "").strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=reason)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return await _weather_response(services)


@router.put("/weather/provider", response_model=WeatherSettingsResponse)
async def set_weather_provider(
    body: WeatherProviderRequest, services: ServicesDep, session: ControlDep
) -> WeatherSettingsResponse:
    """Select and immediately apply the sole provider used for forecasts."""
    del session
    weather = await services.settings.set_weather_provider(body.provider)
    services.weather.set_provider(weather.provider)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return await _weather_response(services)


@router.get("/object-info", response_model=ObjectInfoSettingsResponse)
async def read_object_info(
    services: ServicesDep, session: SessionDep
) -> ObjectInfoSettingsResponse:
    """The click-through info feature's own switch (M11). Off by default —
    THIRD_PARTY_NOTICES.md's recommendation for the one source pairing
    (Wikipedia/Wikidata/Commons) that cleared."""
    del session
    return ObjectInfoSettingsResponse(enabled=services.settings.object_info.enabled)


@router.put("/object-info", response_model=ObjectInfoSettingsResponse)
async def set_object_info_enabled(
    body: ObjectInfoEnabledRequest, services: ServicesDep, session: ControlDep
) -> ObjectInfoSettingsResponse:
    """Flip the switch and apply it immediately — ``ObjectInfoService`` reads
    it fresh on every lookup, so this takes effect on the very next click,
    the same "no restart needed" rule ``set_metno_contact`` above follows."""
    del session
    settings = await services.settings.set_object_info_enabled(body.enabled)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return ObjectInfoSettingsResponse(enabled=settings.enabled)


@router.get("/skymap-autofetch", response_model=SkymapAutofetchSettingsResponse)
async def read_skymap_autofetch(
    services: ServicesDep, session: SessionDep
) -> SkymapAutofetchSettingsResponse:
    """Whether resolving a target also fires an autofetch cutout for it. Off
    by default — commit ``b0c4155`` removed this as a default behaviour, and
    it comes back here only as something a user opts into."""
    del session
    return SkymapAutofetchSettingsResponse(
        cutout_autofetch_enabled=services.settings.skymap.cutout_autofetch_enabled
    )


@router.put("/skymap-autofetch", response_model=SkymapAutofetchSettingsResponse)
async def set_skymap_autofetch_enabled(
    body: SkymapAutofetchEnabledRequest, services: ServicesDep, session: ControlDep
) -> SkymapAutofetchSettingsResponse:
    """Flip the switch and apply it immediately — the next target resolution
    already reads it fresh (``ssc.api.routes.astro.resolve_target``), the same
    "no restart needed" rule ``set_object_info_enabled`` above follows."""
    del session
    settings = await services.settings.set_skymap_cutout_autofetch_enabled(
        body.cutout_autofetch_enabled
    )
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return SkymapAutofetchSettingsResponse(
        cutout_autofetch_enabled=settings.cutout_autofetch_enabled
    )


async def _default_root_str() -> str | None:
    root = await asyncio.to_thread(default_capture_root)
    return None if root is None else str(root)


@router.get("/capture-folder", response_model=CaptureFolderResponse)
async def read_capture_folder(services: ServicesDep, session: SessionDep) -> CaptureFolderResponse:
    del session
    folder = services.settings.capture.folder
    return CaptureFolderResponse(
        folder=None if folder is None else str(folder),
        default_root=await _default_root_str(),
    )


@router.put("/capture-folder", response_model=CaptureFolderResponse)
async def set_capture_folder(
    body: CaptureFolderRequest, services: ServicesDep, session: ControlDep
) -> CaptureFolderResponse:
    """Where SSC writes captured FITS frames (DESIGN.md §9.5.2). Validated
    (exists, is a directory, is writable, has room) before anything is
    persisted — ``services.settings.set_capture_folder``'s own docstring."""
    del session
    try:
        capture = await services.settings.set_capture_folder(
            None if body.folder is None else Path(body.folder)
        )
    except CaptureFolderError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    services.capture.set_folder_configured(capture.folder is not None)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return CaptureFolderResponse(
        folder=None if capture.folder is None else str(capture.folder),
        default_root=await _default_root_str(),
    )


@router.get("/capture-folder-template", response_model=CaptureFolderTemplateResponse)
async def read_capture_folder_template(
    services: ServicesDep, session: SessionDep
) -> CaptureFolderTemplateResponse:
    del session
    return CaptureFolderTemplateResponse(template=services.settings.capture.folder_template)


@router.put("/capture-folder-template", response_model=CaptureFolderTemplateResponse)
async def set_capture_folder_template(
    body: CaptureFolderTemplateRequest, services: ServicesDep, session: ControlDep
) -> CaptureFolderTemplateResponse:
    """Where beneath the capture folder root a frame lands (DESIGN.md §9.5.4).
    Validated by rendering against a synthetic example frame before anything
    is persisted — ``services.settings.set_capture_folder_template``'s own
    docstring; an unknown placeholder or an escaping/absolute rendering is
    refused here, not discovered at the first real capture."""
    del session
    try:
        capture = await services.settings.set_capture_folder_template(body.template)
    except PathTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return CaptureFolderTemplateResponse(template=capture.folder_template)


@router.get("/capture-filename-template", response_model=CaptureFilenameTemplateResponse)
async def read_capture_filename_template(
    services: ServicesDep, session: SessionDep
) -> CaptureFilenameTemplateResponse:
    del session
    return CaptureFilenameTemplateResponse(template=services.settings.capture.filename_template)


@router.put("/capture-filename-template", response_model=CaptureFilenameTemplateResponse)
async def set_capture_filename_template(
    body: CaptureFilenameTemplateRequest, services: ServicesDep, session: ControlDep
) -> CaptureFilenameTemplateResponse:
    """Set the rendered filename stem; validation occurs before persistence."""
    del session
    try:
        capture = await services.settings.set_capture_filename_template(body.template)
    except PathTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return CaptureFilenameTemplateResponse(template=capture.filename_template)


def _calibration_path_templates(services: ServicesDep) -> CalibrationPathTemplates:
    capture = services.settings.capture
    return CalibrationPathTemplates(
        bias_folder_template=capture.calibration_bias_folder_template,
        dark_folder_template=capture.calibration_dark_folder_template,
        flat_folder_template=capture.calibration_flat_folder_template,
        filename_template=capture.calibration_filename_template or capture.filename_template,
    )


@router.get("/calibration-path-templates", response_model=CalibrationPathTemplates)
async def read_calibration_path_templates(
    services: ServicesDep, session: SessionDep
) -> CalibrationPathTemplates:
    del session
    return _calibration_path_templates(services)


@router.put("/calibration-path-templates", response_model=CalibrationPathTemplates)
async def set_calibration_path_templates(
    body: CalibrationPathTemplates, services: ServicesDep, session: ControlDep
) -> CalibrationPathTemplates:
    del session
    try:
        await services.settings.set_calibration_path_templates(
            bias_folder_template=body.bias_folder_template,
            dark_folder_template=body.dark_folder_template,
            flat_folder_template=body.flat_folder_template,
            filename_template=body.filename_template,
        )
    except PathTemplateError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return _calibration_path_templates(services)


@router.get("/capture-rejected-frames", response_model=CaptureRejectedFramesSettings)
async def read_capture_rejected_frames(
    services: ServicesDep, session: SessionDep
) -> CaptureRejectedFramesSettings:
    del session
    return CaptureRejectedFramesSettings(
        keep_rejected_frames=services.settings.capture.keep_rejected_frames
    )


@router.put("/capture-rejected-frames", response_model=CaptureRejectedFramesSettings)
async def set_capture_rejected_frames(
    body: CaptureRejectedFramesSettings, services: ServicesDep, session: ControlDep
) -> CaptureRejectedFramesSettings:
    del session
    capture = await services.settings.set_keep_rejected_frames(body.keep_rejected_frames)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return CaptureRejectedFramesSettings(
        keep_rejected_frames=capture.keep_rejected_frames
    )


@router.get("/capture-dither", response_model=CaptureDitherResponse)
async def read_capture_dither(services: ServicesDep, session: SessionDep) -> CaptureDitherResponse:
    del session
    dither = services.settings.capture
    return CaptureDitherResponse(
        enabled=dither.dither_enabled,
        every_n_frames=dither.dither_every_n_frames,
        min_pixels=dither.dither_min_pixels,
        max_pixels=dither.dither_max_pixels,
    )


@router.get("/capture-center-after-drift", response_model=CaptureCenterAfterDriftResponse)
async def read_capture_center_after_drift(
    services: ServicesDep, session: SessionDep
) -> CaptureCenterAfterDriftResponse:
    del session
    settings = services.settings.capture
    return CaptureCenterAfterDriftResponse(
        enabled=settings.center_after_drift_enabled,
        threshold_pixels=settings.drift_threshold_pixels,
    )


@router.put("/capture-center-after-drift", response_model=CaptureCenterAfterDriftResponse)
async def set_capture_center_after_drift(
    body: CaptureCenterAfterDriftRequest, services: ServicesDep, session: ControlDep
) -> CaptureCenterAfterDriftResponse:
    del session
    settings = await services.settings.set_capture_center_after_drift(
        enabled=body.enabled, threshold_pixels=body.threshold_pixels
    )
    _publish(services)
    return CaptureCenterAfterDriftResponse(
        enabled=settings.center_after_drift_enabled,
        threshold_pixels=settings.drift_threshold_pixels,
    )


@router.put("/capture-dither", response_model=CaptureDitherResponse)
async def set_capture_dither(
    body: CaptureDitherRequest, services: ServicesDep, session: ControlDep
) -> CaptureDitherResponse:
    del session
    dither = await services.settings.set_capture_dither(
        enabled=body.enabled,
        every_n_frames=body.every_n_frames,
        min_pixels=body.min_pixels,
        max_pixels=body.max_pixels,
    )
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return CaptureDitherResponse(
        enabled=dither.dither_enabled,
        every_n_frames=dither.dither_every_n_frames,
        min_pixels=dither.dither_min_pixels,
        max_pixels=dither.dither_max_pixels,
    )


@router.get("/auto-lp-filter", response_model=AutoLpFilterResponse)
async def read_auto_lp_filter(
    services: ServicesDep, session: SessionDep
) -> AutoLpFilterResponse:
    del session
    return AutoLpFilterResponse(
        enabled=services.settings.capture.auto_lp_filter_for_emission_targets
    )


@router.put("/auto-lp-filter", response_model=AutoLpFilterResponse)
async def set_auto_lp_filter(
    body: AutoLpFilterRequest, services: ServicesDep, session: ControlDep
) -> AutoLpFilterResponse:
    del session
    capture = await services.settings.set_auto_lp_filter_for_emission_targets(body.enabled)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return AutoLpFilterResponse(enabled=capture.auto_lp_filter_for_emission_targets)


@router.get("/capture/path-tags", response_model=CapturePathTagsResponse)
async def read_capture_path_tags(session: SessionDep) -> CapturePathTagsResponse:
    """The full ``%PLACEHOLDER%`` vocabulary a capture folder template may
    use, each with an example rendering — generated from
    ``ssc.capture.frame.FrameMetadata``'s own fields plus the derived tags
    (``ssc.capture.pathtemplate``'s own module docstring), so the UI never
    hardcodes this list."""
    del session
    return CapturePathTagsResponse(
        tags=[
            CapturePathTagModel(name=tag.name, description=tag.description, example=example)
            for tag, example in path_tags()
        ]
    )


@router.get("/secrets", response_model=SecretNamesResponse)
async def list_secrets(services: ServicesDep, session: SessionDep) -> SecretNamesResponse:
    """Names only. There is no endpoint that returns a value, by design."""
    del session
    return SecretNamesResponse(names=services.settings.secret_names)


@router.put(
    "/secrets",
    response_model=SecretNamesResponse,
    dependencies=[Depends(rate_limit("secrets"))],
)
async def upsert_secret(
    body: SecretUpsert, services: ServicesDep, session: ControlDep
) -> SecretNamesResponse:
    del session
    _reject_reserved(body.name)
    try:
        await services.secrets.set(body.name, body.value)
    except (SecretError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    names = await services.settings.add_secret_name(body.name)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return SecretNamesResponse(names=names)


@router.delete("/secrets/{name}", response_model=SecretNamesResponse)
async def delete_secret(
    name: str, services: ServicesDep, session: ControlDep
) -> SecretNamesResponse:
    del session
    _reject_reserved(name)
    try:
        await services.secrets.delete(name)
    except (SecretError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    names = await services.settings.remove_secret_name(name)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    return SecretNamesResponse(names=names)


def _security_response(services: ServicesDep) -> SecurityResponse:
    return SecurityResponse(
        auth_required=services.settings.effective_auth_required,
        auth_required_locked=services.settings.auth_required_locked,
    )


@router.get("/security", response_model=SecurityResponse)
async def read_security(services: ServicesDep, session: SessionDep) -> SecurityResponse:
    del session
    return _security_response(services)


@router.put("/security/auth-required", response_model=SecurityResponse)
async def set_auth_required(
    body: AuthRequiredRequest, services: ServicesDep, response: Response, session: ControlDep
) -> SecurityResponse:
    """Turn pairing on or off (owner's request, 2026-09-09: loginless by
    default, with a way back for a more cautious operator).

    ``ControlDep`` gives this the asymmetry the setting needs without any
    extra check: with auth already off, ``ControlDep`` is a no-op, so any LAN
    caller can turn pairing *on* — harmless, it only adds protection. With
    auth on, ``ControlDep`` demands a real control session, so nobody can turn
    it *off* without already being paired.

    Turning it **on** mints a fresh session for *this* caller and sets its
    cookie before the new requirement takes effect on the very same response,
    so the browser making the change is not locked out of it — every other,
    not-yet-paired device is the one that now has to pair. Turning it off, or
    leaving it unchanged, touches no session: existing sessions are left
    exactly as they were in both directions, because neither transition makes
    a session that was legitimately issued any less so.
    """
    del session
    if services.settings.auth_required_locked:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "auth_required is fixed by the SSC_AUTH_REQUIRED environment "
                "variable and cannot be changed here"
            ),
        )
    was_required = services.settings.effective_auth_required
    await services.settings.set_auth_required(body.auth_required)
    services.state.apply(SettingsChanged(settings=services.settings.summary()))
    if body.auth_required and not was_required:
        issued = await services.auth.bootstrap_session(label="settings")
        response.set_cookie(
            SESSION_COOKIE,
            issued.token,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=int((issued.session.expires_at - issued.session.created_at).total_seconds()),
        )
    return _security_response(services)


def _reject_reserved(name: str) -> None:
    try:
        validate_name(name)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    if name == CONTROL_PIN_NAME:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{CONTROL_PIN_NAME!r} is managed by PUT /api/auth/control-pin",
        )
