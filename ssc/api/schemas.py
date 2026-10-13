# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Request and response bodies. Every request model forbids unknown fields.

``extra="forbid"`` is not pedantry: a typo in a client's field name must be a
400 with the offending key named, not a silently ignored setting that leaves the
user staring at a location that will not change.

Nothing in this module can carry a secret value. Secrets go *in* through
:class:`SecretUpsert` and are never echoed back; the only thing that comes out
is a name.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, date, datetime
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from ssc.api.centering_defaults import DEFAULT_CENTERING_TOLERANCE_ARCMIN
from ssc.api.conditions import ConditionFactorName, ConditionsCategory
from ssc.api.security import Scope
from ssc.astro.best_targets import MoonImpact, MoonSensitivity, SizeFit
from ssc.astro.bodies import BodyId, BodyKind
from ssc.astro.catalog import ObjectType
from ssc.astro.events import Outcome
from ssc.astro.framing import DistortionModel, MountMode, Provenance
from ssc.capture import FrameType
from ssc.plan import Diagnostic, Plan, TargetIntent, TimeSpec
from ssc.scheduler import CompiledSchedule
from ssc.settings.models import (
    AppearanceSettings,
    ConfigurationSettings,
    DeviceSettings,
    WeatherProviderName,
)
from ssc.settings.store import RememberedAddress
from ssc.state.models import (
    BulkCutoutJobState,
    CutoutState,
    DeviceDescriptor,
    LocationSource,
    LocationState,
    MountAxis,
    MountTrackingRate,
    PlanSummary,
    PolarAlignmentResult,
    PolarAlignmentStartMode,
    PrefetchJobState,
    RunState,
    StateSnapshot,
)

#: The three RFC 1918 blocks. Mirrors ``ssc.seestar.discovery.RFC1918_BLOCKS``
#: and ``ssc.main.RFC1918_BLOCKS`` — duplicated rather than imported, because
#: ``ssc.api`` may not import ``ssc.seestar`` (architecture policy invariant 1, enforced
#: by the ``import-linter`` contract) and ``ssc.main`` sits above this module.
_RFC1918_BLOCKS: Final[tuple[ipaddress.IPv4Network, ...]] = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)


def _is_lan_host(host: str) -> bool:
    """Loopback or RFC 1918 only — the same fence ``discover()`` scans inside."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv6Address):
        return address.is_link_local or address.is_private
    return any(address in block for block in _RFC1918_BLOCKS)


def _require_lan_host(host: str) -> str:
    """Refuse anything that is not a loopback or RFC 1918 address literal.

    ``discover()`` is carefully fenced to this host's own RFC 1918 subnets
    (CLAUDE.md invariant "Device connection endpoints"); a manually-typed host
    for ``probe``/``connect`` must be held to the same fence, or a client could
    be pointed at an arbitrary public address under the guise of "the scope".
    A name is refused too, for the same reason ``ssc.main.check_bind_address``
    refuses one: what is being contacted must be unambiguous.
    """
    if not _is_lan_host(host):
        raise ValueError(
            f"{host!r} is not a loopback or RFC 1918 address; SSC only talks "
            "to the scope on the LAN, never to a public host or a name"
        )
    return host


#: A device address as typed by hand: bounded length, and fenced to the LAN.
LanHost = Annotated[str, Field(min_length=1, max_length=255), AfterValidator(_require_lan_host)]


def _require_aware_utc(value: datetime) -> datetime:
    """Refuse a naive datetime rather than guess its zone (CLAUDE.md invariant 5)."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            "must be timezone-aware; a naive datetime does not say what it is offset from"
        )
    return value.astimezone(UTC)


#: A client-supplied instant. Astro requests are the first place a client hands
#: this service a *clock reading* rather than the server timestamping its own
#: action, so this is the one place that validation matters.
AwareDatetime = Annotated[datetime, AfterValidator(_require_aware_utc)]


class Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Response(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# -- health ---------------------------------------------------------------- #


class HealthResponse(Response):
    """Liveness. Deliberately uninformative to a caller with no session.

    An unpaired device on the same Wi-Fi can reach ``/api/health`` — that is
    the point of a liveness check — but it gets nothing beyond "the process is
    up". ``seq``, ``database`` and ``device_gateway`` are filled in only for a
    caller already holding a session cookie (CLAUDE.md invariant 8's LAN-security
    counterpart, DESIGN.md §9): a filesystem path naming the OS username and
    home-directory layout has no business answering an anonymous ``curl``.
    """

    status: str
    schema_version: int
    application: Literal["photon-commander"] = "photon-commander"
    desktop_instance: str | None = None
    seq: int | None = None
    database: str | None = None
    device_gateway: str | None = None
    version: str | None = None


class ApplicationRestartRequest(Request):
    confirm: Literal[True]


class ApplicationRestartResponse(Response):
    accepted: bool


class HostRebootStatusResponse(Response):
    available: bool
    reason: str


class HostRebootRequest(Request):
    confirm: Literal[True]


class HostRebootResponse(Response):
    accepted: bool
    previous_epoch: str


# -- auth ------------------------------------------------------------------ #


class PairRequest(Request):
    token: str = Field(min_length=1, max_length=256)
    label: str | None = Field(default=None, max_length=64)


class SessionResponse(Response):
    authenticated: bool
    scope: Scope | None = None
    label: str | None = None
    expires_at: datetime | None = None


class PairingResponse(Response):
    """What the desktop shell encodes into the QR code."""

    pairing_url: str
    token: str
    origin: str


class PinRequest(Request):
    pin: str = Field(min_length=4, max_length=32)


class ControlPinRequest(Request):
    """``pin: null`` removes the PIN and returns every session to control."""

    pin: str | None = Field(default=None, min_length=4, max_length=32)


# -- device ---------------------------------------------------------------- #


class DiscoverRequest(Request):
    timeout_s: float | None = Field(default=None, gt=0.0, le=60.0)


class DiscoverResponse(Response):
    scan_id: str


class ProbeRequest(Request):
    host: LanHost
    #: ``None`` uses the device layer's own default rather than a copy of it.
    control_port: int | None = Field(default=None, ge=1, le=65535)
    timeout_s: float | None = Field(default=None, gt=0.0, le=30.0)


class ProbeResponse(Response):
    """A real Test-connection result — never a green tick for an unknown host."""

    device: DeviceDescriptor
    tested_at: datetime
    remembered: bool


class ConnectRequest(Request):
    """Omit both fields to connect to the remembered address."""

    host: LanHost | None = None
    control_port: int | None = Field(default=None, ge=1, le=65535)


class AddressBookResponse(Response):
    remembered: DeviceSettings
    known: list[RememberedAddress]


class AutoConnectRequest(Request):
    """The persisted master switch (default on), alongside the remembered address."""

    enabled: bool


class EthernetAddressRequest(Request):
    """Preferred wired endpoint; null host clears it and preserves Wi-Fi."""

    host: LanHost | None = None
    control_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def port_requires_host(self) -> EthernetAddressRequest:
        if self.host is None and self.control_port is not None:
            raise ValueError("an Ethernet control port requires an Ethernet host")
        return self


class WifiAddressRequest(Request):
    """Fallback wireless endpoint; null host clears it and preserves Ethernet."""

    host: LanHost | None = None
    control_port: int | None = Field(default=None, ge=1, le=65535)

    @model_validator(mode="after")
    def port_requires_host(self) -> WifiAddressRequest:
        if self.host is None and self.control_port is not None:
            raise ValueError("a Wi-Fi control port requires a Wi-Fi host")
        return self


class HardwareProfileRequest(Request):
    profile_id: str = Field(min_length=1, max_length=80)


# -- mount (DESIGN.md §4.4b) ------------------------------------------------ #

#: The device's own advertised ``AxisRates`` ceiling (DESIGN.md §4.4b) — a
#: structural fact of this mount, not a per-request lookup, so a request can
#: be rejected before it ever reaches the device even when the live
#: ``capabilities.axis_rate_max_deg_s`` has not been read yet. The route
#: additionally checks the live figure when one is known, in case it is ever
#: lower than this.
MOUNT_AXIS_RATE_HARD_CAP_DEG_S: Final = 6.0


class MountCoordinatesRequest(Request):
    """Shared by ``/slew`` and ``/sync`` — both take exactly a target."""

    ra_hours: float = Field(ge=0.0, lt=24.0)
    dec_deg: float = Field(ge=-90.0, le=90.0)
    unpark_if_needed: bool = False


class MountZenithRequest(Request):
    """One flat-panel positioning intent, including its prerequisite."""

    unpark_if_needed: bool = False


class MountMoveAxisRequest(Request):
    """``rate_deg_s: 0`` stops that one axis — the jog-button release, not a
    separate verb. The bound here is the mount's own structural ceiling
    (DESIGN.md §4.4b); the route also checks the live advertised cap, which
    could in principle be lower."""

    axis: MountAxis
    rate_deg_s: float = Field(ge=-MOUNT_AXIS_RATE_HARD_CAP_DEG_S, le=MOUNT_AXIS_RATE_HARD_CAP_DEG_S)


class MountTrackingRequest(Request):
    """``rate`` omitted leaves the tracking rate as it was."""

    enabled: bool
    rate: MountTrackingRate | None = None


class TargetCenteringRequest(MountCoordinatesRequest):
    """J2000 target plus bounded closed-loop pointing settings."""

    tolerance_arcmin: float = Field(default=DEFAULT_CENTERING_TOLERANCE_ARCMIN, gt=0.0, le=30.0)
    exposure_s: float = Field(default=1.0, gt=0.0, le=30.0)
    gain: int | None = Field(default=200, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=5)


class TargetCenteringAttemptModel(Response):
    attempt: int
    commanded_ra_j2000_h: float
    commanded_dec_j2000_deg: float
    solved_ra_j2000_h: float
    solved_dec_j2000_deg: float
    error_arcmin: float


class TargetCenteringResponse(Response):
    converged: bool
    target_ra_j2000_h: float
    target_dec_j2000_deg: float
    tolerance_arcmin: float
    attempts: list[TargetCenteringAttemptModel]


# -- focuser (bead ssc-bkv.7) ----------------------------------------------- #


class FocuserPositionResponse(Response):
    """``None`` when not connected or the device did not answer — never
    guessed (CLAUDE.md invariant 7). ``note`` carries why, mirroring
    ``AlpacaDeviceItem.note``: ``position is None`` alone conflates "not
    connected", "NotImplemented" and "the device timed out" into one
    unexplained gap.

    ``step_size``/``step_size_note`` are the same shape applied to
    ``Focuser.StepSize`` (bead ssc-bkv.10) — a second, independent read, so a
    device that answers ``position`` but not ``stepsize`` (this one, per
    ``ssc/alpaca/devices/focuser.py``: it declares ``StepSize`` unimplemented
    rather than report a guessed microns figure) still gets an honest
    position without ``step_size`` dragging it down to ``None`` too."""

    position: int | None
    note: str | None = None
    is_moving: bool | None = None
    is_moving_note: str | None = None
    step_size: int | None = None
    step_size_note: str | None = None


class FocuserMoveRequest(Request):
    """``Focuser.Move`` — an absolute step position. No upper bound is
    enforced here: ``Focuser.MaxStep`` is device-reported, not a constant
    this schema may hardcode (CLAUDE.md invariant 11's reasoning applied to
    focuser travel, not just an address)."""

    position: int = Field(ge=0)


class AutofocusRequest(Request):
    """SSC focus-curve sampling controls.

    ``step`` and ``fine_step`` are integer focuser position deltas; this
    hardware does not report a physical StepSize, so neither is labelled as microns.
    ``settle_seconds`` cannot be shorter than two seconds because motor-idle
    does not imply an optically settled image.
    """

    step: int = Field(default=10, ge=1)
    fine_step: int = Field(default=2, ge=1)
    exposure_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    settle_seconds: float = Field(default=2.0, ge=2.0, le=60.0)

    @model_validator(mode="after")
    def fine_is_smaller_than_coarse(self) -> AutofocusRequest:
        if self.fine_step >= self.step:
            raise ValueError("fine_step must be smaller than step")
        return self


# -- filter wheel (bead ssc-bkv.6) ----------------------------------------- #


class FilterWheelStateResponse(Response):
    """Live ASCOM filter-wheel slots and current position.

    ``position == -1`` means the wheel is moving. Nullable readings retain
    their device-supplied reason instead of being replaced with a guessed
    slot or an empty list.
    """

    names: list[str] | None
    position: int | None
    names_note: str | None = None
    position_note: str | None = None


class FilterWheelPositionRequest(Request):
    """A zero-based index into the device-reported ``FilterWheel.Names``."""

    position: int = Field(ge=0)


class HeaterStateResponse(Response):
    """Live state of the dew heater exposed as Alpaca ``Switch/0``."""

    enabled: bool | None
    note: str | None = None


class HeaterStateRequest(Request):
    enabled: bool


class HeaterAutomationRequest(Request):
    enabled: bool
    on_margin_c: float = Field(default=5.0, ge=0.0, le=15.0)
    off_margin_c: float = Field(default=7.0, ge=0.0, le=20.0)

    @model_validator(mode="after")
    def off_margin_exceeds_on_margin(self) -> HeaterAutomationRequest:
        if self.off_margin_c <= self.on_margin_c:
            raise ValueError("off_margin_c must be greater than on_margin_c for hysteresis")
        return self


class HeaterAutomationResponse(Response):
    enabled: bool
    on_margin_c: float
    off_margin_c: float
    evaluated_at: datetime | None = None
    dew_margin_c: float | None = None
    action: str | None = None
    note: str | None = None


# -- settings -------------------------------------------------------------- #


class LocationFixRequest(Request):
    source: LocationSource
    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    elevation_m: float | None = Field(default=None, ge=-500.0, le=9000.0)
    timezone: str | None = Field(default=None, max_length=64)
    accuracy_m: float | None = Field(default=None, ge=0.0)


class PreferredSourceRequest(Request):
    source: LocationSource | None = None


class LocationResponse(Response):
    active: LocationState | None
    preferred_source: LocationSource | None
    #: Every fix held, so the UI can show what each source thinks (DESIGN §5).
    sources: dict[LocationSource, LocationState]


class AppearanceRequest(Request):
    workflow_guide_visible: bool = True
    #: ``auto`` is a *choice*, not a display value: the client resolves it to
    #: ``dark`` or ``red`` itself, from the site's own sunset/sunrise
    #: (``GET/POST /api/astro/night``, no target required) — this store never
    #: overwrites the user's ``auto`` choice with whichever of the two is
    #: momentarily in effect.
    theme: str = Field(default="dark", pattern="^(dark|red|auto)$")
    font_scale: str = Field(default="normal", pattern="^(small|normal|large)$")
    accent: str = Field(
        default="blue",
        pattern="^((blue|violet|green|amber|rose|teal|indigo|fuchsia|orange|lime)(-bright|-deep)?|h-alpha|sulfur|oiii)$",
    )
    background_enabled: bool = False
    #: Kept deliberately below 50% so navigation and safety state retain
    #: reliable contrast even over a bright custom astronomy image.
    background_opacity_percent: int = Field(default=15, ge=0, le=40)


class SecretUpsert(Request):
    name: str = Field(min_length=1, max_length=64)
    value: str = Field(min_length=1, max_length=4096)


class SecretNamesResponse(Response):
    """Names only. A value never crosses this boundary (CLAUDE.md invariant 8)."""

    names: list[str]


# -- weather providers (DESIGN.md 6) ---------------------------------------- #
#
# Two providers ship (THIRD_PARTY_NOTICES.md): Met.no, which needs an identifying
# contact rather than a key, and OpenWeatherMap, which needs the user's own
# key. They are configured differently on purpose and the API keeps them
# distinct rather than pretending to a uniform "credential" — a contact is
# published in every request's User-Agent, and a key never leaves the server.


class WeatherProviderStatus(Response):
    """One provider's current, real state.

    Built from the same ``availability()`` call the forecast path makes, so
    this cannot drift from what happens when a forecast is actually requested.
    """

    #: Stable provider identifier, never a display string.
    name: str
    #: Whether a forecast request would try this provider right now.
    available: bool
    #: Why not. Always set when ``available`` is false (invariant 7).
    reason: str | None = None


class WeatherSettingsResponse(Response):
    """What is configured, never the OpenWeatherMap key itself."""

    #: The Met.no contact currently in force, or ``None``. Not a secret — it
    #: is sent in the User-Agent of every Met.no request by design.
    metno_contact: str | None
    #: ``"settings"``, ``"environment"`` or ``"unset"`` — which one won. Shown
    #: because "I set it in Settings but the environment is overriding" is
    #: undiagnosable from the value alone.
    metno_contact_source: str
    #: Whether an OpenWeatherMap key is present in the keyring. The value is
    #: never returned (invariant 8).
    openweathermap_key_set: bool
    weatherapi_key_set: bool
    visualcrossing_key_set: bool
    #: False when the OS keyring itself is unusable, in which case a key
    #: cannot be stored at all and the UI must say so rather than accepting
    #: input that will be lost.
    secrets_backend_available: bool
    secrets_backend_reason: str | None = None
    #: The provider forecast calls actually use.
    provider: WeatherProviderName
    providers: list[WeatherProviderStatus]


class MetNoContactRequest(Request):
    """``None`` or blank clears the stored contact and disables Met.no."""

    contact: str | None = Field(default=None, max_length=200)


class WeatherProviderRequest(Request):
    provider: WeatherProviderName


# -- horizon profile (DESIGN.md 5) ------------------------------------------ #
#
# "Under-appreciated and very high value for a backyard scope": the per-azimuth
# altitude mask for trees, roofs and the neighbour's floodlight. Shaped like
# the location endpoints above, not folded into them, because it is optional
# site data with its own validation rules rather than another location source.


class HorizonPointRequest(Request):
    azimuth_deg: float = Field(ge=0.0, lt=360.0)
    altitude_deg: float = Field(ge=-10.0, le=90.0)


class HorizonRequest(Request):
    """At least two points, no duplicate azimuth — the same rules
    :class:`ssc.astro.horizon.HorizonProfile` enforces, checked here too so the
    rejection names the fix rather than surfacing as an opaque 422 from deep
    inside the astro core."""

    points: list[HorizonPointRequest] = Field(min_length=2, max_length=360)

    @model_validator(mode="after")
    def _no_duplicate_azimuths(self) -> HorizonRequest:
        seen: dict[float, int] = {}
        for index, point in enumerate(self.points):
            earlier = seen.get(point.azimuth_deg)
            if earlier is not None:
                raise ValueError(
                    f"duplicate azimuth {point.azimuth_deg} deg at points {earlier} and "
                    f"{index}: each azimuth may appear once in a horizon profile"
                )
            seen[point.azimuth_deg] = index
        return self


class HorizonPointModel(Response):
    azimuth_deg: float
    altitude_deg: float


class HorizonResponse(Response):
    #: Whether a profile is recorded at all — ``False`` means unknown, not "no
    #: obstructions" (architecture policy invariant 7).
    active: bool
    points: list[HorizonPointModel] | None


class EphemerisSetupResponse(Response):
    loaded: bool
    error: str | None
    folder_path: str
    kernel_path: str
    filename: str
    download_url: str
    expected_size_bytes: int
    expected_sha256: str


class SettingsResponse(Response):
    location: LocationResponse
    horizon: HorizonResponse
    appearance: AppearanceSettings
    device: DeviceSettings
    secret_names: list[str]
    control_pin_set: bool


class ConfigurationBundle(Request):
    schema_version: Literal[1] = 1
    exported_at: datetime
    settings: ConfigurationSettings


class ConfigurationImportResponse(Response):
    schema_version: Literal[1] = 1
    applied_sections: list[str]
    warnings: list[str] = Field(default_factory=list)


class ExternalToolModel(Response):
    """One optional process SSC can use without absorbing its licence."""

    id: str
    name: str
    purpose: str
    status: Literal["installed", "incomplete", "not_installed"]
    detail: str
    version: str | None = None
    detected_path: str | None = None
    license: str
    distribution: Literal["system_package", "separate_process"]
    source_url: str
    guide_article_id: str
    supported_platforms: list[str]


class ExternalToolsResponse(Response):
    platform: str
    architecture: str
    tools: list[ExternalToolModel]


class RemoteAccessResponse(Response):
    """Read-only state of the recommended tailnet-private HTTPS route."""

    state: Literal[
        "not_installed",
        "daemon_unavailable",
        "signed_out",
        "ready",
        "serving",
        "public_exposure",
        "unsafe_bind",
    ]
    detail: str
    detected_path: str | None
    dns_name: str | None
    url: str | None
    funnel_enabled: bool
    setup_command: str
    status_command: str
    disable_command: str


# -- astro (DESIGN.md 3) ----------------------------------------------------- #
#
# Every endpoint here is pure computation and needs no device (they work with
# nothing connected). ``mount`` is always a request field, never inferred from
# a connected scope — planning happens hours before the mount is touched, and
# an alt-az plan must stay possible even while the scope sits in EQ mode. When
# a device *is* connected, ``device_mount_check`` says whether it currently
# agrees with the requested mode; it never overrides the request.


class SensorSummary(Response):
    name: str
    w_px: int
    h_px: int
    pixel_um: float


class LensSummary(Response):
    id: str
    label: str
    focal_length_mm: float
    aperture_mm: float | None
    focal_ratio: float | None
    design: str | None
    distortion: DistortionModel
    sensor: SensorSummary


class CalibrationSummary(Response):
    """Mirrors ``[calibration]`` in the profile TOML (DESIGN.md 3.2, 7). ``measured
    = false`` is the honest default until a plate solve writes real numbers."""

    measured: bool
    pixel_scale_arcsec: float | None
    pa_offset_deg: float | None
    boresight_offset_arcmin: float | None
    solved_at: str | None
    solve_rms_arcsec: float | None


class HardwareCapabilitiesSummary(Response):
    tele_camera: bool
    wide_camera: bool
    wide_camera_autofocus: bool
    focuser: bool
    filter_wheel: bool
    dew_heater: bool


class ProfileDefaultsSummary(Response):
    gain: int
    exposure_stack_ms: int
    exposure_preview_ms: int
    dither_enabled: bool
    dither_pixels: int
    dither_every_n_frames: int
    lp_filter: bool
    dew_heater_percent: int


class ProfileLimitsSummary(Response):
    min_altitude_deg: float
    battery_low_percent: int
    max_exposure_altaz_s: float | None
    max_exposure_eq_s: float | None


class ProfileSummary(Response):
    id: str
    name: str
    vendor: str | None
    mount: MountMode
    eq_mode_available: bool
    aliases: list[str]
    source_urls: list[str]
    capabilities: HardwareCapabilitiesSummary
    defaults: ProfileDefaultsSummary
    limits: ProfileLimitsSummary
    optics: list[LensSummary]
    calibration: CalibrationSummary


class ProfilesResponse(Response):
    profiles: list[ProfileSummary]


class ConstellationResponse(Response):
    """Which of the 88 IAU regions a J2000 point falls in
    (``ssc.astro.constellations``, offline, Delporte 1930 boundaries)."""

    abbreviation: str
    name: str


class ConstellationBoundaryVertexModel(Response):
    ra_j2000_deg: float = Field(ge=0.0, lt=360.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)


class ConstellationBoundaryRegionModel(Response):
    """One IAU region's rings, already precessed to J2000 — see
    :func:`ssc.astro.constellations.boundaries_j2000` for why this is a
    fixed transform (no ``epoch``/``now``) and why the frontend must never
    precess these itself."""

    abbreviation: str
    name: str
    rings: list[list[ConstellationBoundaryVertexModel]]


class ConstellationBoundariesResponse(Response):
    """Every one of the 88 IAU regions' vertex rings, J2000, for drawing —
    static data, safe for a long client-side ``staleTime``
    (``ssc.astro.constellations`` module docstring: 1562 vertices, one file
    read and precessed once per process)."""

    regions: list[ConstellationBoundaryRegionModel]
    #: The data's own credit line (``boundaries_credit()``), read from the
    #: bundled file's header — never a literal string in the UI.
    credit: str


class BrightStarModel(Response):
    """One named bright star (``ssc.astro.stars``, offline, no network) —
    a point, unlike ``FieldObjectModel``: no size, orientation or type, just
    a name, position and magnitude."""

    name: str
    #: The source catalogue's own cross-reference, e.g. ``"HR 2491"``.
    designation: str
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    magnitude: float


class BrightStarsResponse(Response):
    """The bundled named bright stars within a cone, brightest first —
    ``GET /api/astro/stars``. Static data for the process lifetime, like
    ``ConstellationBoundariesResponse`` above; one credit line for the whole
    table (CC BY 4.0, IAU Working Group on Star Names), never a literal
    string in the UI."""

    stars: list[BrightStarModel]
    #: The data's own credit line (``bright_stars_credit()``), read from the
    #: bundled file's header.
    credit: str


class TargetResolutionResponse(Response):
    """A bundled-catalogue lookup (``ssc.astro.catalog``, offline, no network).
    Every field the catalogue did not give stays ``None`` — never a guess."""

    name: str
    designations: list[str]
    common_name: str | None
    object_type: ObjectType | None
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    magnitude: float | None
    size_major_arcmin: float | None
    size_minor_arcmin: float | None
    #: Major-axis position angle, degrees north through east, [0, 180) —
    #: `FieldObjectModel`'s own field, missing here until M11's "add
    #: position_angle_deg" task closed (docs/IMPLEMENTATION.md): a resolved
    #: target's own detail view could not show the orientation its map
    #: outline (`GET /api/astro/objects`) already draws correctly.
    position_angle_deg: float | None
    constellation: str | None
    #: Credit line of the data file the record came from (its licence header),
    #: e.g. OpenNGC CC BY-SA 4.0 — the UI shows it verbatim, never a static string.
    source: str
    #: Every credit line that actually contributed a field to this record,
    #: owning file first, e.g. the Messier core plus OpenNGC when OpenNGC
    #: filled a position angle the core left unset. ``source`` above stays
    #: the owner's alone; this is additive so the UI can name every
    #: contributor without changing what ``source`` means.
    sources: list[str]


class TargetSuggestionsResponse(Response):
    """Ranked, de-duplicated matches from the bundled offline catalogue."""

    suggestions: list[TargetResolutionResponse]


class CometSuggestionModel(Response):
    """One time-resolved result from the bundled JPL SBDB comet snapshot."""

    spk_id: int
    designation: str
    name: str | None
    full_name: str
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    resolved_at: datetime
    element_epoch_jd_tdb: float
    element_age_days: float = Field(ge=0.0)
    solution_date: str | None
    execution_safe: bool
    execution_rejection: str | None


class CometSuggestionsResponse(Response):
    comets: list[CometSuggestionModel]
    unavailable: list[str]
    source: str
    source_url: str
    snapshot_fetched_at: datetime
    snapshot_age_days: float
    stale: bool
    accuracy_note: str


class FieldObjectModel(Response):
    """One catalogue object inside a sky-map field (``GET /api/astro/objects``):
    what the map needs to draw its outline. Size and orientation stay ``None``
    when the catalogue has none — the map draws a circle or nothing, never a
    guessed ellipse."""

    designation: str
    common_name: str | None
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    object_type: ObjectType | None
    size_major_arcmin: float | None
    size_minor_arcmin: float | None
    #: Major-axis position angle, degrees north through east, [0, 180).
    position_angle_deg: float | None
    #: Credit line of the data file the record came from — shown while the
    #: outline is (OpenNGC CC BY-SA, HEASARC).
    source: str
    #: Every credit line that actually contributed a field to this record,
    #: owning file first (see ``TargetResolutionResponse.sources``).
    sources: list[str]


class FieldObjectsResponse(Response):
    objects: list[FieldObjectModel]
    #: Distinct ``source`` credits of the objects returned, first-seen order.
    sources: list[str]


class FieldGeometryModel(Response):
    optic_id: str
    label: str
    pixel_scale_arcsec: float
    width_deg: float
    height_deg: float
    diagonal_deg: float
    #: The guaranteed-keep lower bound (a diameter): 2.24 deg on the S30 Pro tele.
    inscribed_circle_deg: float
    provenance: Provenance
    distortion: DistortionModel
    caveats: list[str]


class OffsetModel(Response):
    """A tangent-plane offset from the target, degrees. ``+x`` east, ``+y`` north."""

    x_deg: float
    y_deg: float


class ShapeModel(Response):
    """A closed polygon of tangent-plane offsets. The closing edge is implied."""

    points: list[OffsetModel]


class RotationSampleModel(Response):
    """One sample of the adaptively-sampled ``PA(t)`` series
    (``ssc.astro.framing.sample_session``) — denser where ``|dq/dt|`` is large.

    Fetched once per session and reused for two things client-side, never a
    round trip per frame: the time-scrubber interpolates ``position_angle_deg``
    between samples, and the **frame stack** (DESIGN.md 3.2, shape 3 of 5) draws
    each sample's rectangle at ``when`` directly, using ``FieldGeometryModel`` to
    turn a position angle into a rotated rectangle the same way
    ``ssc.astro.framing.frame_shape`` does server-side.
    """

    when: datetime
    elapsed_hours: float
    hour_angle_deg: float
    altitude_deg: float
    parallactic_angle_deg: float
    position_angle_deg: float


class TargetPositionModel(Response):
    when: datetime
    hour_angle_deg: float
    declination_deg: float
    altitude_deg: float
    azimuth_deg: float


class SessionEnvelopeModel(Response):
    """Shapes 3-5 of the alt-az overlay (DESIGN.md 3.2) plus the numbers behind
    them. Shapes 1-2 (the solid start rectangle and the ghost end rectangle) are
    ``samples[0]`` and ``samples[-1]`` — also drawn from ``FieldGeometryModel``,
    so they are not repeated here as separate polygons.

    Per-sample or per-region coverage *depth* (how many frames cover a given
    point, not just how many touch it) is not computed here: it needs a 2D
    rasterisation ``ssc.astro`` does not provide (its radial arrays are a 1D
    support function per angle, sufficient for the envelope/keep-region bound
    but not for "how many frames overlap this pixel"). A client already
    rasterising ``samples`` to draw the frame stack is the cheaper place to
    accumulate that count than a second pass here — left to it rather than
    shipped as an approximation.
    """

    samples: list[RotationSampleModel]
    frame_start: ShapeModel
    frame_end: ShapeModel
    swept_envelope: ShapeModel
    keep_region: ShapeModel
    boundary_angles_deg: list[float]
    swept_radii_deg: list[float]
    keep_radii_deg: list[float]
    inscribed_circle_deg: float
    keep_region_min_diameter_deg: float
    #: Keep-region area as a fraction of the frame's, 0..1. The minimum
    #: diameter above is provably constant for a given optic — it is the short
    #: side both at zero rotation and at the inscribed-circle limit — so it
    #: cannot tell a user that a longer session costs sky. This can.
    keep_area_fraction: float
    keep_region_area_deg2: float
    swept_envelope_area_deg2: float
    total_rotation_deg: float
    max_abs_rotation_rate_deg_per_hour: float
    max_altitude_deg: float
    rotation_severity: Literal["none", "amber", "red"]
    high_altitude_transit: bool
    zenith_singularity: bool
    warnings: list[str]


class DeviceMountModeCheck(Response):
    """Whether a *connected* device's live ``mount.equ_mode`` agrees with the
    requested planning mode. Never used to decide ``mount`` — only to warn.

    ``checked`` is ``False`` whenever no live reading was available (nothing
    connected, or the read failed): ``device_equ_mode`` and ``conflict`` are
    then meaningless and must not be read as "no conflict" (CLAUDE.md
    invariant 7 — unknown is not zero).
    """

    checked: bool
    device_equ_mode: bool | None
    conflict: bool
    message: str | None


class FramingRequest(Request):
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    profile_id: str = Field(min_length=1, max_length=64)
    optic_id: str = Field(min_length=1, max_length=64)
    #: User-selected, never inferred from a connected device — see the section
    #: banner above.
    mount: MountMode
    pa_offset_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    start_time: AwareDatetime
    session_length_hours: float = Field(ge=0.0, le=24.0)
    max_pa_step_deg: float = Field(default=0.5, gt=0.0, le=45.0)
    boundary_samples: int = Field(default=360, ge=8, le=1440)


class HorizonIntervalModel(Response):
    """One continuous stretch during which a target is clear of the horizon
    mask (``ssc.astro.events.VisibilityInterval``) — a target can clear a
    treeline, be blocked by a chimney and re-emerge, so this is always a list,
    never flattened to a single rise/set pair (DESIGN.md 5)."""

    start: datetime
    end: datetime
    #: A real horizon crossing vs. the request window simply running out —
    #: never conflated, or a clipped edge would be misread as a rise/set time.
    start_is_window_edge: bool
    end_is_window_edge: bool


class FramingVisibilityModel(Response):
    """Whether the framed target is actually visible over the horizon
    profile, not just above some flat minimum altitude (DESIGN.md 5). A target
    behind a roof is not visible, and claiming otherwise is the silent-wrong-
    answer failure DESIGN.md 5 calls the worst this app has."""

    #: ``True`` only when the site has a *recorded* skyline mask; ``False``
    #: means every field below was computed against the true (0 deg)
    #: geometric horizon instead, for lack of anything better.
    horizon_profile_active: bool
    start_above_horizon: bool
    #: The mask's altitude at the target's azimuth at session start.
    start_horizon_altitude_deg: float
    #: Every clear stretch within ``[start_time, start_time + session_length_hours]``.
    clear_intervals: list[HorizonIntervalModel]


class FramingResponse(Response):
    mount: MountMode
    geometry: FieldGeometryModel
    #: Echoed with its provenance (DESIGN.md 5) so the UI can show which source
    #: this answer was computed against.
    location: LocationState
    start_position: TargetPositionModel
    envelope: SessionEnvelopeModel
    device_mount_check: DeviceMountModeCheck
    visibility: FramingVisibilityModel


class MosaicSpecRequest(Request):
    ra_num: int = Field(ge=1, le=12)
    dec_num: int = Field(ge=1, le=12)
    overlap_pct: float = Field(ge=0.0, lt=100.0)


class MosaicRequest(Request):
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    profile_id: str = Field(min_length=1, max_length=64)
    optic_id: str = Field(min_length=1, max_length=64)
    mount: MountMode
    pa_offset_deg: float = Field(default=0.0, ge=-180.0, le=180.0)
    start_time: AwareDatetime
    session_length_hours: float = Field(ge=0.0, le=24.0)
    mosaic: MosaicSpecRequest
    max_pa_step_deg: float = Field(default=0.5, gt=0.0, le=45.0)
    boundary_samples: int = Field(default=360, ge=8, le=1440)


class PanelCenterModel(Response):
    row: int
    col: int
    ra_j2000_h: float
    dec_j2000_deg: float


class MosaicPanelModel(Response):
    center: PanelCenterModel
    envelope: SessionEnvelopeModel


class MosaicResponse(Response):
    mount: MountMode
    geometry: FieldGeometryModel
    location: LocationState
    start_position: TargetPositionModel
    panel_count: int
    panels: list[MosaicPanelModel]
    #: Worst severity across every panel (DESIGN.md 3.3: mosaics and field
    #: rotation compound badly, so the mosaic's own severity is never lower
    #: than the worst panel's).
    worst_rotation_severity: Literal["none", "amber", "red"]
    warnings: list[str]
    device_mount_check: DeviceMountModeCheck


class NightRequest(Request):
    start_time: AwareDatetime
    end_time: AwareDatetime
    #: Both given or neither. When neither, the response has no target events,
    #: no altitude curve and no moon separation — twilight and moon phase only.
    ra_j2000_h: float | None = Field(default=None, ge=0.0, lt=24.0)
    dec_j2000_deg: float | None = Field(default=None, ge=-90.0, le=90.0)
    min_altitude_deg: float = Field(default=0.0, ge=-90.0, le=90.0)
    altitude_samples: int = Field(default=145, ge=2, le=500)

    @model_validator(mode="after")
    def _target_is_all_or_nothing(self) -> NightRequest:
        given = (self.ra_j2000_h is None, self.dec_j2000_deg is None)
        if given[0] != given[1]:
            raise ValueError("ra_j2000_h and dec_j2000_deg must be given together, or not at all")
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class EventTimeModel(Response):
    event: str
    when: datetime | None
    outcome: Outcome
    threshold_deg: float


class AltitudeSampleModel(Response):
    when: datetime
    altitude_deg: float


class MoonSeparationSampleModel(Response):
    when: datetime
    separation_deg: float = Field(ge=0.0, le=180.0)


class MoonInfoModel(Response):
    illuminated_fraction: float = Field(ge=0.0, le=1.0)
    #: 0-360 deg, 0 New / 90 First Quarter / 180 Full / 270 Last Quarter.
    #: ``illuminated_fraction`` alone is ambiguous (50% is either quarter, and
    #: they are mirror images) — this plus ``waxing`` is what lets a client
    #: draw the correct limb without a second call.
    phase_angle_deg: float = Field(ge=0.0, lt=360.0)
    #: ``True`` for the New-to-Full half of the cycle, ``False`` for Full-to-New.
    waxing: bool
    #: ``None`` only when no target was given — never "no data" mid-computation.
    separation_deg: float | None


class NightResponse(Response):
    location: LocationState
    window_start: datetime
    window_end: datetime
    solar_events: list[EventTimeModel]
    lunar_events: list[EventTimeModel]
    moon: MoonInfoModel
    target_events: list[EventTimeModel] | None
    altitude_curve: list[AltitudeSampleModel] | None
    #: Sparse, ephemeris-backed target-to-Moon separation samples for timeline
    #: hover interpolation; ``None`` when no target was given.
    moon_separation_curve: list[MoonSeparationSampleModel] | None
    #: ``True`` only when the site has a recorded skyline mask; ``False``
    #: means ``visibility_intervals`` (when present) was computed against the
    #: flat ``min_altitude_deg`` limit instead (DESIGN.md 5).
    horizon_profile_active: bool
    #: Every stretch the target is clear of the horizon (mask or flat limit),
    #: in ``[start_time, end_time]``. ``None`` together with ``target_events``
    #: and ``altitude_curve`` — never a guessed answer for a target the caller
    #: never named.
    visibility_intervals: list[HorizonIntervalModel] | None


class AnnualAltitudeRequest(Request):
    """Inputs to ``/api/astro/annual-altitude`` (DESIGN.md 4.4): "when in the
    year is this target actually worth imaging", one summary sample per
    night, over a run of consecutive nights starting at ``start_time``."""

    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    #: The UTC instant night 0's 24h window begins at. Pick a time away from
    #: local midnight (local noon is the usual choice) so a single true
    #: night is not split across two windows — the same convention this
    #: endpoint's sibling ``/night`` already assumes of its own
    #: ``start_time``/``end_time``.
    start_time: AwareDatetime
    nights: int = Field(default=365, ge=1, le=3650)
    min_altitude_deg: float = Field(default=0.0, ge=-90.0, le=90.0)
    #: Sampling step inside each night's astronomically-dark interval; finer
    #: gives a more precise peak time at the cost of latency (DESIGN.md 4.4).
    step_minutes: float = Field(default=15.0, gt=0.0, le=180.0)


class AnnualAltitudeNightModel(Response):
    """One night's reduction — see ``ssc.astro.events.AnnualAltitudeNight``
    for exactly what ``None`` means here versus a real ``0.0``."""

    window_start: datetime
    darkness_hours: float = Field(ge=0.0)
    max_altitude_deg: float = Field(ge=-90.0, le=90.0)
    max_when: datetime
    peak_altitude_deg: float | None = Field(default=None, ge=-90.0, le=90.0)
    peak_when: datetime | None = None
    hours_above_limit: float | None = Field(default=None, ge=0.0)


class AnnualAltitudeResponse(Response):
    location: LocationState
    min_altitude_deg: float
    nights: list[AnnualAltitudeNightModel]


class MoonAltitudeSampleModel(Response):
    at: datetime
    alt_deg: float = Field(ge=-90.0, le=90.0)


class MoonAltitudeSeriesResponse(Response):
    """The Moon's own altitude, sampled uniformly across a window — the
    Scheduling tab's grey moon curve (DESIGN.md 4.4). Distinct from
    ``NightResponse.altitude_curve``, which tracks a fixed-RA/Dec target,
    not the Moon."""

    samples: list[MoonAltitudeSampleModel]


# -- solar-system bodies ----------------------------------------------------- #


class BodyPositionModel(Response):
    """One solar-system body at one instant, for the sky map's Sun/Moon/planets
    layer (``ssc.astro.bodies``, which states every convention below).

    **Topocentric**, computed from the active location — not geocentric. The
    Moon's parallax reaches ~1 deg, twice its own diameter, so this is the
    difference between drawing it in the right place and the wrong one.

    **Apparent place referred to the J2000 (ICRF) frame**: light-time,
    deflection and aberration applied, frame *not* precessed to the equinox
    of date. That matches ``TargetResolutionResponse``, ``BrightStarModel``
    and ``ConstellationBoundariesResponse``, so the sky map can draw all four
    in one coordinate system.
    """

    id: BodyId
    #: Display name, e.g. ``"Jupiter"``. Sent so the client never has to keep
    #: its own id-to-label table in sync with this one.
    name: str
    #: ``sun`` and ``moon`` are drawn as disks at ``apparent_diameter_arcmin``;
    #: a ``planet`` is a marker, since its true disk is under an arcminute.
    kind: BodyKind
    ra_j2000_h: float = Field(ge=0.0, lt=24.0)
    dec_j2000_deg: float = Field(ge=-90.0, le=90.0)
    #: Equatorial angular width of the full disk. Always present — the client
    #: needs it to draw the Sun and Moon at their real ~0.5 deg size rather
    #: than as points.
    apparent_diameter_arcmin: float = Field(gt=0.0)
    #: ``None`` for the Sun, where illumination does not apply. Never 0 for
    #: "not computed" — 0 here means genuinely unlit.
    illuminated_fraction: float | None = Field(ge=0.0, le=1.0)
    #: Sun-body-observer angle: 0 deg fully lit, 180 deg unlit. ``None`` for
    #: the Sun. **Not** the 0-360 deg lunation angle in ``MoonInfoModel``,
    #: which answers "which limb" rather than "how lit"; a client drawing the
    #: lunar terminator should keep using that one.
    phase_angle_deg: float | None = Field(ge=0.0, le=180.0)


class BodiesResponse(Response):
    """``GET /api/astro/bodies`` — the Sun, the Moon and seven planets at one
    instant, ordered Sun, Moon, then outwards from the Sun."""

    #: The instant actually computed for, echoed back: the request parameter
    #: is optional, so a client that omitted it learns which "now" it got.
    instant: datetime
    bodies: list[BodyPositionModel]
    #: Ephemeris and radius-table attribution (``bodies_credit()``), never a
    #: literal string in the UI.
    credit: str


# -- best targets tonight (DESIGN.md 3.5) ------------------------------------ #


class BestTargetsRequest(Request):
    """Inputs to ``/api/astro/best-targets``: the same "one 24h window, picked
    away from local midnight" convention ``NightRequest``/``AnnualAltitudeRequest``
    use for ``start_time``/``end_time``. ``profile_id``/``optic_id`` pick the
    field of view the size-versus-frame judgement is made against, exactly as
    ``FramingRequest`` does."""

    start_time: AwareDatetime
    end_time: AwareDatetime
    min_altitude_deg: float = Field(default=20.0, ge=-90.0, le=90.0)
    top_n: int = Field(default=10, ge=1, le=50)
    profile_id: str = Field(min_length=1, max_length=64)
    optic_id: str = Field(min_length=1, max_length=64)
    max_candidate_magnitude: float = Field(default=13.0, ge=-5.0, le=25.0)
    allowed_object_types: list[ObjectType] | None = None

    @model_validator(mode="after")
    def _window_is_valid(self) -> BestTargetsRequest:
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be after start_time")
        return self


class AutoNightRequest(Request):
    start_time: AwareDatetime
    end_time: AwareDatetime
    profile_id: str = Field(min_length=1, max_length=64)
    optic_id: str = Field(min_length=1, max_length=64)
    mount_mode: MountMode = MountMode.EQ
    allowed_object_types: list[ObjectType] | None = None
    min_altitude_deg: float = Field(default=30, ge=0, le=85)
    max_candidate_magnitude: float = Field(default=13, ge=-5, le=25)
    available_filters: list[Literal["IR", "LP"]] = Field(default_factory=lambda: ["IR", "LP"])
    block_minutes: int = Field(default=90, ge=15, le=240)
    max_targets: int = Field(default=5, ge=1, le=12)
    exposure_sec: float = Field(default=30, gt=0, le=3600)
    gain: int = Field(default=200, ge=0, le=10000)

    @model_validator(mode="after")
    def _window_is_valid(self) -> AutoNightRequest:
        hours = (self.end_time - self.start_time).total_seconds() / 3600
        if not 0 < hours <= 36:
            raise ValueError("window must be positive and no longer than 36 hours")
        return self


class AutoNightBlockModel(Response):
    designation: str
    common_name: str | None
    object_type: ObjectType
    start_time: datetime
    end_time: datetime
    filter_name: Literal["IR", "LP"]
    peak_when: datetime
    peak_altitude_deg: float
    min_altitude_deg: float
    min_moon_separation_deg: float
    max_moon_altitude_deg: float
    max_moon_illuminated_fraction: float
    score: float
    reason_code: str


class AutoNightGapModel(Response):
    start_time: datetime
    end_time: datetime
    reason_code: str


class AutoNightResponse(Response):
    document: Plan
    blocks: list[AutoNightBlockModel]
    gaps: list[AutoNightGapModel]
    moon_illuminated_fraction: float
    darkness_hours: float
    scheduled_hours: float
    candidates_considered: int
    note: str | None


class BestTargetModel(Response):
    """One ranked target plus every component that produced its score — see
    ``ssc.astro.best_targets.BestTargetCandidate``/``BestTargetScore`` for
    what each field means and why it exists; nothing here is a bare number
    with no explanation (CLAUDE.md invariant 7)."""

    designation: str
    common_name: str | None
    object_type: ObjectType
    ra_j2000_h: float
    dec_j2000_deg: float
    magnitude: float | None
    size_major_arcmin: float | None
    score: float = Field(ge=0.0, le=100.0)
    narrowband_friendly: bool
    moon_sensitivity: MoonSensitivity
    moon_impact: MoonImpact
    #: ``None`` iff the object's size or the field geometry (or both) were
    #: not known — never a guessed fit.
    size_fit: SizeFit | None
    altitude_hours_score: float = Field(ge=0.0)
    altitude_peak_score: float = Field(ge=0.0)
    magnitude_bonus: float = Field(ge=0.0)
    moon_penalty: float = Field(ge=0.0)
    size_penalty: float = Field(ge=0.0)
    hours_above_limit: float = Field(ge=0.0)
    peak_altitude_deg: float = Field(ge=-90.0, le=90.0)
    peak_when: datetime
    moon_separation_deg: float = Field(ge=0.0, le=180.0)
    moon_altitude_at_peak_deg: float = Field(ge=-90.0, le=90.0)
    source: str
    sources: list[str]


class BestTargetsResponse(Response):
    location: LocationState
    window_start: datetime
    window_end: datetime
    min_altitude_deg: float
    darkness_hours: float = Field(ge=0.0)
    moon_illuminated_fraction: float = Field(ge=0.0, le=1.0)
    #: Pool size after the cheap ephemeris-free filter, before any target's
    #: altitude was actually solved — lets a caller tell "nothing scored"
    #: apart from "nothing was even a candidate" (e.g. every catalogue
    #: object is too far south to ever clear ``min_altitude_deg`` here).
    candidates_considered: int = Field(ge=0)
    targets: list[BestTargetModel]
    #: Set exactly when there is no astronomical darkness in the window at
    #: all (``targets`` is then always empty) — never a silent empty list.
    note: str | None


# -- click-through info (ssc/objectinfo, M11) -------------------------------- #
#
# The identifier this endpoint takes is ``FieldObjectModel.designation`` —
# whatever ``GET /api/astro/objects`` already gave the frontend for a clicked
# DSO glyph, not a new id scheme. ``configured=False`` is the honest "no
# source configured" state (THIRD_PARTY_NOTICES.md's shipped-disabled
# recommendation): a real, renderable shape, never a bare 404 or an empty
# body, and never conflated with ``found=False`` (the feature *is* enabled
# but no article exists for this object — also a real, cacheable answer).


class ObjectInfoAttributionModel(Response):
    """One credit line, driven by the API that actually produced the
    content it credits — never a static string (CLAUDE.md invariant 7,
    THIRD_PARTY_NOTICES.md requirement 4)."""

    text: str
    url: str | None = None


class ObjectInfoImageModel(Response):
    """One Commons file's own licence — resolved per file from Commons'
    ``imageinfo``/``extmetadata``, never a blanket "CC BY-SA" assumption
    (THIRD_PARTY_NOTICES.md's load-bearing finding for this feature)."""

    url: str
    width: int | None
    height: int | None
    license_short_name: str
    artist: str | None
    credit: str | None
    #: The Commons file description page — the credit line's own link.
    source_url: str


class ObjectInfoResponse(Response):
    """A resolved (or honestly negative, or honestly unconfigured) lookup
    for one catalogue object."""

    designation: str
    #: Whether the feature is turned on at all. ``False`` means every other
    #: field below is empty/``None`` and ``message`` names the fix — never a
    #: blank panel or a spinner (docs/IMPLEMENTATION.md M11 gate).
    configured: bool
    #: Whether a source was found, given the feature is configured. ``False``
    #: with ``configured=True`` means Wikipedia genuinely has no article —
    #: a real answer, not an error.
    found: bool
    common_name: str | None = None
    title: str | None = None
    article_url: str | None = None
    extract: str | None = None
    wikidata_qid: str | None = None
    instance_of: list[str] = Field(default_factory=list)
    discovery_date: str | None = None
    image: ObjectInfoImageModel | None = None
    attributions: list[ObjectInfoAttributionModel] = Field(default_factory=list)
    #: Set whenever there is nothing else to show: "no catalogue source
    #: configured" (matches the vector sky map's own click stub wording) or
    #: "no Wikipedia article found for ...". ``None`` only when ``found``.
    message: str | None = None
    #: Served from the SQLite cache rather than a live fetch just now.
    cached: bool = False
    fetched_at: datetime | None = None


class ObjectInfoSettingsResponse(Response):
    enabled: bool


class ObjectInfoEnabledRequest(Request):
    enabled: bool


class CaptureFolderRequest(Request):
    """``None`` clears the configured folder back to unset — capture then
    refuses (409) rather than picking somewhere on its own (CLAUDE.md
    invariant 11's sibling rule for storage)."""

    folder: str | None = None


class CaptureFolderResponse(Response):
    folder: str | None = None
    #: A *suggestion* only (``ssc.settings.capture_folder.default_capture_root``)
    #: — never what ``folder`` silently defaults to. ``None`` if even
    #: ``$HOME`` could not be resolved on this host.
    default_root: str | None = None


class CaptureFolderTemplateRequest(Request):
    """Refused (422) at set time if it uses an unknown ``%PLACEHOLDER%`` or
    renders to an absolute path or one that escapes via ``..``
    (``ssc.capture.pathtemplate.validate_capture_folder_template``)."""

    template: str


class CaptureFolderTemplateResponse(Response):
    template: str


class CaptureFilenameTemplateRequest(Request):
    """Filename stem template. SSC appends ``.fits`` after rendering."""

    template: str


class CaptureFilenameTemplateResponse(Response):
    template: str


class CalibrationPathTemplates(Request):
    bias_folder_template: str
    dark_folder_template: str
    flat_folder_template: str
    filename_template: str


class CaptureRejectedFramesSettings(Request):
    keep_rejected_frames: bool = True


class CaptureDitherRequest(Request):
    enabled: bool = False
    every_n_frames: int = Field(default=6, ge=1)
    min_pixels: float = Field(default=30.0, ge=0.0)
    max_pixels: float = Field(default=200.0, gt=0.0)

    @model_validator(mode="after")
    def _range_is_ordered(self) -> CaptureDitherRequest:
        if self.min_pixels > self.max_pixels:
            raise ValueError("min_pixels must not exceed max_pixels")
        return self


class CaptureDitherResponse(Response):
    enabled: bool
    every_n_frames: int
    min_pixels: float
    max_pixels: float


class CaptureCenterAfterDriftRequest(Request):
    enabled: bool = False
    threshold_pixels: float = Field(default=400.0, gt=0.0)


class CaptureCenterAfterDriftResponse(Response):
    enabled: bool
    threshold_pixels: float


class AutoLpFilterRequest(Request):
    enabled: bool = False


class AutoLpFilterResponse(Response):
    enabled: bool


class StreamingSettingsRequest(Request):
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


class StreamingSettingsResponse(Response):
    broadcast_when_unparked: bool
    wide_camera_picture_in_picture: bool
    rtsp_host: str
    rtsp_port: int
    rtsp_path: str
    rtsp_width: int
    rtsp_height: int
    rtsp_fps: float
    rtsp_bitrate_kbps: int
    rtsp_update_interval_s: float
    rtsp_auto_crop: bool
    youtube_resolution: Literal["720p", "1080p", "2160p"]
    youtube_stream_key_set: bool
    active: bool
    url: str
    source: Literal["wide_camera", "live_stack"] | None
    last_error: str | None


class CapturePathTagModel(Response):
    """One ``%PLACEHOLDER%`` the capture folder template may use
    (``ssc.capture.pathtemplate.PathTag``), with an example rendering so the
    Settings UI never hardcodes the vocabulary."""

    name: str
    description: str
    example: str


class CapturePathTagsResponse(Response):
    tags: list[CapturePathTagModel]


class SkymapAutofetchSettingsResponse(Response):
    """Whether resolving a target also fires one autofetch cutout for it
    (``ssc.settings.models.SkymapSettings``'s own docstring — off by default)."""

    cutout_autofetch_enabled: bool


class SkymapAutofetchEnabledRequest(Request):
    cutout_autofetch_enabled: bool


# -- security (DESIGN.md §9) -------------------------------------------------- #


class SecurityResponse(Response):
    """Whether pairing is currently enforced. ``auth_required`` mirrors
    ``StateSnapshot.settings.auth_required`` exactly — this endpoint exists so
    Settings has something to ``GET``/``PUT`` without waiting on the next
    state event."""

    auth_required: bool
    #: ``True`` when ``SSC_AUTH_REQUIRED`` pins the value — the Settings
    #: toggle exists but a ``PUT`` to change it is refused (409), because it
    #: would have no effect and silently appearing to accept it would be
    #: exactly the degradation architecture policy invariant 7 forbids.
    auth_required_locked: bool


class AuthRequiredRequest(Request):
    auth_required: bool


# -- weather ----------------------------------------------------------------- #


class ForecastPointModel(Response):
    """One timestamp's forecast, from exactly one provider — the wire shape of
    :class:`ssc.weather.base.ForecastPoint`, minus ``seeing_arcsec`` and
    ``transparency``: both are always ``None`` from every shipped provider
    (`THIRD_PARTY_NOTICES.md`), so they are omitted here rather than shipped as a
    field that never carries anything.
    """

    t: datetime
    cloud_total: float | None
    cloud_low: float | None
    cloud_mid: float | None
    cloud_high: float | None
    precip_prob: float | None
    humidity: float | None
    dew_point_c: float | None
    temp_c: float | None
    wind_ms: float | None
    wind_direction_deg: float | None = None
    visibility_m: float | None
    #: Provenance — which provider produced this row (DESIGN.md §6).
    source: str


class WeatherForecastResponse(Response):
    location: LocationState
    #: When this series actually left a provider (or, for a merged series,
    #: the earliest such time among its inputs).
    fetched_at: datetime
    #: ``None`` for a fresh fetch. Set to the last good fetch's time when a
    #: live fetch failed and a cached series was served instead — a dark site
    #: with no signal is the normal case, never rendered as current
    #: (architecture policy invariant 7).
    stale_since: datetime | None
    points: list[ForecastPointModel]


# -- tonight (the header widget) --------------------------------------------- #


class TonightCloudModel(Response):
    total: float | None
    low: float | None
    mid: float | None
    high: float | None


class TonightWeatherRowModel(Response):
    """One evidence row behind ``conditions`` — never a substitute for it."""

    t: datetime
    cloud: TonightCloudModel
    precip_prob: float | None
    humidity: float | None
    dew_point_c: float | None
    temp_c: float | None
    wind_ms: float | None
    wind_direction_deg: float | None = None
    source: str


class TonightWeatherModel(Response):
    fetched_at: datetime
    stale_since: datetime | None
    #: The forecast rows falling inside tonight's dark window — the evidence
    #: for ``conditions``, chronological.
    rows: list[TonightWeatherRowModel]


class CaptureConditionFactorModel(Response):
    name: ConditionFactorName
    measurement: str
    points_deducted: float | None


class CaptureConditionsModel(Response):
    """SSC's own synthesis of cloud, moon and dew-point margin for tonight —
    a judgement call, not a measurement. Never confuse with
    ``ForecastPointModel.transparency``/``seeing_arcsec`` (always ``None`` —
    no shipped provider supplies either; see ``ssc.api.conditions``)."""

    category: ConditionsCategory
    score: float = Field(ge=0.0, le=100.0)
    #: Always non-empty — why the score is what it is, in the order each
    #: factor was evaluated (``ssc.api.conditions.assess_capture_conditions``).
    reasons: list[str]
    #: Complete scoring evidence in scoring order. A null deduction means the
    #: input was unavailable; zero means it was measured and had no impact.
    factors: list[CaptureConditionFactorModel]


class MoonTonightModel(Response):
    illuminated_fraction: float = Field(ge=0.0, le=1.0)
    phase_angle_deg: float = Field(ge=0.0, lt=360.0)
    waxing: bool
    #: ``outcome`` carries the "does not occur tonight" case (the Moon can
    #: stay up or stay down all night) rather than a null time a client would
    #: render as blank (architecture policy invariant 7 / ``ssc.astro.events.Outcome``).
    moonrise: EventTimeModel
    moonset: EventTimeModel


class TonightResponse(Response):
    location: LocationState
    generated_at: datetime
    #: Both ``None`` when the solved solar events show no astronomical
    #: darkness in the searched span at all — see ``dark_window_note``.
    dark_window_start: datetime | None
    dark_window_end: datetime | None
    dark_window_note: str | None
    moon: MoonTonightModel
    #: ``None`` iff ``weather_unavailable_reason`` is set — no cached forecast
    #: at all, fresh or stale, for this location right now.
    weather: TonightWeatherModel | None
    weather_unavailable_reason: str | None
    #: ``None`` iff ``conditions_unavailable_reason`` is set (weather itself
    #: unavailable, or no forecast row falls inside tonight's window).
    conditions: CaptureConditionsModel | None
    conditions_unavailable_reason: str | None


# -- state ----------------------------------------------------------------- #


class StateResponse(Response):
    #: This server process's identity. A client must hold onto it alongside
    #: ``seq`` and present both together when it resumes over ``/ws`` — a
    #: ``seq`` alone cannot tell a restarted process from the one before it.
    epoch: str
    seq: int
    #: The oldest ``seq`` a client may still replay from. Below this, resnapshot.
    oldest_replayable_seq: int
    snapshot: StateSnapshot


# -- system telemetry ------------------------------------------------------- #


class SystemProcessMetrics(Response):
    pid: int
    uptime_s: float = Field(ge=0.0)
    cpu_percent: float = Field(ge=0.0)
    memory_bytes: int = Field(ge=0)
    threads: int = Field(ge=0)


class SystemCpuMetrics(Response):
    percent: float = Field(ge=0.0)
    logical_cores: int = Field(ge=0)
    load_1m: float = Field(ge=0.0)
    load_5m: float = Field(ge=0.0)
    load_15m: float = Field(ge=0.0)
    temperature_c: float | None
    throttled_raw: str | None
    throttle_flags: list[str] | None


class SystemMemoryMetrics(Response):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)
    percent_used: float = Field(ge=0.0, le=100.0)


