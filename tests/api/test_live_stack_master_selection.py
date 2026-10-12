# SPDX-License-Identifier: GPL-3.0-only

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from ssc.capture.fits import write_fits_file
from ssc.capture.frame import FrameType
from tests.api.test_capture_routes import (
    _configure_folder,
    _install_capture_service,
    _live_metadata,
)


@pytest.mark.parametrize("total,expected", [(1800 * 1024**2, 2), (3 * 1024**3 - 1, 2),
                                           (3 * 1024**3, 1), (3800 * 1024**2, 1),
                                           (8 * 1024**3, 1)])
def test_stack_reset_uses_host_ram_but_preserves_explicit_resolution(
    paired: TestClient, tmp_path: Path, monkeypatch, total: int, expected: int
) -> None:
    import ssc.system_metrics as system_metrics

    monkeypatch.setattr(system_metrics.psutil, "virtual_memory", lambda: SimpleNamespace(total=total))
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "ram-default-lights"
    watched.mkdir()
    started = paired.post("/api/capture/live-stack/reset", json={"watch_folder": str(watched)})
    assert started.status_code == 200, started.text
    assert started.json()["binning"] == expected
    explicit = 1 if expected == 2 else 2
    selected = paired.post("/api/capture/live-stack/reset",
                           json={"watch_folder": str(watched), "binning": explicit})
    assert selected.status_code == 200, selected.text
    assert selected.json()["binning"] == explicit
    # A subsequent automatic reset/panel must not override the user's choice.
    reset = paired.post("/api/capture/live-stack/reset", json={"watch_folder": str(watched)})
    assert reset.status_code == 200, reset.text
    assert reset.json()["binning"] == explicit
    assert paired.get("/api/capture/live-stack").json()["binning"] == explicit
    paired.post("/api/capture/live-stack/stop")


