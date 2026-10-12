# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from ssc.capture.fits import write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.capture.masters import CalibrationMasterError, build_calibration_master
from ssc.skymap.fits import read_fits_image


def _write(path: Path, pixels: np.ndarray, kind: FrameType, *, gain: int = 10) -> Path:
    metadata = FrameMetadata(
        date_obs=datetime(2026, 9, 12, tzinfo=UTC),
        exposure_s=0.1,
        gain=gain,
        ccd_temp_c=20.0,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name="UV/IR" if kind is FrameType.FLAT else None,
        object_name=None,
        frame_type=kind,
        focal_length_mm=None,
        telescope_name="test",
        ssc_version="test",
        device_firmware=None,
        optic_id="tele" if kind is FrameType.FLAT else None,
        flat_source="panel" if kind is FrameType.FLAT else None,
        bin_x=1,
        bin_y=1,
        subframe_x=0,
        subframe_y=0,
        pixel_size_y_um=2.9,
        sensor_name="IMX585",
        focuser_position=1210,
        focuser_temperature_c=31.3125,
        camera_offset=1108,
        max_adu=65535,
        readout_mode=0,
        alignment_mode=1,
        aperture_diameter_mm=30.0,
        focal_ratio=160.0 / 30.0,
        wide_camera=False,
        total_exposure_s=0.1,
    )
    write_fits_file(path, pixels, metadata)
    return path


def test_bias_master_is_unnormalised_robust_mean(tmp_path: Path) -> None:
    values = (*([1000] * 9), 60000)
    paths = tuple(
        _write(tmp_path / f"bias-{index}.fits", np.full((4, 5), value, np.uint16), FrameType.BIAS)
        for index, value in enumerate(values)
    )
    output = tmp_path / "master_bias.fits"
    result = build_calibration_master(
        frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
    )
    image = read_fits_image(output.read_bytes())
    assert np.allclose(image.data, 1000)
    assert image.header["STACKCNT"] == 10
    assert image.header["TOTALEXP"] == pytest.approx(1.0)
    assert image.header["FOCUSPOS"] == 1210
    assert image.header["FOCTEMP"] == pytest.approx(31.3125)
    assert image.header["INSTRUME"] == "IMX585"
    assert image.header["BAYERPAT"] == "GRBG"
    assert result.frame_count == 10
    assert result.rejected_pixels == 20
    provenance = json.loads(result.provenance_path.read_text())
    assert provenance["method"] == "unnormalised quantization-aware clipped mean"
    assert provenance["integration"]["sigma"] == pytest.approx(5.0)
    assert provenance["integration"]["accepted_samples_per_pixel"] == {
        "minimum": 9,
        "median": 9.0,
        "maximum": 9,
    }


def test_flat_master_subtracts_bias_and_normalises_each_frame(tmp_path: Path) -> None:
    pattern = np.array([[1.0, 2.0], [3.0, 4.0]])
    bias_paths = tuple(
        _write(tmp_path / f"bias-{index}.fits", np.full((2, 2), 100, np.uint16), FrameType.BIAS)
        for index in range(10)
    )
    flat_paths = tuple(
        _write(
            tmp_path / f"flat-{index}.fits",
            (100 + pattern * scale).astype(np.uint16),
            FrameType.FLAT,
        )
        for index, scale in enumerate(np.linspace(11_500, 12_500, 10))
    )
    output = tmp_path / "master_flat.fits"
    build_calibration_master(
        frame_type=FrameType.FLAT,
        source_paths=flat_paths,
        output_path=output,
        bias_paths=bias_paths,
        ssc_version="test",
        max_adu=65535,
    )
    actual = read_fits_image(output.read_bytes()).data
    assert np.median(actual) == pytest.approx(1.0)
    assert actual == pytest.approx(pattern / np.median(pattern), abs=2e-5)
    quality = json.loads(output.with_suffix(".json").read_text())["quality"]
    assert quality["median"] == pytest.approx(1.0)
    assert sum(quality["histogram"]["counts"]) == 4
    provenance = json.loads(output.with_suffix(".json").read_text())
    assert provenance["pedestal_integration"]["rejected_samples"] == 0


