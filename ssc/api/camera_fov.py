# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Lifecycle helpers for the camera footprint measured by plate solving."""

from __future__ import annotations

from ssc.platesolve import PlateSolveResult
from ssc.state.events import CameraFovChanged
from ssc.state.store import StateStore


def publish_solved_camera_fov(state: StateStore, result: PlateSolveResult) -> None:
    """Attach a solve's center and rotation to the known camera dimensions."""
    camera_fov = state.snapshot.camera_fov
    if camera_fov is None:
        return
    state.apply(
        CameraFovChanged(
            camera_fov=camera_fov.model_copy(
                update={
                    "rotation_deg": result.rotation_deg,
                    "center_ra_j2000_h": float(result.coord.ra_h),
                    "center_dec_j2000_deg": float(result.coord.dec_deg),
                    "solved_at": result.date_obs,
                }
            )
        )
    )


def invalidate_solved_camera_fov(state: StateStore) -> None:
    """Keep physical dimensions but drop a footprint invalidated by motion."""
    camera_fov = state.snapshot.camera_fov
    if camera_fov is None or (
        camera_fov.rotation_deg is None
        and camera_fov.center_ra_j2000_h is None
        and camera_fov.center_dec_j2000_deg is None
        and camera_fov.solved_at is None
    ):
        return
    state.apply(
        CameraFovChanged(
            camera_fov=camera_fov.model_copy(
                update={
                    "rotation_deg": None,
                    "center_ra_j2000_h": None,
                    "center_dec_j2000_deg": None,
                    "solved_at": None,
                }
            )
        )
    )


__all__ = ["invalidate_solved_camera_fov", "publish_solved_camera_fov"]
