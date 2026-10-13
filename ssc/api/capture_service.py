# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Driving one exposure over the Alpaca seam, end to end (DESIGN.md §9.5.1/§9.5.2).

``ssc.capture`` deliberately does not talk to a device — it only ever writes
what it is handed (``ssc.capture.frame.RawFrame``). Something above it has to
command ``Camera.StartExposure``, poll ``Camera.ImageReady``, pull
``Camera.ImageArray``, and read every other Alpaca property the FITS header
needs *at that moment* — never before, never by a later poll (CLAUDE.md
invariant 7). This module is that something, and it hands the result straight
to ``ssc.capture.pipeline.capture_frame`` for the durable write.

Reached only through the seam SSC's own UI uses (CLAUDE.md invariant 2): the
device parameters below are typed as small local :class:`~typing.Protocol`\\
s — matched structurally against ``ssc.client.alpaca.AlpacaDeviceClient`` —
rather than depending on the concrete client, precisely so this module can be
driven end to end by a fake in tests without a live driver or emulator behind
it. ``ssc.alpaca`` is not yet passing ``Camera`` members through to the native
9.16 surface (a concurrent dispatch); until it does, ``StartExposure`` over a
real driver answers ``NotImplemented``, which this module reports as
:class:`ExposureNotAvailableError` rather than papering over it.

**A property this module cannot read is absent from the header, not
zero** (CLAUDE.md invariant 7). Two different kinds of failure are kept
deliberately distinct:

* the driver rejects an optional property read — whether as
  ``NotImplemented`` or a firmware-specific error — degraded to ``None`` and
  logged, exactly as DESIGN.md §9.5.2 requires. The S30 Pro has returned
  generic ``0x4ff`` when its native dictionary omitted ``Latitude``;
* the driver answers something that is not the type the member promises (a
  non-numeric ``Gain``, a filter index outside ``Names``) — not a missing
  capability but a broken answer, and :class:`CaptureMetadataError` is raised
  rather than silently discarded, because a wrong number that looks like a
  right one is worse than no number (DESIGN.md §9.5.2's ``BAYERPAT`` warning
  generalises to every field here).

``BayerOffsetX``/``BayerOffsetY`` are read only after the camera is
connected — disconnected, this device answers a plausible, wrong ``0/0``
(DESIGN.md §9.5.2) — which falls out naturally here because every read in
this module happens after :func:`drive_exposure` has already connected the
camera to start the exposure at all.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Literal, Protocol, TypedDict
from zoneinfo import ZoneInfo

from ssc.astro.events import Ephemeris
from ssc.astro.frames import (
    EquatorialCoord,
    MountFrame,
    UnsupportedMountFrameError,
    mount_frame_from_ascom,
    mount_site,
    to_j2000,
)
from ssc.astro.units import Deg
from ssc.capture import (
    DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    FrameMetadata,
    FrameType,
    RawFrame,
    capture_frame,
)
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError, AlpacaTransportError
from ssc.client.imagebytes import ImageBytesFrame
from ssc.state.models import CaptureStage, LocationState, MountState, mount_stale
from ssc.store.capture import CaptureRepository

_log = logging.getLogger(__name__)

#: Both spellings the Alpaca API Reference uses for "the driver has no verb
#: for this" — restated here for the same reason ``ssc.client.errors`` restates
#: the ASCOM numbers themselves: ``ssc.api`` depends on the protocol, not on
#: ``ssc.alpaca``, the one driver that speaks it.
_NOT_IMPLEMENTED_NUMBERS: Final[frozenset[int]] = frozenset(
    {int(AlpacaErrorNumber.NOT_IMPLEMENTED), int(AlpacaErrorNumber.ACTION_NOT_IMPLEMENTED)}
)

#: Readout has been measured at ~1.8-2.0 s beyond the commanded exposure
#: (DESIGN.md §9.5.2); this is generous headroom above that, not a tuned bound.
DEFAULT_READY_TIMEOUT_MARGIN_S: Final = 60.0

DEFAULT_POLL_INTERVAL_S: Final = 0.1

# Native Seestar firmware can briefly return NOT_CONNECTED while its HTTP
# surface reconnects, even though the exposure itself is still in progress.
# Keep polling that same exposure rather than issuing StartExposure again.
DEFAULT_READY_TRANSPORT_RETRIES: Final = 3


class ExposureNotAvailableError(RuntimeError):
    """The driver refused a member this loop needs (StartExposure/ImageReady/
    ImageArray) as not implemented. Expected against a 9.16+ driver until the
    native Camera surface lands — see the module docstring."""


class ExposureTimeoutError(RuntimeError):
    """``Camera.ImageReady`` never went true within the allotted time."""


class CaptureMetadataError(RuntimeError):
    """The driver answered a property with a value that is not the type the
    Alpaca member promises. Never silently coerced or discarded — see the
    module docstring's distinction from "not implemented"."""


# --- the device surface this module needs, structurally --------------------- #


class Device(Protocol):
    """The subset of ``AlpacaDeviceClient`` every device role needs."""

    async def is_connected(self) -> bool: ...
    async def connect(self) -> None: ...
    async def get(self, member: str, **params: object) -> object: ...


class CameraDevice(Device, Protocol):
    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None: ...
    async def image_ready(self) -> bool: ...
    async def image_array(self) -> ImageBytesFrame: ...