class SystemSwapMetrics(Response):
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    percent_used: float = Field(ge=0.0, le=100.0)


class SystemVolumeMetrics(Response):
    kind: Literal["os", "data"]
    label: str
    path: str
    device: str | None
    filesystem: str | None
    total_bytes: int = Field(ge=0)
    used_bytes: int = Field(ge=0)
    free_bytes: int = Field(ge=0)
    percent_used: float = Field(ge=0.0, le=100.0)
    same_device_as_os: bool


class SystemNetworkInterface(Response):
    name: str
    ipv4_address: str


class SystemNetworkMetrics(Response):
    interfaces: list[SystemNetworkInterface]
    bytes_sent: int = Field(ge=0)
    bytes_received: int = Field(ge=0)


class SystemMetricsResponse(Response):
    collected_at: datetime
    hostname: str
    platform: str
    architecture: str
    model: str | None
    os_name: str
    kernel: str
    system_uptime_s: float = Field(ge=0.0)
    process: SystemProcessMetrics
    cpu: SystemCpuMetrics
    memory: SystemMemoryMetrics
    swap: SystemSwapMetrics
    volumes: list[SystemVolumeMetrics]
    network: SystemNetworkMetrics


# -- plans (DESIGN.md 4) ------------------------------------------------------ #
#
# ``Plan``/``PlanItem``/``TimeSpec`` (``ssc.plan``) and ``CompiledSchedule``
# (``ssc.scheduler``) are reused here directly rather than re-declared: both
# are already Pydantic v2 models with ``extra="forbid"`` and, for
# ``PlanItem``/``TimeSpec``, a discriminated union on ``kind`` — exactly what
# the generated TypeScript client needs, and re-declaring them would be the
# hand-mirrored union architecture policy warns against, just one layer further out.


