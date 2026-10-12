# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Persisted configuration. Never a secret, never a device address default.

Two rules shape this module:

* **Provenance is data, not a comment.** DESIGN.md §5 requires the UI to show
  *which* source produced the active location, because a silently wrong location
  produces silently wrong twilight times — the worst failure this app has. So a
  fix is stored per source, all of them are kept, and
  :func:`ssc.settings.location.resolve` picks one by a written-down rule.
* **No address ships.** ``DeviceSettings`` has no default host and no default
  port beyond the protocol's own; an unconfigured install has ``None``
  (CLAUDE.md invariant 11).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ssc.capture.pathtemplate import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    DEFAULT_CAPTURE_FOLDER_TEMPLATE,
)
from ssc.state.models import LocationSource

#: ``config`` table keys. One JSON document per section.
SECTION_LOCATION: Final = "location"
SECTION_DEVICE: Final = "device"
SECTION_APPEARANCE: Final = "appearance"
SECTION_SECURITY: Final = "security"
SECTION_SECRETS: Final = "secrets"
SECTION_HORIZON: Final = "horizon"
SECTION_WEATHER: Final = "weather"
SECTION_OBJECT_INFO: Final = "object_info"
SECTION_CAPTURE: Final = "capture"
SECTION_SKYMAP: Final = "skymap"
SECTION_HEATER: Final = "heater"
SECTION_STREAMING: Final = "streaming"
SECTION_CLIENT: Final = "client"
SECTION_EXTERNAL_TOOLS: Final = "external_tools"


class SettingsModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ExternalToolsSettings(SettingsModel):
    astap_cli: Path | None = None
    astap_data_dir: Path | None = None

    @model_validator(mode="after")
    def _absolute_paths(self) -> ExternalToolsSettings:
        for path in (self.astap_cli, self.astap_data_dir):
            if path is not None and not path.is_absolute():
                raise ValueError("tool paths must be absolute paths on the server computer")
        return self


class ClientConfigurationSettings(SettingsModel):
    """Portable durable browser preferences; interaction-only state stays local."""

    entries: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded_entries(self) -> ClientConfigurationSettings:
        if len(self.entries) > 256:
            raise ValueError("client configuration may contain at most 256 entries")
        if any(len(key) > 200 for key in self.entries):
            raise ValueError("client configuration key or value is too large")
        if sum(len(key) + len(value) for key, value in self.entries.items()) > 2 * 1024 * 1024:
            raise ValueError("client configuration may contain at most 2 MiB")
        return self


class LocationFix(SettingsModel):
    """One source's answer to "where is the scope?"."""

    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    elevation_m: float | None = None
    timezone: str | None = None
    #: Reported horizontal accuracy. ``None`` means the source does not say.
    accuracy_m: float | None = None
    acquired_at: datetime


class LocationSettings(SettingsModel):
    """Every fix we hold, keyed by where it came from."""

    fixes: dict[LocationSource, LocationFix] = Field(default_factory=dict)
    #: Pin the active source instead of using the default precedence. ``None``
    #: means "use the precedence order", which is the normal case.
    preferred_source: LocationSource | None = None


class DeviceSettings(SettingsModel):
    """The remembered scope addresses. Empty until discovery or manual entry.

    ``host`` remains the ordinary/Wi-Fi address for backward compatibility.
    The persisted last healthy configured route is preferred, followed by
    Ethernet and ``host`` as the automatic Wi-Fi fallback.
    """

    host: str | None = None
    control_port: int | None = None
    ethernet_host: str | None = None
    ethernet_control_port: int | None = None
    label: str | None = None
    last_good_at: datetime | None = None
    last_healthy_host: str | None = None
    last_healthy_control_port: int | None = None
    hardware_profile_id: str | None = None
    hardware_profile_source: Literal["detected", "manual"] | None = None
    #: Whether ``ssc-core`` should connect to this address on its own — on
    #: startup, and again after an unexpected drop. Defaults on; an explicit
    #: user disconnect is tracked separately, at runtime, as intent — never by
    #: flipping this persisted setting (see ``ssc.state.models.AutoConnectState``).
    auto_connect: bool = True


