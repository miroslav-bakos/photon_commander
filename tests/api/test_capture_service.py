# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``ssc.api.capture_service``: the exposure loop that joins ``ssc.client``'s
transport to ``ssc.capture``'s writer (DESIGN.md §9.5.1/§9.5.2).

No live driver or emulator: the ``alpaca-driver`` dispatch making ``Camera``
members pass through to the native 9.16 surface is concurrent work, so this
drives the loop end to end against a fake camera/telescope/filterwheel that
speaks the same structural ``Device``/``CameraDevice`` protocol
``ssc.client.alpaca.AlpacaDeviceClient`` does — see the module docstring for
why this module is typed that way.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from ssc.api.capture_service import (
    CaptureMetadataError,
    ExposureContext,
    ExposureNotAvailableError,
    ExposureTimeoutError,
    capture_one_exposure,
    drive_exposure,
    exposure_metadata_fallbacks,
)
from ssc.astro.events import Ephemeris, load_ephemeris
from ssc.astro.frames import MountFrame, j2000_coord, mount_site, to_mount_frame
from ssc.astro.units import Deg
from ssc.capture import DEFAULT_CAPTURE_FOLDER_TEMPLATE, FrameType
from ssc.client.errors import AlpacaErrorNumber, AlpacaRemoteError, AlpacaTransportError
from ssc.client.imagebytes import ImageArrayElementType, ImageBytesFrame
from ssc.skymap.fits import read_fits_image
from ssc.state.models import LocationSource, LocationState, MountState
from ssc.store.capture import CaptureRepository
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate

FIXED_NOW = datetime(2026, 9, 10, 3, 0, 0, tzinfo=UTC)


@pytest.fixture
async def database(tmp_path: Path) -> AsyncIterator[Database]:
    handle = await open_database(tmp_path / "ssc.sqlite3")
    try:
        yield handle
    finally:
        await handle.aclose()


@pytest.fixture
async def migrated(database: Database) -> Database:
    await migrate(database)
    return database


@pytest.fixture
def repo(migrated: Database) -> CaptureRepository:
    return CaptureRepository(migrated)


@pytest.fixture(scope="module")
def ephemeris() -> Iterator[Ephemeris]:
    with load_ephemeris() as handle:
        yield handle


def _image_frame(*, fill: int = 7) -> ImageBytesFrame:
    array = (np.arange(12, dtype=np.uint16).reshape(3, 4) + fill).astype(np.uint16)
    return ImageBytesFrame(
        metadata_version=1,
        client_transaction_id=1,
        server_transaction_id=1,
        image_element_type=ImageArrayElementType.UINT16,
        transmission_element_type=ImageArrayElementType.UINT16,
        array=array,
    )


class FakeDevice:
    """A ``Device`` that answers exactly the properties a test hands it, and
    raises the ASCOM "not implemented" pair for anything named in
    ``not_implemented`` — the two distinct failure modes ``_read_optional``
    itself distinguishes."""

    def __init__(
        self,
        *,
        properties: dict[str, object] | None = None,
        not_implemented: frozenset[str] = frozenset(),
        remote_errors: dict[str, tuple[int, str]] | None = None,
        transport_errors: frozenset[str] = frozenset(),
        connected: bool = False,
    ) -> None:
        self.properties = dict(properties or {})
        self.not_implemented = not_implemented
        self.remote_errors = dict(remote_errors or {})
        self.transport_errors = transport_errors
        self.connected = connected
        self.connect_calls = 0
        self.get_calls: list[str] = []

    async def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True
        self.connect_calls += 1

    async def get(self, member: str, **params: object) -> object:
        self.get_calls.append(member)
        if member in self.transport_errors:
            raise AlpacaTransportError(f"Fake.{member}: ReadError (the client reported no detail)")
        if member in self.remote_errors:
            number, message = self.remote_errors[member]
            raise AlpacaRemoteError(number, message, member=f"Fake.{member}")
        if member in self.not_implemented:
            raise AlpacaRemoteError(
                int(AlpacaErrorNumber.NOT_IMPLEMENTED), "not implemented", member=f"Fake.{member}"
            )
        return self.properties[member]


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
        self.image_array_calls = 0

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
        self.image_array_calls += 1
        return self.frame