class PlanListResponse(Response):
    #: Identity only (id/name/revision/updated_at) — the same projection the
    #: snapshot's ``plans`` section carries. Fetch a document with
    #: ``GET /api/plans/{id}``.
    plans: list[PlanSummary]


class PlanRecordResponse(Response):
    """One stored plan: the document, its revision, and this row's own
    persistence times — distinct from the ``created_at``/``updated_at`` the
    document itself carries (``ssc/store/plans.py``)."""

    document: Plan
    revision: int
    created_at: datetime
    updated_at: datetime
    night_date: date | None


class PlanCreateRequest(Request):
    document: Plan
    night_date: date | None = None


class PlanUpdateRequest(Request):
    document: Plan
    #: The revision the client last saw. A mismatch is a 409: reload and
    #: reapply the edit, rather than silently overwrite someone else's newer
    #: change (architecture policy invariant 4 — clients render state, never own it).
    expected_revision: int


class PlanNightRequest(Request):
    night_date: date | None


class PlanApplyNightRequest(Request):
    night_date: date
    expected_revision: int = Field(ge=1)


class PlanValidateRequest(Request):
    document: Plan


class PlanValidateResponse(Response):
    diagnostics: list[Diagnostic]


class PlanCompileRequest(Request):
    #: The local calendar date of the night to compile, in the sense
    #: ``ssc.scheduler.compiler`` uses it (evening of this date to morning of
    #: the next, local). ``None`` defaults to tonight, per
    #: ``ssc.api.plans_service.current_night_date``.
    night_date: date | None = None