def test_flat_master_refuses_to_guess_pedestal(tmp_path: Path) -> None:
    flats = tuple(
        _write(tmp_path / f"flat-{index}.fits", np.full((2, 2), 30_000, np.uint16), FrameType.FLAT)
        for index in range(10)
    )
    with pytest.raises(CalibrationMasterError, match="compatible bias"):
        build_calibration_master(
            frame_type=FrameType.FLAT,
            source_paths=flats,
            output_path=tmp_path / "master.fits",
            ssc_version="test",
            max_adu=65535,
        )
    assert not (tmp_path / "master.fits").exists()


def test_dark_master_preserves_repeatable_glow_and_hot_pixels_for_subtraction(
    tmp_path: Path,
) -> None:
    _yy, xx = np.indices((40, 60))
    fixed_pattern = 900 + (xx / 59) ** 3 * 300
    fixed_pattern[8, 11] = 5000
    rng = np.random.default_rng(42)
    paths = tuple(
        _write(
            tmp_path / f"dark-{index}.fits",
            np.clip(fixed_pattern + rng.normal(0, 8, fixed_pattern.shape), 0, 65535).astype(
                np.uint16
            ),
            FrameType.DARK,
        )
        for index in range(20)
    )
    output = tmp_path / "master_dark.fits"
    build_calibration_master(
        frame_type=FrameType.DARK,
        source_paths=paths,
        output_path=output,
        ssc_version="test",
    )
    master = read_fits_image(output.read_bytes()).data
    assert master[8, 11] > 4500
    assert np.median(master[:, -6:]) > np.median(master[:, 20:40])
    report = json.loads(output.with_suffix(".json").read_text())["quality"]["dark_pattern"]
    assert report["stable_hot_pixel_candidates"] >= 1
    assert report["edge_glow_excess"] > 0


def test_master_never_allocates_complete_source_cube(tmp_path: Path, monkeypatch) -> None:
    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{index}.fits", np.full((129, 11), 1000, np.uint16), FrameType.BIAS)
        for index in range(10)
    )
    original_empty = np.empty

    def bounded_empty(shape, *args, **kwargs):
        assert tuple(shape) != (10, 129, 11), "complete source cube allocated"
        return original_empty(shape, *args, **kwargs)

    monkeypatch.setattr(masters.np, "empty", bounded_empty)
    build_calibration_master(
        frame_type=FrameType.BIAS,
        source_paths=paths,
        output_path=tmp_path / "master.fits",
        ssc_version="test",
    )


@pytest.mark.parametrize("kind", [FrameType.DARK, FrameType.FLAT])
def test_streamed_master_matches_full_cube_pixels_and_provenance(
    tmp_path: Path, monkeypatch, kind
) -> None:
    import ssc.capture.masters as masters
    from ssc.stacking.integration import sigma_clip_stack

    rng = np.random.default_rng(84)
    shape = (131, 21)
    yy, xx = np.indices(shape)
    bias_paths = tuple(
        _write(
            tmp_path / f"bias-{i}.fits",
            (1000 + rng.normal(0, 7, shape)).astype(np.float32),
            FrameType.BIAS,
        )
        for i in range(10)
    )
    sources = []
    for i in range(10):
        signal = 1000 + xx * 3 + yy * 2 + rng.normal(0, 8, shape)
        if kind is FrameType.FLAT:
            signal = 33000 + xx * 50 + yy * 2 + i * 80 + rng.normal(0, 8, shape)
        signal[75, 12] += 500 if i == 2 else 0
        sources.append(_write(tmp_path / f"source-{i}.fits", signal.astype(np.float32), kind))
    options = {
        "frame_type": kind,
        "source_paths": tuple(sources),
        "ssc_version": "test",
        "bias_paths": bias_paths if kind is FrameType.FLAT else (),
        "max_adu": 65535,
    }
    streamed = build_calibration_master(output_path=tmp_path / "streamed.fits", **options)

    def cube_reference(sources, *, bias=None, medians=None):
        cube = np.array(
            [read_fits_image(s.path.read_bytes()).data for s in sources], dtype=np.float32
        )
        if bias is not None:
            cube -= bias
        if medians is not None:
            cube_medians = np.median(cube, axis=(1, 2))
            np.testing.assert_array_equal(medians, cube_medians)
            cube /= cube_medians[:, None, None]
        return sigma_clip_stack(cube, sigma=5, iterations=1)

    monkeypatch.setattr(masters, "_integrate", cube_reference)
    reference = build_calibration_master(output_path=tmp_path / "reference.fits", **options)
    np.testing.assert_array_equal(
        read_fits_image(streamed.path.read_bytes()).data,
        read_fits_image(reference.path.read_bytes()).data,
    )
    actual = json.loads(streamed.provenance_path.read_text())
    expected = json.loads(reference.provenance_path.read_text())
    for key in (
        "integration",
        "pedestal_integration",
        "quality",
        "dark_pattern",
        "rejected_pixels",
    ):
        assert actual.get(key) == expected.get(key)


