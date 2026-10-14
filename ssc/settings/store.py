# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Reading and writing configuration, with the cache the API layer reads from.

The ``config`` table holds one JSON document per section, validated through the
Pydantic model on the way in and on the way out. A section that fails validation
is a loud startup error, not a reset to defaults: silently discarding a user's
observing location is exactly the kind of quiet degradation invariant 7 forbids.

Remembered device addresses live in their own table rather than a JSON blob,
because "which address last completed a handshake" is a query, and because the
history is what makes a scope that moved between subnets findable again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Concatenate, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from ssc.astro.horizon import HorizonProfile
from ssc.capture.pathtemplate import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    LEGACY_DEFAULT_CAPTURE_FILENAME_TEMPLATES,
    validate_capture_filename_template,
    validate_capture_folder_template,
)
from ssc.settings.capture_folder import validate_capture_folder
from ssc.settings.horizon import resolve as resolve_horizon
from ssc.settings.location import resolve as resolve_location
from ssc.settings.models import (
    SECTION_APPEARANCE,
    SECTION_CAPTURE,
    SECTION_CLIENT,
    SECTION_DEVICE,
    SECTION_EXTERNAL_TOOLS,
    SECTION_HEATER,
    SECTION_HORIZON,
    SECTION_LOCATION,
    SECTION_OBJECT_INFO,
    SECTION_SECRETS,
    SECTION_SECURITY,
    SECTION_SKYMAP,
    SECTION_STREAMING,
    SECTION_WEATHER,
    AppearanceSettings,
    CaptureSettings,
    ClientConfigurationSettings,
    ConfigurationSettings,
    DeviceSettings,
    ExternalToolsSettings,
    HeaterAutomationSettings,
    HorizonPointFix,
    HorizonSettings,
    LocationFix,
    LocationSettings,
    ObjectInfoSettings,
    SecretRegistry,
    SecuritySettings,
    SkymapSettings,
    StreamingSettings,
    WeatherProviderName,
    WeatherSettings,
)
from ssc.state.models import DeviceDescriptor, LocationSource, LocationState, SettingsSummary
from ssc.store.database import Database, StoreError

_log = logging.getLogger(__name__)