class PlanCompileResponse(Response):
    schedule: CompiledSchedule
    #: The structural findings from ``ssc.plan.validate_plan`` — id
    #: uniqueness, chain shape, mosaic grid sanity — checkable on the document
    #: alone. Per-item astronomical diagnostics (visibility, Moon separation,
    #: meridian crossings) are already inline on ``schedule.items[*].diagnostics``.
    diagnostics: list[Diagnostic]


class FromIntentRequest(Request):
    """DESIGN.md 3.4's "Send to Scheduling" handoff: append one item built
    from a ``TargetIntent`` to a stored plan."""

    intent: TargetIntent
    #: Where the new item starts. ``None`` defaults to ``asap`` — "after
    #: whatever the plan already does" — matching a plain "send to scheduling"
    #: with no explicit time chosen.
    start: TimeSpec | None = None
    duration_min: float | None = None


class ScheduleIntentRequest(FromIntentRequest):
    """Atomic selection/create plus append used by the Planning screen."""

    plan_id: str | None = None
    new_plan: Plan | None = None
    night_date: date | None = None

    @model_validator(mode="after")
    def _new_plan_required_without_id(self) -> ScheduleIntentRequest:
        if self.plan_id is None and self.new_plan is None:
            raise ValueError("new_plan is required when plan_id is absent")
        return self


class FromIntentResponse(Response):
    document: Plan
    revision: int
    #: The appended item's id, so the caller can select/scroll to it without
    #: re-diffing the returned document against what it sent.
    item_id: str