class AppearanceSettings(SettingsModel):
    theme: str = "dark"
    font_scale: str = "normal"
    accent: str = "blue"
    background_enabled: bool = False
    background_opacity_percent: int = Field(default=15, ge=0, le=40)


class SecuritySettings(SettingsModel):
    """Flags only. The PIN itself lives in the OS keyring (invariant 8).

    Session lifetime is process configuration (``AppConfig.session_ttl_days``,
    read once at startup by ``AuthService``), not a stored setting — it was
    duplicated here too and never read from, which is exactly the kind of drift
    two independently-invented TTLs cause.
    """

    control_pin_set: bool = False
    #: Whether a session is required to reach a mutating endpoint at all.
    #: Pairing is required by default. Explicit deployment overrides and the
    #: authenticated Settings toggle still control deliberate open access.
    auth_required: bool = True



class SecretRegistry(SettingsModel):
    """The *names* of secrets held in the keyring. Values are never stored here."""

    names: list[str] = Field(default_factory=list)


WeatherProviderName = Literal["metno", "openweathermap", "weatherapi", "visualcrossing"]


class WeatherSettings(SettingsModel):
    """Provider configuration the user can set from Settings.

    Only Met.no's contact lives here. It is deliberately **not** a secret:
    Met.no requires it to be sent in the ``User-Agent`` of every request, so
    it is published by design and storing it in the keyring would imply a
    confidentiality this value does not have. OpenWeatherMap's API key *is* a
    secret and lives in the keyring under ``openweathermap_api_key``
    (invariant 8) — this model never sees it.

    ``None`` means no contact has been set here; ``AppConfig.weather_contact``
    (from ``SSC_WEATHER_CONTACT``) is then the fallback. A value stored here
    wins, so that setting it in the UI visibly takes effect on a deployment
    whose environment already carries one.
    """

    #: The deploying operator's own identifying contact — an email address or
    #: a URL Met.no can reach them at.
    metno_contact: str | None = None
    #: The sole provider used for forecast requests. Explicit rather than an
    #: implicit rank/fallback so Settings always describes actual behaviour.
    provider: WeatherProviderName = "metno"


class HeaterAutomationSettings(SettingsModel):
    """Opt-in weather-driven dew-heater policy with explicit hysteresis."""

    enabled: bool = False
    on_margin_c: float = Field(default=5.0, ge=0.0, le=15.0)
    off_margin_c: float = Field(default=7.0, ge=0.0, le=20.0)

    @model_validator(mode="after")
    def off_margin_exceeds_on_margin(self) -> HeaterAutomationSettings:
        if self.off_margin_c <= self.on_margin_c:
            raise ValueError("off_margin_c must be greater than on_margin_c for hysteresis")
        return self


class ObjectInfoSettings(SettingsModel):
    """The click-through info feature's own on/off switch (M11).

    **Off by default** — the compliance finding's own recommendation
    (`THIRD_PARTY_NOTICES.md` "Click-through info sources"): Wikipedia/Wikidata
    clear unconditionally, but enabling any external call by default is a
    product decision, not one inherited implicitly from a licence reading.
    No BYO endpoint here (deliberately, see `ssc.objectinfo`'s module
    docstring) — the one pairing that cleared is Wikipedia+Wikidata+Commons,
    keyless, and there is nowhere else compliant to point this at yet.
    """

    enabled: bool = False


class SkymapSettings(SettingsModel):
    """The sky map's own feature switches — currently just cutout autofetch.

    **Off by default.** Automatic cutout fetching on target resolution was
    removed in commit ``b0c4155`` because it was surprising and expensive —
    a fetch a user never asked for, on every target they merely looked at.
    This setting brings the behaviour back (``CutoutService.schedule_autofetch``,
    ``ssc/skymap/cutouts.py``), but as something a user opts into, never as
    restored default behaviour. Do not flip this default to ``True``: that
    would repeat exactly what ``b0c4155`` undid, for exactly the reason it
    was undone.
    """

    cutout_autofetch_enabled: bool = False


