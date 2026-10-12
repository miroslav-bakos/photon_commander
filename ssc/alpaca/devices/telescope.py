# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""ASCOM ``ITelescopeV3`` over the Seestar mount.

What the mount can and cannot do, as observed on firmware 8.46:

===========================  ==========================================
ASCOM member                 Seestar reality
===========================  ==========================================
``SlewToCoordinatesAsync``   ``start_auto_goto`` — verified on hardware.
                             RA in hours, dec in degrees, ``is_j2000``
                             flag. The units already agree with ASCOM;
                             the conversion is still written out.
``AbortSlew``                ``stop_auto_goto`` — verified.
``Slewing``                  ``mount.move_type == "ScopeGoto"``.
``Tracking`` (read)          ``mount.tracking``.
``AtPark``                   ``mount.close`` — the arm stow state.
``AlignmentMode``            ``mount.equ_mode``.
``RightAscension``/``Dec``   **No method exists.** Every ``scope_get_*``
                             name returns 103 on 8.46, so there is no way
                             to read where the mount is pointing.
``Park``/``Unpark``          **No verb exists.** Fourteen candidate names
                             all returned 103. ``AtPark`` is readable and
                             ``Park`` is not — an odd pair, and the honest
                             one.
===========================  ==========================================

The slew precondition matters: ``start_auto_goto`` answers **210** ("not
supported", not "not found") unless the device is in star view mode with the arm
open. That is mapped to ``InvalidOperation``, and this driver does **not**
silently issue ``iscope_start_view`` on the client's behalf — a hidden side
effect that opens the arm and starts the imaging pipeline is not something a
slew request consents to. Clients call the ``ssc.start_view`` action first.

**On 9.16+, every table entry above is superseded by a direct pass-through** to
the device's own ``telescope/0`` (``prefer_native_getters``/``prefer_native_setters``
in ``ssc/alpaca/devices/base.py``) — the mount answers its *own*
``RightAscension``, ``Park``/``Unpark``, ``MoveAxis`` and the rest, which this
8.46 mapping could never do at all. Measured 9.31 (``docs/M0-FINDINGS.md``),
against this file's own reasoning above:

* ``CanPark``/``CanUnpark``/``CanFindHome``/``CanSync`` are all true; ``CanSlewAltAz``
  and ``CanSetPierSide`` are false. Pass-through reports whatever the device
  currently says, never this module's 8.46-only constants.
* **``Park`` refuses below the horizon** with a device error (``[1279]``, which
  is ASCOM's own ``0x4FF`` in decimal — see ``translate_native_error`` in
  ``ssc/alpaca/backend.py``); ``FindHome`` recovers it. This driver surfaces
  that refusal rather than swallowing it or retrying on the client's behalf —
  a silent retry here would hide a park failure from whatever *does* decide to
  retry (``ssc.shutdown``, owned by ``scheduler-engine``).
* **``Unpark`` alone moves nothing, by design** — it only permits motion; the
  arm rises on the next command that actually moves the mount (``MoveAxis`` or
  a slew). Do not read a still ``Altitude`` after ``Unpark`` as a failure.
* **``EquatorialSystem`` reads 1 (topocentric-of-date), not J2000**, and
  ``AlignmentMode`` reads *polar* on a physically alt-az mount. Both are
  reported exactly as the device states them; this driver performs no
  precession or frame conversion of its own, and no member here infers the
  physical mount configuration from ``AlignmentMode``.
* ``CanMoveAxis`` is true for axes 0 and 1, false for axis 2; ``AxisRates`` is
  ``{Minimum: 0, Maximum: 6}`` deg/s on both moving axes — enforced by the
  device itself (a `Rate` outside it is refused), not re-validated here.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import ClassVar, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ssc.alpaca import actions
from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.capabilities import unavailable_reason
from ssc.alpaca.devices.base import (
    AlpacaDevice,
    Getter,
    Setter,
    const,
    unimplemented,
    unimplemented_put,
)
from ssc.alpaca.errors import InvalidValueError, ValueNotSetError
from ssc.alpaca.wire import RequestParams
from ssc.astro.events import LocalTimeKind, local_time_kind
from ssc.seestar.models import MountState

_log = logging.getLogger(__name__)