class RememberedAddress(BaseModel):
    """One address SSC has seen. Not a default — a memory of something real."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    control_port: int
    label: str | None = None
    firmware: str | None = None
    model: str | None = None
    serial: str | None = None
    hardware_profile_id: str | None = None
    last_seen_at: datetime | None = None
    last_good_at: datetime | None = None
    created_at: datetime


class SettingsError(StoreError):
    """A stored settings section could not be read back."""


def _serialized_mutation[**P, T](method: Callable[Concatenate[SettingsStore, P], Awaitable[T]],
) -> Callable[Concatenate[SettingsStore, P], Awaitable[T]]:
    """Keep cached read, durable write and cache publication in one order."""
    @wraps(method)
    async def serialized(self: SettingsStore, *args: P.args, **kwargs: P.kwargs) -> T:
        async with self._mutation_lock:
            return await method(self, *args, **kwargs)
    return serialized


class SettingsStore:
    """The config table plus the address book, cached in memory after load."""

    def __init__(self, database: Database, *, auth_required_override: bool | None = None) -> None:
        self._mutation_lock = asyncio.Lock()
        self._database = database
        self._location = LocationSettings()
        self._device = DeviceSettings()
        self._appearance = AppearanceSettings()
        self._security = SecuritySettings()
        self._secrets = SecretRegistry()
        self._horizon = HorizonSettings()
        self._weather = WeatherSettings()
        self._heater = HeaterAutomationSettings()
        self._capture = CaptureSettings()
        self._client = ClientConfigurationSettings()
        self._external_tools = ExternalToolsSettings()
        self._skymap = SkymapSettings()
        self._streaming = StreamingSettings()
        self._loaded = False
        #: ``AppConfig.auth_required`` (``SSC_AUTH_REQUIRED``). ``None`` means
        #: "no environment opinion" — the stored toggle decides. Set, it always
        #: wins: an operator who pinned this in their deployment's environment
        #: must not have it silently reopened by an API call (see
        #: :meth:`effective_auth_required`).
        self._auth_required_override = auth_required_override

    # -- lifecycle ---------------------------------------------------------- #

    async def load(self) -> None:
        self._location = await self._read(SECTION_LOCATION, LocationSettings)
        self._device = await self._read(SECTION_DEVICE, DeviceSettings)
        self._appearance = await self._read(SECTION_APPEARANCE, AppearanceSettings)
        self._security = await self._read(SECTION_SECURITY, SecuritySettings)
        self._secrets = await self._read(SECTION_SECRETS, SecretRegistry)
        self._horizon = await self._read(SECTION_HORIZON, HorizonSettings)
        self._weather = await self._read(SECTION_WEATHER, WeatherSettings)
        self._heater = await self._read(SECTION_HEATER, HeaterAutomationSettings)
        self._object_info = await self._read(SECTION_OBJECT_INFO, ObjectInfoSettings)
        self._capture = await self._read(SECTION_CAPTURE, CaptureSettings)
        if self._capture.filename_template in LEGACY_DEFAULT_CAPTURE_FILENAME_TEMPLATES:
            self._capture = self._capture.model_copy(
                update={"filename_template": DEFAULT_CAPTURE_FILENAME_TEMPLATE}
            )
            await self._write(SECTION_CAPTURE, self._capture)
        self._skymap = await self._read(SECTION_SKYMAP, SkymapSettings)
        self._streaming = await self._read(SECTION_STREAMING, StreamingSettings)
        self._client = await self._read(SECTION_CLIENT, ClientConfigurationSettings)
        self._external_tools = await self._read(SECTION_EXTERNAL_TOOLS, ExternalToolsSettings)
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    # -- sections ----------------------------------------------------------- #

    @property
    def location(self) -> LocationSettings:
        return self._location

    @property
    def device(self) -> DeviceSettings:
        return self._device

    @property
    def appearance(self) -> AppearanceSettings:
        return self._appearance

    @property
    def security(self) -> SecuritySettings:
        return self._security

    @property
    def auth_required_locked(self) -> bool:
        """Whether ``SSC_AUTH_REQUIRED`` pins the value, so the API must
        refuse an attempt to change the stored toggle instead of accepting a
        write that would then have no effect."""
        return self._auth_required_override is not None

    @property
    def effective_auth_required(self) -> bool:
        """Whether a session is currently required — what every dependency
        and the state snapshot actually enforce/report, as opposed to
        :attr:`security`'s ``auth_required``, which is only the stored half
        of the answer."""
        if self._auth_required_override is not None:
            return self._auth_required_override
        return self._security.auth_required

    @property
    def secret_names(self) -> list[str]:
        return list(self._secrets.names)

    @property
    def horizon(self) -> HorizonSettings:
        return self._horizon

    @property
    def weather(self) -> WeatherSettings:
        return self._weather

    @property
    def heater(self) -> HeaterAutomationSettings:
        return self._heater

    @property
    def object_info(self) -> ObjectInfoSettings:
        return self._object_info

    @property
    def capture(self) -> CaptureSettings:
        return self._capture

    @property
    def external_tools(self) -> ExternalToolsSettings:
        return self._external_tools

    @_serialized_mutation
    async def set_external_tools(self, tools: ExternalToolsSettings) -> ExternalToolsSettings:
        await self._write(SECTION_EXTERNAL_TOOLS, tools)
        self._external_tools = tools
        return tools

    @property
    def skymap(self) -> SkymapSettings:
        return self._skymap

    @property
    def streaming(self) -> StreamingSettings:
        return self._streaming

    @property
    def client(self) -> ClientConfigurationSettings:
        return self._client

    def active_location(self) -> LocationState | None:
        return resolve_location(self._location)

    def active_horizon_profile(self) -> HorizonProfile | None:
        """The site's skyline mask, or ``None`` when none has been recorded —
        the feed :func:`ssc.astro.events.horizon_visibility` and the sky map
        both want (DESIGN.md 5)."""
        return resolve_horizon(self._horizon)

    def summary(self) -> SettingsSummary:
        """The non-secret projection the snapshot carries."""
        return SettingsSummary(
            location_source_preference=self._location.preferred_source,
            remembered_host=self._device.host,
            remembered_control_port=self._device.control_port,
            ethernet_host=self._device.ethernet_host,
            ethernet_control_port=self._device.ethernet_control_port,
            hardware_profile_id=self._device.hardware_profile_id,
            hardware_profile_source=self._device.hardware_profile_source,
            configured_secret_names=sorted(self._secrets.names),
            control_pin_set=self._security.control_pin_set,
            auth_required=self.effective_auth_required,
            workflow_guide_visible=self._appearance.workflow_guide_visible,
            theme=self._appearance.theme,
            font_scale=self._appearance.font_scale,
            accent=self._appearance.accent,
            background_enabled=self._appearance.background_enabled,
            background_opacity_percent=self._appearance.background_opacity_percent,
            horizon_profile_set=self._horizon.points is not None,
        )

    def configuration(self) -> ConfigurationSettings:
        """Return all portable settings, never secrets or security state."""
        return ConfigurationSettings(
            location=self._location,
            device=self._device,
            appearance=self._appearance,
            horizon=self._horizon,
            weather=self._weather,
            heater=self._heater,
            object_info=self._object_info,
            capture=self._capture,
            skymap=self._skymap,
            streaming=self._streaming,
            client=self._client,
            external_tools=self._external_tools,
        )

    @_serialized_mutation
    async def replace_configuration(self, configuration: ConfigurationSettings) -> None:
        """Validate and atomically replace every portable settings section."""
        if configuration.capture.folder is not None:
            validate_capture_folder(configuration.capture.folder)
        validate_capture_folder_template(configuration.capture.folder_template)
        validate_capture_filename_template(configuration.capture.filename_template)
        validate_capture_folder_template(configuration.capture.calibration_bias_folder_template)
        validate_capture_folder_template(configuration.capture.calibration_dark_folder_template)
        validate_capture_folder_template(configuration.capture.calibration_flat_folder_template)
        if configuration.capture.calibration_filename_template is not None:
            validate_capture_filename_template(
                configuration.capture.calibration_filename_template
            )
        sections = (
            (SECTION_LOCATION, configuration.location),
            (SECTION_DEVICE, configuration.device),
            (SECTION_APPEARANCE, configuration.appearance),
            (SECTION_HORIZON, configuration.horizon),
            (SECTION_WEATHER, configuration.weather),
            (SECTION_HEATER, configuration.heater),
            (SECTION_OBJECT_INFO, configuration.object_info),
            (SECTION_CAPTURE, configuration.capture),
            (SECTION_SKYMAP, configuration.skymap),
            (SECTION_STREAMING, configuration.streaming),
            (SECTION_CLIENT, configuration.client),
            (SECTION_EXTERNAL_TOOLS, configuration.external_tools),
        )
        now = datetime.now(tz=UTC).isoformat()
        placeholders = ", ".join("(?, ?, ?)" for _ in sections)
        values = tuple(
            entry
            for section, value in sections
            for entry in (section, value.model_dump_json(), now)
        )
        await self._database.execute(
            f"INSERT INTO config (key, value, updated_at) VALUES {placeholders} "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            values,
        )
        self._location = configuration.location
        self._device = configuration.device
        self._appearance = configuration.appearance
        self._horizon = configuration.horizon
        self._weather = configuration.weather
        self._heater = configuration.heater
        self._object_info = configuration.object_info
        self._capture = configuration.capture
        self._skymap = configuration.skymap
        self._streaming = configuration.streaming
        self._client = configuration.client
        self._external_tools = configuration.external_tools

    @_serialized_mutation
    async def set_client_configuration(
        self, client: ClientConfigurationSettings
    ) -> ClientConfigurationSettings:
        await self._write(SECTION_CLIENT, client)
        self._client = client
        return client

    # -- mutation ----------------------------------------------------------- #

    @_serialized_mutation
    async def record_fix(self, source: LocationSource, fix: LocationFix) -> LocationSettings:
        fixes = dict(self._location.fixes)
        fixes[source] = fix
        return await self._write_location(self._location.model_copy(update={"fixes": fixes}))

    @_serialized_mutation
    async def clear_fix(self, source: LocationSource) -> LocationSettings:
        fixes = {key: value for key, value in self._location.fixes.items() if key != source}
        return await self._write_location(self._location.model_copy(update={"fixes": fixes}))

    @_serialized_mutation
    async def set_preferred_source(self, source: LocationSource | None) -> LocationSettings:
        if source is not None and source not in self._location.fixes:
            raise ValueError(f"no {source.value} location fix has been recorded yet")
        return await self._write_location(
            self._location.model_copy(update={"preferred_source": source})
        )

    @_serialized_mutation
    async def set_appearance(self, appearance: AppearanceSettings) -> AppearanceSettings:
        await self._write(SECTION_APPEARANCE, appearance)
        self._appearance = appearance
        return appearance

    @_serialized_mutation
    async def set_control_pin_set(self, is_set: bool) -> SecuritySettings:
        security = self._security.model_copy(update={"control_pin_set": is_set})
        await self._write(SECTION_SECURITY, security)
        self._security = security
        return security

    @_serialized_mutation
    async def set_auth_required(self, required: bool) -> SecuritySettings:
        """Persist the stored toggle. Refused by the caller (409, not here)
        when :attr:`auth_required_locked` — this layer just writes what it is
        handed, the same discipline every other setter here follows."""
        security = self._security.model_copy(update={"auth_required": required})
        await self._write(SECTION_SECURITY, security)
        self._security = security
        return security

    @_serialized_mutation
    async def set_horizon(self, points: list[HorizonPointFix] | None) -> HorizonSettings:
        """Replace the site's skyline mask. ``None`` clears it back to unknown.

        Point-count/duplicate-azimuth validation happens at the API boundary
        (``ssc.api.schemas.HorizonRequest``) before this is ever called; this
        layer only persists what it is handed.
        """
        horizon = HorizonSettings(points=points)
        await self._write(SECTION_HORIZON, horizon)
        self._horizon = horizon
        return horizon

    @_serialized_mutation
    async def set_metno_contact(self, contact: str | None) -> WeatherSettings:
        """Store Met.no's identifying contact, or clear it with ``None``.

        Whitespace-only is stored as ``None`` rather than as a blank string —
        a contact of ``" "`` would satisfy a truthiness check and then fail
        Met.no's own validation at request time, which is the kind of
        almost-configured state invariant 7 exists to prevent.
        """
        cleaned = contact.strip() if contact is not None else None
        weather = self._weather.model_copy(update={"metno_contact": cleaned or None})
        await self._write(SECTION_WEATHER, weather)
        self._weather = weather
        return weather

    @_serialized_mutation
    async def set_weather_provider(self, provider: WeatherProviderName) -> WeatherSettings:
        """Persist the one provider forecast requests should use."""
        weather = self._weather.model_copy(update={"provider": provider})
        await self._write(SECTION_WEATHER, weather)
        self._weather = weather
        return weather

    @_serialized_mutation
    async def set_object_info_enabled(self, enabled: bool) -> ObjectInfoSettings:
        """Flip the click-through info feature's own switch. Off by default
        (THIRD_PARTY_NOTICES.md's recommendation) — this is the only way it ever
        turns on."""
        object_info = ObjectInfoSettings(enabled=enabled)
        await self._write(SECTION_OBJECT_INFO, object_info)
        self._object_info = object_info
        return object_info

    @_serialized_mutation
    async def set_capture_folder(self, folder: Path | None) -> CaptureSettings:
        """Validate (CLAUDE.md invariant 7 — exists, is a directory, is
        writable, has room) and persist where SSC writes captured FITS
        frames. ``None`` clears it back to unset: capture must then refuse to
        start rather than pick somewhere on its own (DESIGN.md §9.5.2).

        Raises :class:`ssc.settings.capture_folder.CaptureFolderError` if the
        folder fails validation; nothing is persisted in that case.
        """
        if folder is not None:
            validate_capture_folder(folder)
        capture = self._capture.model_copy(update={"folder": folder})
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_capture_folder_template(self, template: str) -> CaptureSettings:
        """Validate (:func:`ssc.capture.pathtemplate.validate_capture_folder_template`
        — an unknown placeholder, or a rendering that escapes via ``..`` or is
        absolute) and persist the templated layout beneath the capture folder.

        Raises :class:`ssc.capture.pathtemplate.PathTemplateError` naming the
        problem if the template fails validation; nothing is persisted in
        that case.
        """
        validate_capture_folder_template(template)
        capture = self._capture.model_copy(update={"folder_template": template})
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_capture_filename_template(self, template: str) -> CaptureSettings:
        """Validate and persist the FITS filename stem template."""
        validate_capture_filename_template(template)
        capture = self._capture.model_copy(update={"filename_template": template})
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_calibration_path_templates(
        self,
        *,
        bias_folder_template: str,
        dark_folder_template: str,
        flat_folder_template: str,
        filename_template: str,
    ) -> CaptureSettings:
        """Validate and persist calibration-specific folders and filename."""
        for template in (
            bias_folder_template,
            dark_folder_template,
            flat_folder_template,
        ):
            validate_capture_folder_template(template)
        validate_capture_filename_template(filename_template)
        capture = self._capture.model_copy(
            update={
                "calibration_bias_folder_template": bias_folder_template,
                "calibration_dark_folder_template": dark_folder_template,
                "calibration_flat_folder_template": flat_folder_template,
                "calibration_filename_template": filename_template,
            }
        )
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_keep_rejected_frames(self, enabled: bool) -> CaptureSettings:
        """Persist whether quality-rejected light frames remain on disk."""
        capture = self._capture.model_copy(update={"keep_rejected_frames": enabled})
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_capture_dither(
        self, *, enabled: bool, every_n_frames: int, min_pixels: float, max_pixels: float
    ) -> CaptureSettings:
        capture = self._capture.model_copy(
            update={
                "dither_enabled": enabled,
                "dither_every_n_frames": every_n_frames,
                "dither_min_pixels": min_pixels,
                "dither_max_pixels": max_pixels,
            }
        )
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_capture_center_after_drift(
        self, *, enabled: bool, threshold_pixels: float
    ) -> CaptureSettings:
        capture = self._capture.model_copy(
            update={
                "center_after_drift_enabled": enabled,
                "drift_threshold_pixels": threshold_pixels,
            }
        )
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_wide_companion_enabled(self, enabled: bool) -> CaptureSettings:
        capture = self._capture.model_copy(update={"wide_companion_enabled": enabled})
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_auto_lp_filter_for_emission_targets(self, enabled: bool) -> CaptureSettings:
        capture = self._capture.model_copy(
            update={"auto_lp_filter_for_emission_targets": enabled}
        )
        await self._write(SECTION_CAPTURE, capture)
        self._capture = capture
        return capture

    @_serialized_mutation
    async def set_skymap_cutout_autofetch_enabled(self, enabled: bool) -> SkymapSettings:
        """Flip the autofetch-on-resolve switch. Off by default
        (``SkymapSettings``'s own docstring) — this is the only way it ever
        turns on."""
        skymap = SkymapSettings(cutout_autofetch_enabled=enabled)
        await self._write(SECTION_SKYMAP, skymap)
        self._skymap = skymap
        return skymap

    @_serialized_mutation
    async def set_streaming(
        self,
        *,
        broadcast_when_unparked: bool,
        wide_camera_picture_in_picture: bool,
        rtsp_host: str | None = None,
        rtsp_port: int | None = None,
        rtsp_path: str | None = None,
        rtsp_width: int | None = None,
        rtsp_height: int | None = None,
        rtsp_fps: float | None = None,
        rtsp_bitrate_kbps: int | None = None,
        rtsp_update_interval_s: float | None = None,
        rtsp_auto_crop: bool | None = None,
        youtube_resolution: Literal["720p", "1080p", "2160p"] | None = None,
    ) -> StreamingSettings:
        updates = {
            "broadcast_when_unparked": broadcast_when_unparked,
            "wide_camera_picture_in_picture": wide_camera_picture_in_picture,
            **({} if rtsp_host is None else {"rtsp_host": rtsp_host}),
            **({} if rtsp_port is None else {"rtsp_port": rtsp_port}),
            **({} if rtsp_path is None else {"rtsp_path": rtsp_path}),
            **({} if rtsp_width is None else {"rtsp_width": rtsp_width}),
            **({} if rtsp_height is None else {"rtsp_height": rtsp_height}),
            **({} if rtsp_fps is None else {"rtsp_fps": rtsp_fps}),
            **({} if rtsp_bitrate_kbps is None else {"rtsp_bitrate_kbps": rtsp_bitrate_kbps}),
            **(
                {}
                if rtsp_update_interval_s is None
                else {"rtsp_update_interval_s": rtsp_update_interval_s}
            ),
            **({} if rtsp_auto_crop is None else {"rtsp_auto_crop": rtsp_auto_crop}),
            **({} if youtube_resolution is None else {"youtube_resolution": youtube_resolution}),
        }
        streaming = self._streaming.model_copy(
            update=updates,
        )
        await self._write(SECTION_STREAMING, streaming)
        self._streaming = streaming
        return streaming

    @_serialized_mutation
    async def set_broadcast_when_unparked(self, enabled: bool) -> StreamingSettings:
        streaming = self._streaming.model_copy(update={"broadcast_when_unparked": enabled})
        await self._write(SECTION_STREAMING, streaming)
        self._streaming = streaming
        return streaming

    @_serialized_mutation
    async def set_heater_automation(
        self, *, enabled: bool, on_margin_c: float, off_margin_c: float
    ) -> HeaterAutomationSettings:
        heater = HeaterAutomationSettings(
            enabled=enabled,
            on_margin_c=on_margin_c,
            off_margin_c=off_margin_c,
        )
        await self._write(SECTION_HEATER, heater)
        self._heater = heater
        return heater

    @_serialized_mutation
    async def add_secret_name(self, name: str) -> list[str]:
        names = sorted({*self._secrets.names, name})
        await self._write(SECTION_SECRETS, SecretRegistry(names=names))
        self._secrets = SecretRegistry(names=names)
        return names

    @_serialized_mutation
    async def remove_secret_name(self, name: str) -> list[str]:
        names = sorted(entry for entry in self._secrets.names if entry != name)
        await self._write(SECTION_SECRETS, SecretRegistry(names=names))
        self._secrets = SecretRegistry(names=names)
        return names

    # -- device address book ------------------------------------------------ #

    @_serialized_mutation
    async def remember_address(
        self,
        descriptor: DeviceDescriptor,
        *,
        label: str | None = None,
        now: datetime | None = None,
        hardware_profile_id: str | None = None,
        promote: bool = True,
    ) -> DeviceSettings:
        """Record a probe result, and promote it to *last good* if it answered.

        Returns the resulting :class:`DeviceSettings`; unchanged if the address
        did not answer, because an unreachable host must not become the
        remembered one.
        """
        moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
        last_good = moment.isoformat() if descriptor.reachable else None
        await self._database.execute(
            """
            INSERT INTO device_address (
                host, control_port, label, firmware, model, serial,
                hardware_profile_id, last_seen_at, last_good_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (host, control_port) DO UPDATE SET
                label        = COALESCE(excluded.label, device_address.label),
                firmware     = COALESCE(excluded.firmware, device_address.firmware),
                model        = COALESCE(excluded.model, device_address.model),
                serial       = COALESCE(excluded.serial, device_address.serial),
                hardware_profile_id = COALESCE(
                    excluded.hardware_profile_id, device_address.hardware_profile_id
                ),
                last_seen_at = excluded.last_seen_at,
                last_good_at = COALESCE(excluded.last_good_at, device_address.last_good_at)
            """,
            (
                descriptor.host,
                descriptor.control_port,
                label,
                descriptor.firmware,
                descriptor.model,
                descriptor.serial,
                hardware_profile_id,
                moment.isoformat(),
                last_good,
                moment.isoformat(),
            ),
        )
        if not descriptor.reachable or not promote:
            return self._device
        same_address = (
            descriptor.host == self._device.host
            and descriptor.control_port == self._device.control_port
        )
        device = DeviceSettings(
            host=descriptor.host,
            control_port=descriptor.control_port,
            ethernet_host=self._device.ethernet_host,
            ethernet_control_port=self._device.ethernet_control_port,
            label=label or self._device.label,
            last_good_at=moment,
            last_healthy_host=self._device.last_healthy_host,
            last_healthy_control_port=self._device.last_healthy_control_port,
            hardware_profile_id=(
                hardware_profile_id
                if hardware_profile_id is not None
                else self._device.hardware_profile_id
                if same_address
                else None
            ),
            hardware_profile_source=(
                "detected"
                if hardware_profile_id is not None
                else self._device.hardware_profile_source
                if same_address
                else None
            ),
            auto_connect=self._device.auto_connect,
        )
        await self._write(SECTION_DEVICE, device)
        self._device = device
        return device

    @_serialized_mutation
    async def forget_address(self, host: str, control_port: int) -> DeviceSettings:
        await self._database.execute(
            "DELETE FROM device_address WHERE host = ? AND control_port = ?",
            (host, control_port),
        )
        forget_wifi = self._device.host == host and self._device.control_port == control_port
        forget_ethernet = (
            self._device.ethernet_host == host
            and (self._device.ethernet_control_port or control_port) == control_port
        )
        if forget_wifi or forget_ethernet:
            device = self._device.model_copy(
                update={
                    "host": None if forget_wifi else self._device.host,
                    "control_port": None if forget_wifi else self._device.control_port,
                    "ethernet_host": None if forget_ethernet else self._device.ethernet_host,
                    "ethernet_control_port": (
                        None if forget_ethernet else self._device.ethernet_control_port
                    ),
                }
            )
            await self._write(SECTION_DEVICE, device)
            self._device = device
        return self._device

    @_serialized_mutation
    async def set_ethernet_address(
        self, host: str | None, control_port: int | None
    ) -> DeviceSettings:
        """Set or clear the preferred wired endpoint without replacing Wi-Fi."""
        device = self._device.model_copy(
            update={"ethernet_host": host, "ethernet_control_port": control_port}
        )
        await self._write(SECTION_DEVICE, device)
        self._device = device
        return device

    @_serialized_mutation
    async def set_wifi_address(self, host: str | None, control_port: int | None) -> DeviceSettings:
        """Set or clear the fallback endpoint without changing Ethernet."""
        device = self._device.model_copy(update={"host": host, "control_port": control_port})
        await self._write(SECTION_DEVICE, device)
        self._device = device
        return device

    @_serialized_mutation
    async def set_auto_connect(self, enabled: bool) -> DeviceSettings:
        """Flip the master switch (default on). Independent of *which* address
        is remembered — an operator turning this off wants no automatic
        connection at all, address or none."""
        device = self._device.model_copy(update={"auto_connect": enabled})
        await self._write(SECTION_DEVICE, device)
        self._device = device
        return device

    @_serialized_mutation
    async def remember_healthy_endpoint(self, host: str, control_port: int) -> None:
        device = self._device.model_copy(update={
            "last_healthy_host": host, "last_healthy_control_port": control_port,
        })
        if device != self._device:
            await self._write(SECTION_DEVICE, device)
            self._device = device

    @_serialized_mutation
    async def set_hardware_profile(self, profile_id: str) -> DeviceSettings:
        """Explicitly bind the active/remembered telescope to a bundled profile."""
        device = self._device.model_copy(
            update={"hardware_profile_id": profile_id, "hardware_profile_source": "manual"}
        )
        await self._write(SECTION_DEVICE, device)
        if device.host is not None and device.control_port is not None:
            await self._database.execute(
                "UPDATE device_address SET hardware_profile_id = ? WHERE host = ? AND control_port = ?",
                (profile_id, device.host, device.control_port),
            )
        self._device = device
        return device

    @_serialized_mutation
    async def set_detected_hardware_profile(self, profile_id: str) -> DeviceSettings:
        """Record an exact live model match without changing either network role."""
        device = self._device.model_copy(
            update={"hardware_profile_id": profile_id, "hardware_profile_source": "detected"}
        )
        await self._write(SECTION_DEVICE, device)
        self._device = device
        return device

    async def known_addresses(self) -> list[RememberedAddress]:
        rows = await self._database.fetch_all(
            """
            SELECT host, control_port, label, firmware, model, serial, hardware_profile_id,
                   last_seen_at, last_good_at, created_at
            FROM device_address
            ORDER BY last_good_at IS NULL, last_good_at DESC,
                     last_seen_at IS NULL, last_seen_at DESC, host
            """
        )
        return [RememberedAddress.model_validate(dict(row)) for row in rows]

    # -- internals ---------------------------------------------------------- #

    async def _write_location(self, location: LocationSettings) -> LocationSettings:
        await self._write(SECTION_LOCATION, location)
        self._location = location
        return location

    async def _read[T: BaseModel](self, section: str, model: type[T]) -> T:
        row = await self._database.fetch_one("SELECT value FROM config WHERE key = ?", (section,))
        if row is None:
            return model()
        raw = str(row["value"])
        try:
            return model.model_validate_json(raw)
        except ValidationError as exc:
            tolerated = self._tolerate_dropped_fields(section, model, raw, exc)
            if tolerated is not None:
                return tolerated
            raise SettingsError(
                f"stored settings section {section!r} is not valid for this build: {exc}. "
                "Refusing to start rather than silently resetting it."
            ) from exc

    def _tolerate_dropped_fields[T: BaseModel](
        self, section: str, model: type[T], raw: str, exc: ValidationError
    ) -> T | None:
        """Load a row written by an older build that carried a field this
        build has since removed (e.g. ``appearance.units``/``.language``,
        dead controls deleted rather than wired up).

        Only ``extra_forbidden`` errors are tolerated here — anything else
        (a bad enum value, a wrong type) is a real corruption and must still
        fail loudly per :meth:`_read`'s own docstring. Tolerating a field
        removal is not the same as tolerating bad data.
        """
        errors = exc.errors()
        if not errors or any(error["type"] != "extra_forbidden" for error in errors):
            return None
        data = json.loads(raw)
        dropped = [str(error["loc"][-1]) for error in errors]
        for key in dropped:
            data.pop(key, None)
        _log.warning(
            "settings section %r carried field(s) %s that this build no longer has; "
            "dropping them and loading the rest",
            section,
            dropped,
        )
        return model.model_validate(data)

    async def _write(self, section: str, value: BaseModel) -> None:
        await self._database.execute(
            "INSERT INTO config (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (section, value.model_dump_json(), datetime.now(tz=UTC).isoformat()),
        )


__all__ = ["RememberedAddress", "SettingsError", "SettingsStore"]