# -- runs (ssc/api/run_service.py) --------------------------------------------- #


class RunStartRequest(Request):
    plan_id: str = Field(min_length=1)
    #: As for ``PlanCompileRequest``: ``None`` means tonight.
    night_date: date | None = None


class RunControlRequest(Request):
    """``pause``/``resume``/``stop``: run-level, so no item. The reason is
    journalled verbatim — say why, for the morning."""

    reason: str = ""


class RunItemControlRequest(Request):
    """``skip``/``retry`` name the item they act on."""

    item_id: str = Field(min_length=1)
    reason: str = ""


class RunResponse(Response):
    #: ``None`` from ``GET /api/runs/current`` when nothing has ever run.
    run: RunState | None


class RunHistoryModel(Response):
    run_id: str
    plan_name: str
    plan_revision: int
    night_date: date
    status: str
    started_at: datetime


class RunHistoryResponse(Response):
    runs: list[RunHistoryModel]


class RunEffectModel(Response):
    """``ssc.scheduler.executor.EffectCall`` as journalled."""

    name: str
    args: dict[str, str | float | int | bool | None]
    completion: str
    event_kind: str | None
    deadline_s: float | None


class RunJournalRecordModel(Response):
    """One ``JournalRecord`` — the post-mortem's unit. Field for field the
    executor's own record; ``seq`` is the store's per-run ordinal."""

    seq: int
    at: datetime
    kind: str
    reason: str
    item_id: str | None
    panel_index: int | None
    attempt: int
    step: int
    status: str | None
    held: bool | None
    effect: RunEffectModel | None
    effect_outcome: str | None
    control: str | None
    run_status: str | None


class RunCaptureSummaryModel(Response):
    saved_frames: int
    pending_frames: int
    captured_integration_s: float
    unknown_exposure_frames: int
    accepted_frames: int
    rejected_frames: int
    stack_error_frames: int
    stack_unmeasured_frames: int
    accepted_integration_s: float | None


class RunFinalMountModel(Response):
    polled_at: datetime | None
    at_park: bool | None
    tracking: bool | None
    slewing: bool | None


class RunDetailResponse(Response):
    run: RunState
    journal: list[RunJournalRecordModel]
    final_mount: RunFinalMountModel | None
    capture_summary: RunCaptureSummaryModel


class HistoricalFrameSummary(Response):
    """Totals over unique retained catalogue frames, never exposure intents."""

    saved_frames: int
    pending_frames: int
    captured_integration_s: float
    unknown_exposure_frames: int
    outcome_measured_frames: int
    stack_unmeasured_frames: int
    accepted_frames: int | None
    rejected_frames: int | None
    stack_error_frames: int | None
    accepted_integration_s: float | None
    rejected_integration_s: float | None
    stack_error_integration_s: float | None
    unknown_outcome_exposure_frames: int


class HistoricalMeasurement(Response):
    value: float | None = None
    unit: str | None = None
    sample_count: int = 0
    total_samples: int
    source: str | None = None
    first_at: datetime | None = None
    last_at: datetime | None = None
    unavailable_reason: str = "No historical measurement was recorded."


class HistoricalMeasurements(Response):
    sky_quality: HistoricalMeasurement
    weather: HistoricalMeasurement
    hfr: HistoricalMeasurement
    fwhm: HistoricalMeasurement


class HistoricalItemReport(Response):
    item_id: str | None
    kind: str | None
    object_name: str | None
    # Elapsed compiled window, not planned science exposure integration.
    planned_duration_s: float | None
    panel_index: int | None
    panel_attribution_available: bool
    lights: HistoricalFrameSummary
    calibration: HistoricalFrameSummary


class HistoricalExecutionReport(Response):
    run: RunState
    plan: Plan
    schedule: CompiledSchedule
    lights: HistoricalFrameSummary
    calibration: HistoricalFrameSummary
    items: list[HistoricalItemReport]


class HistoricalObjectReport(Response):
    object_name: str | None
    lights: HistoricalFrameSummary
    calibration: HistoricalFrameSummary


class HistoricalNightReportResponse(Response):
    night_date: date
    executions: list[HistoricalExecutionReport]
    lights: HistoricalFrameSummary
    calibration: HistoricalFrameSummary
    objects: list[HistoricalObjectReport]
    measurements: HistoricalMeasurements


# -- capture (ssc/api/capture_orchestrator.py, ssc/api/capture_service.py) --- #


class CaptureExposureRequest(Request):
    """``POST /api/capture/exposure``'s pinned body — see the dispatch
    contract. ``duration_s`` is validated against the camera's own
    ``ExposureMin``/``ExposureMax`` at request time, not here (those bounds
    are per-device, read live — CLAUDE.md invariant 11's sibling rule).

    ``frame_type`` replaces the old ``light: bool`` — a dark and a flat are
    both ``light=false``, and the folder template needs to tell them apart
    (CLAUDE.md's capture contract change, 2026-09-10). Today's capture UI
    only offers ``light``/``dark``; ``flat``/``bias`` are accepted here too
    because the type is a closed, already-settled set — see
    ``ssc.capture.frame.FrameType``'s own docstring for why.
    """

    duration_s: float
    gain: int | None = Field(default=200, ge=0)
    object: str | None = None
    frame_type: FrameType
    optic_id: Literal["tele", "wide"] | None = None
    flat_source: Literal["panel", "sky"] | None = None
    capture_mode: Literal["single", "loop"] = "single"
    frame_limit: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def frame_limit_requires_loop(self) -> CaptureExposureRequest:
        if self.frame_limit is not None and self.capture_mode != "loop":
            raise ValueError("frame_limit requires capture_mode=loop")
        return self

    @model_validator(mode="after")
    def bias_uses_zero_duration_sentinel(self) -> CaptureExposureRequest:
        if self.frame_type is FrameType.BIAS and self.duration_s != 0:
            raise ValueError(
                "a bias capture requires duration_s=0; SSC resolves zero to the "
                "camera's live ExposureMin"
            )
        if self.frame_type is FrameType.FLAT and self.optic_id is None:
            raise ValueError("a flat capture requires an optic_id")
        if self.frame_type is FrameType.FLAT and self.flat_source is None:
            raise ValueError("a flat capture requires flat_source=panel or flat_source=sky")
        return self


class CalibrationCaptureStartRequest(Request):
    """Start one server-owned dark/bias set or per-filter flat run."""

    kind: Literal["bias", "dark", "flat"]
    count: int = Field(
        default=30,
        ge=1,
        le=500,
        description="Frame count; UI quality defaults are 100 bias, 50 dark, 30 per flat filter.",
    )
    duration_s: float | None = Field(default=None, gt=0)
    gain: int | None = Field(default=200, ge=0)
    filter_positions: list[int] = Field(default_factory=list, max_length=16)
    optic_id: str | None = Field(default=None, min_length=1, max_length=64)
    flat_source: Literal["panel", "sky"] | None = None
    generate_master: bool = False
    keep_only_master: bool = False

    @model_validator(mode="after")
    def flat_fields_match_kind(self) -> CalibrationCaptureStartRequest:
        if self.kind == "flat":
            if not self.filter_positions:
                raise ValueError("an automatic flat run requires at least one filter position")
            if len(set(self.filter_positions)) != len(self.filter_positions):
                raise ValueError("flat filter positions must be unique")
            if any(position < 0 for position in self.filter_positions):
                raise ValueError("flat filter positions must be non-negative")
            if self.optic_id is None or self.flat_source is None:
                raise ValueError("an automatic flat run requires optic_id and flat_source")
        elif self.filter_positions or self.optic_id is not None or self.flat_source is not None:
            raise ValueError("bias/dark runs do not use flat filter/source/optic fields")
        if self.kind == "dark" and self.duration_s is None:
            raise ValueError("an automatic dark run requires duration_s")
        if self.kind != "dark" and self.duration_s is not None:
            raise ValueError("duration_s is only used by dark runs")
        if self.keep_only_master and not self.generate_master:
            raise ValueError("keep_only_master requires generate_master=true")
        if self.generate_master and self.count < 10:
            raise ValueError("a verified master requires at least 10 source frames")
        return self


class LiveStackResetRequest(Request):
    """Calibration masters selected from the catalogue or uploaded store."""

    dark_master_id: int | None = Field(default=None, ge=1)
    bias_master_id: int | None = Field(default=None, ge=1)
    flat_master_id: int | None = Field(default=None, ge=1)
    dark_master_path: str | None = Field(default=None, min_length=1)
    bias_master_path: str | None = Field(default=None, min_length=1)
    flat_master_path: str | None = Field(default=None, min_length=1)
    auto_select_masters: bool = True
    watch_folder: str | None = Field(default=None, min_length=1, max_length=4096)
    watch_recursive: bool = False
    optic_id: str | None = Field(default=None, min_length=1, max_length=64)
    binning: Literal[1, 2] | None = None


class LiveStackMasterModel(Response):
    name: str
    path: str
    frame_type: FrameType


class CalibrationMasterModel(Response):
    name: str
    path: str
    frame_type: FrameType
    status: Literal["active", "archived"]
    provenance_available: bool


class CalibrationMastersResponse(Response):
    masters: list[CalibrationMasterModel]


class CalibrationSourceSetModel(Response):
    frame_ids: list[int]
    frame_type: FrameType
    frame_count: int
    gain: int | None
    exposure_s: float | None
    filter_name: str | None
    width: int
    height: int
    started_at: AwareDatetime
    ended_at: AwareDatetime
    can_build: bool
    blocker: str | None
    compatible_bias_frames: int


class CalibrationSourceSetsResponse(Response):
    sets: list[CalibrationSourceSetModel]


class CalibrationMasterBuildRequest(Request):
    frame_ids: list[int] = Field(min_length=10, max_length=500)

    @model_validator(mode="after")
    def unique_sources(self) -> CalibrationMasterBuildRequest:
        if len(set(self.frame_ids)) != len(self.frame_ids):
            raise ValueError("calibration master source frame ids must be unique")
        return self


class CalibrationMasterActionRequest(Request):
    frame_type: FrameType
    name: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def calibration_only(self) -> CalibrationMasterActionRequest:
        if self.frame_type is FrameType.LIGHT:
            raise ValueError("light frames are not calibration masters")
        return self


class LiveStackMastersResponse(Response):
    masters: list[LiveStackMasterModel]


class LiveStackDirectoryResponse(Response):
    path: str
    parent: str | None
    directories: list[str]


class LiveStackResponse(Response):
    sequence: int
    output_path: str
    parameters_path: str
    frame_count: int
    dark_master: str | None
    bias_master: str | None
    flat_master: str | None
    active: bool
    error: str | None
    auto_select_masters: bool = True
    master_selection_message: str | None = None
    watch_folder: str | None = None
    watch_recursive: bool = False
    processing_file: str | None = None
    pending_files: int = 0
    binning: Literal[1, 2] = 1
    last_shift_y_px: float | None = None
    last_shift_x_px: float | None = None
    registration_peak: float | None = None
    registration_peak_ratio: float | None = None
    object_name: str | None = None
    filter_name: str | None = None
    exposure_s: float | None = None
    gain: int | None = None
    ccd_temp_c: float | None = None
    hfr_px: float | None = None
    fwhm_px: float | None = None
    star_count: int | None = None
    detected_source_count: int | None = None
    frame_contrast_snr: float | None = None
    snr_estimate: float | None = None
    rejected_count: int = 0
    last_rejection_reason: str | None = None
    rejected_folder: str | None = None
    total_integration_s: float | None = None
    target_header: str | None = None


class LiveStackRtspStartRequest(Request):
    host: str = Field(default="0.0.0.0", min_length=1, max_length=255)
    port: int = Field(default=8554, ge=1, le=65535)
    path: str = Field(default="live", min_length=1, max_length=64)
    width: int = Field(default=1280, ge=320, le=3840, multiple_of=2)
    height: int = Field(default=720, ge=180, le=2160, multiple_of=2)
    fps: float = Field(default=1.0, ge=0.2, le=5.0)
    bitrate_kbps: int = Field(default=1500, ge=100, le=20_000)
    update_interval_s: Literal[5, 10, 30] = 5
    auto_crop: bool = True
    wide_camera_until_stacked: bool = False


class LiveStackRtspResponse(Response):
    active: bool
    url: str | None
    clients: int
    encoded_version: str | None
    last_error: str | None
    encoder: str | None
    host: str | None
    port: int | None
    path: str | None
    width: int | None
    height: int | None
    fps: float | None
    bitrate_kbps: int | None
    update_interval_s: float | None
    auto_crop: bool | None
    wide_camera_until_stacked: bool | None
    source: Literal["wide_camera", "live_stack"] | None = None


class YoutubeStreamResponse(Response):
    publishing: bool = False
    active: bool
    configured: bool
    last_error: str | None


# -- polar alignment (ssc/api/polar_alignment_service.py) ------------------- #


