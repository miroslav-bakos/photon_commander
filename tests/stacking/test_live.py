import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from ssc.capture.fits import debayer, write_fits_file
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.capture.quality import FrameQuality, measure_frame_quality
from ssc.skymap.fits import read_fits_image
from ssc.stacking.integration import IncrementalTrimmedMean
from ssc.stacking.live import LiveStackEngine, LiveStackError, LiveStackFrameRejectedError
from ssc.stacking.registration import RegistrationResult
from tests.capture.test_motion import star_field


def _metadata() -> FrameMetadata:
    return FrameMetadata(
        date_obs=None,
        exposure_s=None,
        gain=None,
        ccd_temp_c=None,
        bayer_offset_x=None,
        bayer_offset_y=None,
        pixel_size_um=None,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=None,
        object_name=None,
        frame_type=FrameType.LIGHT,
        focal_length_mm=None,
        telescope_name="test",
        ssc_version="0.0.0",
        device_firmware=None,
    )


def test_motion_rejection_preserves_raw_and_previous_stack(tmp_path: Path) -> None:
    clean = star_field()
    damaged = clean + 0.45 * np.roll(clean - 100, (4, 140), (0, 1))
    good_path, bad_path = tmp_path / "good.fits", tmp_path / "smudged.fits"
    write_fits_file(good_path, clean.astype(np.uint16), _metadata())
    write_fits_file(bad_path, damaged.astype(np.uint16), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(good_path, measure_quality=True)
    previous_pixels = engine.pixels.copy()
    with pytest.raises(LiveStackFrameRejectedError, match="motion quality gate"):
        engine.add_frame(bad_path, measure_quality=True)
    assert bad_path.exists()
    assert engine.run.frame_paths == (good_path,)
    np.testing.assert_array_equal(engine.pixels, previous_pixels)


def test_reset_creates_new_prefix_and_preserves_raw_frames(tmp_path: Path) -> None:
    first = tmp_path / "raw-1.fits"
    second = tmp_path / "raw-2.fits"
    first_pixels = np.zeros((16, 16), dtype=np.uint16)
    first_pixels[4, 5] = 10
    first_pixels[10, 11] = 20
    second_pixels = np.roll(first_pixels * 2, (2, -3), axis=(0, 1))
    write_fits_file(first, first_pixels, _metadata())
    write_fits_file(second, second_pixels, _metadata())
    engine = LiveStackEngine(tmp_path)
    run = engine.reset()
    engine.add_frame(first)
    engine.add_frame(second)
    assert run.output_path.name == "live_001.fits"
    assert first.exists() and second.exists()
    assert not run.output_path.exists()
    assert not run.parameters_path.exists()
    run = engine.save()
    stacked = read_fits_image(run.output_path.read_bytes()).data
    assert np.unravel_index(int(np.argmax(stacked)), stacked.shape) == (10, 11)
    assert stacked[10, 11] == 30
    restarted = engine.reset()
    assert restarted.output_path.name == "live_002.fits"
    assert not restarted.parameters_path.exists()


def test_saved_stack_carries_count_integration_and_source_provenance(tmp_path: Path) -> None:
    pixels = np.zeros((64, 64), dtype=np.uint16)
    pixels[12, 19] = 1000
    pixels[31, 44] = 700
    pixels[50, 8] = 500
    started = datetime(2026, 9, 15, 21, 0, tzinfo=UTC)
    metadata = replace(
        _metadata(), date_obs=started, exposure_s=30.0, gain=200,
        pixel_size_um=2.0, focal_length_mm=160.0, ra_deg=42.0,
        dec_deg=61.0, site_lat_deg=49.0, site_lon_deg=18.0,
        object_name="IC 1805 1", filter_name="LP", sensor_name="Seestar S30 Pro",
    )
    first = tmp_path / "first.fits"
    second = tmp_path / "second.fits"
    write_fits_file(first, pixels, metadata)
    write_fits_file(second, pixels, replace(metadata, date_obs=started + timedelta(seconds=35)))

    engine = LiveStackEngine(tmp_path)
    run = engine.reset()
    engine.add_frame(first)
    engine.add_frame(second)
    run = engine.save()

    header = read_fits_image(run.output_path.read_bytes()).header
    assert header["STACKCNT"] == header["NCOMBINE"] == 2
    assert header["TOTALEXP"] == 60.0
    assert header["EXPTIME"] == 30.0
    assert header["DATE-OBS"] == started.isoformat()
    assert header["RA"] == 42.0 and header["DEC"] == 61.0
    assert header["SITELAT"] == 49.0 and header["SITELONG"] == 18.0
    assert header["XPIXSZ"] == 2.0 and header["FOCALLEN"] == 160.0
    assert header["OBJECT"] == "IC 1805 1" and header["FILTER"] == "LP"
    assert header["INSTRUME"] == "Seestar S30 Pro"


def test_reset_waits_for_in_flight_frame_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "raw.fits"
    pixels = np.zeros((32, 32), dtype=np.uint16)
    pixels[5, 7] = 1000
    pixels[20, 24] = 500
    write_fits_file(raw, pixels, _metadata())
    engine = LiveStackEngine(tmp_path)
    first_run = engine.reset()
    integration_entered = threading.Event()
    allow_integration_to_finish = threading.Event()
    reset_started = threading.Event()
    original_add = IncrementalTrimmedMean.add

    def paused_add(self, values, valid=None):
        original_add(self, values, valid)
        integration_entered.set()
        assert allow_integration_to_finish.wait(timeout=5)

    def reset_engine():
        reset_started.set()
        return engine.reset()

    monkeypatch.setattr(IncrementalTrimmedMean, "add", paused_add)
    with ThreadPoolExecutor(max_workers=2) as pool:
        add_future = pool.submit(engine.add_frame, raw)
        assert integration_entered.wait(timeout=5)
        reset_future = pool.submit(reset_engine)
        assert reset_started.wait(timeout=5)
        assert not reset_future.done()
        allow_integration_to_finish.set()
        integrated = add_future.result(timeout=5)
        restarted = reset_future.result(timeout=5)

    assert integrated.sequence == first_run.sequence
    assert integrated.frame_paths == (raw,)
    assert restarted.sequence == first_run.sequence + 1
    assert restarted.frame_paths == ()


def test_dithered_lights_are_registered_before_integration(tmp_path: Path) -> None:
    reference = np.zeros((64, 64), dtype=np.uint16)
    reference[12, 19] = 1000
    reference[31, 44] = 700
    reference[50, 8] = 500
    first = tmp_path / "light-1.fits"
    second = tmp_path / "light-2.fits"
    write_fits_file(first, reference, _metadata())
    write_fits_file(second, np.roll(reference, (4, -6), axis=(0, 1)), _metadata())

    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(first)
    engine.add_frame(second)

    assert engine.pixels is not None
    stacked = engine.pixels
    assert np.array_equal(stacked, reference.astype(np.float32))
    engine.save()
    manifest = json.loads(engine.run.parameters_path.read_text())
    assert manifest["registrations"][1]["shift_y_px"] == -4
    assert manifest["registrations"][1]["shift_x_px"] == 6
    assert manifest["registrations"][1]["peak_ratio"] > 10


def test_sequential_dithers_accumulate_into_the_original_stack_frame(tmp_path: Path) -> None:
    reference = np.zeros((64, 64), dtype=np.uint16)
    reference[12, 19] = 1000
    reference[31, 44] = 700
    reference[50, 8] = 500
    paths = [tmp_path / f"light-{index}.fits" for index in range(3)]
    for path, offset in zip(paths, ((0, 0), (4, -6), (7, -2)), strict=True):
        write_fits_file(path, np.roll(reference, offset, axis=(0, 1)), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    for path in paths:
        engine.add_frame(path)

    assert engine.run is not None
    assert engine.run.last_shift_y_px == -7
    assert engine.run.last_shift_x_px == 2
    assert [item["shift_y_px"] for item in engine._registrations] == [0.0, -4.0, -7.0]
    assert [item["shift_x_px"] for item in engine._registrations] == [0.0, 6.0, 2.0]
    assert engine.pixels is not None
    assert np.array_equal(engine.pixels, reference.astype(np.float32))


def test_auto_crop_removes_only_incomplete_registration_edges(tmp_path: Path) -> None:
    reference = np.zeros((64, 64), dtype=np.uint16)
    reference[12, 19] = 1000
    reference[31, 44] = 700
    reference[50, 8] = 500
    first = tmp_path / "light-1.fits"
    second = tmp_path / "light-2.fits"
    write_fits_file(first, reference, _metadata())
    write_fits_file(second, np.roll(reference, (4, -6), axis=(0, 1)), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(first)
    engine.add_frame(second)

    full = engine.presentation_pixels(auto_crop=False)
    cropped = engine.presentation_pixels(auto_crop=True)

    assert full is not None and full.shape == (64, 64)
    assert cropped is not None and cropped.shape == (60, 58)
    assert engine.pixels is full


def test_auto_crop_never_collapses_a_long_stack_to_a_sliver(tmp_path: Path) -> None:
    engine = LiveStackEngine(tmp_path)
    engine._bayer_pattern = None
    engine._integration = IncrementalTrimmedMean((100, 100))
    engine._integration._frame_count = 20
    engine._integration._count[:, 49:51] = 20

    assert engine.presentation_pixels(auto_crop=True) is engine.pixels


def test_field_snr_estimate_uses_complete_coverage_and_robust_sky_noise() -> None:
    rng = np.random.default_rng(20260914)
    pixels = rng.normal(1000.0, 10.0, size=(512, 512)).astype(np.float32)
    pixels[::20, ::20] += 100.0
    coverage = np.ones_like(pixels, dtype=bool)
    coverage[:8] = False
    pixels[:8] = 100_000.0

    estimate = LiveStackEngine._estimate_field_snr(pixels, coverage)

    assert estimate is not None
    assert 8.0 < estimate < 13.0


def test_full_resolution_live_stack_integrates_full_size_rgb(tmp_path: Path) -> None:
    metadata = replace(_metadata(), bayer_offset_x=0, bayer_offset_y=1)
    raw = tmp_path / "bayer-light.fits"
    pixels = np.arange(64, dtype=np.uint16).reshape(8, 8)
    write_fits_file(raw, pixels, metadata)

    engine = LiveStackEngine(tmp_path)
    engine.reset()
    run = engine.add_frame(raw)
    run = engine.save()

    assert engine.pixels is not None
    assert engine.pixels.shape == (8, 8, 3)
    np.testing.assert_allclose(
        engine.pixels,
        debayer(pixels.astype(np.float64), "GBRG").astype(np.float32),
    )
    persisted = read_fits_image(run.output_path.read_bytes())
    assert persisted.data.shape == (3, 8, 8)
    assert "BAYERPAT" not in persisted.header


@pytest.mark.parametrize("binning", [1, 2])
def test_live_stack_quality_uses_debayered_green_at_source_resolution(
    tmp_path: Path, binning: int
) -> None:
    metadata = replace(_metadata(), bayer_offset_x=0, bayer_offset_y=1)
    raw = tmp_path / "bayer-stars.fits"
    yy, xx = np.mgrid[:128, :128]
    pixels = np.full((128, 128), 1000.0)
    for y, x, flux in ((30, 31, 5000), (60, 45, 4500), (75, 95, 4000), (100, 101, 3500)):
        pixels += flux * np.exp(-((yy - y) ** 2 + (xx - x) ** 2) / (2 * 1.3**2))
    pixels += np.random.default_rng(20260916).normal(0, 8, pixels.shape)
    pixels = np.clip(np.rint(pixels), 0, 65535).astype(np.uint16)
    write_fits_file(raw, pixels, metadata)

    expected = measure_frame_quality(
        debayer(pixels.astype(np.float64), "GBRG")[..., 1],
        pixel_size_um=None,
        focal_length_mm=None,
    )
    bayered = measure_frame_quality(
        pixels.astype(np.float64), pixel_size_um=None, focal_length_mm=None
    )
    assert expected.hfr_px is not None and bayered.hfr_px is not None
    assert abs(expected.hfr_px - bayered.hfr_px) > 0.1

    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=binning)
    run = engine.add_frame(raw, measure_quality=True)

    assert run.hfr_px == pytest.approx(expected.hfr_px, abs=1e-5)
    assert run.fwhm_px == pytest.approx(expected.fwhm_px, abs=1e-5)
    assert run.star_count == expected.star_count


def test_cloud_quality_gate_rejects_before_integration_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = tmp_path / "cloudy.fits"
    write_fits_file(raw, np.arange(64, dtype=np.uint16).reshape(8, 8), _metadata())
    clear = FrameQuality(
        star_count=40,
        hfr_px=1.5,
        fwhm_px=3.8,
        hfr_arcsec=None,
        fwhm_arcsec=None,
        background=1180.0,
        noise=28.0,
        detected_source_count=5000,
        contrast_snr=31.0,
    )
    cloudy = replace(
        clear,
        star_count=38,
        hfr_px=1.15,
        detected_source_count=600,
        contrast_snr=4.0,
    )
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine._quality_history.extend([clear, clear, clear])
    monkeypatch.setattr(engine, "_measure_candidate_quality", lambda *_args: cloudy)

    with pytest.raises(LiveStackFrameRejectedError, match="cloud quality gate"):
        engine.add_frame(raw, measure_quality=True)

    assert engine.latest_frame_quality == (raw, cloudy)
    assert engine.run is not None
    assert engine.run.frame_paths == ()
    monkeypatch.setattr(engine, "_measure_candidate_quality", lambda *_args: clear)
    run = engine.add_frame(raw, measure_quality=True)
    assert run.frame_paths == (raw,)
    assert engine.latest_frame_quality == (raw, clear)


def test_low_fraction_rigid_registration_is_a_rejected_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first.fits"
    second = tmp_path / "second.fits"
    pixels = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    write_fits_file(first, pixels, _metadata())
    write_fits_file(second, pixels, _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=2)
    engine.add_frame(first)
    monkeypatch.setattr(
        "ssc.stacking.live.register_translation",
        lambda *_args: RegistrationResult(
            0.5,
            1.0,
            None,
            None,
            None,
            None,
            1.0,
            0.1,
            10.0,
            rotation_deg=0.0,
            matched_star_count=17,
            star_match_fraction=0.28,
            star_rms_px=0.61,
        ),
    )

    with pytest.raises(
        LiveStackFrameRejectedError,
        match=r"17 matches, 28% of the smaller field, 0.61px RMS",
    ):
        engine.add_frame(second)


def test_full_resolution_rgb_registration_keeps_odd_pixel_precision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = replace(_metadata(), bayer_offset_x=1, bayer_offset_y=0)
    first = tmp_path / "first.fits"
    second = tmp_path / "second.fits"
    pixels = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    write_fits_file(first, pixels, metadata)
    write_fits_file(second, pixels, metadata)
    monkeypatch.setattr(
        "ssc.stacking.live.register_translation",
        lambda *_args: RegistrationResult(3.0, -5.0, None, None, None, None, 0.9, 0.1, 9.0),
    )

    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=1)
    engine.add_frame(first)
    run = engine.add_frame(second)

    assert run.last_shift_y_px == 3.0
    assert run.last_shift_x_px == -5.0
    assert engine.pixels is not None
    assert engine.pixels.shape == (64, 64, 3)


def test_cfa_flat_is_equalized_per_color_before_calibrating_light(tmp_path: Path) -> None:
    metadata = replace(_metadata(), bayer_offset_x=1, bayer_offset_y=0)
    raw = tmp_path / "light.fits"
    flat = tmp_path / "flat.fits"
    light_pixels = np.full((8, 8), 1000, dtype=np.uint16)
    flat_pixels = np.empty((8, 8), dtype=np.uint16)
    flat_pixels[0::2, 0::2] = 40000
    flat_pixels[0::2, 1::2] = 25000
    flat_pixels[1::2, 0::2] = 20000
    flat_pixels[1::2, 1::2] = 40000
    write_fits_file(raw, light_pixels, metadata)
    write_fits_file(flat, flat_pixels, metadata)

    engine = LiveStackEngine(tmp_path)
    engine.reset(flat_master=flat)
    run = engine.add_frame(raw)
    run = engine.save()

    calibrated = read_fits_image(run.output_path.read_bytes()).data
    assert calibrated.shape == (3, 8, 8)
    np.testing.assert_allclose(calibrated, 1000.0)


def test_reset_advances_past_runs_from_an_earlier_process(tmp_path: Path) -> None:
    (tmp_path / "live_003.fits").touch()
    (tmp_path / "live_008.json").touch()
    (tmp_path / "live_invalid.fits").touch()

    run = LiveStackEngine(tmp_path).reset()

    assert run.output_path.name == "live_009.fits"
    assert run.parameters_path.name == "live_009.json"
    assert (tmp_path / "live_003.fits").exists()
    assert (tmp_path / "live_008.json").exists()


def test_transposed_master_is_aligned_in_memory(tmp_path: Path) -> None:
    raw = tmp_path / "raw.fits"
    master = tmp_path / "master.fits"
    write_fits_file(raw, np.full((4, 6), 20, dtype=np.uint16), _metadata())
    write_fits_file(master, np.full((6, 4), 5, dtype=np.uint16), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset(dark_master=master)
    run = engine.add_frame(raw)
    run = engine.save()
    assert read_fits_image(run.output_path.read_bytes()).data.mean() == 15


def test_dark_and_bias_are_not_both_subtracted(tmp_path: Path) -> None:
    raw = tmp_path / "raw.fits"
    dark = tmp_path / "dark.fits"
    bias = tmp_path / "bias.fits"
    write_fits_file(raw, np.full((4, 4), 120, dtype=np.uint16), _metadata())
    write_fits_file(dark, np.full((4, 4), 20, dtype=np.uint16), _metadata())
    write_fits_file(bias, np.full((4, 4), 5, dtype=np.uint16), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset(dark_master=dark, bias_master=bias)
    run = engine.add_frame(raw)
    run = engine.save()
    assert read_fits_image(run.output_path.read_bytes()).data.mean() == 100


def test_each_light_and_master_is_read_only_once(tmp_path: Path, monkeypatch) -> None:
    master = tmp_path / "dark.fits"
    write_fits_file(master, np.full((32, 32), 10, dtype=np.uint16), _metadata())
    paths = []
    pixels = np.zeros((32, 32), dtype=np.uint16)
    pixels[5, 7] = 1000
    pixels[20, 24] = 500
    for index in range(6):
        path = tmp_path / f"light-{index}.fits"
        write_fits_file(path, np.roll(pixels + 20, (index, -index), axis=(0, 1)), _metadata())
        paths.append(path)

    reads: dict[Path, int] = {}
    original = Path.read_bytes

    def counted(path: Path) -> bytes:
        reads[path] = reads.get(path, 0) + 1
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", counted)
    engine = LiveStackEngine(tmp_path)
    engine.reset(dark_master=master)
    for path in paths:
        engine.add_frame(path)

    assert [reads[path] for path in paths] == [1] * len(paths)
    assert reads[master] == 1


def test_working_memory_does_not_grow_with_frame_count(tmp_path: Path) -> None:
    pixels = np.zeros((32, 32), dtype=np.uint16)
    pixels[5, 7] = 1000
    pixels[20, 24] = 500
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    memory = []
    for index in range(12):
        path = tmp_path / f"light-{index}.fits"
        offset = index % 3
        write_fits_file(path, np.roll(pixels, (offset, -offset), axis=(0, 1)), _metadata())
        engine.add_frame(path)
        memory.append(engine.memory_bytes)
    assert len(set(memory)) == 1
    # Five-frame group rejection adds a fixed pending sum/count and completed
    # group count, but still never grows with the number of lights.
    assert memory[0] <= pixels.size * 40


def test_2x2_binning_preserves_bayer_color_at_half_resolution(tmp_path: Path) -> None:
    metadata = replace(_metadata(), bayer_offset_x=1, bayer_offset_y=0)
    raw = tmp_path / "bayer-light.fits"
    pixels = np.empty((8, 10), dtype=np.uint16)
    pixels[0::2, 0::2] = 200  # G in GRBG
    pixels[0::2, 1::2] = 100  # R
    pixels[1::2, 0::2] = 50  # B
    pixels[1::2, 1::2] = 300  # G
    write_fits_file(raw, pixels, metadata)

    engine = LiveStackEngine(tmp_path)
    run = engine.reset(binning=2)
    engine.add_frame(raw)

    assert engine.pixels is not None
    assert engine.pixels.shape == (4, 5, 3)
    np.testing.assert_array_equal(engine.pixels[..., 0], 100)
    np.testing.assert_array_equal(engine.pixels[..., 1], 250)
    np.testing.assert_array_equal(engine.pixels[..., 2], 50)
    run = engine.save()
    persisted = read_fits_image(run.output_path.read_bytes())
    assert persisted.data.shape == (3, 4, 5)
    assert "BAYERPAT" not in persisted.header


def test_2x2_quality_avoids_full_resolution_debayer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = replace(
        _metadata(),
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        focal_length_mm=150.0,
    )
    raw = tmp_path / "bayer-quality.fits"
    pixels = np.arange(64, dtype=np.uint16).reshape(8, 8)
    write_fits_file(raw, pixels, metadata)

    def unexpected_debayer(*_args: object, **_kwargs: object) -> np.ndarray:
        raise AssertionError("2x2 live-stack quality must not allocate a full-resolution RGB image")

    monkeypatch.setattr("ssc.stacking.live.debayer", unexpected_debayer)
    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=2)

    run = engine.add_frame(raw, measure_quality=True)

    assert run.star_count == 0
    assert engine.pixels is not None
    assert engine.pixels.shape == (4, 4, 3)


def test_masters_can_be_selected_before_the_first_light_only(tmp_path: Path) -> None:
    dark = tmp_path / "dark.fits"
    flat = tmp_path / "flat.fits"
    light = tmp_path / "light.fits"
    write_fits_file(dark, np.zeros((8, 8), dtype=np.uint16), _metadata())
    write_fits_file(flat, np.ones((8, 8), dtype=np.uint16), _metadata())
    write_fits_file(light, np.full((8, 8), 100, dtype=np.uint16), _metadata())

    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=2)
    selected = engine.set_calibration_masters(
        dark_master=dark, bias_master=None, flat_master=flat
    )
    assert selected.dark_master == dark
    assert selected.flat_master == flat

    engine.add_frame(light)
    with pytest.raises(LiveStackError, match="after stacking has started"):
        engine.set_calibration_masters(
            dark_master=None, bias_master=None, flat_master=None
        )


def test_ambiguous_registration_is_rejected_and_not_counted(tmp_path: Path) -> None:
    checker = (np.indices((32, 32)).sum(axis=0) % 2 * 1000).astype(np.uint16)
    first = tmp_path / "first.fits"
    ambiguous = tmp_path / "ambiguous.fits"
    write_fits_file(first, checker, _metadata())
    write_fits_file(ambiguous, checker, _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(first)

    with pytest.raises(LiveStackError, match="unambiguously"):
        engine.add_frame(ambiguous)
    assert engine.run is not None
    assert engine.run.frame_paths == (first,)


def test_stack_filename_contains_safe_target_and_sequence_survives_restart(tmp_path: Path) -> None:
    frame = tmp_path / "raw.fits"
    write_fits_file(frame, np.zeros((16, 16), dtype=np.uint16), replace(_metadata(), object_name="NGC 7000 2 / test"))
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(frame)
    saved = engine.save()
    assert saved.output_path.name == "live_001_NGC_7000_2_test.fits"
    assert saved.output_path.exists()
    assert saved.parameters_path.with_suffix(".fits") == saved.output_path
    assert LiveStackEngine(tmp_path).reset().sequence == 2


@pytest.mark.parametrize("pattern", ["RGGB", "GRBG", "GBRG", "BGGR"])
@pytest.mark.parametrize("shape", [(1, 1), (127, 9), (128, 12), (129, 11), (259, 17)])
def test_striped_full_resolution_debayer_matches_existing_arithmetic(
    pattern: str, shape: tuple[int, int]
) -> None:
    pixels = np.random.default_rng(42).normal(1000, 200, shape).astype(np.float32)
    expected = debayer(pixels.astype(np.float64), pattern).astype(np.float32)
    actual = LiveStackEngine._debayer_full_resolution(pixels, pattern)
    assert actual.dtype == np.float32
    np.testing.assert_array_equal(actual, expected)


def test_full_resolution_debayer_bounds_temporary_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = []
    original = debayer

    def record_rows(pixels: np.ndarray, pattern: str) -> np.ndarray:
        rows.append(pixels.shape[0])
        return original(pixels, pattern)

    monkeypatch.setattr("ssc.stacking.live.debayer", record_rows)
    pixels = np.ones((2160, 20), dtype=np.float32)
    actual = LiveStackEngine._debayer_full_resolution(pixels, "RGGB")
    assert actual.shape == (2160, 20, 3)
    assert max(rows) <= 132
    assert len(rows) == 17


def test_save_evaluates_stack_snapshot_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = tmp_path / "light.fits"
    write_fits_file(raw, np.ones((8, 8), dtype=np.uint16), _metadata())
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    engine.add_frame(raw)
    calls = 0
    original = LiveStackEngine.pixels.fget

    def snapshot(instance: LiveStackEngine) -> np.ndarray | None:
        nonlocal calls
        calls += 1
        return original(instance)

    monkeypatch.setattr(LiveStackEngine, "pixels", property(snapshot))
    engine.save()
    assert calls == 1


def test_successful_dither_boundaries_group_accepted_frames_and_persist_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "ssc.stacking.live.register_translation",
        lambda *_args: RegistrationResult(0, 0, None, None, None, None, 1.0, 0.1, 10.0),
    )
    engine = LiveStackEngine(tmp_path)
    engine.reset(group_by_dither=True)
    baseline = np.full((8, 8), 100, dtype=np.uint16)
    hot = baseline.copy()
    hot[2, 2] = 5000
    memory = []
    # The shortened second block models accepted-frame gaps from rejected
    # exposures. It still ends at the next actual successful dither.
    for block, length in enumerate((6, 2, 6, 6, 6)):
        boundary_id = f"success-{block}"
        engine.begin_dither_group(boundary_id)
        engine.begin_dither_group(boundary_id)  # replay is idempotent
        for index in range(length):
            raw = tmp_path / f"raw-{block}-{index}.fits"
            write_fits_file(raw, hot if block == 0 else baseline, replace(_metadata(), dither_group_id=boundary_id))
            engine.add_frame(raw)
            memory.append(engine.memory_bytes)
    assert len(set(memory)) == 1
    np.testing.assert_array_equal(engine.pixels, baseline)
    run = engine.run
    assert run is not None
    assert len(run.frame_paths) == 26
    assert run.dither_group_boundaries == (
        ("success-0", 0), ("success-1", 6), ("success-2", 8), ("success-3", 14), ("success-4", 20)
    )
    saved = json.loads(run.parameters_path.read_text())
    assert saved["integration_grouping"] == "dither_boundary"
    assert saved["dither_group_boundaries"][-1] == {
        "id": "success-4", "accepted_frame_index": 20
    }
    assert all(path.exists() for path in run.frame_paths)


def test_manual_stack_without_boundary_information_uses_per_frame_rejection(tmp_path: Path) -> None:
    engine = LiveStackEngine(tmp_path)
    run = engine.reset()
    assert run.integration_grouping == "per_frame"
    with pytest.raises(LiveStackError, match="boundary-grouped"):
        engine.begin_dither_group("unavailable")
    engine.reset(group_by_dither=True)
    with pytest.raises(LiveStackError, match="must not be empty"):
        engine.begin_dither_group("")
    assert engine.run.dither_group_boundaries == ()


def test_frame_group_header_boundary_survives_rejected_first_frame(tmp_path, monkeypatch):
    engine = LiveStackEngine(tmp_path)
    engine.reset()
    first = tmp_path / "first.fits"
    next_group = tmp_path / "rejected.fits"
    write_fits_file(first, np.full((8, 8), 100, np.uint16), replace(_metadata(), dither_group_id="epoch:0"))
    write_fits_file(next_group, np.full((8, 8), 100, np.uint16), replace(_metadata(), dither_group_id="epoch:1"))
    engine.add_frame(first)
    monkeypatch.setattr(engine, "_measure_candidate_quality", lambda *_: _reject_quality())
    monkeypatch.setattr("ssc.stacking.live.frame_motion_reason", lambda *_: "trailed")
    with pytest.raises(LiveStackFrameRejectedError):
        engine.add_frame(next_group, measure_quality=True)
    assert engine.run.dither_group_boundaries == (("epoch:0", 0), ("epoch:1", 1))
    with pytest.raises(LiveStackFrameRejectedError):
        engine.add_frame(next_group, measure_quality=True)
    assert len(engine.run.dither_group_boundaries) == 2
    assert engine.run.frame_paths == (first,)


def _reject_quality():
    return measure_frame_quality(np.arange(64).reshape(8, 8).astype(np.float64), pixel_size_um=None, focal_length_mm=None)


@pytest.mark.parametrize("science_temp,dark_temp,delta", [(25.8125,16.5,9.3125),(None,16.5,None),(25.8125,None,None)])
def test_saved_stack_persists_measured_dark_temperature_delta(tmp_path, science_temp, dark_temp, delta):
    dark = tmp_path / "dark.fits"
    light = tmp_path / "light.fits"
    write_fits_file(dark, np.zeros((8, 8), np.uint16), replace(_metadata(), ccd_temp_c=dark_temp))
    write_fits_file(light, np.full((8, 8),100,np.uint16), replace(_metadata(), ccd_temp_c=science_temp))
    engine = LiveStackEngine(tmp_path)
    engine.reset(dark_master=dark)
    engine.add_frame(light)
    run = engine.save()
    payload = json.loads(run.parameters_path.read_text())
    assert payload["reference_ccd_temp_c"] == science_temp
    assert payload["dark_ccd_temp_c"] == dark_temp
    assert payload["dark_temperature_delta_c"] == delta


def test_header_grouping_rejects_mixed_and_out_of_order_provenance(tmp_path):
    engine = LiveStackEngine(tmp_path)
    paths = {}
    for name, token in (("a", "epoch:0"), ("b", "epoch:1"), ("old", "epoch:0"), ("legacy", None)):
        path = tmp_path / f"{name}.fits"
        write_fits_file(path, np.full((8, 8), 100, np.uint16), replace(_metadata(), dither_group_id=token))
        paths[name] = path
    engine.add_frame(paths["a"])
    with pytest.raises(LiveStackError, match="require DITHGRP"):
        engine.add_frame(paths["legacy"])
    # A rejected first frame still establishes chronology without requiring registration.
    engine.begin_dither_group("epoch:1")
    with pytest.raises(LiveStackError, match="chronological"):
        engine.add_frame(paths["old"])
    engine.reset()
    engine.add_frame(paths["legacy"])
    with pytest.raises(LiveStackError, match="cannot mix"):
        engine.add_frame(paths["a"])


@pytest.mark.parametrize("binning", [1, 2])
def test_missing_cfa_preserves_stack_and_dither_group_until_next_valid_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, binning: int
) -> None:
    metadata = replace(_metadata(), bayer_offset_x=1, bayer_offset_y=0, dither_group_id="A")
    pixels = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    first, incomplete, next_valid = (tmp_path / name for name in ("first.fits", "4572.fits", "next.fits"))
    write_fits_file(first, pixels, metadata)
    write_fits_file(incomplete, pixels, replace(metadata, bayer_offset_y=None, dither_group_id="B"))
    write_fits_file(next_valid, pixels, replace(metadata, dither_group_id="B"))
    monkeypatch.setattr(
        "ssc.stacking.live.register_translation",
        lambda *_args: RegistrationResult(0.0, 0.0, None, None, None, None, 0.9, 0.1, 9.0),
    )
    engine = LiveStackEngine(tmp_path)
    engine.reset(binning=binning)
    before = engine.add_frame(first)
    previous_pixels = engine.pixels.copy()
    with pytest.raises(LiveStackError, match="Bayer provenance is missing"):
        engine.add_frame(incomplete)
    assert engine.run == before
    np.testing.assert_array_equal(engine.pixels, previous_pixels)
    assert incomplete.exists()
    assert "BAYERPAT" not in read_fits_image(incomplete.read_bytes()).header
    recovered = engine.add_frame(next_valid)
    assert recovered.frame_paths == (first, next_valid)


