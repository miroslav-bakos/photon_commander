"""``ssc.capture.pipeline``: the row-before-bytes write, and crash recovery."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from ssc.capture.frame import FrameMetadata, FrameType, RawFrame
from ssc.capture.pathtemplate import DEFAULT_CAPTURE_FOLDER_TEMPLATE
from ssc.capture.pipeline import capture_frame, reconcile_incomplete
from ssc.skymap.fits import read_fits_image
from ssc.store.capture import CaptureRepository
from ssc.store.database import Database

DATE_OBS = datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC)


def _raw_frame(
    *, object_name: str | None = "M42", frame_type: FrameType = FrameType.LIGHT
) -> RawFrame:
    pixels = np.arange(16, dtype=np.uint16).reshape(4, 4)
    metadata = FrameMetadata(
        date_obs=DATE_OBS,
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        ra_deg=10.0,
        dec_deg=20.0,
        site_lat_deg=48.0,
        site_lon_deg=16.0,
        filter_name="LP",
        object_name=object_name,
        frame_type=frame_type,
        focal_length_mm=250.0,
        telescope_name="Seestar S30 Pro",
        ssc_version="0.1.0",
        device_firmware="9.16",
    )
    return RawFrame(pixels=pixels, metadata=metadata)


@pytest.fixture
def repo(migrated: Database) -> CaptureRepository:
    return CaptureRepository(migrated)


async def test_capture_frame_writes_a_complete_row_and_a_readable_file(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame = _raw_frame()
    frame_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=frame,
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
    )

    complete = await repo.list_complete()
    assert [record.id for record in complete] == [frame_id]
    record = complete[0]
    assert record.status == "complete"
    assert record.frame_type == "light"
    assert record.path.exists()
    # The default template's own directory layout, end to end.
    assert record.path.parent == tmp_path / "M42" / "2026-09-10" / "lights"
    assert (
        record.path.name
        == "lights_M42_30s_+20.000_LP_20260910-220000_0000.fits"
    )

    image = read_fits_image(record.path.read_bytes())
    assert np.array_equal(image.data, frame.pixels.astype(np.float64))
    assert image.header["OBJECT"] == "M42"
    assert image.header["BAYERPAT"] == "GRBG"


async def test_capture_frame_uses_the_configured_filename_template(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        filename_template="%IMAGETYPE%-%SEQUENCE%",
        tz=UTC,
        sequence=7,
    )
    complete = {record.id: record for record in await repo.list_complete()}
    assert complete[frame_id].path.name == "lights-0007.fits"


async def test_default_calibration_filename_omits_the_target_component(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name="must-not-appear", frame_type=FrameType.FLAT),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        sequence=7,
    )
    complete = {record.id: record for record in await repo.list_complete()}
    assert (
        complete[frame_id].path.name
        == "flats_30s_+20.000_LP_20260910-220000_0007_g200.fits"
    )


async def test_calibration_filename_adds_exposure_and_gain_for_portability(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name=None, frame_type=FrameType.FLAT),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        filename_template="flat-LP-%SEQUENCE%",
        tz=UTC,
        sequence=7,
    )
    complete = {record.id: record for record in await repo.list_complete()}
    assert complete[frame_id].path.name == "flat-LP-0007_30s_g200.fits"


async def test_calibration_filename_does_not_duplicate_explicit_compatibility_tokens(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name=None, frame_type=FrameType.BIAS),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        filename_template="bias-%EXPOSURE%-g%GAIN%-%SEQUENCE%",
        tz=UTC,
        sequence=8,
    )
    complete = {record.id: record for record in await repo.list_complete()}
    assert complete[frame_id].path.name == "bias-30s-g200-0008.fits"


async def test_capture_frame_suffixes_a_colliding_filename(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    ids = []
    for _ in range(2):
        ids.append(
            await capture_frame(
                repo=repo,
                folder=tmp_path,
                frame=_raw_frame(),
                date_obs=DATE_OBS,
                template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
                filename_template="same",
                tz=UTC,
            )
        )

    complete = {record.id: record for record in await repo.list_complete()}
    assert [complete[frame_id].path.name for frame_id in ids] == ["same.fits", "same-2.fits"]
    assert all(complete[frame_id].path.exists() for frame_id in ids)


# -- dispatch verification bullet 1: one directory per frame type ------------ #


async def test_each_frame_type_lands_under_its_own_plural_directory(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    light_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name="M42", frame_type=FrameType.LIGHT),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        sequence=1,
    )
    # A dark has no target at all — %TARGET% must render the literal
    # "unknown", never an empty segment and never "None".
    dark_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name=None, frame_type=FrameType.DARK),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        sequence=2,
    )
    # A target whose name needs sanitising: spaces and punctuation. (A
    # non-ASCII target name is exercised separately, at the path-template
    # layer alone, in tests/capture/test_pathtemplate.py — the FITS OBJECT
    # card this same call also writes is ASCII-only per the FITS standard,
    # ssc.capture.fits._format_card, which is a pre-existing constraint this
    # dispatch does not touch.)
    flat_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name="NGC 7000 (North America)", frame_type=FrameType.FLAT),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        sequence=3,
    )
    bias_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(object_name=None, frame_type=FrameType.BIAS),
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
        sequence=4,
    )

    complete = {record.id: record for record in await repo.list_complete()}

    assert complete[light_id].path.parent == tmp_path / "M42" / "2026-09-10" / "lights"
    assert complete[dark_id].path.parent == tmp_path / "unknown" / "2026-09-10" / "darks"
    flat_parent = complete[flat_id].path.parent.relative_to(tmp_path)
    assert flat_parent.parts[1:] == ("2026-09-10", "flats")
    assert " " not in flat_parent.parts[0]
    assert "None" not in str(flat_parent)
    assert complete[bias_id].path.parent == tmp_path / "unknown" / "2026-09-10" / "biases"

    for record in complete.values():
        assert record.path.exists()


# -- dispatch verification bullet 2: the noon boundary, end to end ----------- #


async def test_capture_frame_places_the_noon_boundary_correctly(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    vienna = ZoneInfo("Europe/Vienna")
    template = "%DATEOFCAPTURE%"

    evening = datetime(2026, 9, 9, 19, 0, 0, tzinfo=UTC)  # 21:00 CEST, 2026-09-09
    small_hours = datetime(2026, 9, 9, 23, 0, 0, tzinfo=UTC)  # 01:00 CEST, 2026-09-10
    afternoon = datetime(2026, 9, 10, 11, 0, 0, tzinfo=UTC)  # 13:00 CEST, 2026-09-10

    evening_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(),
        date_obs=evening,
        template=template,
        tz=vienna,
        sequence=1,
    )
    small_hours_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(),
        date_obs=small_hours,
        template=template,
        tz=vienna,
        sequence=2,
    )
    afternoon_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=_raw_frame(),
        date_obs=afternoon,
        template=template,
        tz=vienna,
        sequence=3,
    )

    complete = {record.id: record for record in await repo.list_complete()}
    assert complete[evening_id].path.parent.name == "2026-09-09"
    assert complete[small_hours_id].path.parent.name == "2026-09-09"
    assert complete[afternoon_id].path.parent.name == "2026-09-10"


async def test_a_process_killed_between_begin_and_complete_leaves_a_writing_row(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    """Simulates the crash CLAUDE.md invariant 6 is about: the row is
    inserted, but the FITS write (and the completion update) never
    happens."""
    frame = _raw_frame()
    frame_id = await repo.begin_frame(
        path=tmp_path / "orphan.fits",
        date_obs=DATE_OBS,
        object_name=frame.metadata.object_name,
        filter_name=frame.metadata.filter_name,
        frame_type=frame.metadata.frame_type.value,
        exposure_s=frame.metadata.exposure_s,
        gain=frame.metadata.gain,
        ccd_temp_c=frame.metadata.ccd_temp_c,
        width=4,
        height=4,
    )
    # No file was ever written at this path — exactly what a kill between
    # begin_frame and write_fits_file leaves behind.
    assert not (tmp_path / "orphan.fits").exists()

    writing = await repo.list_writing()
    assert [record.id for record in writing] == [frame_id]
    assert await repo.list_complete() == []


async def test_reconcile_preserves_invalid_finals_and_reports_failure(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    partial_path = tmp_path / "partial.fits"
    partial_path.write_bytes(b"not a complete fits file")
    await repo.begin_frame(
        path=partial_path,
        date_obs=DATE_OBS,
        object_name="M42",
        filter_name="LP",
        frame_type="light",
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=4,
        height=4,
    )

    # A real, completed frame must survive reconciliation untouched.
    good_frame = _raw_frame(object_name="M31")
    good_id = await capture_frame(
        repo=repo,
        folder=tmp_path,
        frame=good_frame,
        date_obs=DATE_OBS,
        template=DEFAULT_CAPTURE_FOLDER_TEMPLATE,
        tz=UTC,
    )

    with pytest.raises(RuntimeError, match=r"recovery.*preserved"):
        await reconcile_incomplete(repo)

    assert partial_path.read_bytes() == b"not a complete fits file"
    assert len(await repo.list_writing()) == 1
    complete = await repo.list_complete()
    assert [record.id for record in complete] == [good_id]


async def test_reconcile_recovers_final_fits_after_crash_before_completion(
    repo: CaptureRepository, tmp_path: Path,
) -> None:
    from ssc.capture.fits import write_fits_file
    frame = _raw_frame()
    path = tmp_path / "finished.fits"
    frame_id = await repo.begin_frame(
        path=path, date_obs=DATE_OBS, object_name="M42", filter_name="LP", frame_type="light",
        exposure_s=30, gain=200, ccd_temp_c=20, width=4, height=4,
    )
    write_fits_file(path, frame.pixels, frame.metadata)
    before = path.read_bytes()
    assert await reconcile_incomplete(repo) == []
    assert path.read_bytes() == before
    assert (await repo.get_frame(frame_id)).status == "complete"


@pytest.mark.parametrize("damage", ["truncated", "dimensions", "exposure", "frame_type", "missing_metadata"])
async def test_reconcile_does_not_count_or_delete_unverified_final(
    repo: CaptureRepository, tmp_path: Path, damage: str,
) -> None:
    from ssc.capture.fits import write_fits_file
    frame = _raw_frame()
    path = tmp_path / "uncertain.fits"
    frame_id = await repo.begin_frame(
        path=path, date_obs=DATE_OBS, object_name="M42", filter_name="LP", frame_type="light",
        exposure_s=31 if damage == "exposure" else 30, gain=200, ccd_temp_c=20,
        width=5 if damage == "dimensions" else 4, height=4,
    )
    write_fits_file(path, frame.pixels, frame.metadata)
    content = path.read_bytes()
    if damage == "truncated":
        content = content[:2880]
    elif damage == "frame_type":
        content = content.replace(b"'light'", b"'dark '")
    elif damage == "missing_metadata":
        content = content.replace(b"EXPTIME ", b"UNKNOWN ")
    path.write_bytes(content)
    with pytest.raises(RuntimeError, match=r"recovery.*preserved"):
        await reconcile_incomplete(repo)
    assert path.read_bytes() == content
    assert (await repo.get_frame(frame_id)).status == "writing"
    assert await repo.list_complete() == []


async def test_reconcile_discards_only_missing_final_placeholder(repo: CaptureRepository, tmp_path: Path) -> None:
    path = tmp_path / "never-written.fits"
    await repo.begin_frame(path=path, date_obs=DATE_OBS, object_name=None, filter_name=None,
        frame_type="light", exposure_s=None, gain=None, ccd_temp_c=None, width=4, height=4)
    assert await reconcile_incomplete(repo) == [path]
    assert await repo.list_writing() == []


async def test_reconcile_matches_unicode_catalogue_to_writer_ascii_headers(
    repo: CaptureRepository, tmp_path: Path,
) -> None:
    from ssc.capture.fits import write_fits_file
    frame = _raw_frame(object_name="Červená hmlovina \N{GREEK SMALL LETTER ALPHA}")
    frame = replace(frame, metadata=replace(frame.metadata, filter_name="Úzkopásmový H\N{GREEK SMALL LETTER ALPHA}"))
    path = tmp_path / "unicode-final.fits"
    frame_id = await repo.begin_frame(
        path=path, date_obs=DATE_OBS, object_name=frame.metadata.object_name,
        filter_name=frame.metadata.filter_name, frame_type="light", exposure_s=30,
        gain=200, ccd_temp_c=20, width=4, height=4,
    )
    write_fits_file(path, frame.pixels, frame.metadata)
    before = path.read_bytes()
    assert read_fits_image(before).header["OBJECT"] == "Cervena hmlovina alpha"
    assert read_fits_image(before).header["FILTER"] == "Uzkopasmovy Halpha"
    assert await reconcile_incomplete(repo) == []
    recovered = await repo.get_frame(frame_id)
    assert recovered.status == "complete"
    assert recovered.object_name == "Červená hmlovina \N{GREEK SMALL LETTER ALPHA}"
    assert recovered.filter_name == "Úzkopásmový H\N{GREEK SMALL LETTER ALPHA}"
    assert path.read_bytes() == before
