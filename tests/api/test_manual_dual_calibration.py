"""Manual lens ownership and physical Wide-cover contract, using fake hardware."""
import asyncio
from pathlib import Path

import pytest

from ssc.api.capture_orchestrator import CalibrationConfigurationError
from ssc.skymap.fits import read_fits_image
from tests.api.conftest import TestClient
from tests.api.test_capture_routes import (
    EXPOSURE_MIN_S,
    FakeDevice,
    _camera,
    _configure_folder,
    _image_frame,
    _install_capture_service,
    _wait_for,
)


@pytest.mark.parametrize("kind", ["dark", "bias"])
@pytest.mark.parametrize("optic", ["wide", "both"])
def test_wide_calibration_requires_cover_before_camera_commands(paired: TestClient, tmp_path: Path, kind: str, optic: str) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera()
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    body = {"kind": kind, "optic_id": optic, "count": 1}
    if kind == "dark":
        body["duration_s"] = 1
    response = paired.post("/api/capture/calibration/start", json=body)
    assert response.status_code == 422
    assert "covered" in response.json()["detail"]
    assert not tele.start_exposure_calls and not wide.start_exposure_calls
    assert not tele.put_calls and not wide.put_calls


def test_manual_both_captures_separate_lens_files_and_never_uses_tele_filter_for_wide(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera(properties={"exposuremin": 0.002, "lastexposureduration": 0.002})
    tele.properties["lastexposureduration"] = EXPOSURE_MIN_S
    wheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR"]})
    _install_capture_service(paired, camera=tele, wide_camera=wide, filterwheel=wheel)
    response = paired.post("/api/capture/calibration/start", json={
        "kind": "bias", "optic_id": "both", "count": 1,
        "covered_lens_confirmed": True, "generate_master": False,
    })
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda value: value["in_progress"] is False)
    assert final["calibration"]["status"] == "completed", final
    assert tele.start_exposure_calls == [(EXPOSURE_MIN_S, False)]
    assert wide.start_exposure_calls == [(0.002, False)]
    assert wheel.put_calls == [("position", {"Position": 0})]
    files = list(tmp_path.rglob("*.fits"))
    assert len(files) == 2
    headers = {read_fits_image(path.read_bytes()).header["OPTICID"]: (path, read_fits_image(path.read_bytes()).header) for path in files}
    assert headers.keys() == {"tele", "wide"}
    assert "tele" in headers["tele"][0].parts
    assert "wide" in headers["wide"][0].parts
    assert "FILTER" not in headers["wide"][1]


def test_scheduled_calibration_cannot_request_wide(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    _, service = _install_capture_service(paired, camera=_camera(), wide_camera=_camera())
    async def attempt() -> None:
        with pytest.raises(CalibrationConfigurationError, match="Tele only"):
            await service.start_calibration(kind="bias", count=1, duration_s=None, gain=100,
                filter_positions=[], optic_id="both", flat_source=None, generate_master=False,
                keep_only_master=False, covered_lens_confirmed=True, run_id="r", block_id="b")
    paired.portal.call(attempt)


def test_cancelling_tele_set_keeps_raw_and_does_not_start_wide(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera()
    _, service = _install_capture_service(paired, camera=tele, wide_camera=wide)
    entered, release = asyncio.Event(), asyncio.Event()
    async def held_image():
        entered.set()
        await release.wait()
        return tele.frame
    tele.image_array = held_image
    response = paired.post("/api/capture/calibration/start", json={
        "kind": "bias", "optic_id": "both", "count": 2, "covered_lens_confirmed": True,
    })
    assert response.status_code == 202
    async def cancel() -> None:
        await asyncio.wait_for(entered.wait(), timeout=5)
        await service.cancel_calibration()
        release.set()
    paired.portal.call(cancel)
    final = _wait_for(paired, lambda value: value["in_progress"] is False)
    assert final["calibration"]["status"] == "cancelled"
    assert len(list(tmp_path.rglob("*.fits"))) == 1
    assert not wide.start_exposure_calls


def test_both_bias_masters_and_source_sets_keep_same_dimension_lenses_separate(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera()
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    response = paired.post("/api/capture/calibration/start", json={
        "kind": "bias", "optic_id": "both", "count": 10,
        "covered_lens_confirmed": True, "generate_master": True,
    })
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda value: value["in_progress"] is False)
    assert final["calibration"]["status"] == "completed", final["last_error"]
    paths = final["calibration"]["master_paths"]
    assert len(paths) == 2
    assert any("master_bias_tele_" in path for path in paths)
    assert any("master_bias_wide_" in path for path in paths)
    assert {read_fits_image(Path(path).read_bytes()).header["OPTICID"] for path in paths} == {"tele", "wide"}
    response = paired.get("/api/capture/calibration/source-sets")
    assert response.status_code == 200
    groups = response.json()["sets"]
    assert len(groups) == 2
    assert {group["optic_id"] for group in groups} == {"tele", "wide"}
    assert all(group["frame_count"] == 10 for group in groups)


def test_wide_flat_uses_no_filter_wheel_and_rejects_tele_only_bias_dependency(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera(frame=_image_frame(fill=30000))
    wheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR"]})
    _install_capture_service(paired, camera=tele, wide_camera=wide, filterwheel=wheel)
    bias = paired.post("/api/capture/calibration/start", json={"kind": "bias", "count": 10, "gain": 100})
    assert bias.status_code == 202
    _wait_for(paired, lambda value: value["in_progress"] is False)
    body = {"kind": "flat", "count": 10, "gain": 100, "optic_id": "wide", "flat_source": "panel", "generate_master": True}
    rejected = paired.post("/api/capture/calibration/start", json=body)
    assert rejected.status_code == 422
    assert not wide.start_exposure_calls
    wheel.put_calls.clear()
    body.update(count=1, generate_master=False)
    response = paired.post("/api/capture/calibration/start", json=body)
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda value: value["in_progress"] is False)
    assert final["calibration"]["status"] == "completed", final["last_error"]
    assert len(wide.start_exposure_calls) == 2  # illumination probe then retained flat
    assert wheel.put_calls == []
    flats = [path for path in tmp_path.rglob("*.fits") if read_fits_image(path.read_bytes()).header["IMAGETYP"] == "flat"]
    assert len(flats) == 1
    header = read_fits_image(flats[0].read_bytes()).header
    assert header["OPTICID"] == "wide"
    assert "FILTER" not in header
    sets = paired.get("/api/capture/calibration/source-sets").json()["sets"]
    flat_set = next(group for group in sets if group["frame_type"] == "flat")
    assert flat_set["compatible_bias_frames"] == 0


async def test_legacy_wide_flat_reads_its_own_camera_maxadu(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from ssc.api import capture_orchestrator

    path = tmp_path / "legacy-wide-flat.fits"
    path.write_bytes(b"legacy header handled by reader")
    monkeypatch.setattr(capture_orchestrator, "read_fits_image", lambda data: SimpleNamespace(header={"OPTICID": "wide"}))
    service = object.__new__(capture_orchestrator.CaptureService)
    service._camera = SimpleNamespace(get=AsyncMock(return_value=4095))
    service._wide_camera = SimpleNamespace(get=AsyncMock(return_value=65535))
    assert await service._master_max_adu([SimpleNamespace(path=path)]) == 65535
    service._camera.get.assert_not_awaited()
    service._wide_camera.get.assert_awaited_once_with("maxadu")
