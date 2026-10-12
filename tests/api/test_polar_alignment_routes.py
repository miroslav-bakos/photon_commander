# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc/api/routes/polar_alignment.py`` + ``ssc/api/polar_alignment_service.py``:
the HTTP surface. ``tests/api/test_polar_alignment_service.py`` already drives
the orchestration (retries, settling, cancellation) directly against the
service; this file only proves the wiring — status codes, the request/response
shape, and that a real ``PolarAlignmentService`` swapped into ``Services``
(mirrors ``tests/api/test_capture_routes.py``'s own ``_install_capture_service``)
reaches ``GET /api/state`` the normal way.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import numpy as np
import pytest

from ssc.api.deps import Services
from ssc.api.polar_alignment_service import PolarAlignmentService, _build_targets
from ssc.astro.events import Ephemeris
from ssc.astro.frames import MountFrame, j2000_coord
from ssc.astro.units import Deg, wrap_hours
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError
from ssc.client.imagebytes import ImageArrayElementType, ImageBytesFrame
from ssc.platesolve import PlateSolveResult, SolveIssue, SolverAvailability, SolveTimedOutError
from ssc.state.events import CameraFovChanged, MountChanged
from ssc.state.models import CameraFieldOfView, MountState
from tests.api.conftest import FakeGateway, TestClient, services_of


def _terminal_operation(client: TestClient, response: Any) -> dict[str, Any]:
    operation_id = response.json()["id"]
    for _ in range(100):
        operation = client.get(f"/api/operations/{operation_id}").json()
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    return operation


def test_artificial_sky_route_solves_without_a_mount_or_camera(paired: TestClient) -> None:
    response = paired.post(
        "/api/mount/polar-alignment/simulate",
        json={"altitude_adjustment_deg": -1, "azimuth_adjustment_deg": 1, "point_count": 5},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["simulated"] is True
    assert len(body["points"]) == 5
    assert body["result"]["has_residual_check"] is True
    assert body["result"]["total_error_deg"] * 60 < 0.2

WHEN = datetime(2026, 9, 12, 21, 0, tzinfo=UTC)
DEC_DEG = 5.0
STEP_DEG = 30.0
SITE_LAT = 48.2
SITE_LON = 16.4


def _lst_hours(ephemeris: Ephemeris, when: datetime) -> float:
    """Local sidereal time at ``when``, for a start RA that keeps every
    point well above the horizon — see
    ``tests/api/test_polar_alignment_service.py``'s module docstring."""
    time: Any = ephemeris.timescale.from_datetime(when)
    return float(wrap_hours(float(time.gast) + SITE_LON / 15.0))


class FakeDevice:
    async def is_connected(self) -> bool:
        return True

    async def connect(self) -> None:
        return None

    async def get(self, member: str, **params: object) -> object:
        del params
        raise AlpacaRemoteError(
            int(AlpacaErrorNumber.NOT_IMPLEMENTED), "not implemented", member=f"Fake.{member}"
        )


class FakeCamera(FakeDevice):
    async def put(self, member: str, **params: object) -> None:
        del member, params

    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
        del duration_s, light

    async def image_ready(self) -> bool:
        return True

    async def image_array(self) -> ImageBytesFrame:
        array = np.arange(64, dtype=np.uint16).reshape(8, 8)
        return ImageBytesFrame(
            metadata_version=1,
            client_transaction_id=1,
            server_transaction_id=1,
            image_element_type=ImageArrayElementType.UINT16,
            transmission_element_type=ImageArrayElementType.UINT16,
            array=array,
        )


def _target_index(fits_path: Path) -> int:
    # ``PolarAlignmentService._capture_and_solve`` names each solve's FITS
    # ``point-{index}.fits`` — parsed back out here so the fake solver can
    # answer with *that* point's own target rather than one canned position
    # every point would otherwise collide on (coincident points refuse the fit).
    return int(fits_path.stem.removeprefix("point-"))


def _make_fake_solve(
    targets: list[tuple[float, float]],
) -> Callable[[Path], Awaitable[PlateSolveResult]]:
    async def _fake_solve(fits_path: Path) -> PlateSolveResult:
        ra_h, dec_deg = targets[_target_index(fits_path)]
        return PlateSolveResult(
            coord=j2000_coord(Deg(ra_h * 15.0), Deg(dec_deg)),
            date_obs=WHEN,
            pixel_scale_arcsec=2.4,
            rotation_deg=0.0,
            solver="fake",
            solver_version="1.0",
        )

    return _fake_solve


async def _available() -> SolverAvailability:
    return SolverAvailability(available=True, version="fake-1.0")


def _seed_mount(client: TestClient, *, alignment_mode: int, ra_h: float = 10.0) -> None:
    services = services_of(client)
    gateway = services.device._gateway  # type: ignore[attr-defined]
    assert isinstance(gateway, FakeGateway)
    gateway.mount_alignment_mode_value = alignment_mode
    mount = MountState(
        polled_at=datetime.now(tz=UTC),
        stale=False,
        ra_hours=ra_h,
        dec_deg=DEC_DEG,
        ra_j2000_h=ra_h,
        dec_j2000_deg=DEC_DEG,
        slewing=False,
        equatorial_system=MountFrame.J2000,
        site_latitude_deg=SITE_LAT,
        site_longitude_deg=SITE_LON,
    )
    gateway.mount_state = mount
    services.state.apply(MountChanged(mount=mount))


def _install_polar_alignment_service(
    client: TestClient, *, targets: list[tuple[float, float]]
) -> Services:
    services = services_of(client)
    gateway = services.device._gateway  # type: ignore[attr-defined]
    assert isinstance(gateway, FakeGateway)
    original_mount_slew = gateway.mount_slew

    async def mount_slew_and_publish_target(*, ra_hours: float, dec_deg: float) -> None:
        await original_mount_slew(ra_hours=ra_hours, dec_deg=dec_deg)
        mount = gateway.mount_state.model_copy(
            update={
                "polled_at": datetime.now(tz=UTC),
                "ra_hours": ra_hours,
                "dec_deg": dec_deg,
                "ra_j2000_h": ra_hours,
                "dec_j2000_deg": dec_deg,
                "slewing": False,
            }
        )
        gateway.mount_state = mount
        gateway.push_mount(mount)

    # The real gateway's poller publishes the reached coordinates after an
    # asynchronous slew. This route-level fake has no poller, so model that
    # event here rather than allowing stale pre-slew coordinates to settle.
    gateway.mount_slew = mount_slew_and_publish_target

    async def poll_sleep(seconds: float) -> None:
        await asyncio.sleep(seconds)
        if gateway.mount_state is not None:
            mount = gateway.mount_state.model_copy(update={"polled_at": datetime.now(tz=UTC)})
            gateway.mount_state = mount
            gateway.push_mount(mount)

    service = PolarAlignmentService(
        camera=FakeCamera(),
        telescope=FakeDevice(),
        filterwheel=None,
        device=services.device,
        state=services.state,
        astro=services.astro,
        tasks=services.tasks,
        section=services.polar_alignment.section,
        solve=_make_fake_solve(targets),
        availability=_available,
        slew_retry_delay_s=0.0,
        settle_timeout_s=5.0,
        settle_poll_interval_s=0.02,
        post_slew_settle_s=0.0,
        solve_retry_delay_s=0.0,
        sleep=poll_sleep,
    )
    services.polar_alignment = service
    return services


def _wait_for(
    client: TestClient, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get("/api/state")
        assert response.status_code == 200, response.text
        last = response.json()["snapshot"]["polar_alignment"]
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout_s}s; last section: {last}")


def _unlimited(client: TestClient) -> None:
    services_of(client).limiters.pop("mount", None)


@pytest.mark.skipif(
    not Path("data/ephemeris/de440s.bsp").exists(),
    reason="the pinned ephemeris kernel is not present on this machine",
)
def test_start_returns_202_and_reaches_completed(paired: TestClient) -> None:
    _unlimited(paired)
    ephemeris = services_of(paired).astro.ephemeris
    assert ephemeris is not None
    start_ra_h = _lst_hours(ephemeris, WHEN) - (STEP_DEG / 15.0)  # point 1 of 3 transits at WHEN
    targets = _build_targets(start_ra_h, DEC_DEG, 3, STEP_DEG)
    _seed_mount(paired, alignment_mode=1, ra_h=start_ra_h)
    _install_polar_alignment_service(paired, targets=targets)

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={
            "point_count": 3,
            "step_deg": STEP_DEG,
            "exposure_s": 1.0,
            "start_point": "current",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["status"] == "running"

    final = _wait_for(paired, lambda section: section["status"] != "running")
    assert final["status"] == "completed", final.get("error")
    assert final["result"] is not None
    assert final["result"]["point_count"] == 3


def test_start_forwards_auto_refine_choice(paired: TestClient) -> None:
    _unlimited(paired)
    services = services_of(paired)
    start = AsyncMock(return_value=services.polar_alignment.section)
    services.polar_alignment.start = start  # type: ignore[method-assign]

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={"point_count": 3, "step_deg": 30, "exposure_s": 1, "auto_refine": True},
    )

    assert response.status_code == 202, response.text
    assert start.await_args.kwargs["auto_refine"] is True


def test_start_defaults_to_three_point_sweep_without_live_refinement(paired: TestClient) -> None:
    _unlimited(paired)
    services = services_of(paired)
    start = AsyncMock(return_value=services.polar_alignment.section)
    services.polar_alignment.start = start  # type: ignore[method-assign]

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={},
    )

    assert response.status_code == 202, response.text
    assert start.await_args.kwargs["point_count"] == 3
    assert start.await_args.kwargs["step_deg"] == 30.0
    assert start.await_args.kwargs["exposure_s"] == 2.0
    assert start.await_args.kwargs["auto_refine"] is False


def test_start_refuses_409_on_an_alt_az_mount(paired: TestClient) -> None:
    _unlimited(paired)
    _seed_mount(paired, alignment_mode=0)
    _install_polar_alignment_service(paired, targets=[])

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={"point_count": 3, "step_deg": STEP_DEG, "exposure_s": 1.0},
    )
    assert response.status_code == 409, response.text
    assert "AlignmentMode" in response.json()["detail"]