class PlateSolveHereRequest(Request):
    """``POST /api/mount/polar-alignment/solve-here``'s body.

    No start point and no point count: this solves where the scope is
    already pointing and moves nothing. ``gain`` is applied before the
    exposure exactly as a sweep applies it, and is unbounded here for the
    same reason — the camera's own ``GainMin``/``GainMax`` are per-device and
    read live by the driver, never guessed at in this schema.
    """

    exposure_s: float = Field(gt=0.0, le=120.0)
    gain: int | None = Field(default=200, ge=0)


class PlateSolveHereResponse(Response):
    """Where the scope actually points, next to where it believes it points.

    Both halves matter and neither is derivable from the other: the solved
    coordinates are the measurement, and ``mount_*`` is what the mount
    reported for the same instant. The difference between them is the whole
    answer to "is this preset pointing where it claims" — so they are
    returned together rather than leaving a caller to fetch the snapshot
    separately and hope it had not moved in between.

    ``mount_*`` is ``None`` when the mount has not reported that member yet;
    that is "unknown", never zero (CLAUDE.md invariant 7).
    """

    ra_j2000_hours: float
    dec_j2000_deg: float
    #: The frame's own ``DATE-OBS`` — when the scope was *there*, not when
    #: the solver finished.
    date_obs: datetime
    pixel_scale_arcsec: float
    rotation_deg: float
    solver: str
    solver_version: str | None = None

    mount_ra_j2000_hours: float | None = None
    mount_dec_j2000_deg: float | None = None
    mount_altitude_deg: float | None = None
    mount_azimuth_deg: float | None = None


class PolarAlignmentStartRequest(Request):
    """``POST /api/mount/polar-alignment/start``'s body.

    ``point_count`` defaults to 3 over a 60-degree arc (30-degree steps) for a
    fast initial measurement. Selecting five or more points adds residual
    degrees of freedom for a stronger quality check.

    ``start_point`` is DESIGN.md §4.5's "Start point" setting: ``best`` (the
    default, since 2026-09-11 — ``ssc.astro.polar.best_conditioned_field``),
    ``current`` (the mount's own current J2000 position — the old default,
    and still the right answer for anyone whose sky is not clear),
    ``seestar`` (the official-app field: local hour angle +15 degrees and
    apparent declination equal to site latitude), or
    ``explicit`` (``start_ra_hours``/``start_dec_deg``, both required
    together). Declination is held fixed across every point either way
    (``ssc.astro.polar``: rotate in RA only). Giving a coordinate while
    ``start_point`` is not ``explicit`` is refused rather than silently
    picking one meaning (CLAUDE.md invariant 7) — likewise ``explicit``
    with either coordinate missing. ``step_deg``'s **sign** is DESIGN.md
    §4.5's "direction" setting (east/west) — there is no separate field for
    it, since a negative step already rotates the other way
    (``ssc.api.polar_alignment_service.PolarAlignmentService.start``'s own
    docstring). ``gain`` is DESIGN.md §4.5's own setting too: applied once,
    before the first exposure — unbounded here since the camera's own
    ``GainMin``/``GainMax`` are per-device and read live by the driver,
    never guessed at in this schema (CLAUDE.md invariant 11's sibling rule).
    """

    point_count: int = Field(default=3, ge=3, le=12)
    step_deg: float = Field(default=30.0, ge=-180.0, le=180.0)
    exposure_s: float = Field(default=2.0, gt=0.0, le=120.0)
    gain: int | None = Field(default=200, ge=0)
    apply_refraction: bool = True
    auto_refine: bool = False
    start_point: PolarAlignmentStartMode = PolarAlignmentStartMode.BEST
    start_ra_hours: float | None = Field(default=None, ge=0.0, lt=24.0)
    start_dec_deg: float | None = Field(default=None, ge=-90.0, le=90.0)
    unpark_if_needed: bool = False

    @model_validator(mode="after")
    def _start_coords_match_start_point(self) -> PolarAlignmentStartRequest:
        given = self.start_ra_hours is not None or self.start_dec_deg is not None
        if self.start_point is PolarAlignmentStartMode.EXPLICIT:
            if self.start_ra_hours is None or self.start_dec_deg is None:
                raise ValueError(
                    "start_point 'explicit' requires both start_ra_hours and start_dec_deg"
                )
        elif given:
            raise ValueError(
                f"start_ra_hours/start_dec_deg were given but start_point is "
                f"{self.start_point.value!r}, not 'explicit': which one wins is ambiguous, "
                "so this refuses rather than guessing"
            )
        return self


class PolarAlignmentRecheckRequest(Request):
    """Start an independent same-direction sweep over the same sky arc."""

    point_count: int | None = Field(default=None, ge=3, le=12)
    exposure_s: float = Field(default=2.0, gt=0.0, le=120.0)
    gain: int | None = Field(default=200, ge=0)
    apply_refraction: bool = True
    unpark_if_needed: bool = False


class PolarSimulationRequest(Request):
    """A synthetic wedge position; no device command is issued."""

    altitude_adjustment_deg: float = Field(ge=-15.0, le=15.0)
    azimuth_adjustment_deg: float = Field(ge=-15.0, le=15.0)
    point_count: int = Field(default=3, ge=3, le=12)


class PolarSimulationPoint(Response):
    index: int
    when: datetime
    ra_j2000_hours: float
    dec_j2000_deg: float


class PolarSimulationResponse(Response):
    """Artificial plate solves and the production three-point PA result."""

    simulated: bool = True
    site_latitude_deg: float
    site_longitude_deg: float
    points: list[PolarSimulationPoint]
    result: PolarAlignmentResult


class PolarRefinementStartRequest(Request):
    """``POST /api/mount/polar-alignment/refine/start``'s body.

    Exposure and gain only — the refinement loop never slews and targets
    nothing new: every other input comes from the sweep it refines
    (``ssc.api.polar_alignment_service.PolarAlignmentService._sweep_model``)
    and from a reference solve the loop takes for itself at the mount's
    current, tracking-stopped pointing.
    """

    exposure_s: float = Field(gt=0.0, le=120.0)
    gain: int | None = Field(default=200, ge=0)


class CaptureFrameModel(Response):
    """One catalogued frame (``ssc.store.capture.CaptureFrameRecord``)."""

    id: int
    path: str
    object_name: str | None = None
    filter_name: str | None = None
    frame_type: FrameType
    date_obs: datetime
    exposure_s: float | None = None
    gain: int | None = None
    ccd_temp_c: float | None = None
    width: int
    height: int
    status: str
    created_at: datetime


class CaptureFramesResponse(Response):
    frames: list[CaptureFrameModel]


class CaptureFrameQualityModel(Response):
    """A catalogued frame's numpy-only HFR/FWHM star metric
    (``ssc.capture.quality.FrameQuality``, ``ssc-bkv.8``). ``star_count=0``
    means every size field is ``None`` — an empty or starless frame, never a
    fabricated size (CLAUDE.md invariant 7). ``*_arcsec`` fields are
    ``None`` whenever the frame's FITS header carries no usable pixel
    scale, even when the ``*_px`` fields are known."""

    star_count: int
    hfr_px: float | None = None
    fwhm_px: float | None = None
    hfr_arcsec: float | None = None
    fwhm_arcsec: float | None = None
    background: float
    noise: float
    detected_source_count: int = 0
    contrast_snr: float | None = None


class CaptureFramePlateSolveModel(Response):
    """Astrometry measured from one already-catalogued capture frame."""

    ra_j2000_hours: float
    dec_j2000_deg: float
    date_obs: datetime
    pixel_scale_arcsec: float
    rotation_deg: float
    solver: str
    solver_version: str | None = None


class CaptureSessionModel(Response):
    """One contiguous run of frames on a target
    (``ssc.capture.catalogue.Session``, DESIGN.md §9.5.4)."""

    object_name: str | None = None
    started_at_utc: datetime
    ended_at_utc: datetime
    frame_count: int
    filters: list[str]


class CaptureSessionsResponse(Response):
    sessions: list[CaptureSessionModel]


class ArchiveEntryModel(Response):
    """One read-only image discovered beneath the configured capture root."""

    key: str
    name: str
    directory: str
    kind: Literal["fits", "image"]
    size_bytes: int = Field(ge=0)
    modified_at: datetime
    date_obs: datetime
    date_source: Literal["capture_catalogue", "fits_header", "filesystem_mtime"]
    catalogue_id: int | None = None
    object_name: str | None = None
    filter_name: str | None = None
    frame_type: str | None = None
    exposure_s: float | None = None
    gain: int | None = None
    ccd_temp_c: float | None = None
    width: int | None = None
    height: int | None = None
    bayer_pattern: str | None = None
    preview_available: bool


class ArchiveEntriesResponse(Response):
    root_name: str
    storage_total_bytes: int = Field(ge=0)
    storage_free_bytes: int = Field(ge=0)
    entries: list[ArchiveEntryModel]
    directories: list[str]
    truncated: bool


class ArchiveDeleteFilesRequest(Request):
    keys: list[str] = Field(min_length=1, max_length=5000)

    @model_validator(mode="after")
    def unique_keys(self) -> ArchiveDeleteFilesRequest:
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("archive file keys must be unique")
        return self


class ArchiveDeleteFolderRequest(Request):
    directory: str = Field(min_length=1, max_length=2_048)


class ArchiveDeleteResponse(Response):
    files_deleted: int = Field(ge=0)
    catalogue_rows_deleted: int = Field(ge=0)
    bytes_deleted: int = Field(ge=0)
    folder_deleted: str | None = None


class ArchiveTransferRequest(Request):
    keys: list[str] = Field(default_factory=list, max_length=5000)
    directories: list[str] = Field(default_factory=list, max_length=5000)
    destination: str = Field(min_length=1, max_length=2_048)
    mode: Literal["move", "copy"]

    @model_validator(mode="after")
    def unique_keys(self) -> ArchiveTransferRequest:
        if not 1 <= len(self.keys) + len(self.directories) <= 5000:
            raise ValueError("between one and 5000 archive items must be selected")
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("archive file keys must be unique")
        if len(set(self.directories)) != len(self.directories):
            raise ValueError("archive folder keys must be unique")
        return self


class ArchiveTransferResponse(Response):
    files_transferred: int = Field(ge=0)
    directories_transferred: int = Field(ge=0)
    bytes_transferred: int = Field(ge=0)
    catalogue_rows_updated: int = Field(ge=0)
    destination: str
    mode: Literal["move", "copy"]


class ArchiveZipRequest(Request):
    """Selected archive files and recursive folder trees to download together."""

    keys: list[str] = Field(default_factory=list, max_length=5000)
    directories: list[str] = Field(default_factory=list, max_length=5000)

    @model_validator(mode="after")
    def unique_keys(self) -> ArchiveZipRequest:
        if not 1 <= len(self.keys) + len(self.directories) <= 5000:
            raise ValueError("between one and 5000 archive items must be selected")
        if len(set(self.keys)) != len(self.keys):
            raise ValueError("archive file keys must be unique")
        if len(set(self.directories)) != len(self.directories):
            raise ValueError("archive folder keys must be unique")
        return self


# -- sky-survey tiles (ssc/skymap) ------------------------------------------- #


class SurveyCreditModel(Response):
    """The credit line the UI must show for a survey — data, never a literal
    in the frame (``THIRD_PARTY_NOTICES.md`` requirement 4)."""

    label: str
    url: str


class SurveyModel(Response):
    """One registry entry plus its live availability and cache coverage."""

    id: str
    label: str
    kind: str
    #: ``"hips"`` or ``"toast"`` — decides which placeholders ``tile_url_template`` has.
    scheme: str
    frame: str
    tile_format: str
    max_order: int
    credit: SurveyCreditModel
    upstream_host: str
    bundle_allowed: bool
    redistributable_pack_url: str | None
    blend_only: bool
    bundled: bool
    enabled: bool
    #: Why ``enabled`` is ``False``; ``None`` when it is not.
    reason: str | None
    production_cleared: bool
    rights_notice: str | None
    #: Same-origin template with ``{order}``/``{dir}``/``{npix}`` (HiPS, the
    #: standard ``Norder/Dir/Npix`` layout) or ``{level}``/``{x}``/``{y}``
    #: (TOAST) left for the map engine to fill.
    tile_url_template: str
    #: Same-origin URL of the HiPS ``properties`` file; ``None`` for TOAST.
    properties_url: str | None
    cached_tiles: int
    cached_bytes: int
    missing_tiles: int
    pack_complete_order: int | None


class CutoutSourceModel(Response):
    """One archive `POST /api/skymap/cutouts` may fetch a field from — listed
    up front, credit included, so the panel can offer a "Fetch for this
    field" row before any job exists (never a hardcoded label or credit
    line in the frontend: ``THIRD_PARTY_NOTICES.md`` requirement 4)."""

    id: str
    label: str
    credit: SurveyCreditModel
    bands: tuple[str, ...]
    default_bands: tuple[str, ...]
    #: An optional source: on only when the field is confirmed in its
    #: footprint, never an error when it is not (Legacy Surveys).
    optional: bool
    max_pixels: int


class SurveysResponse(Response):
    surveys: list[SurveyModel]
    cache_bytes: int
    cache_max_bytes: int
    #: The target-field cutout registry (``ssc/skymap/cutouts.py``) —
    #: distinct from ``surveys`` because a cutout source is not a tile
    #: pyramid (no ``scheme``, nothing to prefetch as a pack).
    cutout_sources: list[CutoutSourceModel]


