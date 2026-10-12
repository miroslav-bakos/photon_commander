"""The mount mapping — including the parts that honestly do not work."""

from __future__ import annotations

import json
import math
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from ssc.alpaca.devices.telescope import (
    ALIGNMENT_ALT_AZ,
    ALIGNMENT_POLAR,
    EQUATORIAL_J2000,
    MOVE_TYPE_SLEWING,
    _SlewLatch,
    sidereal_hours,
)
from ssc.alpaca.errors import ErrorNumber
from ssc.seestar.emulator import SeestarEmulator
from tests.alpaca.conftest import (
    action,
    api_get,
    api_put,
    error_number,
    override_mount,
    put_error_number,
    record_calls,
    value,
)

STAR_VIEW = '{"mode":"star"}'


async def test_mount_state_drives_alignment_park_and_tracking(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    assert await value(linked, "telescope", "alignmentmode") == ALIGNMENT_POLAR
    assert await value(linked, "telescope", "atpark") is True  # mount.close: arm stowed
    assert await value(linked, "telescope", "tracking") is False
    assert await value(linked, "telescope", "slewing") is False

    override_mount(emulator, equ_mode=False, close=False, tracking=True)
    assert await value(linked, "telescope", "alignmentmode") == ALIGNMENT_ALT_AZ
    assert await value(linked, "telescope", "atpark") is False
    assert await value(linked, "telescope", "tracking") is True


async def test_slewing_follows_move_type(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, move_type=MOVE_TYPE_SLEWING)
    assert await value(linked, "telescope", "slewing") is True
    override_mount(emulator, move_type="none")
    assert await value(linked, "telescope", "slewing") is False


async def test_an_unreported_mount_field_is_valuenotset_not_a_guess(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, move_type=None, close=None)
    assert await error_number(linked, "telescope", "slewing") == ErrorNumber.VALUE_NOT_SET
    assert await error_number(linked, "telescope", "atpark") == ErrorNumber.VALUE_NOT_SET


async def test_there_is_no_coordinate_feed_on_this_firmware(linked: httpx.AsyncClient) -> None:
    for member in ("rightascension", "declination", "altitude", "azimuth"):
        assert await error_number(linked, "telescope", member) == ErrorNumber.NOT_IMPLEMENTED, (
            member
        )


async def test_park_and_unpark_are_notimplemented_although_atpark_reads(
    linked: httpx.AsyncClient,
) -> None:
    assert await value(linked, "telescope", "canpark") is False
    assert await value(linked, "telescope", "canunpark") is False
    assert isinstance(await value(linked, "telescope", "atpark"), bool)
    for member in ("park", "unpark", "setpark", "findhome"):
        assert await put_error_number(linked, "telescope", member) == ErrorNumber.NOT_IMPLEMENTED, (
            member
        )


async def test_the_capability_flags_admit_what_is_missing(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "telescope", "canslewasync") is True
    for member in (
        "canslew",
        "cansync",
        "cansyncaltaz",
        "canslewaltaz",
        "canslewaltazasync",
        "cansettracking",
        "canpulseguide",
        "cansetpierside",
        "canfindhome",
    ):
        assert await value(linked, "telescope", member) is False, member
    assert await value(linked, "telescope", "equatorialsystem") == EQUATORIAL_J2000


# --------------------------------------------------------------------------- #
# The slew path
# --------------------------------------------------------------------------- #


async def test_a_slew_from_a_stowed_arm_says_what_the_user_must_do(
    linked: httpx.AsyncClient,
) -> None:
    """No firmware 8.46 verb raises the arm, so the driver says so itself.

    Both a stowed arm and a missing star view make the device answer 210. Only
    one of them is actionable, and it is actionable by the person, not the
    driver — so it is reported in words rather than as a bare "not supported".
    """
    response = await api_put(
        linked, "telescope", "slewtocoordinatesasync", RightAscension=5.5, Declination=-5.4
    )
    body = response.json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_OPERATION
    assert "arm is stowed" in body["ErrorMessage"]
    assert "Raise the arm on the device" in body["ErrorMessage"]


async def test_a_slew_without_star_view_is_invalidoperation_not_notimplemented(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    """Device code 210 means "exists but unavailable now" — never 0x400.

    Reading 210 as NotImplemented would permanently disable a verb that works.
    """
    override_mount(emulator, close=False)  # the user raised the arm from the app
    response = await api_put(
        linked, "telescope", "slewtocoordinatesasync", RightAscension=5.5, Declination=-5.4
    )
    body = response.json()
    assert body["ErrorNumber"] == ErrorNumber.INVALID_OPERATION
    assert "210" in body["ErrorMessage"]
    assert "star view" in body["ErrorMessage"]


async def test_a_slew_from_star_view_reaches_the_wire_in_hours_and_degrees(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, close=False)  # the user raised the arm from the app
    calls = record_calls(emulator, "start_auto_goto")
    assert (await action(linked, "ssc.start_view", STAR_VIEW)).json()["ErrorNumber"] == 0

    response = await api_put(
        linked, "telescope", "slewtocoordinatesasync", RightAscension=5.5, Declination=-5.4
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ErrorNumber"] == 0, body
    assert "Value" not in body, "an async slew returns no value"

    assert len(calls) == 1
    call = calls[0]
    assert call["ra"] == pytest.approx(5.5), "RA goes on the wire in hours"
    assert call["dec"] == pytest.approx(-5.4), "dec goes on the wire in degrees"
    assert call["is_j2000"] is True
    assert isinstance(call["target_name"], str)

    # ASCOM: Slewing is true as soon as the async call returns.
    assert await value(linked, "telescope", "slewing") is True

    # The commanded target is remembered; the actual position still is not known.
    assert await value(linked, "telescope", "targetrightascension") == pytest.approx(5.5)
    assert await error_number(linked, "telescope", "rightascension") == ErrorNumber.NOT_IMPLEMENTED


async def test_abortslew_stops_the_goto_and_clears_the_latch(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, close=False)
    stops = record_calls(emulator, "stop_auto_goto")
    await action(linked, "ssc.start_view", STAR_VIEW)
    await api_put(
        linked, "telescope", "slewtocoordinatesasync", RightAscension=1.0, Declination=1.0
    )
    assert await value(linked, "telescope", "slewing") is True

    assert (await api_put(linked, "telescope", "abortslew")).json()["ErrorNumber"] == 0
    assert len(stops) == 1
    assert await value(linked, "telescope", "slewing") is False


@pytest.mark.parametrize(("ra", "dec"), [(24.0, 0.0), (-0.1, 0.0), (0.0, 90.1), (0.0, -90.1)])
async def test_out_of_range_slew_coordinates_are_invalidvalue(
    linked: httpx.AsyncClient, emulator: SeestarEmulator, ra: float, dec: float
) -> None:
    override_mount(emulator, close=False)
    calls = record_calls(emulator, "start_auto_goto")
    await action(linked, "ssc.start_view", STAR_VIEW)
    number = await put_error_number(
        linked, "telescope", "slewtocoordinatesasync", RightAscension=ra, Declination=dec
    )
    assert number == ErrorNumber.INVALID_VALUE
    assert calls == [], "an out-of-range coordinate never reaches the mount"


async def test_slewtotargetasync_needs_a_target_first(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    override_mount(emulator, close=False)
    calls = record_calls(emulator, "start_auto_goto")
    await action(linked, "ssc.start_view", STAR_VIEW)
    assert (
        await put_error_number(linked, "telescope", "slewtotargetasync")
        == ErrorNumber.VALUE_NOT_SET
    )
    await api_put(linked, "telescope", "targetrightascension", TargetRightAscension=3.0)
    await api_put(linked, "telescope", "targetdeclination", TargetDeclination=45.0)
    assert (await api_put(linked, "telescope", "slewtotargetasync")).json()["ErrorNumber"] == 0
    assert calls[0]["ra"] == pytest.approx(3.0)
    assert calls[0]["dec"] == pytest.approx(45.0)


async def test_synchronous_slew_members_refuse_rather_than_block(
    linked: httpx.AsyncClient,
) -> None:
    for member in ("slewtocoordinates", "slewtotarget", "slewtoaltaz", "slewtoaltazasync"):
        assert (
            await put_error_number(
                linked,
                "telescope",
                member,
                RightAscension=1.0,
                Declination=1.0,
                Azimuth=1.0,
                Altitude=1.0,
            )
            == ErrorNumber.NOT_IMPLEMENTED
        ), member


async def test_sync_and_tracking_writes_are_notimplemented(linked: httpx.AsyncClient) -> None:
    for member in ("synctocoordinates", "synctotarget", "synctoaltaz"):
        assert (
            await put_error_number(
                linked,
                "telescope",
                member,
                RightAscension=1.0,
                Declination=1.0,
                Azimuth=1.0,
                Altitude=1.0,
            )
            == ErrorNumber.NOT_IMPLEMENTED
        ), member
    assert (
        await put_error_number(linked, "telescope", "tracking", Tracking=True)
        == ErrorNumber.NOT_IMPLEMENTED
    )


# --------------------------------------------------------------------------- #
# Target and site
# --------------------------------------------------------------------------- #


async def test_target_coordinates_round_trip_and_validate(linked: httpx.AsyncClient) -> None:
    assert (
        await error_number(linked, "telescope", "targetrightascension") == ErrorNumber.VALUE_NOT_SET
    )
    await api_put(linked, "telescope", "targetrightascension", TargetRightAscension=12.25)
    assert await value(linked, "telescope", "targetrightascension") == pytest.approx(12.25)
    assert (
        await put_error_number(linked, "telescope", "targetdeclination", TargetDeclination=91)
        == ErrorNumber.INVALID_VALUE
    )


async def test_the_site_is_the_clients_to_supply(linked: httpx.AsyncClient) -> None:
    assert await error_number(linked, "telescope", "sitelatitude") == ErrorNumber.VALUE_NOT_SET
    await api_put(linked, "telescope", "sitelatitude", SiteLatitude=48.15)
    await api_put(linked, "telescope", "sitelongitude", SiteLongitude=17.11)
    await api_put(linked, "telescope", "siteelevation", SiteElevation=140.0)
    assert await value(linked, "telescope", "sitelatitude") == pytest.approx(48.15)
    assert await value(linked, "telescope", "sitelongitude") == pytest.approx(17.11)
    assert await value(linked, "telescope", "siteelevation") == pytest.approx(140.0)
    assert (
        await put_error_number(linked, "telescope", "sitelatitude", SiteLatitude=95)
        == ErrorNumber.INVALID_VALUE
    )


async def test_a_non_numeric_parameter_is_a_malformed_request(linked: httpx.AsyncClient) -> None:
    response = await api_put(linked, "telescope", "sitelatitude", SiteLatitude="north")
    assert response.status_code == 400


# --------------------------------------------------------------------------- #
# Optics, clock and sidereal time
# --------------------------------------------------------------------------- #


async def test_optics_are_derived_from_what_the_device_reports(
    linked: httpx.AsyncClient,
) -> None:
    # focal_len 160.0 mm with fnumber 5.0 -> a 32 mm aperture.
    assert await value(linked, "telescope", "focallength") == pytest.approx(0.160)
    assert await value(linked, "telescope", "aperturediameter") == pytest.approx(0.032)
    area = await value(linked, "telescope", "aperturearea")
    assert isinstance(area, float)
    assert area == pytest.approx(math.pi * 0.016**2)


async def test_utcdate_is_the_devices_local_clock_resolved_through_its_own_zone(
    linked: httpx.AsyncClient,
) -> None:
    reported = json.loads((await action(linked, "ssc.device_time")).json()["Value"])
    local = datetime(
        reported["year"],
        reported["mon"],
        reported["day"],
        reported["hour"],
        reported["min"],
        reported["sec"],
        tzinfo=ZoneInfo(reported["time_zone"]),
    )

    stamp = await value(linked, "telescope", "utcdate")
    assert isinstance(stamp, str)
    assert stamp.endswith("Z"), "ASCOM UTCDate is UTC and says so"
    assert datetime.fromisoformat(stamp.replace("Z", "+00:00")) == local


async def test_utcdate_is_not_settable_without_a_verified_verb(
    linked: httpx.AsyncClient,
) -> None:
    assert (
        await put_error_number(linked, "telescope", "utcdate", UTCDate="2026-08-28T19:30:00Z")
        == ErrorNumber.NOT_IMPLEMENTED
    )


async def test_sidereal_time_needs_a_site_first(linked: httpx.AsyncClient) -> None:
    assert await error_number(linked, "telescope", "siderealtime") == ErrorNumber.VALUE_NOT_SET
    await api_put(linked, "telescope", "sitelongitude", SiteLongitude=0.0)
    lst = await value(linked, "telescope", "siderealtime")
    assert isinstance(lst, float)
    assert 0.0 <= lst < 24.0


def test_sidereal_time_matches_the_j2000_reference() -> None:
    """GMST at J2000.0 (2000-01-01 12:00 UT) is 18h 41m 50.55s."""
    utc = ZoneInfo("UTC")
    at_epoch = sidereal_hours(datetime(2000, 1, 1, 12, 0, tzinfo=utc), 0.0)
    assert at_epoch == pytest.approx(18 + 41 / 60 + 50.548 / 3600, abs=1e-4)
    # One sidereal day (23h 56m 04.0905s) later, the answer repeats.
    assert sidereal_hours(datetime(2000, 1, 2, 11, 56, 4, 90500, tzinfo=utc), 0.0) == pytest.approx(
        at_epoch, abs=1e-4
    )
    # Longitude advances local sidereal time by one hour per 15 degrees east.
    assert sidereal_hours(datetime(2000, 1, 1, 12, 0, tzinfo=utc), 15.0) == pytest.approx(
        (at_epoch + 1.0) % 24.0, abs=1e-4
    )


async def test_an_unreadable_device_clock_is_valuenotset(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    from tests.alpaca.conftest import override_method

    override_method(emulator, "pi_get_time", {"time_zone": "Europe/Bratislava"})
    assert await error_number(linked, "telescope", "utcdate") == ErrorNumber.VALUE_NOT_SET

    override_method(
        emulator,
        "pi_get_time",
        {
            "year": 2026,
            "mon": 8,
            "day": 28,
            "hour": 9,
            "min": 37,
            "sec": 53,
            "time_zone": "Mars/Olympus",
        },
    )
    assert await error_number(linked, "telescope", "utcdate") == ErrorNumber.VALUE_NOT_SET


async def test_an_ambiguous_local_time_is_refused_rather_than_guessed(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    """CLAUDE.md invariant 5: a repeated DST hour is unknown, not "probably the first"."""
    from tests.alpaca.conftest import override_method

    # 02:30 on 2026-10-25 happens twice in Europe/Bratislava.
    override_method(
        emulator,
        "pi_get_time",
        {
            "year": 2026,
            "mon": 10,
            "day": 25,
            "hour": 2,
            "min": 30,
            "sec": 0,
            "time_zone": "Europe/Bratislava",
        },
    )
    assert await error_number(linked, "telescope", "utcdate") == ErrorNumber.VALUE_NOT_SET


async def test_a_nonexistent_spring_forward_time_is_not_misdiagnosed_as_ambiguous(
    linked: httpx.AsyncClient, emulator: SeestarEmulator
) -> None:
    """``code-reviewer`` finding 7.

    02:30 on 2026-03-08 does not exist in America/New_York — the spring-forward
    gap skips 02:00-03:00 that night. Refusing is correct; the *reason* must
    not be "ambiguous", because comparing ``fold=0``/``fold=1`` offsets for
    plain inequality is true for both DST transitions. Only ordering the
    offsets (what ``ssc.astro.events.local_time_kind`` does) tells a gap from a
    repeat.
    """
    from tests.alpaca.conftest import override_method

    override_method(
        emulator,
        "pi_get_time",
        {
            "year": 2026,
            "mon": 3,
            "day": 8,
            "hour": 2,
            "min": 30,
            "sec": 0,
            "time_zone": "America/New_York",
        },
    )
    response = await api_get(linked, "telescope", "utcdate")
    body = response.json()
    assert body["ErrorNumber"] == ErrorNumber.VALUE_NOT_SET
    message = body["ErrorMessage"]
    assert "does not exist" in message and "spring-forward" in message
    assert "ambiguous" not in message and "fall-back" not in message


# --------------------------------------------------------------------------- #
# Axis members
# --------------------------------------------------------------------------- #


async def test_axis_members_validate_the_axis(linked: httpx.AsyncClient) -> None:
    assert await value(linked, "telescope", "canmoveaxis", Axis=0) is False
    assert await value(linked, "telescope", "axisrates", Axis=1) == []
    assert await error_number(linked, "telescope", "axisrates", Axis=7) == ErrorNumber.INVALID_VALUE
    assert (await api_get(linked, "telescope", "axisrates")).status_code == 400
    assert (
        await put_error_number(linked, "telescope", "moveaxis", Axis=0, Rate=1.0)
        == ErrorNumber.NOT_IMPLEMENTED
    )


# --------------------------------------------------------------------------- #
# The slew latch, in isolation
# --------------------------------------------------------------------------- #


def test_the_slew_latch_reports_motion_before_the_device_catches_up() -> None:
    now = [0.0]
    latch = _SlewLatch(grace_s=10.0, clock=lambda: now[0])
    assert latch.slewing("none") is False

    latch.arm()
    assert latch.slewing("none") is True, "the command was accepted; the mount is committed"
    now[0] = 4.0
    assert latch.slewing(MOVE_TYPE_SLEWING) is True
    assert latch.slewing("none") is False, "seen moving, then settled: the slew is over"
    assert latch.slewing("none") is False


def test_the_slew_latch_gives_up_rather_than_lying_forever() -> None:
    now = [0.0]
    latch = _SlewLatch(grace_s=10.0, clock=lambda: now[0])
    latch.arm()
    assert latch.slewing("none") is True
    now[0] = 10.0
    assert latch.slewing("none") is False
    assert latch.slewing("none") is False