def test_explicit_binned_stack_is_preserved_in_status_and_stop(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "binned-lights"
    watched.mkdir()

    started = paired.post(
        "/api/capture/live-stack/reset",
        json={"watch_folder": str(watched), "binning": 2},
    )
    assert started.status_code == 200, started.text
    assert started.json()["binning"] == 2
    assert paired.get("/api/capture/live-stack").json()["binning"] == 2
    stopped = paired.post("/api/capture/live-stack/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["binning"] == 2


def test_first_arriving_light_does_not_change_the_masters_frozen_at_start(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "incoming"
    watched.mkdir()
    pixels = np.full((16, 16), 100, dtype=np.uint16)
    light_metadata = _live_metadata(
        exposure_s=30,
        gain=80,
        ccd_temp_c=10.0,
        filter_name="LP",
        object_name="M31",
    )
    dark = tmp_path / "Calibration" / "darks" / "matching-dark.fits"
    flat = tmp_path / "Calibration" / "flats" / "matching-flat.fits"
    dark.parent.mkdir(parents=True)
    flat.parent.mkdir(parents=True)
    write_fits_file(dark, np.zeros_like(pixels), replace(light_metadata, frame_type=FrameType.DARK))
    write_fits_file(
        flat,
        np.ones_like(pixels),
        replace(light_metadata, frame_type=FrameType.FLAT, optic_id="main"),
    )

    started = paired.post(
        "/api/capture/live-stack/reset",
        json={"watch_folder": str(watched), "optic_id": "main"},
    )
    assert started.status_code == 200, started.text
    assert started.json()["binning"] == 1
    assert started.json()["auto_select_masters"] is True
    assert started.json()["dark_master"] is None
    assert "continue without calibration masters" in started.json()["master_selection_message"]

    light = watched / "first-light.fits"
    write_fits_file(light, pixels, light_metadata)
    assert paired.post("/api/capture/live-stack/poll").status_code == 200
    integrated = paired.post("/api/capture/live-stack/poll")
    assert integrated.status_code == 200, integrated.text
    payload = integrated.json()
    assert payload["dark_master"] is None
    assert payload["flat_master"] is None
    assert payload["frame_count"] == 1
    assert payload["auto_select_masters"] is True
    assert "continue without calibration masters" in payload["master_selection_message"]

    stopped = paired.post("/api/capture/live-stack/stop")
    assert stopped.status_code == 200, stopped.text


def test_disabled_automatic_master_selection_is_reported_by_every_status(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "already-calibrated"
    watched.mkdir()

    started = paired.post(
        "/api/capture/live-stack/reset",
        json={"watch_folder": str(watched), "auto_select_masters": False},
    )

    assert started.status_code == 200, started.text
    assert started.json()["auto_select_masters"] is False
    status = paired.get("/api/capture/live-stack")
    assert status.status_code == 200, status.text
    assert status.json()["auto_select_masters"] is False


def test_seestar_ircut_and_widecam_headers_match_ssc_ir_tele_masters(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "seestar-lights"
    watched.mkdir()
    pixels = np.full((16, 16), 100, dtype=np.uint16)
    light = watched / "Light_M31_30s_IRCUT.fit"
    write_fits_file(
        light,
        pixels,
        replace(
            _live_metadata(exposure_s=30, gain=200, filter_name="IRCUT"),
            wide_camera=False,
        ),
    )
    bias = tmp_path / "Calibration" / "bias" / "master-bias.fits"
    flat = tmp_path / "Calibration" / "flats" / "master-flat.fits"
    bias.parent.mkdir(parents=True)
    flat.parent.mkdir(parents=True)
    write_fits_file(
        bias,
        np.zeros_like(pixels),
        replace(
            _live_metadata(exposure_s=0.00003, gain=200, filter_name="Dark"),
            frame_type=FrameType.BIAS,
        ),
    )
    write_fits_file(
        flat,
        np.ones_like(pixels),
        replace(
            _live_metadata(exposure_s=0.1, gain=200, filter_name="IR"),
            frame_type=FrameType.FLAT,
            optic_id="tele",
        ),
    )

    started = paired.post(
        "/api/capture/live-stack/reset",
        json={"watch_folder": str(watched), "auto_select_masters": True},
    )

    assert started.status_code == 200, started.text
    assert started.json()["bias_master"] == str(bias)
    assert started.json()["flat_master"] == str(flat)


def test_nearest_dark_temperature_is_selected_and_mismatch_is_prominent(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    watched = tmp_path / "lights"
    watched.mkdir()
    pixels = np.full((16, 16), 100, dtype=np.uint16)
    light = watched / "light.fits"
    write_fits_file(light, pixels, _live_metadata(exposure_s=30, gain=200, ccd_temp_c=20.0))
    dark_folder = tmp_path / "Calibration" / "darks"
    dark_folder.mkdir(parents=True)
    far = dark_folder / "far.fits"
    near = dark_folder / "near.fits"
    write_fits_file(
        far,
        np.zeros_like(pixels),
        replace(_live_metadata(exposure_s=30, gain=200, ccd_temp_c=10.0), frame_type=FrameType.DARK),
    )
    write_fits_file(
        near,
        np.zeros_like(pixels),
        replace(_live_metadata(exposure_s=30, gain=200, ccd_temp_c=19.0), frame_type=FrameType.DARK),
    )

    started = paired.post(
        "/api/capture/live-stack/reset",
        json={"watch_folder": str(watched), "auto_select_masters": True},
    )

    assert started.status_code == 200, started.text
    assert started.json()["dark_master"] == str(near)
    message = started.json()["master_selection_message"]
    assert "WARNING: dark temperature mismatch is 1.0 C" in message
    assert "light +20.0 C, master +19.0 C" in message


def test_live_stack_master_list_excludes_retained_raw_calibration_frames(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    raw = tmp_path / "Calibration" / "flats" / "IR" / "flat-raw.fits"
    master = tmp_path / "Calibration" / "flats" / "master-flat.fits"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"raw")
    master.write_bytes(b"master")

    response = paired.get("/api/capture/live-stack/masters")

    assert response.status_code == 200, response.text
    assert [item["path"] for item in response.json()["masters"]] == [str(master)]