class PrefetchRequest(Request):
    """Start (or estimate) an offline-pack download. ``sky`` fetches every
    tile of orders ``0..max_order``; ``cone`` the tiles covering a cone at
    each of those orders (HiPS surveys only)."""

    survey: str
    mode: Literal["sky", "cone"]
    max_order: int = Field(ge=0, le=29)
    ra_deg: float | None = Field(default=None, ge=0.0, lt=360.0)
    dec_deg: float | None = Field(default=None, ge=-90.0, le=90.0)
    radius_deg: float | None = Field(default=None, gt=0.0, le=180.0)

    @model_validator(mode="after")
    def _cone_needs_a_centre(self) -> PrefetchRequest:
        if self.mode == "cone" and (
            self.ra_deg is None or self.dec_deg is None or self.radius_deg is None
        ):
            raise ValueError("mode 'cone' requires ra_deg, dec_deg and radius_deg")
        return self


class PrefetchEstimateResponse(Response):
    survey: str
    tile_count: int
    already_cached: int
    #: Measured — from cached tiles, or one freshly sampled tile when
    #: nothing was cached yet; ``None`` only when neither is possible (the
    #: archive is unreachable right now) — never a guess.
    mean_tile_bytes: int | None
    #: The *remaining* download's size (``tile_count - already_cached``
    #: tiles), not the whole pack's.
    estimated_bytes: int | None
    #: Wall-clock estimate for the remaining download, from this survey's
    #: own measured round-trip time divided across the server's prefetch
    #: concurrency. ``None`` under the same condition as ``estimated_bytes``.
    estimated_seconds: float | None


class PrefetchJobResponse(Response):
    job: PrefetchJobState


class CacheClearResponse(Response):
    survey: str
    freed_bytes: int


# -- target-field cutouts (ssc/skymap/cutouts.py) ---------------------------- #


class CutoutRequest(Request):
    """Fetch a target-field cutout from one archive. ``size_deg`` is capped
    at 6 — a field, not a survey (M11 spec). Every source but unWISE still
    self-limits to 3 via its own request logic (``web/src/skymap/SkyMap
    .tsx``'s ``MAX_CUTOUT_SIZE_DEG``); unWISE alone always requests exactly
    this ceiling, deliberately more than the lens-fitted frame, so the map
    can show framing context beyond the exact frame (``UNWISE_MAX_CUTOUT_SIZE
    _DEG``, reported live 2026-09-07). Server-side, ``_fetch_unwise`` tiles
    that one 6 deg request into a 2x2 grid of 3 deg sub-requests for sharper
    imagery (reported live 2026-09-08) — an internal implementation detail,
    still one job/one response from this endpoint's point of view. This
    field is a shared upper bound, not a per-source guarantee — nothing here
    enforces which source may ask for how much of it."""

    survey: str
    ra_deg: float = Field(ge=0.0, lt=360.0)
    dec_deg: float = Field(ge=-90.0, le=90.0)
    size_deg: float = Field(gt=0.0, le=6.0)
    #: The bands to fetch; ``None`` uses the source's own default.
    bands: tuple[str, ...] | None = None


class CutoutWcsModel(Response):
    """A linear (TAN/SIN-tangent-plane) WCS as the plain numbers the client
    draws with — never an opaque object."""

    crpix1: float
    crpix2: float
    crval1: float
    crval2: float
    cd1_1: float
    cd1_2: float
    cd2_1: float
    cd2_2: float
    ctype1: str
    ctype2: str


class CutoutPieceModel(Response):
    idx: int
    band: str | None
    #: The raw format this piece was fetched in — ``"fits"`` or ``"jpg"``.
    format: str
    bytes: int
    width: int | None
    height: int | None
    #: ``None`` only for a piece with no header WCS (never fabricated —
    #: architecture policy invariant 7).
    wcs: CutoutWcsModel | None
    #: The stretch that produced ``display_png_url``; ``None`` when the raw
    #: piece is already a display image itself (SDSS's getjpeg quick-look).
    stretch: str | None
    fits_url: str
    display_png_url: str | None


class CutoutManifestResponse(Response):
    id: str
    source: str
    ra_deg: float
    dec_deg: float
    size_deg: float
    status: str
    bytes: int
    error: str | None
    error_detail: str | None
    credit_label: str
    credit_url: str
    created_at: datetime
    updated_at: datetime
    pieces: list[CutoutPieceModel]


class CutoutJobResponse(Response):
    cutout: CutoutState


class CutoutListResponse(Response):
    cutouts: list[CutoutState]


# -- bulk target-field cutout prefetch (ssc/skymap/bulk_prefetch.py) --------- #


class BulkCutoutMagnitudeScope(Request):
    """The ``magnitude`` scope's one parameter: NGC/IC/Sharpless objects at
    or brighter than this cutoff. A cutoff producing more than
    ``ssc.skymap.bulk_prefetch.MAX_MAGNITUDE_SCOPE_OBJECTS`` objects is
    refused with the count in the message — never silently truncated."""

    magnitude_lte: float


class BulkCutoutRequest(Request):
    """Estimate (the default) or start (``confirm: true``) a bulk cutout
    prefetch over a scoped catalogue subset. Never "the whole catalogue" —
    ``scope`` is either the literal ``"messier"`` (the 110-object public-domain
    core) or a magnitude cutoff over the rest of the bundled catalogue
    (``ssc.skymap.bulk_prefetch`` module docstring says why)."""

    scope: Literal["messier"] | BulkCutoutMagnitudeScope
    sources: tuple[str, ...] = Field(min_length=1)
    size_deg: float = Field(gt=0.0, le=3.0)
    #: Without this, the endpoint only estimates — nothing is queued. A
    #: multi-hundred-request job is never started on one call.
    confirm: bool = False


class BulkCutoutEstimateResponse(Response):
    object_count: int
    #: ``object_count * len(sources)``, before removing already-cached pairs.
    pair_count: int
    already_cached: int
    #: ``pair_count - already_cached`` pairs still to fetch, per source id.
    per_source_request_counts: dict[str, int]
    #: Measured mean bytes of a complete cutout, per source id, from what is
    #: already cached; ``None`` for a source with nothing cached yet.
    mean_bytes_by_source: dict[str, int | None]
    #: ``None`` whenever any source with pairs left to fetch has an unknown
    #: mean — never a guess, and never a partial sum presented as the total.
    estimated_bytes: int | None


class BulkCutoutJobResponse(Response):
    job: BulkCutoutJobState


class BulkCutoutResponse(Response):
    """Exactly one of the two is set: the estimate when ``confirm`` was not
    sent, the started job when it was."""

    estimate: BulkCutoutEstimateResponse | None = None
    job: BulkCutoutJobState | None = None


# -- offline cache export/import (ssc/skymap/export_import.py) -------------- #


class SkymapImportResponse(Response):
    """Counts from one ``POST /api/skymap/import`` — never a per-row list,
    since an archive can carry thousands of rows; ``note`` is the one place
    that says, in a sentence, why a skipped or rejected count is what it is.
    The server log carries the per-entry detail."""

    tiles_imported: int
    tiles_skipped: int
    tiles_rejected: int
    cutouts_imported: int
    cutouts_skipped: int
    cutouts_rejected: int
    note: str


__all__ = [
    "MOUNT_AXIS_RATE_HARD_CAP_DEG_S",
    "AddressBookResponse",
    "AltitudeSampleModel",
    "AppearanceRequest",
    "ApplicationRestartRequest",
    "ApplicationRestartResponse",
    "ArchiveDeleteFilesRequest",
    "ArchiveDeleteFolderRequest",
    "ArchiveDeleteResponse",
    "ArchiveEntriesResponse",
    "ArchiveEntryModel",
    "AutoConnectRequest",
    "AutoLpFilterRequest",
    "AutoLpFilterResponse",
    "AwareDatetime",
    "BestTargetModel",
    "BestTargetsRequest",
    "BestTargetsResponse",
    "BulkCutoutEstimateResponse",
    "BulkCutoutJobResponse",
    "BulkCutoutMagnitudeScope",
    "BulkCutoutRequest",
    "BulkCutoutResponse",
    "CacheClearResponse",
    "CalibrationCaptureStartRequest",
    "CalibrationSummary",
    "CaptureCenterAfterDriftRequest",
    "CaptureCenterAfterDriftResponse",
    "CaptureConditionFactorModel",
    "CaptureConditionsModel",
    "CaptureDitherRequest",
    "CaptureDitherResponse",
    "CaptureExposureRequest",
    "CaptureFilenameTemplateRequest",
    "CaptureFilenameTemplateResponse",
    "CaptureFolderRequest",
    "CaptureFolderResponse",
    "CaptureFolderTemplateRequest",
    "CaptureFolderTemplateResponse",
    "CaptureFrameModel",
    "CaptureFramesResponse",
    "CapturePathTagModel",
    "CapturePathTagsResponse",
    "CaptureSessionModel",
    "CaptureSessionsResponse",
    "ConnectRequest",
    "ControlPinRequest",
    "CutoutJobResponse",
    "CutoutListResponse",
    "CutoutManifestResponse",
    "CutoutPieceModel",
    "CutoutRequest",
    "CutoutSourceModel",
    "CutoutWcsModel",
    "DeviceMountModeCheck",
    "DiscoverRequest",
    "DiscoverResponse",
    "EphemerisSetupResponse",
    "EthernetAddressRequest",
    "EventTimeModel",
    "ExternalToolModel",
    "ExternalToolsResponse",
    "FieldGeometryModel",
    "FocuserMoveRequest",
    "FocuserPositionResponse",
    "ForecastPointModel",
    "FramingRequest",
    "FramingResponse",
    "FramingVisibilityModel",
    "FromIntentRequest",
    "FromIntentResponse",
    "HealthResponse",
    "HeaterAutomationRequest",
    "HeaterAutomationResponse",
    "HeaterStateRequest",
    "HeaterStateResponse",
    "HorizonIntervalModel",
    "HorizonPointModel",
    "HorizonPointRequest",
    "HorizonRequest",
    "HorizonResponse",
    "LensSummary",
    "LiveStackMasterModel",
    "LiveStackMastersResponse",
    "LiveStackResetRequest",
    "LiveStackResponse",
    "LiveStackRtspResponse",
    "LiveStackRtspStartRequest",
    "LocationFixRequest",
    "LocationResponse",
    "MetNoContactRequest",
    "MoonAltitudeSampleModel",
    "MoonAltitudeSeriesResponse",
    "MoonInfoModel",
    "MoonTonightModel",
    "MosaicPanelModel",
    "MosaicRequest",
    "MosaicResponse",
    "MosaicSpecRequest",
    "MountCoordinatesRequest",
    "MountMoveAxisRequest",
    "MountTrackingRequest",
    "NightRequest",
    "NightResponse",
    "OffsetModel",
    "PairRequest",
    "PairingResponse",
    "PanelCenterModel",
    "PinRequest",
    "PlanCompileRequest",
    "PlanCompileResponse",
    "PlanCreateRequest",
    "PlanListResponse",
    "PlanRecordResponse",
    "PlanUpdateRequest",
    "PlanValidateRequest",
    "PlanValidateResponse",
    "PolarAlignmentRecheckRequest",
    "PolarAlignmentStartRequest",
    "PolarRefinementStartRequest",
    "PolarSimulationPoint",
    "PolarSimulationRequest",
    "PolarSimulationResponse",
    "PreferredSourceRequest",
    "PrefetchEstimateResponse",
    "PrefetchJobResponse",
    "PrefetchRequest",
    "ProbeRequest",
    "ProbeResponse",
    "ProfileSummary",
    "ProfilesResponse",
    "RemoteAccessResponse",
    "Request",
    "Response",
    "RotationSampleModel",
    "RunControlRequest",
    "RunDetailResponse",
    "RunEffectModel",
    "RunItemControlRequest",
    "RunJournalRecordModel",
    "RunResponse",
    "RunStartRequest",
    "SecretNamesResponse",
    "SecretUpsert",
    "SensorSummary",
    "SessionEnvelopeModel",
    "SessionResponse",
    "SettingsResponse",
    "ShapeModel",
    "SkymapAutofetchEnabledRequest",
    "SkymapAutofetchSettingsResponse",
    "SkymapImportResponse",
    "StateResponse",
    "StreamingSettingsRequest",
    "StreamingSettingsResponse",
    "SurveyCreditModel",
    "SurveyModel",
    "SurveysResponse",
    "SystemMetricsResponse",
    "TargetCenteringAttemptModel",
    "TargetCenteringRequest",
    "TargetCenteringResponse",
    "TargetPositionModel",
    "TargetResolutionResponse",
    "TonightCloudModel",
    "TonightResponse",
    "TonightWeatherModel",
    "TonightWeatherRowModel",
    "WeatherForecastResponse",
    "WeatherProviderRequest",
    "WeatherProviderStatus",
    "WeatherSettingsResponse",
    "WifiAddressRequest",
    "YoutubeStreamResponse",
]
