# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc/api/routes/capture.py`` + ``ssc/api/capture_orchestrator.py``: the
one way in for ``ssc.api.capture_service`` (dispatch: "Piece 1 — the capture
API"). No endpoint existed before this; nothing could trigger a capture.

The app here is the ordinary test wiring (``FakeGateway``, no hosted Alpaca
driver — ``tests/api/conftest.py``), so ``services.capture`` starts with no
camera. Each test that needs one swaps ``services.capture`` for a
``CaptureService`` built against a fake camera/telescope/filterwheel — the
same structural fake ``tests/api/test_capture_service.py`` drives
``capture_service.drive_exposure`` with directly, extended with ``put`` for
``Camera.Gain``.
"""

from __future__ import annotations

import asyncio
import contextlib
import struct
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import ssc.api.capture_orchestrator as capture_orchestrator
from ssc.api.capture_orchestrator import CaptureService
from ssc.api.deps import Services
from ssc.astro.frames import j2000_coord
from ssc.astro.units import Deg
from ssc.capture.fits import write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError
from ssc.client.imagebytes import ImageArrayElementType, ImageBytesFrame
from ssc.platesolve import PlateSolveResult, SolveIssue, SolverAvailability
from ssc.skymap.fits import read_fits_image
from ssc.stacking.live import LiveStackFrameRejectedError
from ssc.store.capture import CaptureRepository
from tests.api.conftest import TestClient, services_of


def _live_metadata(
    *,
    exposure_s: float | None = None,
    gain: int | None = None,
    ccd_temp_c: float | None = None,
    filter_name: str | None = None,
    object_name: str | None = None,
) -> FrameMetadata:
    return FrameMetadata(
        date_obs=None,
        exposure_s=exposure_s,
        gain=gain,
        ccd_temp_c=ccd_temp_c,
        bayer_offset_x=None,
        bayer_offset_y=None,
        pixel_size_um=None,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=filter_name,
        object_name=object_name,
        frame_type=FrameType.LIGHT,
        focal_length_mm=None,
        telescope_name="test",
        ssc_version="0.0.0",
        device_firmware=None,
    )


def _image_frame(*, fill: int = 7) -> ImageBytesFrame:
    array = (np.arange(16, dtype=np.uint16).reshape(4, 4) + fill).astype(np.uint16)
    return ImageBytesFrame(
        metadata_version=1,
        client_transaction_id=1,
        server_transaction_id=1,
        image_element_type=ImageArrayElementType.UINT16,
        transmission_element_type=ImageArrayElementType.UINT16,
        array=array,
    )


def _image_frame_sized(height: int, width: int, *, fill: int = 7) -> ImageBytesFrame:
    """Like ``_image_frame`` but at a chosen size — needed to actually
    exercise ``?thumbnail=true``'s downsample, which is a no-op below
    ``_THUMBNAIL_MAX_DIM`` (``ssc.api.capture_orchestrator``)."""
    size = height * width
    array = (np.arange(size, dtype=np.uint16).reshape(height, width) % 4096 + fill).astype(
        np.uint16
    )
    return ImageBytesFrame(
        metadata_version=1,
        client_transaction_id=1,
        server_transaction_id=1,
        image_element_type=ImageArrayElementType.UINT16,
        transmission_element_type=ImageArrayElementType.UINT16,
        array=array,
    )


def _png_ihdr(data: bytes) -> tuple[int, int, int]:
    """(width, height, colour_type) read straight out of the PNG's own IHDR
    chunk — the dependency-free way to check what ``ssc.skymap.png`` actually
    encoded, without adding a PNG-decoding library to the test suite."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    _length, chunk_type = struct.unpack(">I4s", data[8:16])
    assert chunk_type == b"IHDR"
    width, height, _bit_depth, colour_type = struct.unpack(">IIBB", data[16:26])
    return width, height, colour_type


class FakeDevice:
    """A ``Device`` (plus ``put``, for ``GainSettableCamera``) that answers
    exactly what a test hands it — mirrors
    ``tests.api.test_capture_service.FakeDevice``."""

    def __init__(
        self,
        *,
        properties: dict[str, object] | None = None,
        not_implemented: frozenset[str] = frozenset(),
        connected: bool = True,
    ) -> None:
        self.properties = dict(properties or {})
        self.not_implemented = not_implemented
        self.connected = connected
        self.get_calls: list[str] = []
        self.put_calls: list[tuple[str, dict[str, object]]] = []

    async def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def get(self, member: str, **params: object) -> object:
        self.get_calls.append(member)
        if member in self.not_implemented:
            raise AlpacaRemoteError(
                int(AlpacaErrorNumber.NOT_IMPLEMENTED), "not implemented", member=f"Fake.{member}"
            )
        return self.properties[member]

    async def put(self, member: str, **params: object) -> object:
        self.put_calls.append((member, params))
        return None


class FakeCamera(FakeDevice):
    def __init__(
        self,
        *,
        frame: ImageBytesFrame,
        ready_after_polls: int = 1,
        start_exposure_not_implemented: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.frame = frame
        self.ready_after_polls = ready_after_polls
        self.start_exposure_not_implemented = start_exposure_not_implemented
        self.start_exposure_calls: list[tuple[float, bool]] = []
        self.image_ready_calls = 0

    async def start_exposure(self, duration_s: float, *, light: bool = True) -> None:
        if self.start_exposure_not_implemented:
            raise AlpacaRemoteError(
                int(AlpacaErrorNumber.NOT_IMPLEMENTED),
                "StartExposure not implemented on this driver",
                member="Camera.StartExposure",
            )
        self.start_exposure_calls.append((duration_s, light))

    async def image_ready(self) -> bool:
        self.image_ready_calls += 1
        return self.image_ready_calls >= self.ready_after_polls

    async def image_array(self) -> ImageBytesFrame:
        return self.frame


#: This device's own measured limits (dispatch brief) — deliberately not a
#: round number, so a test that only passed by luck against a hardcoded
#: [0, 3600] guess is caught.
EXPOSURE_MIN_S = 3e-05
EXPOSURE_MAX_S = 2000.0


def _camera(**kwargs: object) -> FakeCamera:
    defaults: dict[str, object] = {
        "properties": {
            "exposuremin": EXPOSURE_MIN_S,
            "exposuremax": EXPOSURE_MAX_S,
            "gain": 100,
            "ccdtemperature": 20.0,
            "bayeroffsetx": 0,
            "bayeroffsety": 1,
            "pixelsizex": 2.9,
            "pixelsizey": 2.9,
            "binx": 1,
            "biny": 1,
            "startx": 0,
            "starty": 0,
            "sensorname": "IMX585",
            "offset": 1108,
            "maxadu": 65535,
            "readoutmode": 0,
            "numx": 4,
            "numy": 4,
            "cameraxsize": 4,
            "cameraysize": 4,
            "lastexposurestarttime": "2026-09-10T14:00:00",
            "lastexposureduration": 1.0,
        },
        "frame": _image_frame(),
    }
    provided_properties = kwargs.pop("properties", None)
    if provided_properties is not None:
        assert isinstance(provided_properties, dict)
        properties = defaults["properties"]
        assert isinstance(properties, dict)
        properties.update(provided_properties)
    defaults.update(kwargs)
    return FakeCamera(**defaults)  # type: ignore[arg-type]


def _telescope() -> FakeDevice:
    return FakeDevice(
        properties={
            "rightascension": 5.5833,
            "declination": -5.39,
            "equatorialsystem": 2,
            "sitelatitude": 48.2,
            "sitelongitude": 16.4,
            "alignmentmode": 1,
            "focallength": 0.16,
            "aperturediameter": 0.03,
        }
    )


def _filterwheel() -> FakeDevice:
    return FakeDevice(properties={"position": 0, "names": ["LP"]})


def _configure_folder(client: TestClient, folder: Path) -> None:
    """Configure the capture folder, and a location fix — the default
    capture folder template (``%TARGET%/%DATEOFCAPTURE%/%IMAGETYPE%``) needs
    a resolvable timezone, so any test whose capture is meant to actually
    proceed needs a location set too (CLAUDE.md invariant 5)."""
    response = client.put("/api/settings/capture-folder", json={"folder": str(folder)})
    assert response.status_code == 200, response.text
    response = client.put(
        "/api/settings/location/fix",
        json={
            "source": "manual",
            "latitude_deg": 48.2082,
            "longitude_deg": 16.3738,
            "timezone": "Europe/Vienna",
        },
    )
    assert response.status_code == 200, response.text


def _install_capture_service(
    client: TestClient,
    *,
    camera: FakeCamera | None,
    wide_camera: FakeCamera | None = None,
    telescope: FakeDevice | None = None,
    filterwheel: FakeDevice | None = None,
    focuser: FakeDevice | None = None,
    solve: Callable[[Path], Awaitable[PlateSolveResult]] | None = None,
    availability: Callable[[], Awaitable[SolverAvailability]] | None = None,
    dither: Callable[[float, float], Awaitable[None]] | None = None,
    recenter: Callable[[float, float], Awaitable[None]] | None = None,
) -> tuple[Services, CaptureService]:
    services = services_of(client)
    repo = CaptureRepository(services.database)

    async def deterministic_solve(_path: Path) -> PlateSolveResult:
        return PlateSolveResult(
            coord=j2000_coord(Deg(150.0), Deg(20.0)),
            date_obs=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
            pixel_scale_arcsec=2.4,
            rotation_deg=0.0,
            solver="test",
            solver_version="1",
        )

    async def deterministic_availability() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    kwargs: dict[str, object] = {
        "solve": solve or deterministic_solve,
        "availability": availability or deterministic_availability,
    }
    if dither is not None:
        kwargs["dither"] = dither
    if recenter is not None:
        kwargs["recenter"] = recenter
    service = CaptureService(
        camera=camera,
        wide_camera=wide_camera,
        telescope=telescope,
        filterwheel=filterwheel,
        focuser=focuser,
        repo=repo,
        settings=services.settings,
        state=services.state,
        tasks=services.tasks,
        section=services.capture.section,
        **kwargs,
    )
    services.capture = service
    return services, service


def _wait_for(
    client: TestClient,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout_s: float = 10.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        response = client.get("/api/state")
        assert response.status_code == 200, response.text
        last = response.json()["snapshot"]["capture"]
        assert last is not None
        if predicate(last):
            return last
        time.sleep(0.02)
    raise AssertionError(f"condition not met within {timeout_s}s; last capture section: {last}")


def _terminal_operation(client: TestClient, response: Any) -> dict[str, Any]:
    operation_id = response.json()["id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        operation = client.get(f"/api/operations/{operation_id}").json()
        if operation["status"] in {"succeeded", "failed", "cancelled"}:
            return operation
        time.sleep(0.01)
    raise AssertionError(f"operation {operation_id} did not finish")


# -- the happy path: 202, no blocking, catalogued, snapshot reflects it ----- #


def test_flat_calibration_rejects_the_dark_shutter_position(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    filterwheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR", "LP"]})
    _install_capture_service(paired, camera=camera, filterwheel=filterwheel)

    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "flat",
            "count": 1,
            "gain": 100,
            "filter_positions": [0, 1],
            "optic_id": "tele",
            "flat_source": "panel",
            "generate_master": False,
        },
    )

    assert response.status_code == 422, response.text
    assert "Dark shutter position cannot be used for flat frames" in response.json()["detail"]
    assert camera.start_exposure_calls == []
    assert filterwheel.put_calls == []


def test_flat_master_preflight_rejects_missing_biases_before_capture(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera(not_implemented=frozenset({"numx", "numy"}))
    filterwheel = FakeDevice(properties={"position": 1, "names": ["Dark", "IR", "LP"]})
    _install_capture_service(paired, camera=camera, filterwheel=filterwheel)

    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "flat",
            "count": 30,
            "gain": 200,
            "filter_positions": [1],
            "optic_id": "tele",
            "flat_source": "panel",
            "generate_master": True,
        },
    )

    assert response.status_code == 422, response.text
    assert response.json()["detail"] == (
        "a flat master requires at least 10 retained bias frames at gain 200 and 4x4; "
        "capture a bias set first. No flat frames were captured."
    )
    assert camera.start_exposure_calls == []
    assert "cameraxsize" in camera.get_calls and "cameraysize" in camera.get_calls
    assert "numx" not in camera.get_calls and "numy" not in camera.get_calls
    assert filterwheel.put_calls == []


def test_rebuilds_flat_master_from_retained_catalogue_set(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)

    async def plant() -> tuple[list[int], list[Path]]:
        repo = CaptureRepository(services.database)
        flat_ids: list[int] = []
        paths: list[Path] = []
        started = datetime(2026, 9, 14, 7, 0, tzinfo=UTC)
        for kind, count, value, filter_name, offset in (
            (FrameType.FLAT, 10, 30_000, "IR", 0),
            (FrameType.BIAS, 10, 1000, "Dark", 3600),
        ):
            for index in range(count):
                observed = started + timedelta(seconds=offset + index)
                path = tmp_path / "Calibration" / kind.value / f"{kind.value}-{index}.fits"
                metadata = replace(
                    _live_metadata(
                        exposure_s=0.1 if kind is FrameType.FLAT else EXPOSURE_MIN_S,
                        gain=200,
                        ccd_temp_c=30.0,
                        filter_name=filter_name,
                    ),
                    frame_type=kind,
                    optic_id="tele" if kind is FrameType.FLAT else None,
                    flat_source="panel" if kind is FrameType.FLAT else None,
                    max_adu=65535.0,
                )
                path.parent.mkdir(parents=True, exist_ok=True)
                write_fits_file(path, np.full((4, 4), value, np.uint16), metadata)
                frame_id = await repo.begin_frame(
                    path=path,
                    date_obs=observed,
                    object_name=None,
                    filter_name=filter_name,
                    frame_type=kind.value,
                    exposure_s=metadata.exposure_s,
                    gain=200,
                    ccd_temp_c=30.0,
                    width=4,
                    height=4,
                )
                await repo.complete_frame(frame_id)
                paths.append(path)
                if kind is FrameType.FLAT:
                    flat_ids.append(frame_id)
        return flat_ids, paths

    flat_ids, source_paths = paired.portal.call(plant)  # type: ignore[attr-defined]
    candidates = paired.get("/api/capture/calibration/source-sets")
    assert candidates.status_code == 200, candidates.text
    flat_set = next(item for item in candidates.json()["sets"] if item["frame_type"] == "flat")
    assert flat_set["frame_ids"] == flat_ids
    assert flat_set["can_build"] is True
    assert flat_set["compatible_bias_frames"] == 10

    response = paired.post("/api/capture/calibration/masters/build", json={"frame_ids": flat_ids})
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    master = Path(final["calibration"]["master_paths"][0])
    assert master.is_file()
    assert np.median(read_fits_image(master.read_bytes()).data) == pytest.approx(1.0)
    assert all(path.is_file() for path in source_paths)


def test_automatic_bias_set_selects_the_named_dark_filter(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    filterwheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR", "LP"]})
    _install_capture_service(paired, camera=camera, filterwheel=filterwheel)

    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "bias",
            "count": 1,
            "gain": 100,
            "generate_master": False,
        },
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["last_error"] is None
    assert final["calibration"]["status"] == "completed"
    assert filterwheel.put_calls == [("position", {"Position": 0})]
    assert camera.start_exposure_calls == [(EXPOSURE_MIN_S, False)]


def test_calibration_capture_uses_its_configured_path_and_filename(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    configured = paired.put(
        "/api/settings/calibration-path-templates",
        json={
            "bias_folder_template": "Cal/%IMAGETYPE%/%GAIN%",
            "dark_folder_template": "Cal/darks",
            "flat_folder_template": "Cal/flats/%FILTER%",
            "filename_template": "%IMAGETYPE%-%GAIN%-%SEQUENCE%",
        },
    )
    assert configured.status_code == 200, configured.text
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _install_capture_service(paired, camera=camera)

    response = paired.post(
        "/api/capture/calibration/start",
        json={"kind": "bias", "count": 1, "gain": 100, "generate_master": False},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    files = list((tmp_path / "Cal" / "biases" / "100").glob("*.fits"))
    assert len(files) == 1
    assert files[0].name.startswith("biases-100-")


def test_running_calibration_count_can_be_shortened_between_frames(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _, service = _install_capture_service(paired, camera=camera)

    response = paired.post(
        "/api/capture/calibration/start",
        json={"kind": "bias", "count": 100, "gain": 100, "generate_master": False},
    )
    assert response.status_code == 202, response.text
    running = _wait_for(
        paired,
        lambda section: section["calibration"]["completed_frames"] >= 1,
    )
    completed = running["calibration"]["completed_frames"]

    updated = paired.portal.call(  # type: ignore[attr-defined]
        service.update_active_calibration_count, completed + 1
    )
    assert updated.calibration.requested_count == completed + 1
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    assert final["calibration"]["completed_frames"] == completed + 1
    assert len(camera.start_exposure_calls) == completed + 1


def test_calibration_survives_frontend_disconnect_and_cancelled_waiter(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    _, service = _install_capture_service(paired, camera=camera)
    gate = asyncio.Event()

    async def held_image() -> ImageBytesFrame:
        await gate.wait()
        return camera.frame

    camera.image_array = held_image  # type: ignore[method-assign]
    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "dark",
            "count": 2,
            "duration_s": 0.01,
            "gain": 100,
            "generate_master": False,
        },
    )
    assert response.status_code == 202, response.text
    with paired.websocket_connect("/ws") as websocket:
        assert websocket.receive_json()["type"] == "snapshot"
    with paired.websocket_connect("/ws") as websocket:
        snapshot = websocket.receive_json()["snapshot"]
        assert snapshot["capture"]["calibration"]["status"] == "running"

    async def cancel_waiter() -> None:
        waiter = asyncio.create_task(service.wait_for_calibration())
        await asyncio.sleep(0)
        waiter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await waiter
        assert service._calibration_task is not None
        assert not service._calibration_task.done()
        gate.set()

    paired.portal.call(cancel_waiter)  # type: ignore[attr-defined]
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    assert final["calibration"]["completed_frames"] == 2


def test_master_only_calibration_removes_sources_after_verified_master(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _install_capture_service(paired, camera=camera)

    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "bias",
            "count": 10,
            "generate_master": True,
            "keep_only_master": True,
        },
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    assert final["calibration"]["removed_source_frames"] == 10
    assert "10 source frame(s) removed" in final["calibration"]["message"]
    master = Path(final["calibration"]["master_paths"][0])
    assert master.is_file()
    assert master.with_suffix(".json").is_file()
    assert paired.get("/api/capture/frames").json()["frames"] == []
    assert list((tmp_path / "Calibration" / "bias" / "singles").glob("*.fits")) == []


def test_master_integration_clears_the_last_frame_stage(
    paired: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _, service = _install_capture_service(paired, camera=camera)
    observed = False
    original = service._build_one_calibration_master

    async def inspect_transition(**kwargs: Any) -> str:
        nonlocal observed
        section = service.section
        assert section.in_progress is True
        assert section.calibration.phase == "integrating"
        assert section.calibration.message == "Building calibration master"
        assert section.stage is None
        assert section.duration_s is None
        assert section.started_at is None
        assert section.object_name is None
        assert section.frame_type is None
        assert section.gain is None
        observed = True
        return await original(**kwargs)

    monkeypatch.setattr(service, "_build_one_calibration_master", inspect_transition)
    response = paired.post(
        "/api/capture/calibration/start",
        json={"kind": "bias", "count": 10, "gain": 100, "generate_master": True},
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    assert observed is True


def test_master_only_calibration_retains_sources_when_master_build_fails(
    paired: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _install_capture_service(paired, camera=camera)

    def fail_master(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise capture_orchestrator.CalibrationMasterError("synthetic integration failure")

    monkeypatch.setattr(capture_orchestrator, "build_calibration_master", fail_master)
    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "bias",
            "count": 10,
            "generate_master": True,
            "keep_only_master": True,
        },
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "failed"
    assert final["calibration"]["removed_source_frames"] == 0
    frames = paired.get("/api/capture/frames").json()["frames"]
    assert len(frames) == 10
    assert all(Path(frame["path"]).is_file() for frame in frames)


def test_master_only_requires_master_generation(paired: TestClient) -> None:
    response = paired.post(
        "/api/capture/calibration/start",
        json={
            "kind": "bias",
            "count": 10,
            "generate_master": False,
            "keep_only_master": True,
        },
    )
    assert response.status_code == 422, response.text
    assert "keep_only_master requires generate_master=true" in response.text


def test_start_exposure_returns_202_without_blocking_and_catalogues_the_frame(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    focuser = FakeDevice(properties={"position": 1210, "temperature": 31.3125})
    _install_capture_service(
        paired,
        camera=camera,
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        focuser=focuser,
    )

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "object": "M42", "gain": 150, "frame_type": "light"},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    # Accepted, not finished — a blocking implementation would already show
    # this False and a frame id, since the fake camera's own exposure is
    # instant (ready_after_polls=1).
    assert body["in_progress"] is True
    assert body["folder_configured"] is True

    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["last_frame_id"] is not None
    assert final["last_error"] is None
    assert final["last_captured_at"] is not None

    # Gain was set on the camera before the exposure was commanded.
    assert camera.put_calls == [("gain", {"Gain": 150})]
    assert camera.start_exposure_calls == [(1.0, True)]

    frames = paired.get("/api/capture/frames")
    assert frames.status_code == 200, frames.text
    listed = frames.json()["frames"]
    assert len(listed) == 1
    assert listed[0]["id"] == final["last_frame_id"]
    assert listed[0]["object_name"] == "M42"
    assert listed[0]["status"] == "complete"
    image = read_fits_image(Path(listed[0]["path"]).read_bytes())
    assert image.header["FOCUSPOS"] == 1210
    assert image.header["FOCTEMP"] == pytest.approx(31.3125)

    sessions = paired.get("/api/capture/sessions")
    assert sessions.status_code == 200, sessions.text
    assert [s["object_name"] for s in sessions.json()["sessions"]] == ["M42"]
    assert sessions.json()["sessions"][0]["frame_count"] == 1


def test_capture_loop_continues_without_a_browser_request_and_rehydrates_from_state(
    paired: TestClient, tmp_path: Path
) -> None:
    """One HTTP start owns the whole sequence; clients only observe/control it."""
    _configure_folder(paired, tmp_path)
    camera = _camera()

    async def available() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    async def must_not_solve(_path: Path) -> PlateSolveResult:
        raise AssertionError("a default light loop must not plate solve")

    _install_capture_service(
        paired,
        camera=camera,
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        solve=must_not_solve,
        availability=available,
    )

    response = paired.post(
        "/api/capture/exposure",
        json={
            "duration_s": 1.0,
            "object": "M42",
            "gain": 200,
            "frame_type": "light",
            "capture_mode": "loop",
        },
    )
    assert response.status_code == 202, response.text
    assert response.json()["loop_active"] is True

    # No second browser request is made. The server starts subsequent frames
    # itself, and a fresh /api/state reader can recover the counters.
    running = _wait_for(paired, lambda section: section["loop_completed_frames"] >= 2)
    assert running["loop_active"] is True
    assert len(camera.start_exposure_calls) >= 3

    stopped = paired.post("/api/capture/loop/stop")
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["loop_active"] is False
    calls_when_stopped = len(camera.start_exposure_calls)
    _wait_for(paired, lambda section: section["in_progress"] is False)
    time.sleep(0.05)
    assert len(camera.start_exposure_calls) == calls_when_stopped


@pytest.mark.parametrize("frame_limit", [1, 3])
def test_finite_capture_loop_stops_after_saved_frames_without_browser_requests(
    paired: TestClient, tmp_path: Path, frame_limit: int
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    _install_capture_service(
        paired, camera=camera, telescope=_telescope(), filterwheel=_filterwheel()
    )
    response = paired.post(
        "/api/capture/exposure",
        json={
            "duration_s": 1.0,
            "object": "M42",
            "gain": 200,
            "frame_type": "light",
            "capture_mode": "loop",
            "frame_limit": frame_limit,
        },
    )
    assert response.status_code == 202, response.text
    final = _wait_for(
        paired, lambda section: not section["loop_active"] and not section["in_progress"]
    )
    assert final["loop_completed_frames"] == frame_limit
    assert final["loop_frame_limit"] == frame_limit
    assert len(camera.start_exposure_calls) == frame_limit
    assert len(paired.get("/api/capture/frames").json()["frames"]) == frame_limit
    time.sleep(0.05)
    assert len(camera.start_exposure_calls) == frame_limit


@pytest.mark.parametrize(
    "mode,frame_limit", [("loop", 0), ("loop", -1), ("loop", 1.5), ("single", 3)]
)
def test_rejects_invalid_or_single_shot_frame_limit(
    paired: TestClient, mode: str, frame_limit: float
) -> None:
    response = paired.post(
        "/api/capture/exposure",
        json={
            "duration_s": 1.0,
            "frame_type": "light",
            "capture_mode": mode,
            "frame_limit": frame_limit,
        },
    )
    assert response.status_code == 422


def test_center_after_drift_delegates_to_closed_loop_centering_once(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    solves = [
        PlateSolveResult(
            coord=j2000_coord(Deg(150.0), Deg(20.0)),
            date_obs=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
            pixel_scale_arcsec=2.4,
            rotation_deg=0.0,
            solver="fake",
            solver_version="1",
        ),
        PlateSolveResult(
            coord=j2000_coord(Deg(150.1), Deg(20.0)),
            date_obs=datetime(2026, 9, 13, 20, 1, tzinfo=UTC),
            pixel_scale_arcsec=2.4,
            rotation_deg=0.0,
            solver="fake",
            solver_version="1",
        ),
    ]
    recentered: list[tuple[float, float]] = []

    async def available() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    async def solve(_path: Path) -> PlateSolveResult:
        return solves.pop(0)

    async def recenter(ra_h: float, dec_deg: float) -> None:
        recentered.append((ra_h, dec_deg))
        service.stop_capture_loop()

    services, service = _install_capture_service(
        paired,
        camera=_camera(),
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        solve=solve,
        availability=available,
        recenter=recenter,
    )

    async def configure_centering() -> None:
        await services.settings.set_capture_center_after_drift(enabled=True, threshold_pixels=1.0)

    paired.portal.call(configure_centering)  # type: ignore[attr-defined]

    response = paired.post(
        "/api/capture/exposure",
        json={
            "duration_s": 1.0,
            "object": "M42",
            "gain": 150,
            "frame_type": "light",
            "capture_mode": "loop",
        },
    )
    assert response.status_code == 202, response.text
    final = _wait_for(
        paired,
        lambda section: section["in_progress"] is False and section["loop_active"] is False,
    )
    assert final["last_error"] is None

    assert recentered == [(10.0, 20.0)]
    assert solves == [], "closed-loop callback already performs and verifies its own fresh solves"


def test_manual_exposures_never_invoke_loop_centering(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    solve_calls: list[Path] = []

    async def available() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    async def solve(path: Path) -> PlateSolveResult:
        solve_calls.append(path)
        raise AssertionError("a manual one-shot must not plate solve for drift centering")

    async def dither(_ra_h: float, _dec_deg: float) -> None:
        raise AssertionError("a manual one-shot must not dither")

    services, service = _install_capture_service(
        paired,
        camera=_camera(),
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        solve=solve,
        availability=available,
        dither=dither,
    )

    async def configure_loop_automation() -> None:
        await services.settings.set_capture_center_after_drift(enabled=True, threshold_pixels=1.0)
        await services.settings.set_capture_dither(
            enabled=True, every_n_frames=1, min_pixels=15.0, max_pixels=30.0
        )

    paired.portal.call(configure_loop_automation)  # type: ignore[attr-defined]
    service._light_sequence = 1
    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "object": "M42", "gain": 150, "frame_type": "light"},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)

    assert final["last_error"] is None
    assert final["last_frame_id"] is not None
    assert solve_calls == []


def test_bias_uses_the_cameras_shortest_exposure_and_writes_bias_fits(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _install_capture_service(
        paired, camera=camera, telescope=_telescope(), filterwheel=_filterwheel()
    )

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 0, "object": None, "gain": 150, "frame_type": "bias"},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["last_error"] is None
    assert camera.start_exposure_calls == [(EXPOSURE_MIN_S, False)]

    frames = paired.get("/api/capture/frames").json()["frames"]
    assert len(frames) == 1
    assert frames[0]["frame_type"] == "bias"
    assert frames[0]["exposure_s"] == EXPOSURE_MIN_S
    assert "biases" in Path(frames[0]["path"]).parts
    assert Path(frames[0]["path"]).name.startswith("biases_0.00003s_")
    image = read_fits_image(Path(frames[0]["path"]).read_bytes())
    assert image.header["IMAGETYP"] == "bias"
    assert image.header["EXPTIME"] == pytest.approx(EXPOSURE_MIN_S)


def test_bias_selects_the_named_dark_filter_before_exposing(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    filterwheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR", "LP"]})
    _install_capture_service(paired, camera=camera, filterwheel=filterwheel)

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 0, "gain": 150, "frame_type": "bias"},
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["last_error"] is None
    assert filterwheel.put_calls == [("position", {"Position": 0})]
    assert camera.start_exposure_calls == [(EXPOSURE_MIN_S, False)]


def test_dark_button_selects_the_named_dark_filter_before_exposing(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    filterwheel = FakeDevice(properties={"position": 0, "names": ["Dark", "IR", "LP"]})
    _install_capture_service(paired, camera=camera, filterwheel=filterwheel)

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "gain": 150, "frame_type": "dark"},
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["last_error"] is None
    assert filterwheel.put_calls == [("position", {"Position": 0})]
    assert camera.start_exposure_calls == [(1.0, False)]


def test_bias_refuses_a_nonzero_duration(paired: TestClient) -> None:
    response = paired.post("/api/capture/exposure", json={"duration_s": 1, "frame_type": "bias"})
    assert response.status_code == 422, response.text
    assert "duration_s=0" in response.text


# -- each error path, synchronous --------------------------------------------- #


def test_start_exposure_without_a_folder_configured_is_409(paired: TestClient) -> None:
    response = paired.post("/api/capture/exposure", json={"duration_s": 1.0, "frame_type": "light"})
    assert response.status_code == 409, response.text
    assert "folder" in response.json()["detail"]


def test_start_exposure_with_no_camera_hosted_is_503(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    # No camera installed: the default wiring against ``FakeGateway`` hosts
    # no Alpaca driver at all (module docstring).
    response = paired.post("/api/capture/exposure", json={"duration_s": 1.0, "frame_type": "light"})
    assert response.status_code == 503, response.text


def test_omitted_capture_gain_defaults_to_200(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    _install_capture_service(paired, camera=camera)

    response = paired.post("/api/capture/exposure", json={"duration_s": 1.0, "frame_type": "light"})

    assert response.status_code == 202, response.text
    _wait_for(paired, lambda section: section["in_progress"] is False)
    assert ("gain", {"Gain": 200}) in camera.put_calls


def test_start_exposure_while_already_running_is_409(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera(ready_after_polls=1_000_000)
    _install_capture_service(paired, camera=camera)

    first = paired.post("/api/capture/exposure", json={"duration_s": 2.0, "frame_type": "light"})
    assert first.status_code == 202, first.text

    second = paired.post("/api/capture/exposure", json={"duration_s": 2.0, "frame_type": "light"})
    assert second.status_code == 409, second.text
    assert "already running" in second.json()["detail"]


def test_start_exposure_outside_the_cameras_own_limits_is_422_reading_them_live(
    paired: TestClient, tmp_path: Path
) -> None:
    """A hardcoded bound (e.g. a typical 0.001-3600s guess) would have
    accepted this duration; only reading ``Camera.ExposureMin`` live catches it."""
    _configure_folder(paired, tmp_path)
    camera = _camera(properties={"exposuremin": 0.5, "exposuremax": 10.0})
    _install_capture_service(paired, camera=camera)

    response = paired.post(
        "/api/capture/exposure", json={"duration_s": 0.01, "frame_type": "light"}
    )
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "0.5" in detail
    assert "10.0" in detail
    # The camera was actually asked, not assumed.
    assert "exposuremin" in camera.get_calls
    assert "exposuremax" in camera.get_calls
    assert camera.start_exposure_calls == []


def test_start_exposure_when_the_camera_cannot_expose_is_501(
    paired: TestClient, tmp_path: Path
) -> None:
    """The 8.46 path: ``StartExposure`` legitimately unimplemented
    (``ssc.api.capture_service`` module docstring)."""
    _configure_folder(paired, tmp_path)
    camera = _camera(start_exposure_not_implemented=True)
    _install_capture_service(paired, camera=camera)

    response = paired.post("/api/capture/exposure", json={"duration_s": 1.0, "frame_type": "light"})
    assert response.status_code == 501, response.text

    # The in-progress claim was rolled back — a second attempt is not a 409.
    state = paired.get("/api/state").json()["snapshot"]["capture"]
    assert state["in_progress"] is False


# -- GET /frames/{id}/image --------------------------------------------------- #


def test_get_frame_image_returns_a_debayered_full_preview(
    paired: TestClient, tmp_path: Path
) -> None:
    """The default fixture camera reports bayeroffsetx=0/bayeroffsety=1 —
    GBRG (``ssc.capture.fits.bayer_pattern``) — so the served PNG must be a
    colour image, not grayscale, sized exactly to the frame's own mosaic."""
    _configure_folder(paired, tmp_path)
    camera = _camera(frame=_image_frame_sized(4, 4))
    _install_capture_service(
        paired, camera=camera, telescope=_telescope(), filterwheel=_filterwheel()
    )

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "object": "M42", "frame_type": "light"},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    frame_id = final["last_frame_id"]
    assert frame_id is not None

    image = paired.get(f"/api/capture/frames/{frame_id}/image")
    assert image.status_code == 200, image.text
    assert image.headers["content-type"] == "image/png"
    width, height, colour_type = _png_ihdr(image.content)
    assert (width, height) == (4, 4)
    assert colour_type == 2  # debayered -> RGB, not grayscale


def test_get_frame_image_thumbnail_is_downsampled(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera(frame=_image_frame_sized(640, 640))
    _install_capture_service(
        paired, camera=camera, telescope=_telescope(), filterwheel=_filterwheel()
    )

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "object": "M42", "frame_type": "light"},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    frame_id = final["last_frame_id"]

    full = paired.get(f"/api/capture/frames/{frame_id}/image")
    assert full.status_code == 200, full.text
    full_width, full_height, _full_colour = _png_ihdr(full.content)
    assert (full_width, full_height) == (640, 640)

    thumb = paired.get(f"/api/capture/frames/{frame_id}/image", params={"thumbnail": "true"})
    assert thumb.status_code == 200, thumb.text
    thumb_width, thumb_height, _thumb_colour = _png_ihdr(thumb.content)
    assert (thumb_width, thumb_height) == (320, 320)
    assert len(thumb.content) < len(full.content)


def test_get_generated_calibration_master_image_uses_safe_catalogue_lookup(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    master = tmp_path / "Calibration" / "flats" / "master_flat_IR.fits"
    master.parent.mkdir(parents=True)
    write_fits_file(
        master,
        np.arange(16, dtype=np.uint16).reshape(4, 4),
        _live_metadata(filter_name="IR"),
    )

    response = paired.get(
        "/api/capture/calibration/masters/image",
        params={"frame_type": "flat", "name": master.name},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "image/png"
    assert _png_ihdr(response.content)[:2] == (4, 4)

    missing = paired.get(
        "/api/capture/calibration/masters/image",
        params={"frame_type": "flat", "name": "missing.fits"},
    )
    assert missing.status_code == 404


def test_automatic_bias_frames_do_not_replace_the_capture_preview_frame(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()
    camera.properties["lastexposureduration"] = EXPOSURE_MIN_S
    _install_capture_service(paired, camera=camera)

    response = paired.post(
        "/api/capture/calibration/start",
        json={"kind": "bias", "count": 1, "gain": 100, "generate_master": False},
    )

    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    assert final["calibration"]["status"] == "completed"
    assert final["last_frame_id"] is None
    frames = paired.get("/api/capture/frames").json()["frames"]
    assert len(frames) == 1
    assert Path(frames[0]["path"]).name.startswith("biases_0.00003s_")


def test_get_frame_image_unknown_id_is_404(paired: TestClient) -> None:
    response = paired.get("/api/capture/frames/999/image")
    assert response.status_code == 404, response.text


def test_get_frame_image_still_writing_is_409(paired: TestClient, tmp_path: Path) -> None:
    """A row inserted but never completed — the shape a frame mid-fsync has —
    must never be served (``CaptureFrameNotReadyError``, CLAUDE.md invariant 6:
    durable before acted on, in reverse — never *act on* what isn't durable yet)."""
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        return await repo.begin_frame(
            path=tmp_path / "still-writing.fits",
            date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]

    response = paired.get(f"/api/capture/frames/{frame_id}/image")
    assert response.status_code == 409, response.text


def test_get_frame_image_missing_catalogued_file_is_404(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=tmp_path / "deleted.fits",
            date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]

    response = paired.get(f"/api/capture/frames/{frame_id}/image")
    assert response.status_code == 404, response.text


# -- GET /frames/{id}/quality ------------------------------------------------- #


def test_get_frame_quality_returns_a_well_formed_metric(paired: TestClient, tmp_path: Path) -> None:
    """Not asserting an exact star count/HFR against ``_image_frame_sized``'s
    modulo ramp — that pattern's star-likeness is incidental, and the
    algorithm itself is already verified numerically in
    ``tests/capture/test_quality.py``. This checks the wire shape instead:
    the endpoint answers 200 with every field ``CaptureFrameQualityModel``
    declares, and invariant 7 holds at the HTTP boundary too — a
    ``star_count`` of 0 must carry every size field as ``None``, never a
    fabricated number."""
    _configure_folder(paired, tmp_path)
    camera = _camera(frame=_image_frame_sized(64, 64))
    _install_capture_service(
        paired, camera=camera, telescope=_telescope(), filterwheel=_filterwheel()
    )

    response = paired.post(
        "/api/capture/exposure",
        json={"duration_s": 1.0, "object": "M42", "frame_type": "light"},
    )
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda section: section["in_progress"] is False)
    frame_id = final["last_frame_id"]
    assert frame_id is not None

    quality = paired.get(f"/api/capture/frames/{frame_id}/quality")
    assert quality.status_code == 200, quality.text
    body = quality.json()
    assert isinstance(body["star_count"], int)
    assert isinstance(body["background"], float)
    assert isinstance(body["noise"], float)
    if body["star_count"] == 0:
        assert body["hfr_px"] is None
        assert body["fwhm_px"] is None
        assert body["hfr_arcsec"] is None
        assert body["fwhm_arcsec"] is None
    else:
        assert body["hfr_px"] is not None
        assert body["fwhm_px"] is not None


def test_get_frame_quality_unknown_id_is_404(paired: TestClient) -> None:
    response = paired.get("/api/capture/frames/999/quality")
    assert response.status_code == 404, response.text


def test_get_frame_quality_still_writing_is_409(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        return await repo.begin_frame(
            path=tmp_path / "still-writing.fits",
            date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]

    response = paired.get(f"/api/capture/frames/{frame_id}/quality")
    assert response.status_code == 409, response.text


def test_get_frame_quality_missing_catalogued_file_is_404(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=tmp_path / "deleted.fits",
            date_obs=datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]

    response = paired.get(f"/api/capture/frames/{frame_id}/quality")
    assert response.status_code == 404, response.text


# -- POST /frames/{id}/solve ------------------------------------------------- #


def test_solve_existing_frame_returns_astrometry_without_another_exposure(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    camera = _camera()

    async def available() -> SolverAvailability:
        return SolverAvailability(available=True, version="test-1")

    solved_paths: list[Path] = []

    async def solve(path: Path) -> PlateSolveResult:
        solved_paths.append(path)
        return PlateSolveResult(
            coord=j2000_coord(Deg(5.5881 * 15.0), Deg(-5.3911)),
            date_obs=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
            pixel_scale_arcsec=2.42,
            rotation_deg=91.3,
            solver="fake",
            solver_version="test-1",
        )

    services, _service = _install_capture_service(
        paired,
        camera=camera,
        telescope=_telescope(),
        filterwheel=_filterwheel(),
        solve=solve,
        availability=available,
    )

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=tmp_path / "existing.fits",
            date_obs=datetime(2026, 9, 10, 14, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]

    response = paired.post(f"/api/capture/frames/{frame_id}/solve")

    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "succeeded"
    assert operation["result"] == {
        "ra_j2000_hours": 5.5881,
        "dec_j2000_deg": -5.3911,
        "date_obs": "2026-09-10T14:00:00Z",
        "pixel_scale_arcsec": 2.42,
        "rotation_deg": 91.3,
        "solver": "fake",
        "solver_version": "test-1",
    }
    assert len(solved_paths) == 1
    assert solved_paths[0].suffix == ".fits"
    assert camera.start_exposure_calls == []


def test_solve_existing_frame_reports_unavailable_solver(
    paired: TestClient, tmp_path: Path
) -> None:
    services, _service = _install_capture_service(paired, camera=None)

    async def unavailable() -> SolverAvailability:
        return SolverAvailability(
            available=False,
            issue=SolveIssue.DATABASE_UNAVAILABLE,
            reason="ASTAP star database is missing",
        )

    async def must_not_solve(_path: Path) -> PlateSolveResult:
        raise AssertionError("solver must not run when availability is false")

    services.capture = CaptureService(
        camera=None,
        telescope=None,
        filterwheel=None,
        repo=CaptureRepository(services.database),
        settings=services.settings,
        state=services.state,
        tasks=services.tasks,
        section=services.capture.section,
        solve=must_not_solve,
        availability=unavailable,
    )

    async def plant() -> int:
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=tmp_path / "complete.fits",
            date_obs=datetime(2026, 9, 10, 22, 0, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=4,
            height=4,
        )
        await repo.complete_frame(frame_id)
        return frame_id

    frame_id = paired.portal.call(plant)  # type: ignore[attr-defined]
    response = paired.post(f"/api/capture/frames/{frame_id}/solve")
    assert response.status_code == 202, response.text
    operation = _terminal_operation(paired, response)
    assert operation["status"] == "failed"
    assert "ASTAP star database is missing" in operation["error"]


def test_live_stack_baselines_existing_session_lights_and_adds_only_new_frames(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    services, _service = _install_capture_service(paired, camera=None)
    lights = tmp_path / "M42" / "lights"
    lights.mkdir(parents=True)

    async def plant(path: Path, created: datetime) -> None:
        write_fits_file(path, np.arange(16, dtype=np.uint16).reshape(4, 4), _live_metadata())
        frame_id = await CaptureRepository(services.database).begin_frame(
            path=path,
            date_obs=created,
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=10.0,
            gain=100,
            ccd_temp_c=10.0,
            width=4,
            height=4,
        )
        await CaptureRepository(services.database).complete_frame(frame_id)

    paired.portal.call(plant, lights / "existing.fits", datetime(2026, 9, 11, 20, tzinfo=UTC))  # type: ignore[attr-defined]
    started = paired.post("/api/capture/live-stack/reset", json={})
    assert started.status_code == 200
    assert started.json()["active"] is True
    baseline = paired.post("/api/capture/live-stack/poll")
    assert baseline.status_code == 200
    assert baseline.json()["frame_count"] == 0

    paired.portal.call(plant, lights / "new.fits", datetime(2026, 9, 11, 21, tzinfo=UTC))  # type: ignore[attr-defined]
    first_poll = paired.post("/api/capture/live-stack/poll")
    assert first_poll.status_code == 200
    # The background watcher may win the race and consume the stable file
    # before this explicit poll. Either interleaving is correct; the contract
    # here is that the pre-existing baseline is skipped and the new file is
    # integrated exactly once.
    updated = (
        first_poll
        if first_poll.json()["frame_count"] == 1
        else paired.post("/api/capture/live-stack/poll")
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["frame_count"] == 1
    assert updated.json()["star_count"] == 0


def test_live_stack_can_be_stopped_independently(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    _install_capture_service(paired, camera=None)
    lights = tmp_path / "M42" / "lights"
    lights.mkdir(parents=True)
    write_fits_file(
        lights / "existing.fits",
        np.arange(16, dtype=np.uint16).reshape(4, 4),
        _live_metadata(),
    )

    started = paired.post("/api/capture/live-stack/reset", json={})
    assert started.status_code == 200
    assert started.json()["active"] is True

    stopped = paired.post("/api/capture/live-stack/stop")
    assert stopped.status_code == 200
    assert stopped.json()["active"] is False
    status_response = paired.get("/api/capture/live-stack")
    assert status_response.status_code == 200
    assert status_response.json()["active"] is False
    assert status_response.json()["frame_count"] == 0


def test_scheduled_live_stack_queues_in_order_and_stop_drains(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    services, service = _install_capture_service(paired, camera=None)
    lights = tmp_path / "M42" / "lights"
    lights.mkdir(parents=True)

    async def scenario() -> None:
        repo = CaptureRepository(services.database)
        frame_ids: list[int] = []
        for index in range(2):
            path = lights / f"scheduled-{index}.fits"
            write_fits_file(path, np.full((4, 4), index, dtype=np.uint16), _live_metadata())
            frame_id = await repo.begin_frame(
                path=path,
                date_obs=datetime(2026, 9, 11, 20 + index, tzinfo=UTC),
                object_name="M42",
                filter_name="LP",
                frame_type="light",
                exposure_s=60.0,
                gain=100,
                ccd_temp_c=10.0,
                width=4,
                height=4,
            )
            await repo.complete_frame(frame_id)
            frame_ids.append(frame_id)

        service.reset_scheduled_live_stack(lights / "scheduled-0.fits")
        release = asyncio.Event()
        started = asyncio.Event()
        processed: list[int] = []

        async def blocked_add(frame_id: int):  # type: ignore[no-untyped-def]
            processed.append(frame_id)
            started.set()
            await release.wait()
            run = service.live_stack_run()
            assert run is not None
            return run

        service.add_live_stack_frame = blocked_add  # type: ignore[method-assign]
        await service.queue_scheduled_live_stack_frame(frame_ids[0])
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await service.queue_scheduled_live_stack_frame(frame_ids[1])
        # Saturated optional processing must never block the next capture.
        await asyncio.wait_for(service.queue_scheduled_live_stack_frame(frame_ids[1]), 0.1)
        await asyncio.wait_for(service.queue_scheduled_live_stack_frame(frame_ids[1]), 0.1)
        assert service._scheduled_live_stack_queue.qsize() == 2

        stopping = asyncio.create_task(service.stop_live_stack())
        await asyncio.sleep(0)
        assert not stopping.done()
        release.set()
        await asyncio.wait_for(stopping, timeout=1.0)
        assert processed == [frame_ids[0], frame_ids[1], frame_ids[1]]
        assert service.live_stack_pending_count == 0
        # Manual Stop leaves capture delivery as a harmless no-op.
        await asyncio.wait_for(service.queue_scheduled_live_stack_frame(frame_ids[0]), 0.1)
        # A manual restart owns a folder watcher, not the scheduler queue.
        service.reset_live_stack(watch_folder=str(lights), binning=1)
        assert service._scheduled_live_stack_queue is None
        await asyncio.wait_for(service.queue_scheduled_live_stack_frame(frame_ids[0]), 0.1)

    paired.portal.call(scenario)  # type: ignore[attr-defined]


@pytest.mark.parametrize("keep_rejected", [True, False])
def test_scheduled_cloud_rejection_applies_retention_setting(
    paired: TestClient, tmp_path: Path, keep_rejected: bool
) -> None:
    _configure_folder(paired, tmp_path)
    services, service = _install_capture_service(paired, camera=None)
    lights = tmp_path / "M42" / "lights"
    lights.mkdir(parents=True)

    async def scenario() -> None:
        await services.settings.set_keep_rejected_frames(keep_rejected)
        path = lights / "cloudy.fits"
        write_fits_file(path, np.arange(64, dtype=np.uint16).reshape(8, 8), _live_metadata())
        repo = CaptureRepository(services.database)
        frame_id = await repo.begin_frame(
            path=path,
            date_obs=datetime(2026, 9, 24, 2, 40, tzinfo=UTC),
            object_name="M42",
            filter_name="LP",
            frame_type="light",
            exposure_s=60.0,
            gain=100,
            ccd_temp_c=10.0,
            width=8,
            height=8,
        )
        await repo.complete_frame(frame_id)
        service.reset_scheduled_live_stack(path)
        assert service._live_stack is not None

        def reject(*_args: object, **_kwargs: object) -> None:
            raise LiveStackFrameRejectedError("cloud quality gate: test rejection")

        service._live_stack.add_frame = reject  # type: ignore[method-assign]
        with pytest.raises(LiveStackFrameRejectedError, match="test rejection"):
            await service.add_live_stack_frame(frame_id)

        if keep_rejected:
            moved = await repo.get_frame(frame_id)
            assert moved.path == lights / "rejected" / "cloudy.fits"
            assert moved.path.exists()
        else:
            assert await repo.list_complete() == []
            assert not (lights / "rejected").exists()
        assert not path.exists()

    paired.portal.call(scenario)  # type: ignore[attr-defined]


def test_live_stack_directory_browser_is_rooted_at_capture_folder(
    paired: TestClient, tmp_path: Path
) -> None:
    capture_root = tmp_path / "captures"
    nested = capture_root / "M42" / "lights"
    nested.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    _configure_folder(paired, capture_root)

    root_response = paired.get("/api/capture/live-stack/directories")

    assert root_response.status_code == 200, root_response.text
    assert root_response.json() == {
        "path": str(capture_root),
        "parent": None,
        "directories": [str(capture_root / "M42")],
    }

    nested_response = paired.get(
        "/api/capture/live-stack/directories", params={"path": str(nested)}
    )
    assert nested_response.status_code == 200, nested_response.text
    assert nested_response.json()["parent"] == str(capture_root / "M42")

    outside_response = paired.get(
        "/api/capture/live-stack/directories", params={"path": str(outside)}
    )
    assert outside_response.status_code == 403
    assert str(capture_root) in outside_response.json()["detail"]


def test_live_stack_directory_browser_requires_capture_folder(paired: TestClient) -> None:
    response = paired.get("/api/capture/live-stack/directories")

    assert response.status_code == 409
    assert response.json()["detail"] == "capture folder is not configured"


@pytest.mark.parametrize("mode,count", [("single", 1), ("loop", 2)])
@pytest.mark.parametrize("optic", ["tele", "wide"])
@pytest.mark.parametrize("frame_type", ["light", "flat"])
def test_manual_capture_selects_camera_for_exposure_and_saved_frame(
    paired: TestClient, tmp_path: Path, mode: str, count: int, optic: str, frame_type: str
) -> None:
    _configure_folder(paired, tmp_path)
    response = paired.put("/api/settings/capture-dither", json={
        "enabled": False, "every_n_frames": 1, "min_pixels": 2, "max_pixels": 3,
    })
    assert response.status_code == 200, response.text
    tele = _camera(frame=_image_frame(fill=7))
    wide = _camera(frame=_image_frame(fill=71), properties={"exposuremin": 0.02})
    _install_capture_service(
        paired, camera=tele, wide_camera=wide, telescope=_telescope(),
        filterwheel=_filterwheel(),
    )
    body: dict[str, object] = {
        "duration_s": 1.0, "gain": 250, "frame_type": frame_type,
        "optic_id": optic, "capture_mode": mode,
    }
    if frame_type == "flat":
        body["flat_source"] = "panel"
    if mode == "loop":
        body["frame_limit"] = count
    response = paired.post("/api/capture/exposure", json=body)
    assert response.status_code == 202, response.text
    final = _wait_for(paired, lambda state: not state["in_progress"] and not state["loop_active"])
    assert final["last_error"] is None
    selected, other = (wide, tele) if optic == "wide" else (tele, wide)
    assert len(selected.start_exposure_calls) == count
    assert other.start_exposure_calls == []
    assert ("gain", {"Gain": 250}) in selected.put_calls
    assert other.put_calls == []
    paths = list(tmp_path.rglob("*.fits"))
    assert len(paths) == count
    for path in paths:
        image = read_fits_image(path.read_bytes())
        assert image.header["WIDECAM"] == int(optic == "wide")
        assert image.header["OPTICID"] == optic
        assert np.min(image.data) == (71 if optic == "wide" else 7)


@pytest.mark.parametrize("frame_type", ["dark", "bias"])
def test_wide_dark_bias_rejects_before_any_device_commands(
    paired: TestClient, tmp_path: Path, frame_type: str
) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera()
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    response = paired.post("/api/capture/exposure", json={
        "duration_s": 0.0 if frame_type == "bias" else 1.0,
        "frame_type": frame_type, "optic_id": "wide",
    })
    assert response.status_code == 501, response.text
    assert "Wide shutter" in response.json()["detail"]
    assert wide.start_exposure_calls == tele.start_exposure_calls == []
    assert wide.get_calls == tele.get_calls == []


def test_missing_wide_camera_does_not_fall_back_to_tele(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    tele = _camera()
    _install_capture_service(paired, camera=tele)
    response = paired.post("/api/capture/exposure", json={
        "duration_s": 1.0, "frame_type": "light", "optic_id": "wide",
    })
    assert response.status_code == 503, response.text
    assert "Wide camera" in response.json()["detail"]
    assert tele.start_exposure_calls == []


def test_wide_capture_uses_wide_exposure_limits(paired: TestClient, tmp_path: Path) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera(properties={"exposuremin": 0.2})
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    response = paired.post("/api/capture/exposure", json={
        "duration_s": 0.1, "frame_type": "light", "optic_id": "wide",
    })
    assert response.status_code == 422, response.text
    assert wide.start_exposure_calls == tele.start_exposure_calls == []
    assert "exposuremin" in wide.get_calls
    assert tele.get_calls == []


def test_automatic_wide_flat_rejects_before_tele_calibration(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    tele, wide = _camera(), _camera()
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    response = paired.post("/api/capture/calibration/start", json={
        "kind": "flat", "count": 1, "gain": 100, "filter_positions": [1],
        "optic_id": "wide", "flat_source": "panel", "generate_master": False,
    })
    assert response.status_code == 422, response.text
    assert "Automatic Wide calibration" in response.json()["detail"]
    assert wide.start_exposure_calls == tele.start_exposure_calls == []


def test_manual_capture_rejects_unknown_camera_before_device_commands(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    tele = _camera()
    _install_capture_service(paired, camera=tele)
    response = paired.post("/api/capture/exposure", json={
        "duration_s": 1.0, "frame_type": "light", "optic_id": "unknown",
    })
    assert response.status_code == 422, response.text
    assert tele.get_calls == tele.start_exposure_calls == []


def test_wide_loop_rejects_uncalibrated_dither_before_device_commands(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    response = paired.put("/api/settings/capture-dither", json={
        "enabled": True, "every_n_frames": 1, "min_pixels": 2, "max_pixels": 3,
    })
    assert response.status_code == 200, response.text
    tele, wide = _camera(), _camera()
    _install_capture_service(paired, camera=tele, wide_camera=wide)
    response = paired.post("/api/capture/exposure", json={
        "duration_s": 1.0, "frame_type": "light", "optic_id": "wide",
        "capture_mode": "loop", "frame_limit": 2,
    })
    assert response.status_code == 501, response.text
    assert "disable dithering" in response.json()["detail"]
    assert wide.get_calls == tele.get_calls == []
    assert wide.start_exposure_calls == tele.start_exposure_calls == []
    final = _wait_for(paired, lambda section: not section["loop_active"])
    assert final["in_progress"] is False


def test_switching_tele_to_wide_loop_anchors_drift_to_first_wide_frame(
    paired: TestClient, tmp_path: Path
) -> None:
    _configure_folder(paired, tmp_path)
    solutions: list[str] = []
    recentered: list[tuple[float, float]] = []

    async def solve(path: Path) -> PlateSolveResult:
        optic = str(read_fits_image(await asyncio.to_thread(path.read_bytes)).header["OPTICID"])
        solutions.append(optic)
        return PlateSolveResult(
            coord=j2000_coord(Deg(150.0 if optic == "tele" else 151.0), Deg(20.0)),
            date_obs=datetime(2026, 9, 13, 20, 0, tzinfo=UTC),
            pixel_scale_arcsec=2.4 if optic == "tele" else 20.0,
            rotation_deg=0.0, solver="test", solver_version="1",
        )

    async def recenter(ra_h: float, dec_deg: float) -> None:
        recentered.append((ra_h, dec_deg))

    services, service = _install_capture_service(
        paired, camera=_camera(), wide_camera=_camera(), telescope=_telescope(),
        filterwheel=_filterwheel(), solve=solve, recenter=recenter,
    )

    async def configure() -> None:
        await services.settings.set_capture_center_after_drift(enabled=True, threshold_pixels=1.0)
        await services.settings.set_capture_dither(
            enabled=False, every_n_frames=1, min_pixels=2.0, max_pixels=3.0,
        )

    paired.portal.call(configure)  # type: ignore[attr-defined]
    for optic, count in [("tele", 1), ("wide", 2)]:
        response = paired.post("/api/capture/exposure", json={
            "duration_s": 1.0, "frame_type": "light", "optic_id": optic,
            "capture_mode": "loop", "frame_limit": count,
        })
        assert response.status_code == 202, response.text
        final = _wait_for(paired, lambda section: not section["in_progress"] and not section["loop_active"])
        assert final["last_error"] is None
        if optic == "tele":
            tele_reference = service._center_reference
            rejected = paired.post("/api/capture/exposure", json={
                "duration_s": 0.000001, "frame_type": "light", "optic_id": "wide",
            })
            assert rejected.status_code == 422, rejected.text
            assert service._center_reference is tele_reference
    assert solutions == ["tele", "wide", "wide"]
    assert recentered == []
    assert service._center_reference is not None
    assert float(service._center_reference.coord.ra_h) == pytest.approx(151.0 / 15)


def test_live_stack_preview_shares_worker_after_http_cancellation(
    paired: TestClient, tmp_path: Path
) -> None:
    _, service = _install_capture_service(paired, camera=None)
    from types import SimpleNamespace

    pixels = np.ones((8, 8, 3), dtype=np.float32)
    engine = SimpleNamespace(
        pixels=pixels, bayer_pattern=None,
        run=SimpleNamespace(sequence=1, frame_paths=[tmp_path / "light.fits"]),
        presentation_pixels=lambda **_kwargs: pixels,
    )
    service._live_stack = engine  # type: ignore[assignment]
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def render(_pixels: np.ndarray, _pattern: str | None) -> bytes:
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(5)
        return b"native-preview"

    async def exercise() -> None:
        first = asyncio.create_task(service.render_live_stack_preview(render))
        assert await asyncio.to_thread(started.wait, 5)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        consumers = [asyncio.create_task(service.render_live_stack_preview(render)) for _ in range(8)]
        await asyncio.sleep(0)
        release.set()
        assert await asyncio.gather(*consumers) == [b"native-preview"] * 8
        assert await service.render_live_stack_preview(render) == b"native-preview"
        assert calls == 1
        engine.run.frame_paths.append(tmp_path / "next.fits")
        assert await service.render_live_stack_preview(render) == b"native-preview"
        assert calls == 2

    asyncio.run(exercise())


def test_stack_save_failure_retains_integration_until_successful_retry(
    paired: TestClient, tmp_path: Path, monkeypatch
) -> None:
    from ssc.stacking.live import LiveStackError

    _configure_folder(paired, tmp_path)
    _services, service = _install_capture_service(paired, camera=None)
    lights = tmp_path / 'lights'
    lights.mkdir()
    path = lights / 'light.fits'
    write_fits_file(path, np.full((4, 4), 10, dtype=np.uint16), _live_metadata())

    async def scenario():
        service.reset_live_stack(watch_folder=str(lights))
        original_stack = service._live_stack
        original_save = original_stack.save

        def fail_save(**kwargs):
            raise OSError(28, 'No space left on device')

        monkeypatch.setattr(original_stack, 'save', fail_save)
        with pytest.raises(LiveStackError, match='integration retained'):
            await service.stop_live_stack()
        assert service._live_stack is original_stack
        assert not service.live_stack_active
        with pytest.raises(LiveStackError, match='retry Save'):
            service.reset_live_stack(watch_folder=str(lights))
        monkeypatch.setattr(original_stack, 'save', original_save)
        await service.stop_live_stack()
        assert service._live_stack_save_error is None
        service.reset_live_stack(watch_folder=str(lights))

    paired.portal.call(scenario)


def test_fresh_capture_service_cannot_claim_retained_stack_saved(paired: TestClient):
    from ssc.stacking.live import LiveStackError
    _services, service = _install_capture_service(paired, camera=None)

    async def scenario():
        assert await service.stop_scheduled_live_stack() is None
        with pytest.raises(LiveStackError, match='missing after restart'):
            await service.stop_scheduled_live_stack(require_retained=True)
    paired.portal.call(scenario)