@pytest.mark.parametrize("dtype", [np.uint8, np.int16, np.uint16, np.int32, np.float32, np.float64])
def test_fits_row_source_matches_existing_reader(tmp_path: Path, dtype) -> None:
    from ssc.capture.fits_rows import FitsRowSource

    pixels = (np.arange(129 * 11).reshape(129, 11) / 3).astype(dtype)
    if dtype is np.uint16:
        pixels += 32768  # Verify FITS BZERO rather than signed integer reinterpretation.
    path = _write(tmp_path / "source.fits", pixels, FrameType.BIAS)
    source = FitsRowSource.open(path)
    expected = read_fits_image(path.read_bytes()).data.astype(np.float32)
    for start, stop in ((0, 1), (1, 65), (65, 129)):
        np.testing.assert_array_equal(source.read_rows(start, stop), expected[start:stop])
    with pytest.raises(ValueError, match="row span"):
        source.read_rows(0, 130)


def test_fits_row_source_refuses_truncated_source(tmp_path: Path) -> None:
    from ssc.capture.fits_rows import FitsRowSource
    from ssc.skymap.fits import FitsError

    path = _write(tmp_path / "source.fits", np.ones((129, 11), np.uint16), FrameType.BIAS)
    source = FitsRowSource.open(path)
    with path.open("r+b") as stream:
        stream.truncate(source.data_start + 129 * 11 * 2 - 1)
    with pytest.raises(FitsError, match="truncated"):
        FitsRowSource.open(path)
    with pytest.raises(FitsError, match="truncated"):
        source.read_rows(128, 129)


def test_fits_row_source_preserves_nonunit_scaling_before_float32_cast(tmp_path: Path) -> None:
    from ssc.capture.fits_rows import FitsRowSource

    path = _write(
        tmp_path / "scaled.fits",
        np.arange(129 * 11, dtype=np.uint16).reshape(129, 11),
        FrameType.BIAS,
    )
    raw = bytearray(path.read_bytes())
    for offset in range(0, len(raw), 80):
        if raw[offset : offset + 8] == b"BSCALE  ":
            raw[offset : offset + 80] = "BSCALE  = 0.123456789".ljust(80).encode("ascii")
            break
    path.write_bytes(raw)
    expected = read_fits_image(bytes(raw)).data.astype(np.float32)
    source = FitsRowSource.open(path)
    np.testing.assert_array_equal(source.read_rows(65, 129), expected[65:129])


