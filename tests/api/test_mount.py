# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The Mount tab's controls (DESIGN.md §4.4b) — every endpoint the coordinator
pinned, including the three failure paths a UI has to render specifically:
a device refusal (409, with the device's own message and error number), a
member the current driver mapping does not have (501), and a jog rate above
the mount's advertised cap (422).

Also the J2000 <-> mount-frame boundary (``ssc.astro.frames``, wired into
``ssc.api.services.DeviceService`` 2026-09-10): a slew/sync target arrives
here as J2000 and must reach the gateway in whatever frame
``MountState.equatorial_system`` declares.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from ssc.api.target_centering_service import TargetCenteringAttempt, TargetCenteringResult
from ssc.astro.frames import (
    EquatorialCoord,
    MountFrame,
    frame_offset_arcsec,
    j2000_coord,
    mount_site,
    to_j2000,
    to_mount_frame,
)
from ssc.astro.units import Deg, Hours, hours_to_deg
from ssc.settings.config import AppConfig
from ssc.state.events import CameraFovChanged, MountChanged
from ssc.state.models import CameraFieldOfView, MountCapabilities, MountState
from ssc.state.ports import MountNotAvailableError, MountRefusedError
from tests.api.conftest import FakeGateway, TestClient, services_of

#: A real site — Vienna — so the outbound-conversion tests exercise the full
#: skyfield apparent-place chain, not a degenerate lat=lon=0 case.
_SITE_LATITUDE_DEG = 48.2
_SITE_LONGITUDE_DEG = 16.4


def _unlimited(client: TestClient) -> None:
    """Endpoint tests fire several requests back to back; the mount buckets
    are deliberately mean (CLAUDE.md invariant "rate-limit action
    endpoints") and are exercised on their own in a dedicated test."""
    limiters = services_of(client).limiters
    limiters.pop("mount", None)
    limiters.pop("mount-jog", None)


def _seed_mount_frame(
    client: TestClient,
    *,
    frame: MountFrame,
    latitude_deg: float | None = _SITE_LATITUDE_DEG,
    longitude_deg: float | None = _SITE_LONGITUDE_DEG,
    sidereal_time_hours: float | None = 10.0,
) -> None:
    """Publish a ``MountChanged`` declaring the mount's frame (and, unless a
    test says otherwise, its site) — every outbound-conversion test needs one
    of these before it can slew/sync at all (module docstring)."""
    services_of(client).state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                equatorial_system=frame,
                site_latitude_deg=latitude_deg,
                site_longitude_deg=longitude_deg,
                sidereal_time_hours=sidereal_time_hours,
            )
        )
    )


def _complete_zenith_slew_on_return(
    client: TestClient, gateway: FakeGateway, *, altitude_deg: float = 90.0
) -> None:
    """Make the fake publish the fresh completion poll a real driver emits."""
    original_slew = gateway.mount_slew

    async def complete(*, ra_hours: float, dec_deg: float) -> None:
        await original_slew(ra_hours=ra_hours, dec_deg=dec_deg)
        mount = services_of(client).state.snapshot.mount
        assert mount is not None
        services_of(client).state.apply(
            MountChanged(
                mount=mount.model_copy(
                    update={
                        "alt_deg": altitude_deg,
                        "slewing": False,
                        "polled_at": datetime.now(tz=UTC),
                        "stale": False,
                    }
                )
            )
        )

    gateway.mount_slew = complete  # type: ignore[method-assign]


