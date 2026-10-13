# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The authoritative snapshot and the pieces it is made of.

These models are deliberately *not* the device's models. Nothing under
``ssc/state`` may import ``ssc.seestar`` (CLAUDE.md invariant 1), so the link
layer converts into these on the way in. The vocabulary happens to line up with
``SeestarSession.SessionStatus`` because both describe the same connection, not
because one imports the other.

``None`` means unknown, everywhere in this file. It never means zero and it
never means false (CLAUDE.md invariant 7).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from ssc.astro.frames import MountFrame
from ssc.capture import FrameType

#: Bumped whenever a client would misread an older snapshot. The client checks it.
SNAPSHOT_SCHEMA_VERSION: Final = 1

#: Notices are a bounded list so a long night cannot grow the snapshot without
#: limit. The reducer trims deterministically, so a replaying client trims the
#: same way and lands on the same snapshot.
MAX_NOTICES: Final = 50

#: How long a mount reading is trusted as current. Five times the poller's own
#: cadence (``ssc.main.AlpacaDeviceGateway._POLL_INTERVAL_S``, 1s) — headroom
#: for a slow tick or two before a client is told to stop trusting the
#: position it is showing (module docstring: ``None``/stale, never a silent
#: last-known-value). Not imported from ``ssc.main`` — that would invert the
#: dependency direction (``ssc.main`` builds on ``ssc.state``, not the other
#: way around); restated here the same way ``ssc.client.errors`` restates the
#: ASCOM error numbers rather than importing them from the driver.
MOUNT_STALE_AFTER_S: Final = 5.0

#: Exactly the three the device advertises (DESIGN.md §4.4b) — no King rate.
MountTrackingRate = Literal["sidereal", "lunar", "solar"]

#: What ``Telescope.EquatorialSystem`` reports, carried as
#: :class:`~ssc.astro.frames.MountFrame` rather than the raw ASCOM enum so a
#: client never has to know the encoding — and reused rather than duplicated
#: here, so the frame a client is told about and the frame
#: ``ssc.astro.frames.to_j2000``/``to_mount_frame`` actually convert against
#: can never quietly drift apart (architecture policy invariant 7). The mount reads
#: *topocentric* (DESIGN.md §4.4b) — this is not assumed, only reported as
#: measured.

#: ``MoveAxis``'s two moving axes (DESIGN.md §4.4b: axis 2 is unsupported and
#: not offered). Named rather than numbered so a client never has to know
#: ASCOM's own axis-number convention.
MountAxis = Literal["primary", "secondary"]