@pytest.mark.parametrize("existing", [False, True])
def test_late_dark_source_read_failure_cleans_pending_and_preserves_published(
    tmp_path: Path, monkeypatch, existing: bool
) -> None:
    from ssc.capture.fits_rows import FitsRowSource

    paths = tuple(
        _write(tmp_path / f"dark-{i}.fits", np.full((129, 11), 1104, np.uint16), FrameType.DARK)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    pending = output.with_name(f".{output.name}.pending")
    if existing:
        output.write_bytes(b"previous published master")
        provenance.write_bytes(b"previous published provenance")
    original = FitsRowSource.read_rows

    def fail_late(self, start, stop):
        if pending.exists():
            raise OSError("injected late dark QA read failure")
        return original(self, start, stop)

    monkeypatch.setattr(FitsRowSource, "read_rows", fail_late)
    with pytest.raises(OSError, match="late dark QA"):
        build_calibration_master(
            frame_type=FrameType.DARK, source_paths=paths, output_path=output, ssc_version="test"
        )
    assert not pending.exists()
    if existing:
        assert output.read_bytes() == b"previous published master"
        assert provenance.read_bytes() == b"previous published provenance"
    else:
        assert not output.exists()
        assert not provenance.exists()
    assert not list(tmp_path.glob(".master.json.*.part"))


def test_provenance_staging_failure_preserves_published_master(tmp_path: Path, monkeypatch) -> None:
    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    output.write_bytes(b"previous published master")
    provenance.write_bytes(b"previous published provenance")

    def fail_dump(*args, **kwargs):
        raise OSError("injected provenance staging failure")

    monkeypatch.setattr(masters.json, "dump", fail_dump)
    with pytest.raises(OSError, match="provenance staging"):
        build_calibration_master(
            frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
        )
    assert output.read_bytes() == b"previous published master"
    assert provenance.read_bytes() == b"previous published provenance"
    assert not (tmp_path / ".master.fits.pending").exists()
    assert not list(tmp_path.glob(".master.json.*.part"))


@pytest.mark.parametrize("existing", [False, True])
def test_second_publication_rename_failure_preserves_prior_pair(tmp_path, monkeypatch, existing):
    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    pending = output.with_name(f".{output.name}.pending")
    if existing:
        output.write_bytes(b"previous published master")
        provenance.write_bytes(b"previous published provenance")
    original = masters.os.replace

    def fail_second(source, destination):
        if Path(source) == pending and Path(destination) == output:
            raise OSError("injected second publication rename failure")
        return original(source, destination)

    monkeypatch.setattr(masters.os, "replace", fail_second)
    with pytest.raises(OSError, match="second publication rename"):
        build_calibration_master(
            frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
        )
    if existing:
        assert output.read_bytes() == b"previous published master"
        assert provenance.read_bytes() == b"previous published provenance"
    else:
        assert not output.exists()
        assert not provenance.exists()
    assert not pending.exists()
    assert not list(tmp_path.glob("*.rollback"))
    assert not list(tmp_path.glob(".master.json.*.part"))


@pytest.mark.parametrize("failed_link", [1, 2])
def test_publication_backup_failure_leaves_prior_pair_untouched(tmp_path, monkeypatch, failed_link):
    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    output.write_bytes(b"previous published master")
    provenance.write_bytes(b"previous published provenance")
    original = masters.os.link
    links = 0

    def fail_backup(source, destination):
        nonlocal links
        links += 1
        if links == failed_link:
            raise OSError("injected publication backup failure")
        return original(source, destination)

    monkeypatch.setattr(masters.os, "link", fail_backup)
    with pytest.raises(OSError, match="publication backup"):
        build_calibration_master(
            frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
        )
    assert output.read_bytes() == b"previous published master"
    assert provenance.read_bytes() == b"previous published provenance"
    assert not (tmp_path / ".master.fits.pending").exists()
    assert not list(tmp_path.glob("*.rollback"))
    assert not list(tmp_path.glob(".master.json.*.part"))


def test_failed_publication_rollback_preserves_only_prior_provenance_backup(tmp_path, monkeypatch):
    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    pending = tmp_path / ".master.fits.pending"
    output.write_bytes(b"previous published master")
    provenance.write_bytes(b"previous published provenance")
    original = masters.os.replace

    def fail_publication_and_restore(source, destination):
        if Path(source) == pending and Path(destination) == output:
            raise OSError("injected second publication rename failure")
        if str(source).endswith(".rollback") and Path(destination) == provenance:
            raise OSError("injected prior provenance rollback failure")
        return original(source, destination)

    monkeypatch.setattr(masters.os, "replace", fail_publication_and_restore)
    with pytest.raises(ExceptionGroup, match="recovery incomplete") as raised:
        build_calibration_master(
            frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
        )
    assert {str(error) for error in raised.value.exceptions} == {
        "injected second publication rename failure",
        "injected prior provenance rollback failure",
    }
    assert output.read_bytes() == b"previous published master"
    backups = list(tmp_path.glob("*.rollback"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"previous published provenance"
    assert provenance.exists()  # New sidecar remains; the error never claims restored consistency.
    assert not pending.exists()
    assert not list(tmp_path.glob(".master.json.*.part"))


def test_successful_rebuild_publishes_new_pair_and_cleans_backups(tmp_path):
    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    output.write_bytes(b"previous published master")
    provenance.write_bytes(b"previous published provenance")
    result = build_calibration_master(
        frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
    )
    assert np.all(read_fits_image(result.path.read_bytes()).data == 1104)
    assert json.loads(provenance.read_text())["frame_type"] == "bias"
    assert not list(tmp_path.glob("*.rollback"))
    assert not (tmp_path / ".master.fits.pending").exists()


def test_unsupported_hardlinks_copy_backup_and_preserve_prior_pair(tmp_path, monkeypatch):
    import errno

    import ssc.capture.masters as masters

    paths = tuple(
        _write(tmp_path / f"bias-{i}.fits", np.full((4, 5), 1104, np.uint16), FrameType.BIAS)
        for i in range(10)
    )
    output = tmp_path / "master.fits"
    provenance = output.with_suffix(".json")
    pending = tmp_path / ".master.fits.pending"
    output.write_bytes(b"previous published master")
    provenance.write_bytes(b"previous published provenance")
    original_replace = masters.os.replace
    original_copy = masters.shutil.copy2
    copied = []

    def unsupported_link(source, destination):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    def tracked_copy(source, destination):
        copied.append(Path(source))
        return original_copy(source, destination)

    def fail_second(source, destination):
        if Path(source) == pending and Path(destination) == output:
            raise OSError("injected second publication rename failure")
        return original_replace(source, destination)

    monkeypatch.setattr(masters.os, "link", unsupported_link)
    monkeypatch.setattr(masters.shutil, "copy2", tracked_copy)
    monkeypatch.setattr(masters.os, "replace", fail_second)
    with pytest.raises(OSError, match="second publication rename"):
        build_calibration_master(
            frame_type=FrameType.BIAS, source_paths=paths, output_path=output, ssc_version="test"
        )
    assert copied == [provenance, output]
    assert output.read_bytes() == b"previous published master"
    assert provenance.read_bytes() == b"previous published provenance"
    assert not list(tmp_path.glob("*.rollback"))
    assert not pending.exists()


@pytest.mark.parametrize("filesystem,expected", [("vfat", True), ("ext4", False)])
def test_linux_fat_eperm_is_distinguished_from_ordinary_permission_failure(
    tmp_path, monkeypatch, filesystem, expected
):
    import errno
    import os

    from ssc.capture.masters import _unsupported_hardlinks

    original = Path.read_text
    device = tmp_path.stat().st_dev
    key = f"{os.major(device)}:{os.minor(device)}"

    def mount_info(self, *args, **kwargs):
        if self == Path("/proc/self/mountinfo"):
            return f"42 1 {key} / {tmp_path} rw - {filesystem} filesystem rw\n"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", mount_info)
    assert (
        _unsupported_hardlinks(OSError(errno.EPERM, "permission denied"), tmp_path / "master.fits")
        is expected
    )


def test_copy_backup_failure_does_not_touch_prior_file(tmp_path, monkeypatch):
    import errno

    import ssc.capture.masters as masters

    source = tmp_path / "master.fits"
    backup = tmp_path / "owned.rollback"
    source.write_bytes(b"previous published master")

    def unsupported_link(*args):
        raise OSError(errno.EOPNOTSUPP, "hardlinks unsupported")

    def incomplete_copy(*args):
        backup.write_bytes(b"incomplete")
        raise OSError(errno.ENOSPC, "injected disk full during copy")

    monkeypatch.setattr(masters.os, "link", unsupported_link)
    monkeypatch.setattr(masters.shutil, "copy2", incomplete_copy)
    with pytest.raises(OSError, match="disk full"):
        masters._backup_master_artifact(source, backup)
    assert source.read_bytes() == b"previous published master"
    assert not backup.exists()