#: ``mount.move_type`` while a goto is running. Observed transition on hardware:
#: ``"ScopeGoto"`` at t+3.8 s, back to ``"none"`` at t+7.2 s.
MOVE_TYPE_SLEWING: Final = "ScopeGoto"
MOVE_TYPE_IDLE: Final = "none"

#: ASCOM ``AlignmentModes``.
ALIGNMENT_ALT_AZ: Final = 0
ALIGNMENT_POLAR: Final = 1

#: ASCOM ``EquatorialCoordinateType``: ``equJ2000``. SSC sends ``is_j2000: true``.
EQUATORIAL_J2000: Final = 2

#: ASCOM ``DriveRates``: ``driveSidereal``.
DRIVE_SIDEREAL: Final = 0

#: How long after a slew is accepted the driver keeps reporting ``Slewing`` while
#: waiting for ``move_type`` to catch up. The device took 3.8 s to publish
#: ``ScopeGoto``; ASCOM requires ``Slewing`` to be true as soon as the async call
#: returns. Beyond this grace the latch gives up and reports the device's own
#: state, because a latch that never expires is a lie with a timer on it.
SLEW_LATCH_GRACE_S: Final = 15.0

#: JD of the J2000.0 epoch, and the Unix-epoch JD used to convert from a POSIX
#: timestamp. GMST follows the IAU 1982 mean expression, good to well under a
#: second over this driver's lifetime.
_JD_J2000: Final = 2451545.0
_JD_UNIX_EPOCH: Final = 2440587.5
_GMST_AT_J2000_HOURS: Final = 18.697374558
_GMST_HOURS_PER_DAY: Final = 24.06570982441908

Clock = Callable[[], float]


def sidereal_hours(when: datetime, longitude_deg: float) -> float:
    """Local apparent-mean sidereal time in hours, for ``longitude_deg`` east."""
    if when.tzinfo is None:
        raise ValueError("sidereal time needs an aware UTC datetime")
    julian_day = when.timestamp() / 86400.0 + _JD_UNIX_EPOCH
    days = julian_day - _JD_J2000
    gmst = _GMST_AT_J2000_HOURS + _GMST_HOURS_PER_DAY * days
    return (gmst + longitude_deg / 15.0) % 24.0


class _SlewLatch:
    """Bridges the gap between "the device accepted the slew" and ``move_type``.

    Reports slewing from the moment the command is accepted until the device has
    both shown ``ScopeGoto`` and returned to ``none`` — or until the grace period
    expires without the device ever moving, at which point it stops claiming
    anything and lets the device speak for itself.
    """

    def __init__(self, *, grace_s: float = SLEW_LATCH_GRACE_S, clock: Clock = time.monotonic):
        self._grace_s = grace_s
        self._clock = clock
        self._armed_at: float | None = None
        self._saw_motion = False

    def arm(self) -> None:
        self._armed_at = self._clock()
        self._saw_motion = False

    def clear(self) -> None:
        self._armed_at = None
        self._saw_motion = False

    def slewing(self, move_type: str) -> bool:
        moving = move_type == MOVE_TYPE_SLEWING
        if moving:
            self._saw_motion = True
            return True
        if self._armed_at is None:
            return False
        if self._saw_motion:
            self.clear()
            return False
        if self._clock() - self._armed_at >= self._grace_s:
            _log.warning(
                "slew was accepted but mount.move_type never reached %r within %.0fs; "
                "reporting the device's own state from now on",
                MOVE_TYPE_SLEWING,
                self._grace_s,
            )
            self.clear()
            return False
        return True