@pytest.mark.parametrize("sensor_type", [None, 2])
def test_first_missing_cfa_waits_for_first_valid_frame_without_seeding_mono(
    tmp_path: Path, sensor_type: int | None
) -> None:
    pixels = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    incomplete, good = tmp_path / "missing-cfa.fits", tmp_path / "measured-cfa.fits"
    metadata = replace(
        _metadata(), sensor_type=sensor_type, sensor_metadata_checked=True,
        bayer_offset_x=None, bayer_offset_y=None,
    )
    write_fits_file(incomplete, pixels, metadata)
    write_fits_file(good, pixels, replace(metadata, sensor_type=2, bayer_offset_x=1, bayer_offset_y=0))
    engine = LiveStackEngine(tmp_path)
    before = engine.reset()
    with pytest.raises(LiveStackError, match="Bayer provenance is missing"):
        engine.add_frame(incomplete)
    assert engine.run == before
    assert engine.pixels is None
    assert incomplete.exists()
    recovered = engine.add_frame(good)
    assert recovered.frame_paths == (good,)
    assert engine.pixels.shape == (64, 64, 3)


def test_measured_mono_accepts_placeholder_offsets_without_inventing_cfa(tmp_path: Path) -> None:
    frame = tmp_path / "mono.fits"
    pixels = np.arange(64 * 64, dtype=np.uint16).reshape(64, 64)
    write_fits_file(frame, pixels, replace(
        _metadata(), sensor_type=0, sensor_metadata_checked=True,
        bayer_offset_x=0, bayer_offset_y=0,
    ))
    header = read_fits_image(frame.read_bytes()).header
    assert header["SENSTYPE"] == 0
    assert header["CFASTATE"] == "mono"
    assert "BAYERPAT" not in header
    engine = LiveStackEngine(tmp_path)
    engine.add_frame(frame)
    assert engine.pixels.shape == pixels.shape