class StateModel(BaseModel):
    """Base for everything in the snapshot: frozen, and strict on the way in."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class LinkStatus(StrEnum):
    """The device connection, mirroring the session state machine."""

    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    HANDSHAKING = "handshaking"
    READY = "ready"
    DEGRADED = "degraded"


class LocationSource(StrEnum):
    """Where the active location came from — always shown (DESIGN.md §5)."""

    MANUAL = "manual"
    BROWSER = "browser"
    DEVICE = "device"
    IP = "ip"


class NoticeLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class BatteryTelemetry(StateModel):
    percent: float | None = Field(default=None, ge=0, le=100)
    charging_status: str | None = None
    external_power: bool | None = None
    measured_at: datetime | None = None
    note: str | None = None


class DeviceDescriptor(StateModel):
    """What SSC knows about one device. Carries no credential material."""

    host: str
    control_port: int
    reachable: bool
    firmware: str | None = None
    firmware_verified: bool = False
    model: str | None = None
    serial: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)
    #: Why a probe failed, or what is unverified about this device.
    note: str | None = None
    battery: BatteryTelemetry | None = None


class LinkState(StateModel):
    """The state of the connection to the scope."""

    status: LinkStatus = LinkStatus.DISCONNECTED
    endpoint: str | None = None
    reason: str | None = None
    control_connected: bool = False
    events_connected: bool = False
    #: Events dropped because a subscriber could not keep up. Surfaced, not hidden.
    events_dropped: int = 0
    connect_attempts: int = 0
    changed_at: datetime | None = None


class AutoConnectState(StateModel):
    """Whether the connect supervisor will (re)connect on its own, and when.

    Lives above the wire-level reconnect ``ssc/seestar/session.py`` already
    does — this is the *policy* decision of whether to keep re-issuing
    ``connect`` at all, surfaced so the UI can show "retrying in 12s" rather
    than a link that is silently stuck disconnected (CLAUDE.md invariant 7).

    ``suspended`` is the modelled intent of an explicit user disconnect: it is
    never inferred from :class:`LinkState`, because a deliberate disconnect and
    an unexpected drop must stay distinguishable — only the former means "stay
    disconnected until asked otherwise".
    """

    #: The persisted master switch (``DeviceSettings.auto_connect``), mirrored
    #: here so a client reading only the snapshot/WS sees it without a second
    #: request.
    enabled: bool = True
    suspended: bool = False
    #: Consecutive failed reconnect attempts since the link was last ``READY``.
    attempt: int = 0
    next_attempt_at: datetime | None = None
    #: Why the supervisor is doing what it is doing right now — idle, waiting
    #: to retry, connecting — or ``None`` when there is nothing to say.
    reason: str | None = None


class DiscoveryState(StateModel):
    """A LAN scan, in progress or finished. Bounded and cancellable."""

    running: bool = False
    scan_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    completed: int = 0
    total: int = 0
    found: list[DeviceDescriptor] = Field(default_factory=list)
    cancelled: bool = False
    #: The reason a scan could not run or did not finish. ``None`` means no error.
    error: str | None = None


class CameraFieldOfView(StateModel):
    """The active camera's real field of view on sky, in degrees — read once
    per connection (``ssc.main.AlpacaDeviceGateway``), not on every poll
    tick, mirroring :class:`MountCapabilities`: a sensor's size and pixel
    pitch do not change while connected, so there is nothing to gain from
    re-reading them every second.

    Computed from ``Camera.CameraXSize``/``CameraYSize`` (pixels),
    ``Camera.PixelSizeX`` (microns) and ``Telescope.FocalLength`` (ASCOM:
    metres) via the standard small-angle field formula. Published only when
    *all four* inputs were actually read from the device — there is no
    half-known field (CLAUDE.md invariant 7): either ``width_deg``/
    ``height_deg`` are both known, or this whole section stays ``None`` on
    :class:`StateSnapshot` and the sky map keeps its honest marker rather
    than drawing a guess.

    The axes are authoritative: observed ``CameraXSize=2160`` and
    ``CameraYSize=3840`` match the Seestar app's own portrait FITS
    (``NAXIS1=2160``, ``NAXIS2=3840``). ``ssc.main`` therefore maps X to
    width and Y to height directly; sorting the dimensions would rotate the
    field overlay by 90 degrees.

    ``rotation_deg`` is the field's position angle on sky and is
    independently optional even when the size is known: it is a property of
    where the mount is pointing (it varied 87-105 degrees across the sky in
    one night's measurements), not of the camera, and only a *recent* plate
    solve can source it. ``None`` here means "no trustworthy rotation right
    now", never "zero" — a caller must not draw it as due-north.
    """

    width_deg: float
    height_deg: float
    rotation_deg: float | None = None
    center_ra_j2000_h: float | None = None
    center_dec_j2000_deg: float | None = None
    solved_at: datetime | None = None


class MountCapabilities(StateModel):
    """What this mount will let a client ask of it — read once per connection
    (``ssc.main.AlpacaDeviceGateway``), not on every poll tick, since these do
    not change while connected. ``None`` throughout until the first successful
    read; each field is independently ``None`` if its own member could not be
    read, never a guessed or fabricated default (CLAUDE.md invariant 7)."""

    can_slew_async: bool | None = None
    can_sync: bool | None = None
    can_park: bool | None = None
    can_unpark: bool | None = None
    can_find_home: bool | None = None
    can_set_tracking: bool | None = None
    can_move_primary_axis: bool | None = None
    can_move_secondary_axis: bool | None = None
    #: The device's own advertised ``AxisRates`` ceiling — genuinely enforced
    #: (DESIGN.md §4.4b), but **not** the rate actually delivered, which is
    #: unmeasured. Never used to compute or display a slew duration.
    axis_rate_max_deg_s: float | None = None
    tracking_rates: list[MountTrackingRate] | None = None


class MountState(StateModel):
    """Where the mount is pointing, what it is doing, and what it can do.

    Polled, never pushed (Alpaca has no event channel) — ``ssc.main``'s
    ``AlpacaDeviceGateway`` is the sole writer, publishing a fresh reading
    every tick while connected. Every scalar is independently ``None`` when
    its member could not be read on that tick; a failed read is never papered
    over with the previous value (CLAUDE.md invariant 7).

    ``polled_at`` is the last tick that produced *this* reading — carried
    through unmodified while the link is down, rather than reset, so the
    fields keep their last known value and :attr:`stale` is what tells a
    client to stop trusting them, rather than the snapshot silently going
    blank.

    ``stale`` is a plain field, deliberately **not** a ``@computed_field``:
    ``MountState`` is event-sourced (carried by ``MountChanged`` and folded by
    the pure reducer) and must stay exactly round-trippable — the reconnect
    test in ``tests/api/test_ws_replay.py`` re-validates a replayed snapshot
    with this same class, and ``extra="forbid"`` refuses a computed field as
    input even though pydantic happily emits it as output. Whoever constructs
    a fresh reading sets ``stale=False``. The gateway validates its acquisition
    timestamp before publication and emits a stale transition when a retained
    reading expires, independently of blocked or failed polls. Staleness is
    also recomputed by :func:`refresh_mount_staleness` for ``GET /api/state``
    and every WS ``snapshot`` frame. Clients render that server decision;
    their display clocks cannot override it.
    """

    polled_at: datetime | None = None
    ra_hours: float | None = None
    dec_deg: float | None = None
    alt_deg: float | None = None
    az_deg: float | None = None
    sidereal_time_hours: float | None = None
    tracking: bool | None = None
    tracking_rate: MountTrackingRate | None = None
    at_park: bool | None = None
    at_home: bool | None = None
    slewing: bool | None = None
    #: Restated per reading, never assumed — ``ssc.main`` reads it from the
    #: device rather than hardcoding "topocentric" (DESIGN.md §4.4b: measured,
    #: not guaranteed by the standard). Names the frame ``ra_hours``/``dec_deg``
    #: are actually *in* — a client must never assume J2000 of these two.
    equatorial_system: MountFrame | None = None
    #: The mount's own ``SiteLatitude``/``SiteLongitude`` — read back from the
    #: device, not SSC's configured observing location, because these are what
    #: define the frame ``equatorial_system`` is referred to
    #: (``ssc.astro.frames`` module docstring). ``None`` until something has
    #: set them (this device has no independent location source, so that is
    #: ``ssc.main.AlpacaDeviceGateway.set_site`` on connect — see its
    #: docstring) or until the first successful read after that.
    site_latitude_deg: float | None = None
    site_longitude_deg: float | None = None
    #: ``ra_hours``/``dec_deg`` converted to **astrometric J2000**
    #: (``ssc.astro.frames.to_j2000``) — what a client draws on the sky map
    #: without knowing anything about frames. ``None`` whenever the
    #: conversion could not be done yet (frame or site not yet known) or the
    #: device declared a frame SSC does not convert; the raw fields above and
    #: ``equatorial_system`` are still reported either way; see
    #: ``ssc.api.services.DeviceService`` for how this is filled in.
    ra_j2000_h: float | None = None
    dec_j2000_deg: float | None = None
    capabilities: MountCapabilities | None = None
    #: Defaults ``True``: a shell object nobody has actually read yet (e.g. a
    #: test fixture) is stale by construction. A real reading always sets this
    #: explicitly rather than relying on the default.
    stale: bool = True
    #: Server-enforced dead-man state for manual MoveAxis jogging.
    jog_primary_active: bool = False
    jog_secondary_active: bool = False
    jog_lease_expires_at: datetime | None = None


def mount_stale(polled_at: datetime | None, *, now: datetime) -> bool:
    """Whether a mount reading taken at ``polled_at`` is still trusted as of
    ``now``. ``None`` (never polled) is always stale."""
    if polled_at is None:
        return True
    return (now - polled_at) > timedelta(seconds=MOUNT_STALE_AFTER_S)


def refresh_mount_staleness(snapshot: StateSnapshot, *, now: datetime) -> StateSnapshot:
    """``snapshot``, with ``mount.stale`` recomputed against ``now``.

    See :class:`MountState`'s docstring for why this exists instead of a
    computed field. Called at the two moments a client is handed a full
    snapshot as "the truth" (``GET /api/state``, every WS ``snapshot`` frame)
    — a live WS ``mount.changed`` push in between is, by construction, always
    fresh, so it needs no equivalent call.
    """
    if snapshot.mount is None:
        return snapshot
    stale = mount_stale(snapshot.mount.polled_at, now=now)
    if stale == snapshot.mount.stale:
        return snapshot
    return snapshot.model_copy(update={"mount": snapshot.mount.model_copy(update={"stale": stale})})


#: The ASCOM device interfaces ``ssc-alpaca`` may serve — read from its own
#: ``/management/v1/configureddevices`` (``ssc.main.AlpacaDeviceGateway``),
#: never a hardcoded list (architecture policy invariant 11): a different scope or
#: firmware serves a different set, and this is the vocabulary that has been
#: observed, not an assumption of what must exist.
AlpacaDeviceKind = Literal["telescope", "camera", "focuser", "filterwheel", "switch"]


class AlpacaDeviceItem(StateModel):
    """One device ``ssc-alpaca`` currently serves, for the connections footer.

    ``connected`` is genuinely tri-state (CLAUDE.md invariant 7): ``True`` or
    ``False`` is what the device's own ``Connected`` member answered:
    ``None`` means SSC could not ask it at all — a bounded read timed out, or
    the request failed in transport — never a guess standing in for either
    real answer.

    Measured live, 2026-09-10: ``Connected`` is **not a health signal** on
    this device. While the mount was wedged, ``telescope/0/connected`` read
    ``true`` while every other member on it refused with ``NOT_CONNECTED``;
    later it read ``false`` while those same members worked. So this field
    means exactly "the device reports ``Connected``", nothing stronger — it
    must never be read as "this device is working".
    """

    id: str
    kind: AlpacaDeviceKind
    number: int
    label: str
    connected: bool | None = None
    #: The device's own words for why ``connected`` could not be determined,
    #: or is what it is — never SSC's generic phrasing when the device (or
    #: the read attempt) already said something more specific.
    note: str | None = None
    #: Live camera-sensor telemetry, populated only for the primary imaging
    #: camera when its own Alpaca ``CCDTemperature`` member answers. ``None``
    #: is unavailable, never a fabricated zero or a host CPU temperature.
    temperature_c: float | None = None
    temperature_note: str | None = None


class AlpacaDevicesState(StateModel):
    """Every device ``ssc-alpaca`` serves, polled on the same tick as mount
    telemetry (``ssc.main.AlpacaDeviceGateway`` — not a second loop) so a
    desktop and a phone never disagree about which devices are connected,
    the same requirement DESIGN.md's state design exists to satisfy for the
    mount itself.

    ``stale`` mirrors :attr:`MountState.stale` exactly, including why it is a
    plain field rather than a ``@computed_field`` — see that docstring.
    The gateway publishes expiry transitions even when a poll is blocked.
    :func:`refresh_devices_staleness` also refreshes the flag for REST and
    WebSocket snapshots, as for mount telemetry.
    """

    polled_at: datetime | None = None
    items: list[AlpacaDeviceItem] = Field(default_factory=list)
    #: Defaults ``True`` for the same reason as :attr:`MountState.stale`: a
    #: shell object nobody has actually polled yet is stale by construction.
    stale: bool = True


def devices_stale(polled_at: datetime | None, *, now: datetime) -> bool:
    """Whether a device poll taken at ``polled_at`` is still trusted as of
    ``now``. Mirrors :func:`mount_stale` — both are read on the same poll
    tick, so the same threshold applies."""
    if polled_at is None:
        return True
    return (now - polled_at) > timedelta(seconds=MOUNT_STALE_AFTER_S)


def refresh_devices_staleness(snapshot: StateSnapshot, *, now: datetime) -> StateSnapshot:
    """``snapshot``, with ``devices.stale`` recomputed against ``now``. See
    :func:`refresh_mount_staleness`, which this mirrors exactly."""
    if snapshot.devices is None:
        return snapshot
    stale = devices_stale(snapshot.devices.polled_at, now=now)
    if stale == snapshot.devices.stale:
        return snapshot
    return snapshot.model_copy(
        update={"devices": snapshot.devices.model_copy(update={"stale": stale})}
    )


class LocationState(StateModel):
    """The active observing location, with its provenance attached."""

    latitude_deg: float = Field(ge=-90.0, le=90.0)
    longitude_deg: float = Field(ge=-180.0, le=180.0)
    elevation_m: float | None = None
    timezone: str | None = None
    source: LocationSource
    accuracy_m: float | None = None
    acquired_at: datetime


class SettingsSummary(StateModel):
    """The non-secret settings a client renders. Never contains a key or a PIN."""

    location_source_preference: LocationSource | None = None
    remembered_host: str | None = None
    remembered_control_port: int | None = None
    ethernet_host: str | None = None
    ethernet_control_port: int | None = None
    hardware_profile_id: str | None = None
    hardware_profile_source: Literal["detected", "manual"] | None = None
    #: Names only. A secret's value never crosses this boundary (invariant 8).
    configured_secret_names: list[str] = Field(default_factory=list)
    control_pin_set: bool = False
    #: Whether a paired session is currently required to reach a mutating
    #: endpoint (architecture policy invariant 7 — an open, unauthenticated instance is
    #: a real security posture, not a detail, so it is a plain boolean here
    #: rather than left implicit). This is the *effective* value: the
    #: environment override, if any, already resolved in — see
    #: ``ssc.settings.store.SettingsStore.effective_auth_required``.
    auth_required: bool = False
    theme: str = "dark"
    font_scale: str = "normal"
    accent: str = "blue"
    background_enabled: bool = False
    background_opacity_percent: int = Field(default=15, ge=0, le=40)
    #: Whether the site has a recorded skyline mask (DESIGN.md 5). The points
    #: themselves are not carried here — fetched from ``GET
    #: /api/settings/horizon`` when actually needed — this is only the flag a
    #: client uses to know whether visibility answers are horizon-aware.
    horizon_profile_set: bool = False


class PlanSummary(StateModel):
    """One stored plan's identity, enough for a client's plan list without a
    fetch (DESIGN.md 4). The document itself — items, ``TimeSpec``s and all —
    is fetched from ``GET /api/plans/{id}`` when actually opened; carrying it
    here would make every connected client re-download every plan on every
    edit anyone makes to any of them."""

    id: str
    name: str
    revision: int
    updated_at: datetime
    #: The local evening date this plan is assigned to. ``None`` keeps a
    #: reusable or legacy plan visible as unscheduled rather than guessing.
    night_date: date | None = None


class Notice(StateModel):
    """A surfaced degradation. Invariant 7 in model form."""

    id: str
    level: NoticeLevel
    code: str
    message: str
    raised_at: datetime


class SecretsCapability(StateModel):
    """Whether the OS keyring on this host can actually hold a secret.

    Detected once at startup (``ssc.settings.secrets.probe_backend``), not
    per-request. Only weather providers that need an API key are gated by
    this — the device link, astro, scheduler and the rest of the UI are fully
    functional without a keyring (CLAUDE.md invariant 7: degraded and
    visible, never fatal, never silent).
    """

    available: bool
    backend: str | None = None
    reason: str | None = None


class DeviceEventRecord(StateModel):
    """One event pushed by the device, already redacted."""

    name: str
    received_at: datetime
    device_uptime_s: float | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class PrefetchJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: ssc-core restarted while the job was running. It is *not* resumed on
    #: its own; the user starts it again (a gigabyte download is never a
    #: silent background action).
    INTERRUPTED = "interrupted"


class PrefetchJobState(StateModel):
    """One offline-pack download, live or historical."""

    id: str
    survey: str
    #: ``"sky"`` = every tile of orders ``0..max_order``; ``"cone"`` = the
    #: tiles covering ``(ra, dec, radius)`` at each of those orders.
    mode: str
    max_order: int
    ra_deg: float | None = None
    dec_deg: float | None = None
    radius_deg: float | None = None
    status: PrefetchJobStatus
    done: int = 0
    total: int = 0
    #: Tiles the archive does not have (outside the survey's footprint).
    #: Counted separately so a pack with a hole is never reported as fetched.
    missing: int = 0
    bytes: int = 0
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class SurveyCacheState(StateModel):
    """What is on disk for one survey, and whether it may be drawn at all."""

    id: str
    enabled: bool
    #: Why ``enabled`` is ``False``; ``None`` when it is not.
    reason: str | None = None
    #: Ships with the product (its licence permits redistribution); read from
    #: disk, never fetched, never counted below.
    bundled: bool = False
    cached_tiles: int = 0
    cached_bytes: int = 0
    #: Tiles known to be absent upstream (a negative cache entry, no file).
    missing_tiles: int = 0
    #: The deepest ``k`` such that every tile of orders ``0..k`` is cached (or
    #: known missing). ``None`` when even order 0 is incomplete.
    pack_complete_order: int | None = None


class CutoutJobStatus(StrEnum):
    """Mirrors :class:`PrefetchJobStatus`, plus ``UNAVAILABLE`` — the archive
    itself answered and confirmed there is nothing here (off-footprint, or a
    lookup step unreachable with no fallback route), which is not the same
    as ``FAILED`` (an unexpected error worth a retry)."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class CutoutState(StateModel):
    """One target-field cutout job, live or historical — the snapshot's
    summary; the manifest (pieces, WCS) is fetched from ``GET
    /api/skymap/cutouts/{id}`` when actually drawn."""

    id: str
    source: str
    ra_deg: float
    dec_deg: float
    size_deg: float
    status: CutoutJobStatus
    bytes: int = 0
    #: A human-readable sentence when set (``ssc.skymap.cutouts.
    #: _humanize_upstream_error`` for an unexpected upstream failure; the
    #: `_OffFootprintError` message verbatim otherwise — already a
    #: sentence). Never the raw upstream detail; see ``error_detail``.
    error: str | None = None
    #: The raw upstream detail behind ``error`` (e.g. "host answered 500"),
    #: for a title/tooltip — never the primary line a viewer reads. ``None``
    #: whenever ``error`` is, and for an off-footprint result (its message
    #: has no separate raw detail worth keeping).
    error_detail: str | None = None
    created_at: datetime
    updated_at: datetime


class BulkCutoutScope(StrEnum):
    """What a bulk cutout prefetch covers. Deliberately not "everything" —
    the bundled catalogue is ~13,324 objects, far too many to fetch in bulk
    even respecting the one-request-per-host etiquette, and SDSS/Legacy
    Surveys have no documented bulk-use policy (THIRD_PARTY_NOTICES.md, "Endpoint
    gate"). Scoped to the object sets a user would plausibly image."""

    #: The 110 Messier objects — the public-domain core, always offered.
    MESSIER = "messier"
    #: NGC/IC/Sharpless objects at or brighter than a magnitude cutoff the
    #: caller picks; refused outright (never silently truncated) above
    #: ``ssc.skymap.bulk_prefetch.MAX_MAGNITUDE_SCOPE_OBJECTS``.
    MAGNITUDE = "magnitude"


class BulkCutoutJobStatus(StrEnum):
    """Mirrors :class:`PrefetchJobStatus` minus ``UNAVAILABLE`` — that is a
    per-pair outcome here (folded into ``BulkCutoutJobState.failed``), not a
    whole-job one."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class BulkCutoutJobState(StateModel):
    """One bulk target-field cutout prefetch, live or historical
    (``ssc/skymap/bulk_prefetch.py``) — a queue of individual
    :class:`CutoutState` jobs over a scoped catalogue subset. Per-pair status
    is never kept here (it would be hundreds of rows for one job); only the
    aggregate a progress bar needs. Why any one pair failed or was skipped is
    in the server log, not the snapshot."""

    id: str
    scope: BulkCutoutScope
    #: Set iff ``scope`` is ``MAGNITUDE``.
    magnitude_lte: float | None = None
    #: Cutout source ids (``ssc.skymap.surveys.CutoutSourceId``) this job fetches.
    sources: tuple[str, ...] = ()
    size_deg: float
    status: BulkCutoutJobStatus
    #: Object x source pairs in scope, fixed once the job starts.
    total: int = 0
    #: Pairs freshly fetched this run.
    done: int = 0
    #: Pairs that already had a complete cutout — no fetch needed.
    skipped: int = 0
    #: Pairs that ended without usable imagery (upstream failure or a
    #: confirmed off-footprint answer).
    failed: int = 0
    bytes: int = 0
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class SkymapState(StateModel):
    """The tile cache and its offline packs (``ssc/skymap``)."""

    surveys: list[SurveyCacheState] = Field(default_factory=list)
    jobs: list[PrefetchJobState] = Field(default_factory=list)
    cache_bytes: int = 0
    cache_max_bytes: int = 0
    #: Whether the last upstream contact succeeded. ``None`` before any attempt.
    upstream_reachable: bool | None = None
    #: Target-field cutouts (``ssc/skymap/cutouts.py``), newest first.
    cutouts: list[CutoutState] = Field(default_factory=list)
    cutouts_bytes: int = 0
    cutouts_max_bytes: int = 0
    #: Bulk cutout prefetch jobs (``ssc/skymap/bulk_prefetch.py``), newest first.
    bulk_cutout_jobs: list[BulkCutoutJobState] = Field(default_factory=list)


class RunLifecycle(StrEnum):
    """Where a run is in its life. Values other than ``INTERRUPTED`` mirror
    the executor's own ``RunStatus``. Interrupted is this process's addition:
    a run a previous process left unfinished (DESIGN.md 4.2 — surfaced with a
    ``resume`` action, never resumed behind the user's back). ``FAILED`` covers
    both a handled failed plan item and an unexpected executor exception."""

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    INTERRUPTED = "interrupted"
    FAILED = "failed"


class RunItemStatus(StrEnum):
    """DESIGN.md 4.2's item lifecycle. ``WAITING`` is derived (no journal record
    yet, and the item's start has not come), exactly as the M5 amendment says."""

    PENDING = "pending"
    WAITING = "waiting"
    RUNNING = "running"
    DONE = "done"
    SKIPPED = "skipped"
    FAILED = "failed"
    ABORTED = "aborted"


class RunAction(StrEnum):
    """What a client may ask of the run right now, derived from its lifecycle
    so two clients never disagree about which buttons are live."""

    PAUSE = "pause"
    RESUME = "resume"
    SKIP = "skip"
    STOP = "stop"
    RETRY = "retry"
    RESUME_INTERRUPTED = "resume_interrupted"


class RunItemState(StateModel):
    """One compiled item's execution status, with the reason for it."""

    item_id: str
    kind: str
    enabled: bool
    status: RunItemStatus
    attempt: int = 0
    panel_index: int | None = None
    #: True while a gate hold has parked the scope mid-item.
    held: bool = False
    #: The last journalled reason for this item — why it is where it is.
    reason: str = ""
    start_utc: datetime | None = None
    end_utc: datetime | None = None


class RunState(StateModel):
    """The executor's position, published after every journal append so every
    client renders the same run (CLAUDE.md invariant 4). A projection of the
    journal — the journal itself is ``GET /api/runs/{id}``."""

    run_id: str
    plan_id: str
    plan_name: str
    plan_revision: int
    night_date: date
    status: RunLifecycle
    started_at: datetime
    ended_at: datetime | None = None
    ended_reason: str | None = None
    current_item_id: str | None = None
    items: list[RunItemState] = Field(default_factory=list)
    #: A gate is holding the current item (scope parked, stacking stopped).
    gate_held: bool = False
    gate_reason: str | None = None
    #: The reason on the most recent journal record — the executor's last
    #: decision, in its own words (DESIGN.md 4.2: every decision explains itself).
    last_decision: str | None = None
    last_decision_at: datetime | None = None
    #: How many journal records exist. Monotonic within a run; a client can use
    #: it to tell "same section, republished" from "something happened".
    journal_length: int = 0
    available_actions: list[RunAction] = Field(default_factory=list)


class CaptureStage(StrEnum):
    """Where the in-flight capture operation actually is, each value backed
    by a real device action — never a step that isn't implemented yet
    (CLAUDE.md invariant 7). ``dithering`` includes the post-command mount
    settle gate; exposure does not start until that gate succeeds."""

    DITHERING = "dithering"
    EXPOSING = "exposing"
    DOWNLOADING = "downloading"
    TRANSFERRING = "transferring"
    SAVING = "saving"


CalibrationRunStatus = Literal["idle", "running", "cancelling", "completed", "cancelled", "failed"]
CalibrationRunKind = Literal["bias", "dark", "flat"]
CalibrationRunPhase = Literal["preparing", "probing", "capturing", "integrating"]


class FlatProbeState(StateModel):
    filter_name: str
    exposure_s: float
    median_fraction: float
    low_fraction: float
    high_fraction: float
    clipped_pixel_fraction: float
    banding_fraction: float
    banding_axis: Literal["rows", "columns"] | None = None
    decision: Literal["accept", "probe", "adjust_source", "fail"]
    reason: str


class CalibrationRunState(StateModel):
    """Server-owned progress for an automatic bias or per-filter flat set."""

    status: CalibrationRunStatus = "idle"
    kind: CalibrationRunKind | None = None
    phase: CalibrationRunPhase | None = None
    requested_count: int = 0
    filter_names: tuple[str, ...] = ()
    current_filter: str | None = None
    current_filter_index: int | None = None
    completed_frames: int = 0
    total_frames: int = 0
    exposure_s: float | None = None
    gain: int | None = None
    probes: tuple[FlatProbeState, ...] = ()
    generate_master: bool = False
    keep_only_master: bool = False
    master_paths: tuple[str, ...] = ()
    removed_source_frames: int = 0
    message: str | None = None
    error: str | None = None


class CaptureState(StateModel):
    """Whether SSC is mid-capture operation right now, and what the catalogue last
    held (``ssc/api/capture_service.py`` drives the exposure; this is what a
    desktop and a phone render of it — module docstring's own requirement,
    applied to a second live-mutating surface after ``run``).

    Populated at startup (``ssc.main.build_services``) from the catalogue's
    own last complete frame, so ``last_frame_id``/``last_captured_at`` survive
    a restart even though ``in_progress``/``last_error`` do not — mirrors
    ``secrets``' own "populated by the time any client can reach it" rule on
    :class:`StateSnapshot`, not ``run``'s "``None`` until something has
    happened" one.

    ``stage`` describes the current capture operation, including a dither and
    its settle gate before the shutter opens. ``duration_s``/``started_at``
    remain ``None`` during that phase and become known only once the exposure
    starts. All operation fields are cleared back to ``None`` when it ends,
    successfully or not — a client must never read finished-operation values
    as if they still described a live one.
    """

    in_progress: bool = False
    last_frame_id: int | None = None
    last_captured_at: datetime | None = None
    #: The most recent exposure's own failure, in its own words — cleared on
    #: the next successful capture, never silently on the next attempt alone
    #: (architecture policy invariant 7: a client must be able to see *why* the last
    #: one failed for as long as nothing newer has happened).
    last_error: str | None = None
    folder_configured: bool = False
    #: Which phase the current capture operation is in, or ``None`` when
    #: nothing is running. During dithering, duration/start are deliberately
    #: unknown; once exposing, a client computes progress from them against
    #: its own clock rather than being pushed a percentage every tick.
    stage: CaptureStage | None = None
    duration_s: float | None = None
    started_at: datetime | None = None
    object_name: str | None = None
    frame_type: FrameType | None = None
    gain: int | None = None
    #: A repeating light/dark/bias capture is a server-owned job. These
    #: fields let any newly connected browser recover the job's state; the
    #: browser is only a controller/viewer and never drives the next frame.
    loop_active: bool = False
    loop_completed_frames: int = 0
    loop_frame_limit: int | None = Field(default=None, ge=1)
    calibration: CalibrationRunState = Field(default_factory=CalibrationRunState)


AutofocusStatus = Literal["idle", "running", "completed", "failed", "interrupted"]
AutofocusStage = Literal["baseline", "coarse", "fine", "validation", "control"]


class AutofocusSampleState(StateModel):
    """One autofocus measurement, published as soon as it is available."""

    position: int
    hfr_px: float
    star_count: int
    stage: AutofocusStage


class AutofocusCurvePoint(StateModel):
    """One point on the current quadratic fit, for direct chart rendering."""

    position: float
    hfr_px: float


class AutofocusState(StateModel):
    """Current or retained autofocus diagnostics; restart never resumes motion."""

    status: AutofocusStatus = "idle"
    stage: AutofocusStage | None = None
    samples: tuple[AutofocusSampleState, ...] = ()
    curve: tuple[AutofocusCurvePoint, ...] = ()
    candidate_position: int | None = None
    final_position: int | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


class PolarAlignmentStartMode(StrEnum):
    """Which of DESIGN.md §4.5's start points a run actually used —
    ``ssc.api.polar_alignment_service.PolarAlignmentService.start``'s
    ``start_point`` echoed back into the section so a client can show a
    computed field was a compromise rather than let it pass silently
    (CLAUDE.md invariant 7)."""

    #: ``ssc.astro.polar.best_conditioned_field`` — the default since
    #: 2026-09-11 (DESIGN.md §4.5).
    BEST = "best"
    #: The mount's own current J2000 position, held over from before the
    #: default changed — still the right answer for an obstructed site.
    CURRENT = "current"
    #: A three-field geometry measured from the official Seestar app: start
    #: 15 degrees west of the meridian at declination equal to site latitude,
    #: then sweep east. The UI supplies the companion 3 x 25-degree settings.
    SEESTAR = "seestar"
    #: A caller-chosen ``start_ra_hours``/``start_dec_deg``.
    EXPLICIT = "explicit"


class PolarAlignmentRunKind(StrEnum):
    """Whether the current sweep establishes an error or independently checks it."""

    MEASURE = "measure"
    RECHECK = "recheck"


class PolarAlignmentVerdict(StrEnum):
    """The next honest action after a completed full sweep."""

    ADJUST = "adjust"
    RECHECK = "recheck"
    INCONSISTENT = "inconsistent"
    CONFIRMED = "confirmed"


class PolarAlignmentStartInfo(StateModel):
    """Which start point this run used and the effective sweep geometry.

    ``step_deg`` and ``arc_span_deg`` apply to every mode. The remaining
    planning fields are populated where SSC computed them (fully for
    ``BEST``, partly for ``SEESTAR``); ``None`` for ``CURRENT``/``EXPLICIT``
    means that no visibility geometry was computed, not that it was lost.
    """

    mode: PolarAlignmentStartMode
    step_deg: float | None = None
    arc_span_deg: float | None = None
    point_altitudes_deg: list[float] = Field(default_factory=list)
    start_altitude_deg: float | None = None
    start_azimuth_deg: float | None = None
    meridian_altitude_deg: float | None = None
    worst_altitude_deg: float | None = None
    declination_offset_deg: float | None = None
    circle_radius_deg: float | None = None
    sweep_amplification: float | None = None
    #: ``None`` outside ``BEST``. ``False`` — not absent — means a
    #: constraint bound and the field is a reduced-quality compromise; see
    #: :attr:`warnings` for which one (architecture policy invariant 7).
    ideal: bool | None = None
    warnings: list[str] = Field(default_factory=list)


class PolarAlignmentStatus(StrEnum):
    """Where a 3(+)-point polar-alignment measurement run is right now.

    Published throughout the run, not only at the end (``ssc/api/polar_alignment_service.py``'s
    own module docstring): a user standing at a tripod in the dark needs to
    see it working.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PolarAlignmentPointStatus(StrEnum):
    """One point's own progress within the run."""

    PENDING = "pending"
    SLEWING = "slewing"
    SETTLING = "settling"
    CAPTURING = "capturing"
    SOLVING = "solving"
    SOLVED = "solved"
    FAILED = "failed"
    ABORTED = "aborted"
    SKIPPED = "skipped"


class PolarAlignmentPointState(StateModel):
    """One target in the sequence, and how far it got.

    ``attempt`` counts every slew/capture/solve attempt made for this point —
    a point solved on attempt 2 recorded a real failure first (thin cloud,
    low altitude, a 409 mid-slew), which is exactly the "why" CLAUDE.md
    invariant 7 wants kept rather than overwritten silently by the retry
    that eventually worked.
    """

    index: int
    status: PolarAlignmentPointStatus
    target_ra_j2000_h: float
    target_dec_j2000_deg: float
    planned_altitude_deg: float | None = None
    actual_altitude_deg: float | None = None
    actual_azimuth_deg: float | None = None
    mount_slewing: bool | None = None
    mount_tracking: bool | None = None
    solved_ra_j2000_h: float | None = None
    solved_dec_j2000_deg: float | None = None
    attempt: int = 0
    #: The most recent failure for this point, in its own words — cleared
    #: when a retry succeeds. ``None`` never means "no failure happened",
    #: only "none is current" (mirrors ``CaptureState.last_error``).
    error: str | None = None


class PolarAlignmentResult(StateModel):
    """A JSON-safe projection of ``ssc.astro.polar.PolarAlignmentModel``.

    That module is pure and returns dataclasses (``Site`` included) that
    have no business crossing into client-facing state; this is the
    snapshot's own copy of the numbers a client renders, kept in step with
    the model's own field names so the two are easy to compare by eye.
    """

    axis_altitude_deg: float
    axis_azimuth_deg: float
    altitude_error_deg: float
    azimuth_error_deg: float
    azimuth_error_sky_deg: float
    total_error_deg: float
    altitude_correction_deg: float
    azimuth_correction_deg: float
    circle_radius_deg: float
    arc_span_deg: float
    #: ``None`` on a three-point run — ``ssc.astro.polar``'s own module
    #: docstring: three points fit a circle exactly, so there is nothing
    #: left to measure, and reporting "0.00" would be a lie. A client must
    #: not render a missing residual as a perfect one.
    rms_residual_arcsec: float | None
    max_residual_arcsec: float | None
    #: Degrees of freedom left in those residuals, ``point_count - 3``. Zero
    #: is why the residuals above are ``None``; show this next to them.
    residual_dof: int
    noise_amplification: float
    axis_uncertainty_arcsec: float
    point_count: int
    epoch: datetime
    refraction_applied: bool
    #: ``residual_dof > 0`` — sugar so a client does not have to know that
    #: rule itself; kept as its own field because "no residual check" is a
    #: fact about the run a client renders directly (architecture policy invariant 7).
    has_residual_check: bool


class PolarAlignmentHistoryEntry(StateModel):
    """The error measured by one successfully completed alignment sweep.

    Kept separately from :class:`PolarAlignmentResult` so starting the next
    measurement can replace the live point/result presentation without
    erasing the values the observer is comparing while adjusting the wedge.
    """

    run_kind: PolarAlignmentRunKind
    completed_at: datetime
    altitude_error_deg: float
    azimuth_error_deg: float
    total_error_deg: float


class PolarAlignmentAssessment(StateModel):
    """Cross-sweep verdict; one low result alone can never confirm alignment."""

    verdict: PolarAlignmentVerdict
    message: str
    current_total_error_arcmin: float
    current_axis_uncertainty_arcsec: float
    previous_total_error_arcmin: float | None = None
    previous_axis_uncertainty_arcsec: float | None = None
    axis_separation_arcmin: float | None = None
    max_error_arcmin: float = 2.0
    max_axis_separation_arcmin: float = 2.0
    max_axis_uncertainty_arcsec: float = 120.0


class PolarRefinementStatus(StrEnum):
    """Where the live tripod-refinement loop is right now, mirroring
    ``PolarAlignmentStatus``'s own contract for the sweep."""

    RUNNING = "running"
    STOPPED = "stopped"
    FAILED = "failed"


class PolarRefinementStage(StrEnum):
    """The observable camera/solver phase of a live PA refinement."""

    CAPTURING = "capturing"
    SOLVING = "solving"


class PolarRefinementResult(StateModel):
    """A JSON-safe projection of ``ssc.astro.polar.PolarRefinement``,
    following ``PolarAlignmentResult``'s own precedent one level up: the
    updated axis/error/correction figures from ``PolarRefinement.model``,
    plus the refinement-specific figures alongside them so a client need not
    reconstruct one from the other.

    Unlike ``PolarAlignmentResult``, nothing here is ever ``None`` on success —
    ``ssc.astro.polar.PolarRefinement`` itself carries no optional field (it
    has no three-point-style "nothing to measure" case), so there is no
    residual-style gap to protect against a confident zero.
    """

    axis_altitude_deg: float
    axis_azimuth_deg: float
    altitude_error_deg: float
    azimuth_error_deg: float
    azimuth_error_sky_deg: float
    total_error_deg: float
    altitude_correction_deg: float
    azimuth_correction_deg: float
    axis_uncertainty_arcsec: float
    epoch: datetime
    #: How far the axis moved in altitude/azimuth since the *sweep*, degrees —
    #: ``ssc.astro.polar.PolarRefinement.altitude_step_deg``/``azimuth_step_deg``.
    altitude_step_deg: float
    azimuth_step_deg: float
    #: Great-circle distance between the refined axis and the swept one —
    #: the staleness figure, grows with time since the sweep, not with the
    #: number of re-solves.
    axis_shift_deg: float
    #: Great-circle distance between the reference solve and this re-solve.
    boresight_shift_deg: float
    elapsed_seconds: float
    sweep_age_seconds: float
    refinement_uncertainty_arcsec: float
    noise_amplification: float
    #: Always false: a single field cannot verify the axis after physical
    #: movement. Diagnostic numbers are published with a warning, never as a
    #: correction to act on.
    trusted: bool
    warnings: list[str]


class PolarRefinementState(StateModel):
    """Where the live tripod-refinement loop (DESIGN.md M10) is right now.

    Every refinement is differential from the *sweep* that started this loop
    — never from a previous refinement (``ssc.astro.polar.PolarRefinement``'s
    own comment on why chaining would compound the reference solve's own
    noise). ``status is None`` means no refinement loop has run this
    session. A fresh sweep (``PolarAlignmentService.start``) resets this
    section: a new sweep invalidates whatever baseline the old refinement
    measured against.
    """

    status: PolarRefinementStatus | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    stage: PolarRefinementStage | None = None
    exposure_started_at: datetime | None = None
    exposure_duration_s: float | None = None
    #: How many re-solves have produced a usable refinement (trusted or not)
    #: since the loop started. Does not count degenerate iterations.
    iteration: int = 0
    #: How many iterations were skipped as geometrically degenerate — a
    #: tripod shoved too far, an ambiguous or unreachable move. The loop
    #: survives these (architecture policy invariant 7 / this module's own docstring);
    #: only a genuinely broken precondition ends the run.
    degenerate_count: int = 0
    #: The most recent ``ssc.astro.polar.DegeneracyReason`` value, or
    #: ``None`` once a clean iteration has followed it. ``None`` never means
    #: "never happened" — only "not the most recent thing".
    last_degenerate_reason: str | None = None
    #: The most recent degenerate message, or why the loop ended. Cleared on
    #: the next successful iteration, not on the next tick
    #: (``CaptureState.last_error``'s own rule).
    error: str | None = None
    result: PolarRefinementResult | None = None


class PolarAlignmentState(StateModel):
    """Where a polar-alignment measurement run is right now.

    ``status is None`` means no run has happened in the current observing night. Every
    other field is whole-section replacement, mirroring ``CaptureState``
    (that class's own docstring): a desktop and a phone must never disagree
    about whether SSC is mid-run, which point it is on, or what the last
    result was.
    """

    status: PolarAlignmentStatus | None = None
    run_kind: PolarAlignmentRunKind | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    #: Timing for the sweep's current camera exposure. Cleared while the
    #: mount slews/settles and while ASTAP solves the downloaded frame.
    exposure_started_at: datetime | None = None
    exposure_duration_s: float | None = None
    current_point_index: int | None = None
    #: A bounded preflight may wait for the first trustworthy mount poll
    #: before point 0. This is a live reason, not a failure, and clears before
    #: any slew attempt begins.
    preflight_reason: str | None = None
    points: list[PolarAlignmentPointState] = Field(default_factory=list)
    result: PolarAlignmentResult | None = None
    #: Every successfully completed sweep in this observing night, oldest
    #: first. Failed and cancelled attempts contain no measurement and are
    #: deliberately not added.
    history: list[PolarAlignmentHistoryEntry] = Field(default_factory=list)
    assessment: PolarAlignmentAssessment | None = None
    #: Which start point this run used, and the computed field's own
    #: numbers when it was ``BEST`` (see :class:`PolarAlignmentStartInfo`).
    #: ``None`` before any run has started this process.
    start: PolarAlignmentStartInfo | None = None
    #: Why the run failed or was refused, in its own words. Cleared on the
    #: next run's start, not on the next tick (CaptureState.last_error's
    #: own rule).
    error: str | None = None
    #: The live tripod-refinement loop attached to this sweep (see
    #: :class:`PolarRefinementState`). A fresh, never-started section on
    #: every new sweep.
    refinement: PolarRefinementState = Field(default_factory=PolarRefinementState)


class OperationStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OperationState(StateModel):
    """One accepted backend command, retained across frontend reconnects."""

    id: str
    kind: str
    status: OperationStatus
    message: str
    progress_current: float | None = None
    progress_total: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    idempotency_key: str | None = None
    cancellable: bool = False
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None


class StateSnapshot(StateModel):
    """The one authoritative view. Clients render this; they never own it."""

    schema_version: Annotated[int, Field(ge=1)] = SNAPSHOT_SCHEMA_VERSION
    server_started_at: datetime
    link: LinkState = Field(default_factory=LinkState)
    device: DeviceDescriptor | None = None
    #: ``None`` until the first successful poll of a connected mount
    #: (``ssc.main.AlpacaDeviceGateway``); see :class:`MountState` for how it
    #: behaves across a dropped link.
    mount: MountState | None = None
    #: The active camera's real field of view (``ssc.main.AlpacaDeviceGateway``,
    #: read once per connection — see :class:`CameraFieldOfView`). ``None``
    #: until a camera has been found and every input the field needs has been
    #: read; the sky map's mount-pointing overlay falls back to its marker for
    #: exactly as long as this stays ``None``.
    camera_fov: CameraFieldOfView | None = None
    #: ``None`` until the first poll after a connect attempt
    #: (``ssc.main.AlpacaDeviceGateway``) — the connections footer's one
    #: source of truth for every device ``ssc-alpaca`` serves, not only the
    #: mount. See :class:`AlpacaDevicesState`.
    devices: AlpacaDevicesState | None = None
    auto_connect: AutoConnectState = Field(default_factory=AutoConnectState)
    discovery: DiscoveryState = Field(default_factory=DiscoveryState)
    location: LocationState | None = None
    settings: SettingsSummary = Field(default_factory=SettingsSummary)
    #: Every stored plan's identity (DESIGN.md 4), so open clients agree on the
    #: plan list without polling ``GET /api/plans``. Whole-list replacement on
    #: every mutation, like every other snapshot section.
    plans: list[PlanSummary] = Field(default_factory=list)
    #: ``None`` only before the startup probe has run; by the time any client
    #: can reach ``GET /api/state`` this is always populated (see
    #: ``ssc.main.build_services``).
    secrets: SecretsCapability | None = None
    notices: list[Notice] = Field(default_factory=list)
    last_device_event: DeviceEventRecord | None = None
    device_events_seen: int = 0
    #: Sky-survey tile cache and offline packs (``ssc/skymap``).
    skymap: SkymapState = Field(default_factory=SkymapState)
    #: The current (or most recent, or interrupted) executor run. ``None``
    #: when this process has never run a plan and found none to resume.
    run: RunState | None = None
    #: Whether SSC is mid-exposure, and the catalogue's own last frame.
    #: ``None`` only before ``ssc.main.build_services`` has populated it —
    #: by the time any client can reach ``GET /api/state`` this is always
    #: set (see :class:`CaptureState`'s own docstring, mirroring ``secrets``).
    capture: CaptureState | None = None
    #: Current/most recent polar alignment restored from this observing night's
    #: complete persisted details. Restart never resumes physical operations.
    polar_alignment: PolarAlignmentState = Field(default_factory=PolarAlignmentState)
    autofocus: AutofocusState = Field(default_factory=AutofocusState)
    #: Backend-owned commands and their retained terminal outcomes. A new
    #: browser renders these instead of attempting to recreate client work.
    operations: list[OperationState] = Field(default_factory=list)


__all__ = [
    "MAX_NOTICES",
    "MOUNT_STALE_AFTER_S",
    "SNAPSHOT_SCHEMA_VERSION",
    "AlpacaDeviceItem",
    "AlpacaDeviceKind",
    "AlpacaDevicesState",
    "AutoConnectState",
    "AutofocusCurvePoint",
    "AutofocusSampleState",
    "AutofocusStage",
    "AutofocusState",
    "AutofocusStatus",
    "BulkCutoutJobState",
    "BulkCutoutJobStatus",
    "BulkCutoutScope",
    "CalibrationRunKind",
    "CalibrationRunPhase",
    "CalibrationRunState",
    "CalibrationRunStatus",
    "CameraFieldOfView",
    "CaptureState",
    "DeviceDescriptor",
    "DeviceEventRecord",
    "DiscoveryState",
    "FlatProbeState",
    "LinkState",
    "LinkStatus",
    "LocationSource",
    "LocationState",
    "MountAxis",
    "MountCapabilities",
    "MountState",
    "MountTrackingRate",
    "Notice",
    "NoticeLevel",
    "OperationState",
    "OperationStatus",
    "PlanSummary",
    "PolarAlignmentAssessment",
    "PolarAlignmentHistoryEntry",
    "PolarAlignmentPointState",
    "PolarAlignmentPointStatus",
    "PolarAlignmentResult",
    "PolarAlignmentRunKind",
    "PolarAlignmentStartInfo",
    "PolarAlignmentStartMode",
    "PolarAlignmentState",
    "PolarAlignmentStatus",
    "PolarAlignmentVerdict",
    "PolarRefinementResult",
    "PolarRefinementStage",
    "PolarRefinementState",
    "PolarRefinementStatus",
    "PrefetchJobState",
    "PrefetchJobStatus",
    "RunAction",
    "RunItemState",
    "RunItemStatus",
    "RunLifecycle",
    "RunState",
    "SecretsCapability",
    "SettingsSummary",
    "SkymapState",
    "StateModel",
    "StateSnapshot",
    "SurveyCacheState",
    "devices_stale",
    "mount_stale",
    "refresh_devices_staleness",
    "refresh_mount_staleness",
]