class TelescopeDevice(AlpacaDevice):
    """The mount, as much of it as firmware 8.46 actually exposes."""

    device_type: ClassVar[str] = "telescope"
    interface_version: ClassVar[int] = 3
    default_name: ClassVar[str] = "Photon Commander Seestar Mount"
    default_description: ClassVar[str] = "ZWO Seestar mount via Photon Commander"

    def __init__(
        self,
        backend: SeestarBackend,
        *,
        device_number: int = 0,
        name: str | None = None,
        unique_id: str | None = None,
        slew_latch_grace_s: float = SLEW_LATCH_GRACE_S,
        clock: Clock = time.monotonic,
    ) -> None:
        self._latch = _SlewLatch(grace_s=slew_latch_grace_s, clock=clock)
        self._target_ra_hours: float | None = None
        self._target_dec_degrees: float | None = None
        self._site_latitude: float | None = None
        self._site_longitude: float | None = None
        self._site_elevation: float | None = None
        super().__init__(backend, device_number=device_number, name=name, unique_id=unique_id)

    # -- device reads -------------------------------------------------------- #

    async def _mount(self) -> MountState:
        state = await self.backend.device_state()
        if state.mount is None:
            raise ValueNotSetError(
                "the device did not return a 'mount' section, so its state is unknown"
            )
        return state.mount

    async def _utc_now(self) -> datetime:
        """The device's own clock, converted to UTC.

        ``pi_get_time`` reports local civil time plus an IANA zone name. A local
        time that is ambiguous (the repeated hour at a DST fall-back) or
        nonexistent (the skipped hour at spring-forward) is reported as unknown
        rather than resolved by luck — ``CLAUDE.md`` invariant 5.
        """
        payload = await self.backend.call("pi_get_time", cache=True)
        if not isinstance(payload, dict):
            raise ValueNotSetError(f"pi_get_time returned {type(payload).__name__}, not an object")
        try:
            zone_name = str(payload["time_zone"])
            parts = [int(payload[key]) for key in ("year", "mon", "day", "hour", "min", "sec")]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueNotSetError(
                f"pi_get_time did not carry a complete local time and zone ({exc})"
            ) from exc
        try:
            zone = ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueNotSetError(
                f"the device reported timezone {zone_name!r}, which this host cannot resolve"
            ) from exc
        year, month, day, hour, minute, second = parts
        try:
            local = datetime(year, month, day, hour, minute, second, tzinfo=zone)
        except ValueError as exc:
            raise ValueNotSetError(f"the device reported an impossible local time ({exc})") from exc
        naive_local = local.replace(tzinfo=None)
        # ``local_time_kind`` orders the fold-0/fold-1 offsets rather than just
        # comparing them for inequality, which is what actually tells ambiguous
        # (fall-back) apart from nonexistent (spring-forward) — see its
        # docstring in ssc/astro/events.py. A plain "!=" is true for both kinds
        # and would misdiagnose a spring-forward gap as a fall-back repeat.
        kind = local_time_kind(naive_local, zone)
        if kind is LocalTimeKind.AMBIGUOUS:
            raise ValueNotSetError(
                f"the device's local time {naive_local.isoformat()} is ambiguous in {zone_name} "
                "(a DST fall-back repeats it); SSC will not guess which one it meant"
            )
        if kind is LocalTimeKind.NONEXISTENT:
            raise ValueNotSetError(
                f"the device's local time {naive_local.isoformat()} does not exist in {zone_name} "
                "(a DST spring-forward skips it)"
            )
        return local.astimezone(UTC)

    # -- getters ------------------------------------------------------------- #

    def build_getters(self) -> dict[str, Getter]:
        no_coordinate_feed = (
            "firmware 8.46 exposes no method that reports where the mount is "
            "pointing — scope_get_equ_coord, scope_get_ra_dec and "
            "scope_get_horiz_coord all return 103. Use the ssc.solve_result action "
            "for a plate-solved position"
        )
        getters: dict[str, Getter] = {
            "alignmentmode": self._get_alignment_mode,
            "altitude": unimplemented("Telescope.Altitude", no_coordinate_feed),
            "aperturearea": self._get_aperture_area,
            "aperturediameter": self._get_aperture_diameter,
            "athome": const(False),
            "atpark": self._get_at_park,
            "azimuth": unimplemented("Telescope.Azimuth", no_coordinate_feed),
            "canfindhome": const(False),
            "canmoveaxis": self._get_can_move_axis,
            "canpark": const(False),
            "canpulseguide": const(False),
            "cansetdeclinationrate": const(False),
            "cansetguiderates": const(False),
            "cansetpark": const(False),
            "cansetpierside": const(False),
            "cansetrightascensionrate": const(False),
            "cansettracking": const(False),
            "canslew": const(False),
            "canslewaltaz": const(False),
            "canslewaltazasync": const(False),
            "canslewasync": const(True),
            "cansync": const(False),
            "cansyncaltaz": const(False),
            "canunpark": const(False),
            "declination": unimplemented("Telescope.Declination", no_coordinate_feed),
            "declinationrate": const(0.0),
            "destinationsideofpier": unimplemented(
                "Telescope.DestinationSideOfPier",
                "the Seestar is not a German equatorial mount and has no pier side",
            ),
            "doesrefraction": unimplemented(
                "Telescope.DoesRefraction",
                "the device does not report whether it applies a refraction model",
            ),
            "equatorialsystem": const(EQUATORIAL_J2000),
            "focallength": self._get_focal_length,
            "guideratedeclination": unimplemented(
                "Telescope.GuideRateDeclination", "the device exposes no pulse-guide interface"
            ),
            "guideraterightascension": unimplemented(
                "Telescope.GuideRateRightAscension", "the device exposes no pulse-guide interface"
            ),
            "ispulseguiding": unimplemented(
                "Telescope.IsPulseGuiding", "the device exposes no pulse-guide interface"
            ),
            "rightascension": unimplemented("Telescope.RightAscension", no_coordinate_feed),
            "rightascensionrate": const(0.0),
            "sideofpier": unimplemented(
                "Telescope.SideOfPier",
                "the Seestar is not a German equatorial mount and has no pier side",
            ),
            "siderealtime": self._get_sidereal_time,
            "siteelevation": self._get_site_elevation,
            "sitelatitude": self._get_site_latitude,
            "sitelongitude": self._get_site_longitude,
            "slewing": self._get_slewing,
            "slewsettletime": const(0),
            "targetdeclination": self._get_target_declination,
            "targetrightascension": self._get_target_right_ascension,
            "tracking": self._get_tracking,
            "trackingrate": const(DRIVE_SIDEREAL),
            "trackingrates": const([DRIVE_SIDEREAL]),
            "utcdate": self._get_utc_date,
            "axisrates": self._get_axis_rates,
        }
        return self.prefer_native_getters(getters)

    async def _get_alignment_mode(self, _: RequestParams) -> object:
        mount = await self._mount()
        if mount.equ_mode is None:
            raise ValueNotSetError("the device did not report mount.equ_mode")
        return ALIGNMENT_POLAR if mount.equ_mode else ALIGNMENT_ALT_AZ

    async def _get_at_park(self, _: RequestParams) -> object:
        mount = await self._mount()
        if mount.close is None:
            raise ValueNotSetError("the device did not report mount.close (the arm stow state)")
        return mount.close

    async def _get_slewing(self, _: RequestParams) -> object:
        mount = await self._mount()
        if mount.move_type is None:
            raise ValueNotSetError("the device did not report mount.move_type")
        return self._latch.slewing(mount.move_type)

    async def _get_tracking(self, _: RequestParams) -> object:
        mount = await self._mount()
        if mount.tracking is None:
            raise ValueNotSetError("the device did not report mount.tracking")
        return mount.tracking

    async def _optics(self) -> tuple[float, float]:
        """Focal length in mm and f-number, both as the device reports them."""
        state = await self.backend.device_state()
        identity = state.device
        if identity is None or identity.focal_length_mm is None or identity.f_number is None:
            raise ValueNotSetError(
                "the device did not report focal_len and fnumber, so the optics are unknown"
            )
        if identity.f_number <= 0.0:
            raise ValueNotSetError(
                f"the device reported fnumber {identity.f_number}, which is not usable"
            )
        return identity.focal_length_mm, identity.f_number

    async def _get_focal_length(self, _: RequestParams) -> object:
        focal_mm, _f = await self._optics()
        return focal_mm / 1000.0

    async def _get_aperture_diameter(self, _: RequestParams) -> object:
        focal_mm, f_number = await self._optics()
        return focal_mm / f_number / 1000.0

    async def _get_aperture_area(self, _: RequestParams) -> object:
        focal_mm, f_number = await self._optics()
        radius_m = focal_mm / f_number / 2000.0
        return math.pi * radius_m * radius_m

    async def _get_site_latitude(self, _: RequestParams) -> object:
        return self._require_site(self._site_latitude, "SiteLatitude")

    async def _get_site_longitude(self, _: RequestParams) -> object:
        return self._require_site(self._site_longitude, "SiteLongitude")

    async def _get_site_elevation(self, _: RequestParams) -> object:
        return self._require_site(self._site_elevation, "SiteElevation")

    @staticmethod
    def _require_site(value: float | None, member: str) -> float:
        if value is None:
            raise ValueNotSetError(
                f"{member} has not been set. The device exposes no location method "
                "(get_user_location returns 103), so the observing site is the "
                "client's to supply"
            )
        return value

    async def _get_sidereal_time(self, _: RequestParams) -> object:
        longitude = self._require_site(self._site_longitude, "SiteLongitude")
        return sidereal_hours(await self._utc_now(), longitude)

    async def _get_utc_date(self, _: RequestParams) -> object:
        return (await self._utc_now()).isoformat().replace("+00:00", "Z")

    async def _get_target_right_ascension(self, _: RequestParams) -> object:
        if self._target_ra_hours is None:
            raise ValueNotSetError("TargetRightAscension has not been set")
        return self._target_ra_hours

    async def _get_target_declination(self, _: RequestParams) -> object:
        if self._target_dec_degrees is None:
            raise ValueNotSetError("TargetDeclination has not been set")
        return self._target_dec_degrees

    async def _get_axis_rates(self, params: RequestParams) -> object:
        _validate_axis(params)
        return []

    async def _get_can_move_axis(self, params: RequestParams) -> object:
        _validate_axis(params)
        return False

    # -- setters ------------------------------------------------------------- #

    def build_setters(self) -> dict[str, Setter]:
        no_park_verb = unavailable_reason("scope_park")
        setters: dict[str, Setter] = {
            "abortslew": self._put_abort_slew,
            "declinationrate": unimplemented_put(
                "Telescope.DeclinationRate", "the device exposes no offset tracking rate"
            ),
            "doesrefraction": unimplemented_put(
                "Telescope.DoesRefraction", "the device's refraction model is not controllable"
            ),
            "findhome": unimplemented_put(
                "Telescope.FindHome", "the device has no home position and no verb to seek one"
            ),
            "moveaxis": unimplemented_put(
                "Telescope.MoveAxis",
                "scope_speed_move returns 103 on firmware 8.46; there is no rate-based motion verb",
            ),
            "park": unimplemented_put("Telescope.Park", no_park_verb),
            "pulseguide": unimplemented_put(
                "Telescope.PulseGuide", "the device exposes no pulse-guide interface"
            ),
            "rightascensionrate": unimplemented_put(
                "Telescope.RightAscensionRate", "the device exposes no offset tracking rate"
            ),
            "setpark": unimplemented_put("Telescope.SetPark", no_park_verb),
            "sideofpier": unimplemented_put(
                "Telescope.SideOfPier", "the Seestar has no pier side to set"
            ),
            "siteelevation": self._put_site_elevation,
            "sitelatitude": self._put_site_latitude,
            "sitelongitude": self._put_site_longitude,
            "slewsettletime": unimplemented_put(
                "Telescope.SlewSettleTime",
                "the device settles on its own and SSC adds no delay of its own",
            ),
            "slewtoaltaz": unimplemented_put(
                "Telescope.SlewToAltAz", "the device has no horizon-coordinate goto verb"
            ),
            "slewtoaltazasync": unimplemented_put(
                "Telescope.SlewToAltAzAsync", "the device has no horizon-coordinate goto verb"
            ),
            "slewtocoordinates": unimplemented_put(
                "Telescope.SlewToCoordinates",
                "start_auto_goto is asynchronous; use SlewToCoordinatesAsync and poll "
                "Slewing rather than blocking a request on the mount",
            ),
            "slewtocoordinatesasync": self._put_slew_to_coordinates_async,
            "slewtotarget": unimplemented_put(
                "Telescope.SlewToTarget", "use SlewToTargetAsync and poll Slewing"
            ),
            "slewtotargetasync": self._put_slew_to_target_async,
            "synctoaltaz": unimplemented_put(
                "Telescope.SyncToAltAz", "the device has no sync verb"
            ),
            "synctocoordinates": unimplemented_put(
                "Telescope.SyncToCoordinates",
                "scope_sync returns 103 on firmware 8.46; the device syncs itself by plate solving",
            ),
            "synctotarget": unimplemented_put(
                "Telescope.SyncToTarget", "the device has no sync verb"
            ),
            "targetdeclination": self._put_target_declination,
            "targetrightascension": self._put_target_right_ascension,
            "tracking": unimplemented_put(
                "Telescope.Tracking",
                "the device reports mount.tracking but exposes no verb to change it",
            ),
            "trackingrate": unimplemented_put(
                "Telescope.TrackingRate", "the device tracks at the sidereal rate only"
            ),
            "unpark": unimplemented_put("Telescope.Unpark", no_park_verb),
            "utcdate": unimplemented_put(
                "Telescope.UTCDate", "no verified verb sets the device clock"
            ),
        }
        return self.prefer_native_setters(setters)

    async def _put_abort_slew(self, _: RequestParams) -> object:
        await actions.stop_auto_goto(self.backend)
        self._latch.clear()
        return None

    async def _put_slew_to_coordinates_async(self, params: RequestParams) -> object:
        ra_hours = params.as_float("RightAscension")
        dec_degrees = params.as_float("Declination")
        await self._slew(ra_hours, dec_degrees)
        self._target_ra_hours = ra_hours
        self._target_dec_degrees = dec_degrees
        return None

    async def _put_slew_to_target_async(self, _: RequestParams) -> object:
        if self._target_ra_hours is None or self._target_dec_degrees is None:
            raise ValueNotSetError(
                "SlewToTargetAsync needs TargetRightAscension and TargetDeclination to "
                "have been set first"
            )
        await self._slew(self._target_ra_hours, self._target_dec_degrees)
        return None

    async def _slew(self, ra_hours: float, dec_degrees: float) -> None:
        """Issue the goto and latch ``Slewing``. Returns as soon as it is accepted."""
        await actions.start_auto_goto(
            self.backend,
            ra_hours=ra_hours,
            dec_degrees=dec_degrees,
            target_name=f"SSC {ra_hours:.5f}h {dec_degrees:+.5f}d",
            is_j2000=True,
        )
        self._latch.arm()

    async def _put_target_right_ascension(self, params: RequestParams) -> object:
        value = params.as_float("TargetRightAscension")
        if not 0.0 <= value < 24.0:
            raise InvalidValueError(f"TargetRightAscension must be in [0, 24) hours, got {value}")
        self._target_ra_hours = value
        return None

    async def _put_target_declination(self, params: RequestParams) -> object:
        value = params.as_float("TargetDeclination")
        if not -90.0 <= value <= 90.0:
            raise InvalidValueError(f"TargetDeclination must be in [-90, 90] degrees, got {value}")
        self._target_dec_degrees = value
        return None

    async def _put_site_latitude(self, params: RequestParams) -> object:
        value = params.as_float("SiteLatitude")
        if not -90.0 <= value <= 90.0:
            raise InvalidValueError(f"SiteLatitude must be in [-90, 90] degrees, got {value}")
        self._site_latitude = value
        return None

    async def _put_site_longitude(self, params: RequestParams) -> object:
        value = params.as_float("SiteLongitude")
        if not -180.0 <= value <= 180.0:
            raise InvalidValueError(f"SiteLongitude must be in [-180, 180] degrees, got {value}")
        self._site_longitude = value
        return None

    async def _put_site_elevation(self, params: RequestParams) -> object:
        value = params.as_float("SiteElevation")
        if not -300.0 <= value <= 10000.0:
            raise InvalidValueError(f"SiteElevation must be in [-300, 10000] metres, got {value}")
        self._site_elevation = value
        return None


def _validate_axis(params: RequestParams) -> int:
    axis = params.as_int("Axis")
    if axis not in (0, 1, 2):
        raise InvalidValueError(
            f"Axis must be 0, 1 or 2 (primary, secondary, tertiary), got {axis}"
        )
    return axis


__all__ = [
    "ALIGNMENT_ALT_AZ",
    "ALIGNMENT_POLAR",
    "EQUATORIAL_J2000",
    "MOVE_TYPE_IDLE",
    "MOVE_TYPE_SLEWING",
    "SLEW_LATCH_GRACE_S",
    "TelescopeDevice",
    "sidereal_hours",
]