@dataclass(frozen=True, slots=True)
class ExposureContext:
    """Everything about this exposure that only SSC knows — the device
    cannot answer any of these (DESIGN.md §9.5.2's header/source table)."""

    object_name: str | None
    focal_length_mm: float | None
    telescope_name: str | None
    #: SSC's own version. Always known, unlike everything the device answers.
    ssc_version: str
    #: The device firmware this exposure was captured against, already known
    #: to SSC's own state (``ssc.state``) rather than re-read from the device
    #: here — DESIGN.md §9.5.2 wants it recorded, not re-derived.
    device_firmware: str | None
    dither_group_id: str | None = None
    optic_id: str | None = None
    flat_source: str | None = None
    #: Which physical camera was selected. Capture callers know this from the
    #: device number; Alpaca's Camera interface does not expose an optic role.
    wide_camera: bool = False
    #: Pinned ephemeris used to convert the mount's frame-time pointing into
    #: ICRS/J2000. ``None`` means the conversion is unavailable; a fresh
    #: canonical J2000 fallback may still provide truthful coordinates.
    ephemeris: Ephemeris | None = None
    #: Explicit ICRS/J2000 fallback captured from SSC's canonical state
    #: immediately before the exposure. Used when frame-time telescope reads
    #: cannot be converted as a complete, declared pair; stale values are
    #: never put here.
    fallback_ra_hours: float | None = None
    fallback_dec_deg: float | None = None
    fallback_site_lat_deg: float | None = None
    fallback_site_lon_deg: float | None = None


class ExposureMetadataFallbacks(TypedDict):
    fallback_ra_hours: float | None
    fallback_dec_deg: float | None
    fallback_site_lat_deg: float | None
    fallback_site_lon_deg: float | None


def exposure_metadata_fallbacks(
    mount: MountState | None,
    location: LocationState | None,
    *,
    now: datetime,
) -> ExposureMetadataFallbacks:
    """Return trustworthy pre-exposure FITS metadata fallbacks.

    Pointing is accepted only as a complete ICRS/J2000 pair from a fresh mount
    poll.
    Site coordinates prefer the same fresh mount reading, then the configured
    observing location. Returning pairs prevents a transient read from
    combining coordinates taken at different instants or from different sites.
    """
    ra_hours: float | None = None
    dec_deg: float | None = None
    site_lat_deg: float | None = None
    site_lon_deg: float | None = None
    if mount is not None and not mount_stale(mount.polled_at, now=now):
        if mount.ra_j2000_h is not None and mount.dec_j2000_deg is not None:
            ra_hours, dec_deg = mount.ra_j2000_h, mount.dec_j2000_deg
        if mount.site_latitude_deg is not None and mount.site_longitude_deg is not None:
            site_lat_deg, site_lon_deg = mount.site_latitude_deg, mount.site_longitude_deg
    if (site_lat_deg is None or site_lon_deg is None) and location is not None:
        site_lat_deg, site_lon_deg = location.latitude_deg, location.longitude_deg
    return {
        "fallback_ra_hours": ra_hours,
        "fallback_dec_deg": dec_deg,
        "fallback_site_lat_deg": site_lat_deg,
        "fallback_site_lon_deg": site_lon_deg,
    }


def _j2000_pointing(
    *,
    ra_hours: float | None,
    dec_deg: float | None,
    equatorial_system: int | None,
    site_lat_deg: float | None,
    site_lon_deg: float | None,
    observed_at: datetime,
    context: ExposureContext,
) -> tuple[float | None, float | None]:
    """Return a truthful ICRS/J2000 pair for the captured frame.

    A raw Alpaca RA/Dec pair is meaningless without ``EquatorialSystem``.
    Convert it when every required input is present; otherwise use the fresh
    canonical J2000 pair captured in ``context``. Never label an unknown frame
    as J2000 merely because its numeric values look plausible.
    """

    def fallback() -> tuple[float | None, float | None]:
        if context.fallback_ra_hours is None or context.fallback_dec_deg is None:
            return None, None
        return context.fallback_ra_hours * 15.0, context.fallback_dec_deg

    if ra_hours is None or dec_deg is None or equatorial_system is None:
        return fallback()

    try:
        frame = mount_frame_from_ascom(equatorial_system)
    except ValueError as exc:
        _log.warning("FITS pointing omitted frame-time coordinate with unknown frame: %s", exc)
        return fallback()

    native = EquatorialCoord(ra_deg=Deg(ra_hours * 15.0), dec_deg=Deg(dec_deg), frame=frame)
    if frame is MountFrame.J2000:
        return float(native.ra_deg), float(native.dec_deg)
    if context.ephemeris is None or site_lat_deg is None or site_lon_deg is None:
        _log.info("using fresh canonical J2000 pointing for incomplete frame conversion inputs")
        return fallback()
    try:
        converted = to_j2000(
            context.ephemeris,
            mount_site(Deg(site_lat_deg), Deg(site_lon_deg)),
            native,
            observed_at,
        )
    except UnsupportedMountFrameError as exc:
        _log.warning("FITS pointing omitted unsupported frame-time coordinate: %s", exc)
        return fallback()
    return float(converted.ra_deg), float(converted.dec_deg)


#: ASCOM's "not connected" error. A property behind it is *unknown*, not zero.
_NOT_CONNECTED: Final = 0x407