def test_slew_accepts_and_calls_the_gateway_with_the_given_coordinates(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """A mount already declaring J2000 needs no conversion at all — the
    target reaches the gateway exactly as given."""
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    response = paired.post("/api/mount/slew", json={"ra_hours": 12.5, "dec_deg": 45.0})
    assert response.status_code == 202, response.text
    assert gateway.mount_calls == [("slew", {"ra_hours": 12.5, "dec_deg": 45.0})]


def test_atomic_slew_reports_when_unpark_succeeded_but_slew_failed(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    mount = services_of(paired).state.snapshot.mount
    assert mount is not None
    services_of(paired).state.apply(MountChanged(mount=mount.model_copy(update={"at_park": True})))
    gateway.mount_slew = AsyncMock(  # type: ignore[method-assign]
        side_effect=MountRefusedError("axis fault", device_code=4001)
    )

    response = paired.post(
        "/api/mount/slew",
        json={"ra_hours": 12.5, "dec_deg": 45.0, "unpark_if_needed": True},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Mount was unparked, but slewing failed; the mount remains unparked: axis fault"
    )
    assert gateway.mount_calls == [("unpark", {})]


def test_slew_invalidates_a_previous_plate_solved_fov(paired: TestClient) -> None:
    services = services_of(paired)
    services.state.apply(
        CameraFovChanged(
            camera_fov=CameraFieldOfView(
                width_deg=2.24,
                height_deg=3.99,
                rotation_deg=93.5,
                center_ra_j2000_h=10.0,
                center_dec_j2000_deg=41.2,
                solved_at=datetime.now(tz=UTC),
            )
        )
    )
    _seed_mount_frame(paired, frame=MountFrame.J2000)

    response = paired.post("/api/mount/slew", json={"ra_hours": 12.5, "dec_deg": 45.0})

    assert response.status_code == 202, response.text
    fov = services.state.snapshot.camera_fov
    assert fov is not None
    assert (fov.width_deg, fov.height_deg) == (2.24, 3.99)
    assert fov.rotation_deg is None
    assert fov.center_ra_j2000_h is None
    assert fov.center_dec_j2000_deg is None
    assert fov.solved_at is None


def test_center_target_forwards_settings_and_reports_every_solve_attempt(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    center = AsyncMock(
        return_value=TargetCenteringResult(
            converged=True,
            target_ra_j2000_h=12.5,
            target_dec_j2000_deg=45.0,
            tolerance_arcmin=0.5,
            attempts=(
                TargetCenteringAttempt(
                    attempt=1,
                    commanded_ra_j2000_h=12.5,
                    commanded_dec_j2000_deg=45.0,
                    solved_ra_j2000_h=12.49,
                    solved_dec_j2000_deg=44.9,
                    error_arcmin=7.2,
                ),
                TargetCenteringAttempt(
                    attempt=2,
                    commanded_ra_j2000_h=12.51,
                    commanded_dec_j2000_deg=45.1,
                    solved_ra_j2000_h=12.5,
                    solved_dec_j2000_deg=45.0,
                    error_arcmin=0.12,
                ),
            ),
        )
    )
    monkeypatch.setattr(services_of(paired).target_centering, "center", center)

    response = paired.post(
        "/api/mount/center",
        json={
            "ra_hours": 12.5,
            "dec_deg": 45.0,
            "tolerance_arcmin": 0.5,
            "exposure_s": 1.0,
            "gain": 200,
            "max_attempts": 3,
        },
    )

    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    for _ in range(100):
        operation = paired.get(f"/api/operations/{operation_id}").json()
        if operation["status"] == "succeeded":
            break
        time.sleep(0.01)
    center.assert_awaited_once_with(
        ra_j2000_h=12.5,
        dec_j2000_deg=45.0,
        tolerance_arcmin=0.5,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )
    assert operation["result"]["converged"] is True
    assert [attempt["error_arcmin"] for attempt in operation["result"]["attempts"]] == [7.2, 0.12]


def test_center_target_defaults_to_thirty_arcseconds(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    center = AsyncMock(
        return_value=TargetCenteringResult(
            converged=True,
            target_ra_j2000_h=12.5,
            target_dec_j2000_deg=45.0,
            tolerance_arcmin=0.5,
            attempts=(),
        )
    )
    monkeypatch.setattr(services_of(paired).target_centering, "center", center)

    response = paired.post(
        "/api/mount/center", json={"ra_hours": 12.5, "dec_deg": 45.0}
    )

    assert response.status_code == 202, response.text
    operation_id = response.json()["id"]
    for _ in range(100):
        operation = paired.get(f"/api/operations/{operation_id}").json()
        if operation["status"] == "succeeded":
            break
        time.sleep(0.01)
    center.assert_awaited_once_with(
        ra_j2000_h=12.5,
        dec_j2000_deg=45.0,
        tolerance_arcmin=0.5,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )


def test_center_status_reports_the_service_progress(paired: TestClient) -> None:
    response = paired.get("/api/mount/center/status")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "idle",
        "stage": "idle",
        "attempt": 0,
        "max_attempts": 0,
        "target_ra_j2000_h": None,
        "target_dec_j2000_deg": None,
        "tolerance_arcmin": None,
        "attempts": [],
        "message": "No centering operation has run yet.",
        "error": None,
        "updated_at": None,
    }


def test_slew_zenith_calculates_a_j2000_target_and_leaves_tracking_off(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    _complete_zenith_slew_on_return(paired, gateway)
    response = paired.post("/api/mount/slew-zenith")
    assert response.status_code == 202, response.text
    assert len(gateway.mount_calls) == 2
    action, parameters = gateway.mount_calls[0]
    assert action == "slew"
    # The endpoint derives the live apparent zenith from skyfield rather than
    # trusting the deliberately synthetic sidereal-time value in this fixture.
    assert 0 <= float(parameters["ra_hours"]) < 24
    assert abs(float(parameters["dec_deg"]) - _SITE_LATITUDE_DEG) < 0.2
    assert gateway.mount_calls[1] == ("set_tracking", {"enabled": False, "rate": None})
    assert paired.get("/api/state").json()["snapshot"]["mount"]["tracking"] is False


def test_repeated_slew_zenith_at_position_still_turns_tracking_off(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    mount = services_of(paired).state.snapshot.mount
    assert mount is not None
    services_of(paired).state.apply(
        MountChanged(
            mount=mount.model_copy(
                update={"alt_deg": 89.88, "slewing": False, "tracking": True}
            )
        )
    )
    _complete_zenith_slew_on_return(paired, gateway, altitude_deg=89.88)

    response = paired.post("/api/mount/slew-zenith")

    assert response.status_code == 202, response.text
    assert gateway.mount_calls[-1] == ("set_tracking", {"enabled": False, "rate": None})
    assert paired.get("/api/state").json()["snapshot"]["mount"]["tracking"] is False


def test_slew_zenith_refuses_success_when_tracking_remains_on(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    _complete_zenith_slew_on_return(paired, gateway)
    gateway.mount_set_tracking = AsyncMock(return_value=MountState(tracking=True))  # type: ignore[method-assign]

    response = paired.post("/api/mount/slew-zenith")

    assert response.status_code == 409, response.text
    assert "reached the flat-panel position but reports tracking=True" in response.json()["detail"]
    gateway.mount_set_tracking.assert_awaited_once_with(enabled=False, rate=None)


def test_sync_is_204_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    response = paired.post("/api/mount/sync", json={"ra_hours": 1.0, "dec_deg": -10.0})
    assert response.status_code == 204, response.text
    assert gateway.mount_calls == [("sync", {"ra_hours": 1.0, "dec_deg": -10.0})]


def test_slew_refuses_when_the_mount_frame_is_not_known_yet(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Nothing has polled the mount yet: refusing beats guessing it is J2000
    (CLAUDE.md invariant 7) — this is the same refusal a client-side
    workaround used to have to make for itself before this endpoint did the
    conversion (``web/src/skymap/mountFrame.ts``)."""
    response = paired.post("/api/mount/slew", json={"ra_hours": 12.5, "dec_deg": 45.0})
    assert response.status_code == 409, response.text
    assert "EquatorialSystem" in response.json()["detail"]
    assert gateway.mount_calls == []


def test_sync_refuses_a_frame_the_mount_declares_but_ssc_does_not_convert(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """B1950 is a real ASCOM ``EquatorialCoordinateType`` — refused by name,
    not silently treated as J2000 or as topocentric."""
    _seed_mount_frame(paired, frame=MountFrame.B1950)
    response = paired.post("/api/mount/sync", json={"ra_hours": 1.0, "dec_deg": -10.0})
    assert response.status_code == 409, response.text
    assert "b1950" in response.json()["detail"]
    assert gateway.mount_calls == []


def test_slew_refuses_when_the_mounts_own_site_is_not_known_yet(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Topocentric conversion needs an observer; SSC's *configured* location
    is not a substitute for the mount's own reported site
    (``ssc/astro/frames.py``'s module docstring)."""
    _seed_mount_frame(paired, frame=MountFrame.TOPOCENTRIC, latitude_deg=None, longitude_deg=None)
    response = paired.post("/api/mount/slew", json={"ra_hours": 12.5, "dec_deg": 45.0})
    assert response.status_code == 409, response.text
    assert "SiteLatitude" in response.json()["detail"]
    assert gateway.mount_calls == []


def test_slew_converts_a_j2000_target_to_the_mounts_topocentric_frame(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """The direction that matters most (module docstring): a wrong sync is
    durable. Asserted as a *direction*, not just a magnitude — inverting the
    conversion would double the error rather than cancel it, so this checks
    the gateway received the forward conversion's own answer, not its
    reverse.
    """
    _seed_mount_frame(paired, frame=MountFrame.TOPOCENTRIC)
    before = datetime.now(tz=UTC)
    response = paired.post("/api/mount/slew", json={"ra_hours": 12.5, "dec_deg": 45.0})
    after = datetime.now(tz=UTC)
    assert response.status_code == 202, response.text
    assert len(gateway.mount_calls) == 1
    name, kwargs = gateway.mount_calls[0]
    assert name == "slew"
    sent_ra_hours = float(kwargs["ra_hours"])  # type: ignore[arg-type]
    sent_dec_deg = float(kwargs["dec_deg"])  # type: ignore[arg-type]

    astro = services_of(paired).astro
    assert astro.ephemeris is not None, "the ephemeris kernel must be present for this test"

    observer = mount_site(Deg(_SITE_LATITUDE_DEG), Deg(_SITE_LONGITUDE_DEG))
    target = j2000_coord(hours_to_deg(Hours(12.5)), Deg(45.0))
    midpoint = before + (after - before) / 2
    expected = to_mount_frame(astro.ephemeris, observer, target, midpoint, MountFrame.TOPOCENTRIC)

    # Correct direction: within a fraction of an arcsec of wall-clock slop,
    # not the ~20' a skipped or backwards conversion would leave.
    assert abs(sent_ra_hours - float(expected.ra_h)) < 1e-4
    assert abs(sent_dec_deg - float(expected.dec_deg)) < 1e-4
    offset_arcsec = frame_offset_arcsec(target, expected)
    assert offset_arcsec > 60.0, f"expected an arcminute-scale shift, got {offset_arcsec} arcsec"

    # Applying the *inverse* map to the same nominal input — what "the
    # conversion is backwards" would actually send — lands on the opposite
    # side of the input from the true answer and roughly twice as far from
    # it, not the same place: inverting doubles the error rather than
    # cancelling it, so this is what tells the two apart.
    wrong = to_j2000(
        astro.ephemeris,
        observer,
        EquatorialCoord(ra_deg=target.ra_deg, dec_deg=target.dec_deg, frame=MountFrame.TOPOCENTRIC),
        midpoint,
    )
    wrong_offset_from_correct = frame_offset_arcsec(expected, wrong)
    assert wrong_offset_from_correct > 1.5 * offset_arcsec


# -- inbound: a mount reading reaches a client with its J2000 filled in ---- #


def test_a_topocentric_reading_is_published_with_its_j2000_position_filled_in(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """The other direction of the same boundary: what
    ``ssc.main.AlpacaDeviceGateway`` polls (in whatever frame the mount
    declares) reaches ``GET /api/state`` with a J2000 position already
    computed, so a client can draw it without knowing about frames."""
    polled_at = datetime.now(tz=UTC)
    gateway.push_mount(
        MountState(
            polled_at=polled_at,
            stale=False,
            ra_hours=12.5,
            dec_deg=45.0,
            equatorial_system=MountFrame.TOPOCENTRIC,
            site_latitude_deg=_SITE_LATITUDE_DEG,
            site_longitude_deg=_SITE_LONGITUDE_DEG,
        )
    )
    mount = paired.get("/api/state").json()["snapshot"]["mount"]
    assert mount["equatorial_system"] == "topocentric"
    # The raw device-frame reading is untouched...
    assert mount["ra_hours"] == 12.5
    assert mount["dec_deg"] == 45.0
    # ...and its J2000 counterpart is filled in alongside it, computed the
    # same way the outbound conversion is (``to_j2000``, not a hand rotation).
    assert mount["ra_j2000_h"] is not None
    assert mount["dec_j2000_deg"] is not None

    astro = services_of(paired).astro
    assert astro.ephemeris is not None
    observer = mount_site(Deg(_SITE_LATITUDE_DEG), Deg(_SITE_LONGITUDE_DEG))
    coord = EquatorialCoord(
        ra_deg=hours_to_deg(Hours(12.5)), dec_deg=Deg(45.0), frame=MountFrame.TOPOCENTRIC
    )
    expected = to_j2000(astro.ephemeris, observer, coord, polled_at)
    assert abs(mount["ra_j2000_h"] - float(expected.ra_h)) < 1e-6
    assert abs(mount["dec_j2000_deg"] - float(expected.dec_deg)) < 1e-6
    # Distinct from the raw reading by roughly the known ~20' 2026 offset —
    # not silently equal to it (which would mean nothing was converted).
    assert frame_offset_arcsec(coord, expected) > 60.0


def test_a_j2000_reading_needs_no_conversion_to_be_published(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.push_mount(
        MountState(
            polled_at=datetime.now(tz=UTC),
            stale=False,
            ra_hours=12.5,
            dec_deg=45.0,
            equatorial_system=MountFrame.J2000,
        )
    )
    mount = paired.get("/api/state").json()["snapshot"]["mount"]
    assert mount["ra_j2000_h"] == 12.5
    assert mount["dec_j2000_deg"] == 45.0


def test_a_reading_with_no_known_frame_is_published_without_a_j2000_position(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Refusing to guess applies inbound too: no ``equatorial_system`` yet
    (or one SSC does not convert) means no ``ra_j2000_h`` — never a silently
    wrong one, and never dropping the raw reading either."""
    gateway.push_mount(
        MountState(polled_at=datetime.now(tz=UTC), stale=False, ra_hours=12.5, dec_deg=45.0)
    )
    mount = paired.get("/api/state").json()["snapshot"]["mount"]
    assert mount["equatorial_system"] is None
    assert mount["ra_hours"] == 12.5
    assert mount["ra_j2000_h"] is None
    assert mount["dec_j2000_deg"] is None


def test_move_axis_zero_rate_is_the_jog_release_not_a_special_case(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/mount/move-axis", json={"axis": "primary", "rate_deg_s": 0.0})
    assert response.status_code == 204, response.text
    assert gateway.mount_calls == [("move_axis", {"axis": "primary", "rate_deg_s": 0.0})]


def test_stop_is_204_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/mount/stop")
    assert response.status_code == 204, response.text
    assert gateway.mount_calls == [("stop", {})]


def test_park_is_202_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/mount/park")
    assert response.status_code == 202, response.text
    assert gateway.mount_calls == [("park", {})]


def test_unpark_is_204_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/mount/unpark")
    assert response.status_code == 204, response.text
    assert gateway.mount_calls == [("unpark", {})]


def test_find_home_is_202_and_reaches_the_gateway(paired: TestClient, gateway: FakeGateway) -> None:
    response = paired.post("/api/mount/find-home")
    assert response.status_code == 202, response.text
    assert gateway.mount_calls == [("find_home", {})]


def test_tracking_returns_the_read_back_mount_state(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.put("/api/mount/tracking", json={"enabled": True, "rate": "lunar"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tracking"] is True
    assert body["tracking_rate"] == "lunar"
    assert gateway.mount_calls == [("set_tracking", {"enabled": True, "rate": "lunar"})]
    # The read-back was published, not just returned — GET /api/state agrees.
    assert paired.get("/api/state").json()["snapshot"]["mount"]["tracking"] is True


def test_tracking_rate_is_optional_and_left_alone_when_omitted(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.put("/api/mount/tracking", json={"enabled": False})
    assert response.status_code == 200, response.text
    assert gateway.mount_calls == [("set_tracking", {"enabled": False, "rate": None})]


# -- the below-horizon Park refusal, verbatim (DESIGN.md §4.4b) ------------- #


def test_park_refused_below_the_horizon_surfaces_the_devices_own_message(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.mount_error = MountRefusedError(
        "[1279] SET_SCOPE_SET_TRACK_STATE fail ... pointed below the horizon",
        device_code=1279,
    )
    response = paired.post("/api/mount/park")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["detail"] == ("[1279] SET_SCOPE_SET_TRACK_STATE fail ... pointed below the horizon")
    assert body["device_code"] == 1279


def test_find_home_recovers_a_refused_park_and_is_a_separate_call(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """The endpoint never auto-recovers on its own (CLAUDE.md invariant 7 /
    the brief's explicit constraint) — recovering is a second, caller-issued
    request."""
    gateway.mount_error = MountRefusedError("pointed below the horizon", device_code=1279)
    assert paired.post("/api/mount/park").status_code == 409
    gateway.mount_error = None
    response = paired.post("/api/mount/find-home")
    assert response.status_code == 202, response.text
    assert [name for name, _ in gateway.mount_calls] == ["park", "find_home"]


def test_a_refusal_with_no_device_code_still_carries_the_key_as_null(
    paired: TestClient, gateway: FakeGateway
) -> None:
    """Not every refusal comes from the device (e.g. "not connected") — the
    response shape stays the same either way."""
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    gateway.mount_error = MountRefusedError("not connected to the scope")
    response = paired.post("/api/mount/slew", json={"ra_hours": 0.0, "dec_deg": 0.0})
    assert response.status_code == 409, response.text
    assert response.json() == {"detail": "not connected to the scope", "device_code": None}


# -- a member the current driver mapping does not have ---------------------- #


def test_move_axis_not_available_is_501_naming_the_member(
    paired: TestClient, gateway: FakeGateway
) -> None:
    gateway.mount_error = MountNotAvailableError(
        "Telescope.MoveAxis: scope_speed_move returns 103 on firmware 8.46"
    )
    response = paired.post("/api/mount/move-axis", json={"axis": "secondary", "rate_deg_s": 2.0})
    assert response.status_code == 501, response.text
    assert response.json()["detail"] == (
        "Telescope.MoveAxis: scope_speed_move returns 103 on firmware 8.46"
    )


# -- the axis-rate cap (structural 6 deg/s, and the live-advertised one) --- #


def test_move_axis_above_the_structural_cap_is_422_and_never_reaches_the_gateway(
    paired: TestClient, gateway: FakeGateway
) -> None:
    response = paired.post("/api/mount/move-axis", json={"axis": "primary", "rate_deg_s": 6.5})
    assert response.status_code == 422, response.text
    assert gateway.mount_calls == []


def test_move_axis_above_the_live_advertised_cap_is_422(
    paired: TestClient, gateway: FakeGateway
) -> None:
    services = services_of(paired)
    services.state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                capabilities=MountCapabilities(axis_rate_max_deg_s=3.0),
            )
        )
    )
    response = paired.post("/api/mount/move-axis", json={"axis": "primary", "rate_deg_s": 4.0})
    assert response.status_code == 422, response.text
    assert "3.0" in response.json()["detail"]
    assert gateway.mount_calls == []


def test_move_axis_at_or_below_the_live_cap_is_accepted(
    paired: TestClient, gateway: FakeGateway
) -> None:
    services = services_of(paired)
    services.state.apply(
        MountChanged(
            mount=MountState(
                polled_at=datetime.now(tz=UTC),
                stale=False,
                capabilities=MountCapabilities(axis_rate_max_deg_s=3.0),
            )
        )
    )
    response = paired.post("/api/mount/move-axis", json={"axis": "primary", "rate_deg_s": 3.0})
    assert response.status_code == 204, response.text
    assert gateway.mount_calls == [("move_axis", {"axis": "primary", "rate_deg_s": 3.0})]


def test_move_axis_dead_man_stops_when_browser_sends_no_release(
    paired: TestClient, gateway: FakeGateway
) -> None:
    _unlimited(paired)
    _seed_mount_frame(paired, frame=MountFrame.J2000)
    response = paired.post(
        "/api/mount/move-axis", json={"axis": "primary", "rate_deg_s": 1.0}
    )
    assert response.status_code == 204, response.text
    assert services_of(paired).state.snapshot.mount is not None
    assert services_of(paired).state.snapshot.mount.jog_primary_active is True

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if gateway.mount_calls[-1:] == [
            ("move_axis", {"axis": "primary", "rate_deg_s": 0})
        ]:
            break
        time.sleep(0.02)
    assert gateway.mount_calls[-1] == ("move_axis", {"axis": "primary", "rate_deg_s": 0})
    assert services_of(paired).state.snapshot.mount.jog_primary_active is False


# -- every control is control-scoped, matching every other mutating route --- #


@pytest.mark.parametrize("config", [False, True], indirect=True)
def test_stop_requires_a_session_iff_auth_required(client: TestClient, config: AppConfig) -> None:
    """Mirrors ``tests/api/test_auth.py::test_state_requires_a_session_iff_auth_required``
    — one representative mount endpoint standing in for all eight, which share
    the same ``ControlDep``."""
    _unlimited(client)
    response = client.post("/api/mount/stop")
    assert response.status_code == (401 if config.auth_required else 204)


@pytest.mark.parametrize("start_ra,step", [(17.954682, 1.0), (23.5, 1.0), (1.2, -1.0)])
def test_polar_commands_hold_device_declination_and_recover_an_ideal_axis(
    paired: TestClient, gateway: FakeGateway, monkeypatch, start_ra: float, step: float
) -> None:
    """Replay actual outbound commands as ideal mount pointings and fit them.

    Catalogue-constant DEC changes mechanical DEC and creates a false PA axis;
    the production polar command must preserve the first device-frame DEC even
    across RA wrap and negative sweeps. Ordinary GOTO remains unchanged.
    """
    from ssc.astro.polar import PolarPoint, solve_polar_alignment

    _seed_mount_frame(paired, frame=MountFrame.TOPOCENTRIC)
    services = services_of(paired)
    eph = services.astro.ephemeris
    assert eph is not None
    site = mount_site(Deg(_SITE_LATITUDE_DEG), Deg(_SITE_LONGITUDE_DEG))
    base = datetime(2026, 10, 3, 17, 57, 47, tzinfo=UTC) + timedelta(
        hours=((start_ra + 2 * step) - 19.954682) % 24
    )
    instant = base

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant

    monkeypatch.setattr("ssc.api.services.datetime", Clock)

    async def sweep():
        nonlocal instant
        fixed_dec = None
        corrected = []
        old = []
        for index in range(5):
            instant = base + timedelta(seconds=20 * index)
            ra = (start_ra + index * step) % 24
            fixed_dec = await services.device.mount_slew_polar(
                ra_hours=ra,
                dec_deg=0.001370,
                fixed_mount_dec_deg=fixed_dec,
            )
            command = gateway.mount_calls[-1][1]
            assert command["dec_deg"] == fixed_dec
            pointing = EquatorialCoord(
                ra_deg=Deg(float(command["ra_hours"]) * 15),
                dec_deg=Deg(fixed_dec),
                frame=MountFrame.TOPOCENTRIC,
            )
            corrected.append(PolarPoint(coord=to_j2000(eph, site, pointing, instant), when=instant))
            old.append(PolarPoint(coord=j2000_coord(Deg(ra * 15), Deg(0.001370)), when=instant))
        assert len({call[1]["dec_deg"] for call in gateway.mount_calls}) == 1
        assert (
            float(
                solve_polar_alignment(eph, site, corrected, apply_refraction=False).total_error_deg
            )
            < 1e-6
        )
        assert (
            float(solve_polar_alignment(eph, site, old, apply_refraction=False).total_error_deg)
            * 60
            > 5
        )

    asyncio.run(sweep())