def _camera(**kwargs: object) -> FakeCamera:
    defaults: dict[str, object] = {
        "connected": True,
        "properties": {
            "gain": 200,
            "ccdtemperature": 20.5,
            "bayeroffsetx": 1,
            "bayeroffsety": 0,
            "pixelsizex": 2.9,
            "pixelsizey": 2.9,
            "binx": 1,
            "biny": 1,
            "startx": 0,
            "starty": 0,
            "sensorname": "IMX585",
            "sensortype": 2,
            "offset": 1108,
            "maxadu": 65535,
            "readoutmode": 0,
            "lastexposurestarttime": "2026-09-10T03:00:00",
            "lastexposureduration": 5.0,
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


def _telescope(**kwargs: object) -> FakeDevice:
    defaults: dict[str, object] = {
        "connected": True,
        "properties": {
            "rightascension": 5.5833,  # hours
            "declination": -5.39,
            "equatorialsystem": 2,  # equJ2000
            "sitelatitude": 48.2,
            "sitelongitude": 16.4,
            "alignmentmode": 1,
            "focallength": 0.16,
            "aperturediameter": 0.03,
        },
    }
    provided_properties = kwargs.pop("properties", None)
    if provided_properties is not None:
        assert isinstance(provided_properties, dict)
        properties = defaults["properties"]
        assert isinstance(properties, dict)
        properties.update(provided_properties)
    defaults.update(kwargs)
    return FakeDevice(**defaults)  # type: ignore[arg-type]


def _filterwheel(**kwargs: object) -> FakeDevice:
    defaults: dict[str, object] = {
        "connected": True,
        "properties": {"position": 2, "names": ["Dark", "IR", "LP"]},
    }
    defaults.update(kwargs)
    return FakeDevice(**defaults)  # type: ignore[arg-type]


def _focuser(**kwargs: object) -> FakeDevice:
    defaults: dict[str, object] = {
        "connected": True,
        "properties": {"position": 1210, "temperature": 31.3125},
    }
    defaults.update(kwargs)
    return FakeDevice(**defaults)  # type: ignore[arg-type]


async def _no_sleep(_seconds: float) -> None:
    return None


def _context(**kwargs: object) -> ExposureContext:
    defaults: dict[str, object] = {
        "object_name": "M42",
        "focal_length_mm": 250.0,
        "telescope_name": "Seestar S30 Pro",
        "ssc_version": "0.1.0",
        "device_firmware": "9.16",
    }
    defaults.update(kwargs)
    return ExposureContext(**defaults)  # type: ignore[arg-type]


# -- the whole path: exposure commanded, frame pulled, catalogued ----------- #


async def test_capture_one_exposure_drives_the_whole_path_and_catalogues_a_readable_fits(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    camera = _camera()
    telescope = _telescope()
    filterwheel = _filterwheel()
    focuser = _focuser()

    frame_id = await capture_one_exposure(
        camera=camera,
        telescope=telescope,
        filterwheel=filterwheel,
        focuser=focuser,
        repo=repo,
        folder=tmp_path,
        duration_s=5.0,
        context=_context(),
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    # Commanded through the seam, not a shortcut past it.
    assert camera.start_exposure_calls == [(5.0, True)]
    assert camera.image_array_calls == 1
    # Offsets are read only once the camera is known connected.
    assert camera.connect_calls == 0  # it was already connected

    complete = await repo.list_complete()
    assert [record.id for record in complete] == [frame_id]
    record = complete[0]
    assert record.status == "complete"
    assert record.object_name == "M42"
    assert record.date_obs == FIXED_NOW  # SSC's own clock, not the device's

    image = read_fits_image(record.path.read_bytes())
    wire = camera.frame.array
    assert image.data.shape == (4, 3)
    assert image.header["NAXIS1"] == 3
    assert image.header["NAXIS2"] == 4
    assert np.array_equal(image.data, wire.T.astype(np.float64))
    assert image.header["BAYERPAT"] == "GRBG"  # offsets (1, 0)
    assert image.header["OBJECT"] == "M42"
    assert image.header["FILTER"] == "LP"  # position 2 of the fake wheel
    assert image.header["GAIN"] == 200
    assert image.header["FOCALLEN"] == 250.0
    assert image.header["DATE-EXP"] == "2026-09-10T03:00:05+00:00"
    assert image.header["XORGSUBF"] == 0
    assert image.header["YORGSUBF"] == 0
    assert image.header["XBINNING"] == 1
    assert image.header["YBINNING"] == 1
    assert image.header["CCDXBIN"] == 1
    assert image.header["CCDYBIN"] == 1
    assert image.header["YPIXSZ"] == pytest.approx(2.9)
    assert image.header["INSTRUME"] == "IMX585"
    assert image.header["OFFSET"] == 1108
    assert image.header["MAXADU"] == pytest.approx(65535)
    assert image.header["READOUTM"] == 0
    assert image.header["FOCUSPOS"] == 1210
    assert image.header["FOCTEMP"] == pytest.approx(31.3125)
    assert image.header["EQMODE"] == 1
    assert image.header["APTDIA"] == pytest.approx(30.0)
    assert image.header["APERTURE"] == pytest.approx(250.0 / 30.0)
    assert image.header["TOTALEXP"] == pytest.approx(5.0)
    assert image.header["TELESCOP"] == "Seestar S30 Pro"
    assert "9.16" in str(image.header["PROGRAM"])
    # RA read in hours, converted to decimal degrees for the header.
    assert image.header["RA"] == pytest.approx(5.5833 * 15.0)
    assert image.header["RADESYS"] == "ICRS"
    assert image.header["EQUINOX"] == pytest.approx(2000.0)


async def test_a_camera_not_yet_connected_is_connected_before_offsets_are_read(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    """DESIGN.md §9.5.2: disconnected, BayerOffsetX/Y answer a plausible,
    wrong 0/0 — so the camera must be connected before anything is read."""
    class ConnectionCheckingCamera(FakeCamera):
        async def get(self, member: str, **params: object) -> object:
            if member in {"bayeroffsetx", "bayeroffsety"}:
                assert self.connected, "Bayer offsets must be read only after connection"
            return await super().get(member, **params)

    base = _camera()
    camera = ConnectionCheckingCamera(frame=base.frame, properties=base.properties, connected=False)

    await capture_one_exposure(
        camera=camera,
        telescope=None,
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=1.0,
        context=_context(),
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    assert camera.connect_calls == 1
    assert "bayeroffsetx" in camera.get_calls


async def test_image_transfer_cannot_starve_filter_and_sensor_metadata(monkeypatch) -> None:
    import ssc.api.capture_service as service

    monkeypatch.setattr(service, "OPTIONAL_METADATA_TIMEOUT_S", .01)
    transferring = asyncio.Event()
    transfer_done = asyncio.Event()
    completed: set[str] = set()

    async def read_without_transfer_contention(label: str) -> None:
        # Give a concurrently launched transfer time to occupy the driver.
        await asyncio.sleep(0)
        if transferring.is_set():
            await transfer_done.wait()
        completed.add(label)

    class ContendedCamera(FakeCamera):
        async def get(self, member: str, **params: object) -> object:
            if member in {"ccdtemperature", "sensorname"}:
                await read_without_transfer_contention(member)
            return await super().get(member, **params)

        async def image_array(self) -> ImageBytesFrame:
            transferring.set()
            await asyncio.sleep(.03)
            transfer_done.set()
            return await super().image_array()

    class ContendedFilter(FakeDevice):
        async def get(self, member: str, **params: object) -> object:
            await read_without_transfer_contention(member)
            return await super().get(member, **params)

    base = _camera()
    camera = ContendedCamera(frame=base.frame, connected=True, properties=base.properties)
    filterwheel = ContendedFilter(connected=True, properties=_filterwheel().properties)
    frame, _ = await drive_exposure(
        camera=camera, telescope=None, filterwheel=filterwheel,
        duration_s=0, context=_context(), frame_type=FrameType.LIGHT, sleep=_no_sleep,
    )
    assert frame.metadata.filter_name == "LP"
    assert frame.metadata.ccd_temp_c == 20.5
    assert frame.metadata.sensor_name == "IMX585"
    assert completed == {"ccdtemperature", "sensorname", "position", "names"}
    assert np.array_equal(frame.pixels, camera.frame.array.T)


async def test_stalled_metadata_still_downloads_exposed_frame(monkeypatch) -> None:
    import ssc.api.capture_service as service

    monkeypatch.setattr(service, "OPTIONAL_METADATA_TIMEOUT_S", .01)
    cancelled = asyncio.Event()

    class StalledSensorCamera(FakeCamera):
        async def get(self, member: str, **params: object) -> object:
            if member == "sensorname":
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return await super().get(member, **params)

        async def image_array(self) -> ImageBytesFrame:
            assert cancelled.is_set(), "image transfer starts after the bounded metadata phase"
            return await super().image_array()

    base = _camera()
    camera = StalledSensorCamera(frame=base.frame, connected=True, properties=base.properties)
    unsupported: set[str] = set()
    frame, _ = await asyncio.wait_for(drive_exposure(
        camera=camera, telescope=None, filterwheel=None, duration_s=0,
        context=_context(), frame_type=FrameType.LIGHT, sleep=_no_sleep,
        unsupported_members=unsupported,
    ), timeout=.5)
    assert frame.metadata.sensor_name is None
    assert camera.image_array_calls == 1
    assert np.array_equal(frame.pixels, camera.frame.array.T)
    assert unsupported == set()


async def test_default_ready_poll_interval_is_one_tenth_second() -> None:
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    await drive_exposure(
        camera=_camera(ready_after_polls=2),
        telescope=None,
        filterwheel=None,
        duration_s=0.0,
        context=_context(),
        frame_type=FrameType.BIAS,
        sleep=record_sleep,
    )

    assert sleeps == [0.1]


# -- a field the device cannot supply is absent, never zero ----------------- #


async def test_an_unimplemented_property_is_absent_from_the_header_not_zero(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    camera = _camera(
        properties={"sensorname": ""},
        not_implemented=frozenset({"gain", "ccdtemperature"}),
    )
    focuser = _focuser(not_implemented=frozenset({"position", "temperature"}))

    frame_id = await capture_one_exposure(
        camera=camera,
        telescope=None,
        filterwheel=None,
        focuser=focuser,
        repo=repo,
        folder=tmp_path,
        duration_s=1.0,
        context=_context(),
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    record = (await repo.list_complete())[0]
    assert record.id == frame_id
    image = read_fits_image(record.path.read_bytes())
    assert "GAIN" not in image.header
    assert "CCD-TEMP" not in image.header
    assert "FOCUSPOS" not in image.header
    assert "FOCTEMP" not in image.header
    assert "INSTRUME" not in image.header
    # Bayer offsets were implemented, so BAYERPAT is still written.
    assert image.header["BAYERPAT"] == "GRBG"


async def test_a_generic_remote_metadata_error_does_not_abort_an_exposed_frame(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    telescope = _telescope(
        remote_errors={
            "sitelatitude": (
                0x4FF,
                "The given key 'Latitude' was not present in the dictionary.",
            )
        }
    )

    frame_id = await capture_one_exposure(
        camera=_camera(),
        telescope=telescope,
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=0.00003,
        context=_context(object_name=None),
        frame_type=FrameType.BIAS,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    record = (await repo.list_complete())[0]
    assert record.id == frame_id
    image = read_fits_image(record.path.read_bytes())
    assert image.header["IMAGETYP"] == "bias"
    assert "SITELAT" not in image.header
    assert image.header["SITELONG"] == pytest.approx(16.4)
    assert "RA" not in image.header
    assert "DEC" not in image.header


async def test_topocentric_frame_time_pointing_is_written_as_declared_j2000(
    repo: CaptureRepository, tmp_path: Path, ephemeris: Ephemeris
) -> None:
    site = mount_site(Deg(48.2), Deg(16.4))
    expected = j2000_coord(Deg(37.121748), Deg(61.092126))
    native = to_mount_frame(ephemeris, site, expected, FIXED_NOW, MountFrame.TOPOCENTRIC)
    telescope = _telescope(
        properties={
            "rightascension": float(native.ra_h),
            "declination": float(native.dec_deg),
            "equatorialsystem": 1,
        }
    )

    await capture_one_exposure(
        camera=_camera(),
        telescope=telescope,
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=0.00003,
        context=_context(ephemeris=ephemeris),
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    image = read_fits_image((await repo.list_complete())[0].path.read_bytes())
    assert image.header["RA"] == pytest.approx(float(expected.ra_deg), abs=1e-7)
    assert image.header["DEC"] == pytest.approx(float(expected.dec_deg), abs=1e-7)
    assert image.header["RADESYS"] == "ICRS"
    assert image.header["EQUINOX"] == pytest.approx(2000.0)


async def test_unknown_frame_is_not_mislabeled_as_j2000(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    await capture_one_exposure(
        camera=_camera(),
        telescope=_telescope(properties={"equatorialsystem": 99}),
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=0.00003,
        context=_context(),
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    image = read_fits_image((await repo.list_complete())[0].path.read_bytes())
    for card in ("RA", "DEC", "RADESYS", "EQUINOX"):
        assert card not in image.header


async def test_incomplete_telescope_pairs_use_fresh_canonical_fallbacks(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    telescope = _telescope(
        transport_errors=frozenset({"rightascension", "sitelongitude"})
    )
    location = LocationState(
        latitude_deg=48.75,
        longitude_deg=18.25,
        source=LocationSource.MANUAL,
        acquired_at=FIXED_NOW,
    )
    mount = MountState(
        polled_at=FIXED_NOW,
        stale=False,
        ra_hours=2.75,
        dec_deg=61.2,
        ra_j2000_h=2.75,
        dec_j2000_deg=61.2,
        # An incomplete mount site pair deliberately falls through to the
        # complete configured observing location.
        site_latitude_deg=48.75,
    )
    context = _context(**exposure_metadata_fallbacks(mount, location, now=FIXED_NOW))

    await capture_one_exposure(
        camera=_camera(),
        telescope=telescope,
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=0.00003,
        context=context,
        frame_type=FrameType.LIGHT,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    image = read_fits_image((await repo.list_complete())[0].path.read_bytes())
    # Both pointing values come from one fresh snapshot; the direct Dec read
    # is not mixed with a fallback RA.
    assert image.header["RA"] == pytest.approx(2.75 * 15.0)
    assert image.header["DEC"] == pytest.approx(61.2)
    assert image.header["SITELAT"] == pytest.approx(48.75)
    assert image.header["SITELONG"] == pytest.approx(18.25)


def test_stale_mount_pointing_is_never_offered_as_a_fallback() -> None:
    mount = MountState(
        polled_at=FIXED_NOW - timedelta(minutes=1),
        stale=False,
        ra_hours=2.75,
        dec_deg=61.2,
    )

    values = exposure_metadata_fallbacks(mount, None, now=FIXED_NOW)

    assert values["fallback_ra_hours"] is None
    assert values["fallback_dec_deg"] is None


async def test_a_transient_optional_metadata_transport_error_does_not_discard_the_frame(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    """Regression for the Pi failure observed during a calibration run:
    ``LastExposureStartTime`` lost one HTTP read while ImageBytes itself was
    still available.  Optional header enrichment must degrade to an absent
    card, not cancel the concurrent image transfer and the whole capture."""
    camera = _camera(transport_errors=frozenset({"lastexposurestarttime"}))

    frame_id = await capture_one_exposure(
        camera=camera,
        telescope=_telescope(),
        filterwheel=None,
        repo=repo,
        folder=tmp_path,
        duration_s=0.00003,
        context=_context(object_name=None),
        frame_type=FrameType.BIAS,
        folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        now=lambda: FIXED_NOW,
        sleep=_no_sleep,
    )

    record = (await repo.list_complete())[0]
    assert record.id == frame_id
    assert camera.image_array_calls == 1
    image = read_fits_image(record.path.read_bytes())
    assert image.header["DATE-OBS"] == FIXED_NOW.isoformat()
    assert image.header["IMAGETYP"] == "bias"


async def test_not_implemented_optional_metadata_is_cached_between_exposures() -> None:
    camera = _camera(not_implemented=frozenset({"lastexposurestarttime"}))
    unsupported: set[str] = set()

    for _ in range(2):
        await drive_exposure(
            camera=camera,
            telescope=None,
            filterwheel=None,
            duration_s=0.0,
            context=_context(),
            frame_type=FrameType.LIGHT,
            now=lambda: FIXED_NOW,
            sleep=_no_sleep,
            unsupported_members=unsupported,
        )

    assert camera.get_calls.count("lastexposurestarttime") == 1
    assert "Camera.LastExposureStartTime" in unsupported


async def test_a_malformed_property_raises_rather_than_being_silently_dropped(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    camera = _camera(properties={"gain": "not-a-number"})
    # The rest of the required properties for a camera-only path.
    camera.properties.update(
        {
            "ccdtemperature": 20.0,
            "bayeroffsetx": 1,
            "bayeroffsety": 0,
            "pixelsizex": 2.9,
            "lastexposurestarttime": "2026-09-10T03:00:00",
            "lastexposureduration": 1.0,
        }
    )

    with pytest.raises(CaptureMetadataError, match=r"Camera\.Gain"):
        await capture_one_exposure(
            camera=camera,
            telescope=None,
            filterwheel=None,
            repo=repo,
            folder=tmp_path,
            duration_s=1.0,
            context=_context(),
            frame_type=FrameType.LIGHT,
            folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
            tz=UTC,
            now=lambda: FIXED_NOW,
            sleep=_no_sleep,
        )

    # Nothing was catalogued — the failure happened before any durable write.
    assert await repo.list_complete() == []
    assert await repo.list_writing() == []


# -- StartExposure not yet mapped on a 9.16+ driver -------------------------- #


async def test_start_exposure_not_implemented_is_reported_plainly(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    camera = _camera(start_exposure_not_implemented=True)

    with pytest.raises(ExposureNotAvailableError, match="StartExposure"):
        await capture_one_exposure(
            camera=camera,
            telescope=None,
            filterwheel=None,
            repo=repo,
            folder=tmp_path,
            duration_s=1.0,
            context=_context(),
            frame_type=FrameType.LIGHT,
            folder_template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
            tz=UTC,
            now=lambda: FIXED_NOW,
            sleep=_no_sleep,
        )

    assert await repo.list_complete() == []
    assert await repo.list_writing() == []


async def test_image_ready_never_true_times_out_without_cataloguing_anything(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    camera = _camera(ready_after_polls=10_000_000,
                     properties={"camerastate": 2, "percentcompleted": 100})
    ticks = iter([FIXED_NOW, FIXED_NOW, FIXED_NOW.replace(minute=5)])

    def clock() -> datetime:
        return next(ticks, FIXED_NOW.replace(minute=10))

    with pytest.raises(ExposureTimeoutError, match="CameraState=2, PercentCompleted=100"):
        await drive_exposure(
            camera=camera,
            telescope=None,
            filterwheel=None,
            duration_s=1.0,
            context=_context(),
            frame_type=FrameType.LIGHT,
            ready_timeout_s=2.0,
            now=clock,
            sleep=_no_sleep,
        )


async def test_image_ready_transient_disconnect_retries_the_same_exposure() -> None:
    class ReconnectingCamera(FakeCamera):
        async def image_ready(self) -> bool:
            self.image_ready_calls += 1
            if self.image_ready_calls == 1:
                raise AlpacaRemoteError(
                    int(AlpacaErrorNumber.NOT_CONNECTED),
                    "connect timed out after 5s with no response (ConnectTimeout)",
                    member="Camera.ImageReady",
                )
            return True

    base = _camera()
    camera = ReconnectingCamera(
        frame=base.frame,
        connected=True,
        properties=base.properties,
    )

    await drive_exposure(
        camera=camera,
        telescope=None,
        filterwheel=None,
        duration_s=0.0,
        context=_context(),
        frame_type=FrameType.LIGHT,
        sleep=_no_sleep,
    )

    assert camera.start_exposure_calls == [(0.0, True)]
    assert camera.image_ready_calls == 2
    assert camera.image_array_calls == 1


async def test_image_ready_transport_retry_is_bounded() -> None:
    class OfflineCamera(FakeCamera):
        async def image_ready(self) -> bool:
            self.image_ready_calls += 1
            raise AlpacaTransportError("Camera.ImageReady: ConnectTimeout")

    base = _camera()
    camera = OfflineCamera(
        frame=base.frame,
        connected=True,
        properties=base.properties,
    )

    with pytest.raises(AlpacaTransportError, match="ConnectTimeout"):
        await drive_exposure(
            camera=camera,
            telescope=None,
            filterwheel=None,
            duration_s=0.0,
            context=_context(),
            frame_type=FrameType.LIGHT,
            sleep=_no_sleep,
            ready_transport_retries=3,
        )

    assert camera.start_exposure_calls == [(0.0, True)]
    assert camera.image_ready_calls == 4
    assert camera.image_array_calls == 0


async def test_dark_does_not_query_parked_sky_coordinates() -> None:
    telescope = _telescope()
    frame, _ = await drive_exposure(camera=_camera(), telescope=telescope,
        filterwheel=None, duration_s=60, context=_context(),
        frame_type=FrameType.DARK, sleep=_no_sleep)
    assert not {"rightascension", "declination", "equatorialsystem"}.intersection(telescope.get_calls)
    assert frame.metadata.ra_deg is None
    assert frame.metadata.dec_deg is None


async def test_optional_pixel_size_retries_one_connection_close() -> None:
    from ssc.api.capture_service import _read_optional

    class FlakyPixelSize:
        calls = 0
        async def get(self, member: str) -> object:
            self.calls += 1
            if self.calls == 1:
                raise AlpacaTransportError("Server disconnected without sending a response")
            return 2.9

    device = FlakyPixelSize()
    unsupported: set[str] = set()
    value = await _read_optional(device, "pixelsizex", label="Camera.PixelSizeX", unsupported_members=unsupported)
    assert value == 2.9
    assert device.calls == 2
    assert not unsupported


async def test_optional_focus_position_retries_missing_native_dictionary_key() -> None:
    from ssc.api.capture_service import _read_optional

    class FlakyPosition:
        calls = 0
        async def get(self, member: str) -> object:
            self.calls += 1
            if self.calls == 1:
                raise AlpacaRemoteError(0x4FF, "The given key 'position' was not present in the dictionary.", member=member)
            return 1210

    device = FlakyPosition()
    unsupported: set[str] = set()
    assert await _read_optional(device, "position", label="Focuser.Position", unsupported_members=unsupported) == 1210
    assert device.calls == 2
    assert not unsupported


async def test_optional_metadata_timeout_does_not_cancel_required_image(monkeypatch) -> None:
    import asyncio
    from unittest.mock import AsyncMock

    import ssc.api.capture_service as service

    monkeypatch.setattr(service, "OPTIONAL_METADATA_TIMEOUT_S", .01)
    async def stalled(_member):
        await asyncio.Event().wait()
    device = AsyncMock()
    device.get.side_effect = stalled
    image_done = asyncio.Event()
    async def pixels():
        await asyncio.sleep(.02)
        image_done.set()
        return "pixels"
    cache = set()
    optional, image = await asyncio.gather(
        service._read_optional(device, "focallength", label="Telescope.FocalLength", unsupported_members=cache), pixels())
    assert optional is None and image == "pixels" and image_done.is_set()
    assert cache == set()


async def test_dither_group_token_is_preserved_in_frame_metadata() -> None:
    from dataclasses import replace
    frame, _ = await drive_exposure(camera=_camera(), telescope=_telescope(),
        filterwheel=None, duration_s=60,
        context=replace(_context(), dither_group_id="sequence:3"),
        frame_type=FrameType.LIGHT, sleep=_no_sleep)
    assert frame.metadata.dither_group_id == "sequence:3"


@pytest.mark.parametrize("missing_bayer", [False, True])
async def test_autofocus_metadata_only_reads_bayer_and_preserves_unknown(missing_bayer) -> None:
    camera = _camera(
        not_implemented=frozenset({"bayeroffsetx", "bayeroffsety"}) if missing_bayer else frozenset()
    )
    telescope = _telescope(connected=False)
    filterwheel = _filterwheel(connected=False)
    frame, started = await drive_exposure(
        camera=camera,
        telescope=telescope,
        filterwheel=filterwheel,
        duration_s=2,
        context=_context(),
        frame_type=FrameType.LIGHT,
        metadata_mode="autofocus",
        sleep=_no_sleep,
    )
    assert camera.start_exposure_calls == [(2, True)]
    assert camera.image_ready_calls > 0
    assert set(camera.get_calls) <= {"imageready", "bayeroffsetx", "bayeroffsety"}
    assert not telescope.get_calls and not filterwheel.get_calls
    assert telescope.connect_calls == filterwheel.connect_calls == 0
    assert frame.metadata.bayer_offset_x == (None if missing_bayer else 1)
    assert frame.metadata.bayer_offset_y == (None if missing_bayer else 0)
    assert frame.metadata.date_obs == started
    assert frame.metadata.gain is None
    np.testing.assert_array_equal(frame.pixels, camera.frame.array.T)


async def test_capture_prioritizes_measured_cfa_and_filter_before_supplementary_reads() -> None:
    completed: set[str] = set()
    priority = {"bayeroffsetx", "bayeroffsety", "position", "names"}

    class PriorityCamera(FakeCamera):
        async def get(self, member: str, **params: object) -> object:
            if member in {"bayeroffsetx", "bayeroffsety"}:
                await asyncio.sleep(0)
                completed.add(member)
            elif member != "imageready":
                assert completed == priority
            return await super().get(member, **params)

    class PriorityFilter(FakeDevice):
        async def get(self, member: str, **params: object) -> object:
            await asyncio.sleep(0)
            completed.add(member)
            return await super().get(member, **params)

    base = _camera()
    camera = PriorityCamera(frame=base.frame, properties=base.properties, connected=True)
    filterwheel = PriorityFilter(properties=_filterwheel().properties, connected=True)
    frame, _ = await drive_exposure(
        camera=camera, telescope=None, filterwheel=filterwheel, duration_s=0,
        context=_context(), frame_type=FrameType.LIGHT, sleep=_no_sleep,
    )
    assert frame.metadata.bayer_offset_x == 1
    assert frame.metadata.bayer_offset_y == 0
    assert frame.metadata.filter_name == "LP"


@pytest.mark.parametrize("sensor_type", [None, 0, 2])
async def test_capture_records_unknown_cfa_without_inventing_mono_or_filter(sensor_type) -> None:
    from ssc.capture.fits import write_fits_bytes

    missing = {"bayeroffsetx", "bayeroffsety"}
    if sensor_type is None:
        missing.add("sensortype")
    camera = _camera(properties={"sensortype": sensor_type}, not_implemented=frozenset(missing))
    filterwheel = _filterwheel(not_implemented=frozenset({"position"}))
    frame, _ = await drive_exposure(
        camera=camera, telescope=None, filterwheel=filterwheel, duration_s=0,
        context=_context(), frame_type=FrameType.LIGHT, sleep=_no_sleep,
    )
    assert frame.metadata.sensor_type == sensor_type
    assert frame.metadata.sensor_metadata_checked is True
    assert frame.metadata.filter_name is None
    header = read_fits_image(write_fits_bytes(frame.pixels, frame.metadata)).header
    assert header["CFASTATE"] == ("mono" if sensor_type == 0 else "unknown")
    assert "BAYERPAT" not in header
    assert "FILTER" not in header
    if sensor_type is None:
        assert "SENSTYPE" not in header
    else:
        assert header["SENSTYPE"] == sensor_type