#: ASCOM's ``Camera.StartExposure`` has exactly one boolean, ``Light`` — it
#: has no concept of dark/flat/bias, only "does the shutter matter". A dark
#: and a bias are both shutter-closed (``Light=False``); a flat is shutter-
#: open against a panel or the twilight sky, the same as a light frame
#: (``Light=True``). :class:`~ssc.capture.frame.FrameType` is SSC's own,
#: richer classification (DESIGN.md/architecture policy capture contract, 2026-09-10);
#: this is the one place that narrows it back down to what ASCOM can express.
_LIGHT_FRAME_TYPES: Final[frozenset[FrameType]] = frozenset({FrameType.LIGHT, FrameType.FLAT})


def light_flag(frame_type: FrameType) -> bool:
    return frame_type in _LIGHT_FRAME_TYPES


async def _ensure_connected(device: Device) -> None:
    if not await device.is_connected():
        await device.connect()


OPTIONAL_METADATA_TIMEOUT_S: Final = 3.0


async def _read_optional(
    device: Device, member: str, *, label: str,
    unsupported_members: set[str] | None = None,
) -> object | None:
    """Keep supplementary metadata from delaying an already exposed image."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(OPTIONAL_METADATA_TIMEOUT_S):
            return await _read_optional_unbounded(
                device, member, label=label, unsupported_members=unsupported_members
            )
    except TimeoutError:
        _log.warning("%s optional metadata timed out; header field left absent", label)
        return None
    finally:
        elapsed = time.perf_counter() - started
        if elapsed >= 1.0:
            _log.info("optional metadata timing %s %.3fs", label, elapsed)


async def _read_optional_unbounded(
    device: Device,
    member: str,
    *,
    label: str,
    unsupported_members: set[str] | None = None,
) -> object | None:
    """Read optional FITS metadata without sacrificing an exposed frame.

    Alpaca drivers are not consistent about the error used for an unavailable
    property. In particular, the S30 Pro has returned generic ``0x4ff`` for
    ``Telescope.SiteLatitude`` when its native settings dictionary contained no
    ``Latitude`` key. Every call through this helper is supplementary header
    metadata, so any remote rejection means "unknown". Required camera
    operations bypass this helper and remain fatal.
    """
    if unsupported_members is not None and label in unsupported_members:
        return None
    try:
        try:
            return await device.get(member)
        except AlpacaRemoteError as exc:
            if label != "Focuser.Position" or exc.number != 0x4FF or "dictionary" not in exc.message.casefold():
                raise
            # Native firmware occasionally omits position immediately after AF.
            # Retry the read once; never substitute the last commanded position.
            return await device.get(member)
        except AlpacaTransportError:
            # One bounded retry can recover an HTTP connection closed by the
            # native driver. Never cache a transport failure as unsupported.
            return await device.get(member)
    except AlpacaTransportError as exc:
        # A transient failure of one supplementary property must not prevent
        # the required ImageBytes transfer and discard an exposure the camera
        # already completed. The required image read runs outside this helper
        # and remains fatal if the transport itself is genuinely unavailable.
        _log.warning(
            "%s unavailable because its optional metadata request failed (%s); "
            "header field left absent",
            label,
            exc,
        )
        return None
    except AlpacaRemoteError as exc:
        # A metadata read must never fail a capture that has already exposed:
        # the frame exists, and an absent card is a smaller loss than a
        # discarded exposure (invariant 7 — absent, never zero).
        not_implemented = exc.number in _NOT_IMPLEMENTED_NUMBERS
        if not_implemented and unsupported_members is not None:
            unsupported_members.add(label)
        log = (
            _log.info
            if not_implemented or exc.number == _NOT_CONNECTED
            else _log.warning
        )
        log(
            "%s (%s) unavailable on this driver ([0x%x] %s); "
            "header field left absent",
            label,
            exc.member,
            exc.number,
            exc.message,
        )
        return None


def _as_int(value: object | None, *, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CaptureMetadataError(f"{label} returned {value!r}, not a number")
    if isinstance(value, float) and not value.is_integer():
        raise CaptureMetadataError(f"{label} returned non-integral {value!r}")
    return int(value)


def _as_float(value: object | None, *, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CaptureMetadataError(f"{label} returned {value!r}, not a number")
    return float(value)


def _as_str(value: object | None, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CaptureMetadataError(f"{label} returned {value!r}, not a string")
    return value if value.strip() else None


def _as_datetime(value: object | None, *, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CaptureMetadataError(f"{label} returned {value!r}, not a string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise CaptureMetadataError(
            f"{label} returned {value!r}, not an ISO-8601 timestamp"
        ) from exc
    # ASCOM's LastExposureStartTime is UTC with no offset qualifier.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _read_filter_name(
    filterwheel: Device, *, unsupported_members: set[str] | None = None
) -> str | None:
    position = _as_int(
        await _read_optional(
            filterwheel,
            "position",
            label="FilterWheel.Position",
            unsupported_members=unsupported_members,
        ),
        label="FilterWheel.Position",
    )
    if position is None:
        return None
    names = await _read_optional(
        filterwheel,
        "names",
        label="FilterWheel.Names",
        unsupported_members=unsupported_members,
    )
    if names is None:
        return None
    if not isinstance(names, list):
        raise CaptureMetadataError(f"FilterWheel.Names returned {names!r}, not a list")
    if position < 0 or position >= len(names):
        raise CaptureMetadataError(
            f"FilterWheel.Position {position} is out of range for "
            f"FilterWheel.Names of length {len(names)}"
        )
    name = names[position]
    if not isinstance(name, str):
        raise CaptureMetadataError(f"FilterWheel.Names[{position}] returned {name!r}, not a string")
    return name


async def _download_image_array(camera: CameraDevice) -> ImageBytesFrame:
    """Fetch pixels while preserving the public exposure error contract."""
    started = time.perf_counter()
    try:
        return await camera.image_array()
    except AlpacaRemoteError as exc:
        if exc.number in _NOT_IMPLEMENTED_NUMBERS:
            raise ExposureNotAvailableError(f"Camera.ImageArray: {exc.message}") from exc
        raise
    finally:
        _log.info("required image transfer timing %.3fs", time.perf_counter() - started)


async def begin_exposure(
    *,
    camera: CameraDevice,
    telescope: Device | None,
    filterwheel: Device | None,
    duration_s: float,
    light: bool = True,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
) -> datetime:
    """Connect every device this exposure needs and command
    ``Camera.StartExposure``. Returns SSC's own wall-clock record of when it
    was commanded — the same value :func:`drive_exposure` would have taken
    itself.

    Split out of :func:`drive_exposure` so a caller that must answer
    :class:`ExposureNotAvailableError` synchronously — the capture API's own
    ``202``-vs-``501`` boundary, since ``StartExposure`` is one fast Alpaca
    round trip either way, unlike the poll loop that follows a successful
    start — can await exactly this and no more. :func:`drive_exposure`
    remains the single place that performs it when a caller has no reason to
    split the two.
    """
    await _ensure_connected(camera)
    # The telescope and filter wheel are read for header fields, and on this
    # device **every sub-device needs its own `Connected = True`** — the
    # session's handshake connects `telescope/0` only (docs/M0-FINDINGS.md,
    # 9.31). Connecting the camera alone left `FilterWheel.Position` raising
    # `[0x407] requires Connected = True`, which took the whole capture down
    # rather than costing one header card. Found by the first live run; a
    # fake camera cannot reproduce it, because a fake has no sub-devices to
    # leave disconnected.
    for extra in (telescope, filterwheel):
        if extra is not None:
            await _ensure_connected(extra)
    started_at = now()
    try:
        await camera.start_exposure(duration_s, light=light)
    except AlpacaRemoteError as exc:
        if exc.number in _NOT_IMPLEMENTED_NUMBERS:
            raise ExposureNotAvailableError(f"Camera.StartExposure: {exc.message}") from exc
        raise
    return started_at


async def drive_exposure(
    *,
    camera: CameraDevice,
    telescope: Device | None,
    filterwheel: Device | None,
    focuser: Device | None = None,
    duration_s: float,
    context: ExposureContext,
    frame_type: FrameType,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ready_timeout_s: float | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    started_at: datetime | None = None,
    on_stage: Callable[[CaptureStage], None] | None = None,
    metadata_mode: Literal["full", "solver", "autofocus"] = "full",
    ready_transport_retries: int = DEFAULT_READY_TRANSPORT_RETRIES,
    unsupported_members: set[str] | None = None,
) -> tuple[RawFrame, datetime]:
    """Command one exposure and return the decoded frame plus SSC's own
    wall-clock record of when it was commanded.

    ``frame_type`` is SSC's own classification (:class:`~ssc.capture.frame.FrameType`),
    written straight onto the returned frame's metadata; it is narrowed to
    ASCOM's ``Light`` boolean (:func:`light_flag`) only for the
    ``Camera.StartExposure`` call itself.

    That second value — not ``frame.metadata.date_obs``, which may be
    ``None`` — is what the catalogue orders and groups sessions by
    (``ssc.store.capture.CaptureRepository.begin_frame``'s own docstring):
    SSC always knows when it issued ``StartExposure``, regardless of whether
    the device's own ``LastExposureStartTime`` can later be read.

    Polls ``Camera.ImageReady`` rather than sleeping the exposure duration and
    assuming (DESIGN.md §9.5.2) — but does sleep the known-minimum
    ``duration_s`` once before the first poll, since the device cannot
    possibly be ready before then and polling through that stretch would only
    spin for a long exposure.

    ``started_at``: when the exposure was already commanded by a prior call
    to :func:`begin_exposure` (the capture API's own case — see that
    function's docstring), pass its return value here to skip repeating
    ``StartExposure``. ``None`` (the default, every existing caller) drives
    the whole thing here exactly as before.

    ``on_stage``: optional, called synchronously as the exposure moves
    through :class:`~ssc.state.models.CaptureStage` — ``EXPOSING`` before the
    known-minimum sleep, ``DOWNLOADING`` once the ``ImageReady`` poll starts,
    ``TRANSFERRING`` right before ``Camera.ImageArray``. ``None`` (the
    default, every existing caller except ``CaptureService``) skips this
    entirely — added so a caller *can* publish live progress without every
    other caller having to care.

    ``metadata_mode="solver"`` downloads the pixels immediately after they
    become ready and records only SSC's known timing/context. Plate solving
    does not consume optional science-header fields, and waiting serially for
    unsupported/slow driver properties before ``ImageArray`` can turn a
    one-second solve into a minute-long apparent hang. The default ``full``
    path used for saved captures remains unchanged. ``autofocus`` reads only
    Bayer offsets needed for the focus metric before downloading; unavailable
    offsets remain unknown, never replaced with a guessed pattern.
    """
    drive_started = time.perf_counter()
    if metadata_mode not in {"full", "solver", "autofocus"}:
        raise ValueError(f"unknown exposure metadata mode: {metadata_mode}")
    if on_stage is not None:
        on_stage(CaptureStage.EXPOSING)
    if started_at is None:
        started_at = await begin_exposure(
            camera=camera,
            # A solver frame needs pixels plus SSC's known exposure timing;
            # connecting extra devices only to query science-header metadata
            # made a one-second plate solve wait through many 5s driver
            # timeouts before ImageArray was even downloaded.
            telescope=None if metadata_mode != "full" else telescope,
            filterwheel=None if metadata_mode != "full" else filterwheel,
            duration_s=duration_s,
            light=light_flag(frame_type),
            now=now,
        )

    timeout_s = (
        duration_s + DEFAULT_READY_TIMEOUT_MARGIN_S if ready_timeout_s is None else ready_timeout_s
    )
    deadline = started_at + timedelta(seconds=timeout_s)
    if duration_s > 0:
        await sleep(duration_s)
    if on_stage is not None:
        on_stage(CaptureStage.DOWNLOADING)
    transient_failures = 0
    while True:
        try:
            ready = await camera.image_ready()
        except AlpacaRemoteError as exc:
            if exc.number in _NOT_IMPLEMENTED_NUMBERS:
                raise ExposureNotAvailableError(f"Camera.ImageReady: {exc.message}") from exc
            if exc.number != int(AlpacaErrorNumber.NOT_CONNECTED):
                raise
            transient_failures += 1
            if transient_failures > ready_transport_retries:
                raise
            _log.warning(
                "Camera.ImageReady transient connection failure %d/%d; retrying the same "
                "exposure: %s",
                transient_failures,
                ready_transport_retries,
                exc,
            )
            await sleep(poll_interval_s)
            continue
        except AlpacaTransportError as exc:
            transient_failures += 1
            if transient_failures > ready_transport_retries:
                raise
            _log.warning(
                "Camera.ImageReady transport failure %d/%d; retrying the same exposure: %s",
                transient_failures,
                ready_transport_retries,
                exc,
            )
            await sleep(poll_interval_s)
            continue
        transient_failures = 0
        if ready:
            break
        if now() >= deadline:
            async def diagnostic(member: str) -> str:
                try:
                    value = await asyncio.wait_for(camera.get(member), timeout=2.0)
                    if isinstance(value, (int, float)) and not isinstance(value, bool):
                        return str(value)
                    return "unknown"
                except Exception as exc:
                    return f"unavailable ({type(exc).__name__})"

            camera_state, progress = await asyncio.gather(
                diagnostic("camerastate"), diagnostic("percentcompleted")
            )
            raise ExposureTimeoutError(
                f"Camera.ImageReady did not become true within {timeout_s:.1f}s of a "
                f"{duration_s:.1f}s exposure; started={started_at.isoformat()}, "
                f"checked={now().isoformat()}, CameraState={camera_state}, "
                f"PercentCompleted={progress}"
            )
        await sleep(poll_interval_s)

    ready_elapsed_s = time.perf_counter() - drive_started

    if metadata_mode in {"solver", "autofocus"}:
        if on_stage is not None:
            on_stage(CaptureStage.TRANSFERRING)
        metadata_started = time.perf_counter()
        offset_x = offset_y = None
        if metadata_mode == "autofocus":
            raw_x, raw_y = await asyncio.gather(
                _read_optional(
                    camera, "bayeroffsetx", label="Camera.BayerOffsetX",
                    unsupported_members=unsupported_members,
                ),
                _read_optional(
                    camera, "bayeroffsety", label="Camera.BayerOffsetY",
                    unsupported_members=unsupported_members,
                ),
            )
            offset_x = _as_int(raw_x, label="Camera.BayerOffsetX")
            offset_y = _as_int(raw_y, label="Camera.BayerOffsetY")
        metadata_elapsed_s = time.perf_counter() - metadata_started
        transfer_started = time.perf_counter()
        frame_bytes = await _download_image_array(camera)
        transfer_elapsed_s = time.perf_counter() - transfer_started
        _log.info(
            "exposure timing frame=%s mode=%s ready=%.3fs metadata=%.3fs "
            "transfer=%.3fs metadata+transfer=%.3fs total=%.3fs",
            frame_type.value,
            metadata_mode,
            ready_elapsed_s,
            metadata_elapsed_s,
            transfer_elapsed_s,
            metadata_elapsed_s + transfer_elapsed_s,
            time.perf_counter() - drive_started,
        )
        metadata = FrameMetadata(
            date_obs=started_at,
            exposure_s=duration_s,
            gain=None,
            ccd_temp_c=None,
            bayer_offset_x=offset_x,
            bayer_offset_y=offset_y,
            pixel_size_um=None,
            ra_deg=None,
            dec_deg=None,
            site_lat_deg=None,
            site_lon_deg=None,
            filter_name=None,
            object_name=context.object_name,
            frame_type=frame_type,
            focal_length_mm=context.focal_length_mm,
            telescope_name=context.telescope_name,
            ssc_version=context.ssc_version,
            device_firmware=context.device_firmware,
            optic_id=context.optic_id,
            flat_source=context.flat_source,
            timing_from_ssc=True,
        )
        return RawFrame(pixels=frame_bytes.array.T, metadata=metadata), started_at

    # Read everything else right as the frame becomes available — "at
    # exposure time", never polled again afterwards (architecture policy invariant 7).
    camera_members = (
        ("gain", "Camera.Gain"),
        ("ccdtemperature", "Camera.CCDTemperature"),
        ("pixelsizex", "Camera.PixelSizeX"),
        ("pixelsizey", "Camera.PixelSizeY"),
        ("binx", "Camera.BinX"),
        ("biny", "Camera.BinY"),
        ("startx", "Camera.StartX"),
        ("starty", "Camera.StartY"),
        ("sensorname", "Camera.SensorName"),
        ("sensortype", "Camera.SensorType"),
        ("offset", "Camera.Offset"),
        ("maxadu", "Camera.MaxADU"),
        ("readoutmode", "Camera.ReadoutMode"),
        ("lastexposurestarttime", "Camera.LastExposureStartTime"),
        ("lastexposureduration", "Camera.LastExposureDuration"),
    )
    telescope_members: tuple[tuple[str, str], ...] = (
        ("rightascension", "Telescope.RightAscension"),
        ("declination", "Telescope.Declination"),
        ("equatorialsystem", "Telescope.EquatorialSystem"),
        ("sitelatitude", "Telescope.SiteLatitude"),
        ("sitelongitude", "Telescope.SiteLongitude"),
        ("alignmentmode", "Telescope.AlignmentMode"),
        ("focallength", "Telescope.FocalLength"),
        ("aperturediameter", "Telescope.ApertureDiameter"),
    )
    if frame_type in {FrameType.DARK, FrameType.BIAS}:
        # Parked dark/bias captures have no celestial pointing. Do not query
        # firmware coordinates that are unavailable below the horizon.
        telescope_members = tuple(
            entry for entry in telescope_members
            if entry[0] not in {"rightascension", "declination", "equatorialsystem"}
        )
    if on_stage is not None:
        on_stage(CaptureStage.TRANSFERRING)
    transfer_started = time.perf_counter()
    # Keep the bulk ImageArray transfer out of the optional-read phase. The
    # native driver can delay otherwise valid small property responses while
    # streaming pixels, exhausting their deliberately short timeout budgets.
    # Header reads still overlap each other and unknown values remain absent.
    # CFA and filter determine how this raw frame can be interpreted and grouped.
    # Read their measured values before the supplementary header fanout: a busy
    # native driver otherwise queues them behind unrelated reads until timeout.
    # A failed read still stays unknown; never borrow a previous frame's values.
    raw_bayer_x, raw_bayer_y, filter_name = await asyncio.gather(
        _read_optional(
            camera, "bayeroffsetx", label="Camera.BayerOffsetX",
            unsupported_members=unsupported_members,
        ),
        _read_optional(
            camera, "bayeroffsety", label="Camera.BayerOffsetY",
            unsupported_members=unsupported_members,
        ),
        asyncio.sleep(0, result=None)
        if filterwheel is None
        else _read_filter_name(filterwheel, unsupported_members=unsupported_members),
    )
    camera_values, telescope_values, accessory_values = await asyncio.gather(
        asyncio.gather(
            *(
                _read_optional(
                    camera,
                    member,
                    label=label,
                    unsupported_members=unsupported_members,
                )
                for member, label in camera_members
            )
        ),
        asyncio.sleep(0, result=[])
        if telescope is None
        else asyncio.gather(
            *(
                _read_optional(
                    telescope,
                    member,
                    label=label,
                    unsupported_members=unsupported_members,
                )
                for member, label in telescope_members
            )
        ),
        asyncio.gather(
            asyncio.sleep(0, result=None)
            if focuser is None
            else _read_optional(
                focuser,
                "position",
                label="Focuser.Position",
                unsupported_members=unsupported_members,
            ),
            asyncio.sleep(0, result=None)
            if focuser is None
            else _read_optional(
                focuser,
                "temperature",
                label="Focuser.Temperature",
                unsupported_members=unsupported_members,
            ),
        ),
    )
    frame_bytes = await _download_image_array(camera)
    _log.info(
        "exposure timing frame=%s ready=%.3fs metadata+transfer=%.3fs total=%.3fs",
        frame_type.value,
        ready_elapsed_s,
        time.perf_counter() - transfer_started,
        time.perf_counter() - drive_started,
    )
    camera_value = dict(zip((member for member, _label in camera_members), camera_values, strict=True))
    gain = _as_int(camera_value["gain"], label="Camera.Gain")
    ccd_temp_c = _as_float(camera_value["ccdtemperature"], label="Camera.CCDTemperature")
    offset_x = _as_int(raw_bayer_x, label="Camera.BayerOffsetX")
    offset_y = _as_int(raw_bayer_y, label="Camera.BayerOffsetY")
    pixel_size_um = _as_float(camera_value["pixelsizex"], label="Camera.PixelSizeX")
    pixel_size_y_um = _as_float(camera_value["pixelsizey"], label="Camera.PixelSizeY")
    bin_x = _as_int(camera_value["binx"], label="Camera.BinX")
    bin_y = _as_int(camera_value["biny"], label="Camera.BinY")
    subframe_x = _as_int(camera_value["startx"], label="Camera.StartX")
    subframe_y = _as_int(camera_value["starty"], label="Camera.StartY")
    sensor_name = _as_str(camera_value["sensorname"], label="Camera.SensorName")
    camera_offset = _as_int(camera_value["offset"], label="Camera.Offset")
    max_adu = _as_float(camera_value["maxadu"], label="Camera.MaxADU")
    readout_mode = _as_int(camera_value["readoutmode"], label="Camera.ReadoutMode")
    # The device is asked first, because its own record of the exposure it
    # just took beats anything inferred. The S30 Pro's native Alpaca server
    # implements neither member (observed live, firmware behind the native
    # Alpaca surface, 2026-09-10), leaving both unknown.
    #
    # Falling back to SSC's own knowledge is not a guess: `started_at` is the
    # instant SSC commanded StartExposure, and `duration_s` is the length it
    # asked for. Omitting them instead is what a strict reading of "None means
    # unknown" produced, and it wrote a frame with no DATE-OBS and no
    # EXPOSURE -- which no plate solver will accept (SSC's own ASTAP seam
    # refuses it outright) and no stacker can calibrate. A frame that cannot
    # be used is not a safer outcome than one carrying SSC's own timing, so
    # the fallback is taken and its provenance recorded in the header.
    device_date_obs = _as_datetime(
        camera_value["lastexposurestarttime"],
        label="Camera.LastExposureStartTime",
    )
    device_exposure_s = _as_float(
        camera_value["lastexposureduration"],
        label="Camera.LastExposureDuration",
    )
    date_obs = device_date_obs if device_date_obs is not None else started_at
    exposure_s = device_exposure_s if device_exposure_s is not None else duration_s
    timing_from_ssc = device_date_obs is None or device_exposure_s is None

    ra_deg: float | None = None
    dec_deg: float | None = None
    site_lat_deg: float | None = None
    site_lon_deg: float | None = None
    alignment_mode: int | None = None
    device_focal_length_mm: float | None = None
    aperture_diameter_mm: float | None = None
    focal_ratio: float | None = None
    if telescope is not None:
        telescope_value = dict(
            zip((member for member, _label in telescope_members), telescope_values, strict=True)
        )
        ra_hours = _as_float(telescope_value.get("rightascension"), label="Telescope.RightAscension")
        native_dec_deg = _as_float(telescope_value.get("declination"), label="Telescope.Declination")
        equatorial_system = _as_int(
            telescope_value.get("equatorialsystem"), label="Telescope.EquatorialSystem"
        )
        site_lat_deg = _as_float(telescope_value["sitelatitude"], label="Telescope.SiteLatitude")
        site_lon_deg = _as_float(telescope_value["sitelongitude"], label="Telescope.SiteLongitude")
        ra_deg, dec_deg = _j2000_pointing(
            ra_hours=ra_hours,
            dec_deg=native_dec_deg,
            equatorial_system=equatorial_system,
            site_lat_deg=site_lat_deg,
            site_lon_deg=site_lon_deg,
            observed_at=now(),
            context=context,
        )
        alignment_mode = _as_int(telescope_value["alignmentmode"], label="Telescope.AlignmentMode")
        focal_length_m = _as_float(telescope_value["focallength"], label="Telescope.FocalLength")
        aperture_diameter_m = _as_float(
            telescope_value["aperturediameter"], label="Telescope.ApertureDiameter"
        )
        device_focal_length_mm = None if focal_length_m is None else focal_length_m * 1000.0
        aperture_diameter_mm = (
            None if aperture_diameter_m is None else aperture_diameter_m * 1000.0
        )
        resolved_focal_length_mm = (
            context.focal_length_mm
            if context.focal_length_mm is not None
            else device_focal_length_mm
        )
        if resolved_focal_length_mm is not None and aperture_diameter_mm not in {None, 0.0}:
            assert aperture_diameter_mm is not None
            focal_ratio = resolved_focal_length_mm / aperture_diameter_mm
    else:
        resolved_focal_length_mm = context.focal_length_mm

    # Site coordinates remain an independent complete pair. Pointing fallback
    # is handled inside ``_j2000_pointing`` because it must also cover unknown
    # and unsupported coordinate frames, not only missing numeric values.
    if (site_lat_deg is None or site_lon_deg is None) and (
        context.fallback_site_lat_deg is not None and context.fallback_site_lon_deg is not None
    ):
        site_lat_deg = context.fallback_site_lat_deg
        site_lon_deg = context.fallback_site_lon_deg
        _log.info("using canonical observing location for incomplete FITS site metadata")

    focuser_position_raw, focuser_temperature_raw = accessory_values
    focuser_position = _as_int(focuser_position_raw, label="Focuser.Position")
    focuser_temperature_c = _as_float(
        focuser_temperature_raw, label="Focuser.Temperature"
    )

    metadata = FrameMetadata(
        date_obs=date_obs,
        exposure_s=exposure_s,
        gain=gain,
        ccd_temp_c=ccd_temp_c,
        bayer_offset_x=offset_x,
        bayer_offset_y=offset_y,
        pixel_size_um=pixel_size_um,
        ra_deg=ra_deg,
        dec_deg=dec_deg,
        site_lat_deg=site_lat_deg,
        site_lon_deg=site_lon_deg,
        filter_name=filter_name,
        object_name=context.object_name,
        frame_type=frame_type,
        focal_length_mm=resolved_focal_length_mm,
        telescope_name=context.telescope_name,
        ssc_version=context.ssc_version,
        device_firmware=context.device_firmware,
        optic_id=context.optic_id,
        flat_source=context.flat_source,
        date_end=date_obs + timedelta(seconds=exposure_s),
        bin_x=bin_x,
        bin_y=bin_y,
        subframe_x=subframe_x,
        subframe_y=subframe_y,
        pixel_size_y_um=pixel_size_y_um,
        sensor_name=sensor_name,
        sensor_type=_as_int(camera_value["sensortype"], label="Camera.SensorType"),
        sensor_metadata_checked=True,
        camera_offset=camera_offset,
        max_adu=max_adu,
        readout_mode=readout_mode,
        focuser_position=focuser_position,
        focuser_temperature_c=focuser_temperature_c,
        alignment_mode=alignment_mode,
        aperture_diameter_mm=aperture_diameter_mm,
        focal_ratio=focal_ratio,
        wide_camera=context.wide_camera,
        total_exposure_s=exposure_s,
        timing_from_ssc=timing_from_ssc,
        dither_group_id=context.dither_group_id,
    )
    # ImageBytes is Array[NumX, NumY], materialised by the wire decoder as
    # array[x, y]. FITS/numpy image data is pixels[row, column] == pixels[y, x].
    # The Seestar app's own FITS confirms this boundary: Dimension1/NumX=2160
    # becomes NAXIS1=2160, Dimension2/NumY=3840 becomes NAXIS2=3840. Transpose
    # exactly once here, where a transport-shaped frame becomes a RawFrame.
    return RawFrame(pixels=frame_bytes.array.T, metadata=metadata), started_at


async def capture_one_exposure(
    *,
    camera: CameraDevice,
    telescope: Device | None,
    filterwheel: Device | None,
    focuser: Device | None = None,
    repo: CaptureRepository,
    folder: Path,
    duration_s: float,
    context: ExposureContext,
    frame_type: FrameType,
    folder_template: str,
    tz: ZoneInfo,
    filename_template: str = DEFAULT_CAPTURE_FILENAME_TEMPLATE,
    sequence: int = 0,
    run_id: str | None = None,
    block_id: str | None = None,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ready_timeout_s: float | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(tz=UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    started_at: datetime | None = None,
    on_stage: Callable[[CaptureStage], None] | None = None,
    unsupported_members: set[str] | None = None,
) -> int:
    """Command one exposure and catalogue it durably. Returns the new
    catalogue row's id.

    Nothing durable happens until the whole frame — pixels and metadata — is
    already assembled in memory; ``capture_frame`` then writes the row before
    the bytes (CLAUDE.md invariant 6). A kill at any point up to that call
    leaves no row and no file; a kill inside it leaves a ``'writing'`` row
    that ``ssc.capture.reconcile_incomplete`` resolves at the next startup.

    ``started_at``: forwarded to :func:`drive_exposure` unchanged — see its
    own docstring. ``None`` (the default) drives the whole exposure here.

    ``folder_template``/``tz``: forwarded unchanged to
    ``ssc.capture.pipeline.capture_frame``, which renders the templated
    subdirectory beneath ``folder`` (``ssc.capture.pathtemplate``).

    ``run_id``/``block_id``: forwarded unchanged to
    ``ssc.capture.pipeline.capture_frame`` — ``None``/``None`` (the default,
    every route caller) for a manual capture; a scheduled ``take_exposures``
    block's ``CapturePort`` adapter passes its own run/item id, which is what
    ``CaptureRepository.count_committed`` — the executor's resume truth —
    counts against.

    ``on_stage``: forwarded to :func:`drive_exposure` unchanged, plus one more
    call of its own — ``SAVING`` — right before the durable write below.
    """
    capture_started = time.perf_counter()
    frame, started_at = await drive_exposure(
        camera=camera,
        telescope=telescope,
        filterwheel=filterwheel,
        focuser=focuser,
        duration_s=duration_s,
        context=context,
        frame_type=frame_type,
        poll_interval_s=poll_interval_s,
        ready_timeout_s=ready_timeout_s,
        now=now,
        sleep=sleep,
        started_at=started_at,
        on_stage=on_stage,
        unsupported_members=unsupported_members,
    )
    if on_stage is not None:
        on_stage(CaptureStage.SAVING)
    save_started = time.perf_counter()
    frame_id = await capture_frame(
        repo=repo,
        folder=folder,
        frame=frame,
        date_obs=started_at,
        template=folder_template,
        tz=tz,
        filename_template=filename_template,
        sequence=sequence,
        run_id=run_id,
        block_id=block_id,
        now=now(),
    )
    _log.info(
        "capture timing frame=%s acquire=%.3fs save=%.3fs total=%.3fs",
        frame_type.value,
        save_started - capture_started,
        time.perf_counter() - save_started,
        time.perf_counter() - capture_started,
    )
    return frame_id


__all__ = [
    "CameraDevice",
    "CaptureMetadataError",
    "Device",
    "ExposureContext",
    "ExposureNotAvailableError",
    "ExposureTimeoutError",
    "begin_exposure",
    "capture_one_exposure",
    "drive_exposure",
    "exposure_metadata_fallbacks",
    "light_flag",
]