class StreamingSettings(SettingsModel):
    """Server-owned broadcast policy.

    All RTSP encoder and source-selection controls live here so broadcasting
    behaves consistently regardless of which browser tab is mounted.
    """

    broadcast_when_unparked: bool = False
    wide_camera_picture_in_picture: bool = False
    rtsp_host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    rtsp_port: int = Field(default=8554, ge=1, le=65535)
    rtsp_path: str = Field(default="live", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    rtsp_width: int = Field(default=1280, ge=320, le=3840, multiple_of=2)
    rtsp_height: int = Field(default=720, ge=180, le=2160, multiple_of=2)
    rtsp_fps: float = Field(default=1.0, ge=0.2, le=5.0)
    rtsp_bitrate_kbps: int = Field(default=2500, ge=100, le=20_000)
    rtsp_update_interval_s: float = Field(default=5.0, gt=0.0)
    rtsp_auto_crop: bool = True
    youtube_resolution: Literal["720p", "1080p", "2160p"] = "720p"

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_source_policy(cls, value: object) -> object:
        """Collapse the former three overlapping source toggles into one.

        Either former automatic-start option means the user wanted an
        automatic broadcast.  The new policy makes its lifecycle explicit:
        it runs for the whole unparked session and selects the best available
        source without another switch.
        """
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "broadcast_when_unparked" not in data:
            data["broadcast_when_unparked"] = bool(
                data.get("wide_camera_when_idle", False)
                or data.get("auto_start_when_stacking", False)
            )
        data.pop("wide_camera_when_idle", None)
        data.pop("auto_start_when_stacking", None)
        data.pop("wide_camera_until_stacked", None)
        return data


class HorizonPointFix(SettingsModel):
    """One stored vertex of the site's skyline mask (DESIGN.md 5).

    Ranges mirror :class:`ssc.astro.horizon.HorizonPoint` except altitude's
    floor: ``-10`` rather than ``-90``, because a stored obstruction point
    below roughly "the observer is in a pit" is certainly a typo, not a real
    treeline, and the API should say so rather than accept it silently.
    """

    azimuth_deg: float = Field(ge=0.0, lt=360.0)
    altitude_deg: float = Field(ge=-10.0, le=90.0)


class CaptureSettings(SettingsModel):
    """Where SSC writes captured FITS frames (DESIGN.md §9.5.2).

    ``None`` until the user chooses one — deliberately not derived from
    ``AppConfig.data_dir``. This is the one place SSC writes gigabytes a
    night, so a user will want it on a specific external disk, and it must
    never silently default to whatever partition holds the app's own
    database (CLAUDE.md invariant 11's sibling rule for storage). Validated
    on the way in by :func:`ssc.settings.capture_folder.validate_capture_folder`
    — this model itself does no I/O.
    """

    folder: Path | None = None
    #: Where beneath ``folder`` a captured frame lands, e.g.
    #: ``%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%``. Validated on the way in by
    #: :func:`ssc.capture.pathtemplate.validate_capture_folder_template` —
    #: an unknown placeholder or an escaping/absolute rendering is refused at
    #: set time, never discovered at the first real capture.
    folder_template: str = DEFAULT_CAPTURE_FOLDER_TEMPLATE
    #: Filename stem for each captured frame. SSC appends ``.fits`` and
    #: resolves collisions with a suffix instead of overwriting.
    filename_template: str = DEFAULT_CAPTURE_FILENAME_TEMPLATE
    #: Calibration frames have their own layout because they are reusable
    #: across targets and nights. ``None`` preserves the historical behavior
    #: of inheriting ``filename_template`` until the user saves a dedicated
    #: calibration pattern.
    calibration_bias_folder_template: str = "Calibration/bias/singles"
    calibration_dark_folder_template: str = "Calibration/darks/singles"
    calibration_flat_folder_template: str = "Calibration/flats/%FILTER%"
    calibration_filename_template: str | None = None
    #: Preserve lights rejected by the live-stack quality gate for later
    #: inspection. When disabled, their catalogue rows and FITS bytes are
    #: removed after the rejection decision.
    keep_rejected_frames: bool = True
    #: Manual-capture dither defaults for the S30 Pro. Keep new capture loops
    #: dithered unless the user explicitly opts out.
    dither_enabled: bool = True
    dither_every_n_frames: int = Field(default=6, ge=1)
    dither_min_pixels: float = Field(default=30.0, ge=0.0)
    dither_max_pixels: float = Field(default=200.0, gt=0.0)
    # Plate solving every light is useful for an explicitly requested long-running
    # sequence, but it must not be a prerequisite for an ordinary capture loop.
    # In particular, bench tests with a parked/closed telescope still need to save
    # frames and let Live Stacking report its own registration result independently.
    center_after_drift_enabled: bool = False
    drift_threshold_pixels: float = Field(default=400.0, gt=0.0)
    #: When Planning hands a line-emission target to Scheduling, default its
    #: otherwise-unset filter to LP. Existing/explicit plan filters are never
    #: rewritten.
    auto_lp_filter_for_emission_targets: bool = False

    @model_validator(mode="after")
    def _dither_range_is_ordered(self) -> CaptureSettings:
        if self.dither_min_pixels > self.dither_max_pixels:
            raise ValueError("dither_min_pixels must not exceed dither_max_pixels")
        return self


class HorizonSettings(SettingsModel):
    """The site's per-azimuth altitude mask: trees, roofs, the neighbour's
    floodlight (DESIGN.md 5).

    ``points`` is ``None`` when no profile has ever been recorded — *unknown*,
    never an empty list silently standing in for "no obstructions anywhere"
    (CLAUDE.md invariant 7). A recorded profile always has at least two points;
    that is enforced at the API boundary (``ssc.api.schemas.HorizonRequest``),
    not here, so a value already accepted and stored can still be read back
    even if a future build tightens the rule.
    """

    points: list[HorizonPointFix] | None = None


class ConfigurationSettings(SettingsModel):
    """Every portable non-secret settings section in one validated model.

    Security and the secret-name registry are deliberately absent: importing
    a backup must neither copy credentials nor unexpectedly lock out clients.
    """

    location: LocationSettings
    device: DeviceSettings
    appearance: AppearanceSettings
    horizon: HorizonSettings
    weather: WeatherSettings
    heater: HeaterAutomationSettings
    object_info: ObjectInfoSettings
    capture: CaptureSettings
    skymap: SkymapSettings
    streaming: StreamingSettings
    client: ClientConfigurationSettings = Field(default_factory=ClientConfigurationSettings)
    external_tools: ExternalToolsSettings = Field(default_factory=ExternalToolsSettings)


__all__ = [
    "SECTION_APPEARANCE",
    "SECTION_CAPTURE",
    "SECTION_CLIENT",
    "SECTION_DEVICE",
    "SECTION_EXTERNAL_TOOLS",
    "SECTION_HEATER",
    "SECTION_HORIZON",
    "SECTION_LOCATION",
    "SECTION_OBJECT_INFO",
    "SECTION_SECRETS",
    "SECTION_SECURITY",
    "SECTION_SKYMAP",
    "SECTION_STREAMING",
    "SECTION_WEATHER",
    "AppearanceSettings",
    "CaptureSettings",
    "ClientConfigurationSettings",
    "ConfigurationSettings",
    "DeviceSettings",
    "ExternalToolsSettings",
    "HeaterAutomationSettings",
    "HorizonPointFix",
    "HorizonSettings",
    "LocationFix",
    "LocationSettings",
    "ObjectInfoSettings",
    "SecretRegistry",
    "SecuritySettings",
    "SettingsModel",
    "SkymapSettings",
    "StreamingSettings",
    "WeatherProviderName",
    "WeatherSettings",
]
