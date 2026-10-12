# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ssc.api.target_centering_service import TargetCenteringError, TargetCenteringService
from ssc.astro.frames import MountFrame, j2000_coord
from ssc.astro.units import Deg
from ssc.platesolve import PlateSolveResult
from ssc.state.events import CameraFovChanged, MountChanged
from ssc.state.models import CameraFieldOfView, MountState, MountTrackingRate, StateSnapshot
from ssc.state.store import StateStore

WHEN = datetime(2026, 9, 12, 20, 0, tzinfo=UTC)


class FakeDevice:
    def __init__(self, state: StateStore) -> None:
        self.state = state
        self.slews: list[tuple[float, float]] = []
        self.syncs: list[tuple[float, float]] = []
        self.tracking_calls = 0

    async def mount_slew(self, *, ra_hours: float, dec_deg: float) -> None:
        self.slews.append((ra_hours, dec_deg))
        self.state.apply(
            MountChanged(
                mount=MountState(
                    polled_at=WHEN,
                    stale=False,
                    ra_hours=ra_hours,
                    dec_deg=dec_deg,
                    equatorial_system=MountFrame.J2000,
                    slewing=False,
                    tracking=True,
                )
            )
        )

    async def mount_set_tracking(
        self, *, enabled: bool, rate: MountTrackingRate | None
    ) -> MountState:
        del rate
        self.tracking_calls += 1
        mount = self.state.snapshot.mount
        assert mount is not None
        updated = mount.model_copy(update={"tracking": enabled})
        self.state.apply(MountChanged(mount=updated))
        return updated

    async def mount_sync(self, *, ra_hours: float, dec_deg: float) -> None:
        self.syncs.append((ra_hours, dec_deg))
        mount = self.state.snapshot.mount
        assert mount is not None
        self.state.apply(
            MountChanged(
                mount=mount.model_copy(
                    update={
                        "ra_hours": ra_hours,
                        "dec_deg": dec_deg,
                        "ra_j2000_h": ra_hours,
                        "dec_j2000_deg": dec_deg,
                    }
                )
            )
        )


class FakeSolver:
    def __init__(self, positions: list[tuple[float, float]]) -> None:
        self.positions = positions
        self.calls: list[tuple[float, int | None]] = []

    async def solve_here(self, *, exposure_s: float, gain: int | None = None) -> PlateSolveResult:
        self.calls.append((exposure_s, gain))
        ra_h, dec_deg = self.positions.pop(0)
        return PlateSolveResult(
            coord=j2000_coord(Deg(ra_h * 15.0), Deg(dec_deg)),
            date_obs=WHEN,
            pixel_scale_arcsec=2.4,
            rotation_deg=0.0,
            solver="fake",
            solver_version="1",
        )


async def instant_sleep(_seconds: float) -> None:
    return None


def service_for(
    positions: list[tuple[float, float]],
) -> tuple[TargetCenteringService, FakeDevice, FakeSolver, StateStore]:
    state = StateStore(StateSnapshot(server_started_at=WHEN))
    device = FakeDevice(state)
    solver = FakeSolver(positions)
    service = TargetCenteringService(
        device=device,
        solver=solver,
        state=state,
        sleep=instant_sleep,
    )
    return service, device, solver, state


async def test_one_slew_is_enough_when_first_solve_is_within_tolerance() -> None:
    service, device, solver, _state = service_for([(10.00001, 20.0)])

    result = await service.center(
        ra_j2000_h=10.0,
        dec_j2000_deg=20.0,
        tolerance_arcmin=1.0,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )

    assert result.converged is True
    assert len(result.attempts) == 1
    assert device.slews == [(10.0, 20.0)]
    assert device.syncs == [(pytest.approx(10.00001), pytest.approx(20.0))]
    assert solver.calls == [(1.0, 200)]
    assert service.progress.status == "succeeded"
    assert service.progress.stage == "complete"
    assert service.progress.attempt == 1
    assert service.progress.attempts[0].error_arcmin == result.attempts[0].error_arcmin
    assert "Location confirmed" in service.progress.message


async def test_automatic_centering_uses_the_scheduled_sixty_arcsecond_tolerance() -> None:
    service, device, solver, _state = service_for([(10.0, 20.0125)])

    await service.center_default(10.0, 20.0)

    assert service.progress.tolerance_arcmin == 1.0
    assert len(device.slews) == 1
    assert solver.calls == [(1.0, 200)]