def test_cancel_with_nothing_running_is_409(paired: TestClient) -> None:
    response = paired.post("/api/mount/polar-alignment/cancel")
    assert response.status_code == 409, response.text


def test_solve_here_publishes_the_measured_camera_fov_center_and_rotation(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = services_of(paired)
    services.state.apply(
        CameraFovChanged(camera_fov=CameraFieldOfView(width_deg=2.24, height_deg=3.99))
    )
    solve = AsyncMock(
        return_value=PlateSolveResult(
            coord=j2000_coord(Deg(150.0), Deg(41.2)),
            date_obs=WHEN,
            pixel_scale_arcsec=3.74,
            rotation_deg=93.5,
            solver="fake",
            solver_version="1",
        )
    )
    monkeypatch.setattr(services.polar_alignment, "solve_here", solve)

    response = paired.post(
        "/api/mount/polar-alignment/solve-here",
        json={"exposure_s": 1.0, "gain": 200},
    )

    assert response.status_code == 202, response.text
    assert _terminal_operation(paired, response)["status"] == "succeeded"
    fov = services.state.snapshot.camera_fov
    assert fov is not None
    assert fov.width_deg == 2.24
    assert fov.height_deg == 3.99
    assert fov.center_ra_j2000_h == pytest.approx(10.0)
    assert fov.center_dec_j2000_deg == pytest.approx(41.2)
    assert fov.rotation_deg == pytest.approx(93.5)
    assert fov.solved_at == WHEN


def test_solve_here_retains_a_bounded_solver_timeout(
    paired: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    services = services_of(paired)
    solve = AsyncMock(
        side_effect=SolveTimedOutError(
            SolveIssue.TIMED_OUT,
            "plate solver did not finish within 30s solving frame.fits",
        )
    )
    monkeypatch.setattr(services.polar_alignment, "solve_here", solve)

    response = paired.post(
        "/api/mount/polar-alignment/solve-here",
        json={"exposure_s": 1.0, "gain": 200},
    )

    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "failed"
    assert "did not finish within 30s" in operation["error"]


# --- start_point: ambiguous combinations and NO_CLEAR_ARC (DESIGN.md §4.5) --- #


def test_start_refuses_422_when_a_coordinate_is_given_with_a_non_explicit_start_point(
    paired: TestClient,
) -> None:
    _unlimited(paired)
    _seed_mount(paired, alignment_mode=1)
    _install_polar_alignment_service(paired, targets=[])

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={
            "point_count": 3,
            "step_deg": STEP_DEG,
            "exposure_s": 1.0,
            "start_point": "best",
            "start_ra_hours": 10.0,
        },
    )
    assert response.status_code == 422, response.text


def test_start_refuses_422_when_explicit_is_missing_a_coordinate(paired: TestClient) -> None:
    _unlimited(paired)
    _seed_mount(paired, alignment_mode=1)
    _install_polar_alignment_service(paired, targets=[])

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={
            "point_count": 3,
            "step_deg": STEP_DEG,
            "exposure_s": 1.0,
            "start_point": "explicit",
            "start_ra_hours": 10.0,
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.skipif(
    not Path("data/ephemeris/de440s.bsp").exists(),
    reason="the pinned ephemeris kernel is not present on this machine",
)
def test_start_adapts_an_unsafe_best_field_arc(paired: TestClient) -> None:
    _unlimited(paired)
    _seed_mount(paired, alignment_mode=1)
    _install_polar_alignment_service(paired, targets=[])

    response = paired.post(
        "/api/mount/polar-alignment/start",
        json={"point_count": 9, "step_deg": STEP_DEG, "exposure_s": 1.0},
    )
    assert response.status_code == 202, response.text
    start = response.json()["start"]
    requested_span = STEP_DEG * 8
    assert 30.0 <= start["arc_span_deg"] < requested_span
    assert start["step_deg"] == pytest.approx(start["arc_span_deg"] / 8)
    assert min(start["point_altitudes_deg"]) >= 15.0
    assert any("reduced it to" in warning for warning in start["warnings"])
    paired.post("/api/mount/polar-alignment/cancel")


def test_recheck_without_a_completed_measurement_is_409(paired: TestClient) -> None:
    _unlimited(paired)
    _seed_mount(paired, alignment_mode=1)
    _install_polar_alignment_service(paired, targets=[])

    response = paired.post("/api/mount/polar-alignment/recheck", json={"exposure_s": 1.0})
    assert response.status_code == 409, response.text
    assert "no completed polar-alignment measurement" in response.json()["detail"]