async def test_automatic_centering_fails_when_sixty_arcseconds_is_not_reached() -> None:
    service, _device, _solver, _state = service_for([(9.99, 19.9)] * 3)

    with pytest.raises(TargetCenteringError, match="60 arcsecond"):
        await service.center_default(10.0, 20.0)


async def test_scheduled_centering_syncs_solved_position_before_correcting() -> None:
    service, device, _solver, _state = service_for([
        (21.0141679035, 44.1112449302),
        (21.01638039709577, 44.04847403546877),
    ])
    await service.center_default(21.01638039709577, 44.04847403546877)
    assert device.slews == [(21.01638039709577, 44.04847403546877)] * 2
    assert device.syncs == [
        (21.0141679035, 44.1112449302),
        (21.01638039709577, 44.04847403546877),
    ]
    assert service.progress.status == "succeeded"


async def test_scheduled_mosaic_does_not_accept_october_six_residual() -> None:
    service, device, _solver, _state = service_for([
        (21.0141679035, 44.1112449302),
        (21.0162284528, 44.1115005774),
        (21.0163360225, 44.1110442410),
    ])
    with pytest.raises(TargetCenteringError, match="retry required"):
        await service.center_default(21.01638039709577, 44.04847403546877)
    assert len(device.syncs) == 2
    assert service.progress.status == "failed"
    assert service.progress.attempts[-1].error_arcmin == pytest.approx(3.75432, abs=0.001)


async def test_residual_is_applied_to_the_next_command_and_verified() -> None:
    service, device, _solver, _state = service_for([(9.99, 19.9), (10.0, 20.0)])

    result = await service.center(
        ra_j2000_h=10.0,
        dec_j2000_deg=20.0,
        tolerance_arcmin=1.0,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )

    assert result.converged is True
    assert len(result.attempts) == 2
    assert device.slews[0] == (10.0, 20.0)
    assert device.slews[1][0] == pytest.approx(10.01)
    assert device.slews[1][1] == pytest.approx(20.1)
    assert device.syncs == [(pytest.approx(10.0), pytest.approx(20.0))]


async def test_stops_at_the_attempt_cap_and_reports_final_error() -> None:
    service, device, _solver, _state = service_for([(9.99, 19.9)] * 3)

    result = await service.center(
        ra_j2000_h=10.0,
        dec_j2000_deg=20.0,
        tolerance_arcmin=0.1,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )

    assert result.converged is False
    assert len(result.attempts) == 3
    assert len(device.slews) == 3
    assert device.syncs == []
    assert result.attempts[-1].error_arcmin > 0.1
    assert service.progress.status == "failed"
    assert service.progress.stage == "failed"
    assert service.progress.attempt == 3
    assert len(service.progress.attempts) == 3
    assert service.progress.error == "maximum correction attempts reached"


async def test_successful_centering_solve_publishes_the_real_camera_footprint() -> None:
    service, _device, _solver, state = service_for([(10.0, 20.0)])
    state.apply(
        CameraFovChanged(camera_fov=CameraFieldOfView(width_deg=2.24, height_deg=3.99))
    )

    await service.center(
        ra_j2000_h=10.0,
        dec_j2000_deg=20.0,
        tolerance_arcmin=1.0,
        exposure_s=1.0,
        gain=200,
        max_attempts=3,
    )

    fov = state.snapshot.camera_fov
    assert fov is not None
    assert fov.center_ra_j2000_h == pytest.approx(10.0)
    assert fov.center_dec_j2000_deg == pytest.approx(20.0)
    assert fov.rotation_deg == pytest.approx(0.0)
    assert fov.solved_at == WHEN


async def test_centering_confirms_tracking_after_slew_even_if_snapshot_says_true() -> None:
    service, device, solver, _state = service_for([(10.0, 20.0)])
    original_solve = solver.solve_here

    async def checked_solve(**kwargs):
        assert device.tracking_calls == 1
        return await original_solve(**kwargs)

    solver.solve_here = checked_solve
    result = await service.center(
        ra_j2000_h=10.0, dec_j2000_deg=20.0, tolerance_arcmin=1.0,
        exposure_s=1.0, gain=200, max_attempts=1,
    )
    assert result.converged
    assert device.tracking_calls == 1
