import numpy as np
import pytest

from ssc.stacking.integration import IncrementalTrimmedMean, StackIntegrationError, sigma_clip_stack


def test_rejects_isolated_hot_pixel_and_is_reproducible() -> None:
    frames = np.full((3, 4, 4), 10.0)
    frames[2, 1, 2] = 1000.0
    first = sigma_clip_stack(frames, sigma=3.0, iterations=3)
    second = sigma_clip_stack(frames, sigma=3.0, iterations=3)
    assert first.rejected_pixels == 1
    assert first.pixels[1, 2] == pytest.approx(10.0)
    assert np.array_equal(first.pixels, second.pixels)


def test_rejects_invalid_parameters() -> None:
    with pytest.raises(StackIntegrationError):
        sigma_clip_stack(np.ones((2, 2, 2)), sigma=0)


def test_quantized_sensor_noise_is_not_mistaken_for_zero_mad_outliers() -> None:
    rng = np.random.default_rng(20260914)
    frames = 1104.0 + 16.0 * rng.integers(-2, 3, size=(10, 32, 40))
    # Force a majority-tied pixel whose adjacent ADC-code samples are valid.
    frames[:, 4, 7] = np.array([1104] * 6 + [1088, 1120, 1088, 1120])
    result = sigma_clip_stack(frames, sigma=5.0, iterations=1, chunk_rows=7)
    np.testing.assert_allclose(result.pixels, np.mean(frames, axis=0))
    assert result.rejected_pixels == 0
    assert result.pooled_sigma > 0
    assert result.accepted_count_min == 10
    assert result.accepted_count_median == 10
    assert result.accepted_count_max == 10


def test_quantization_aware_floor_still_rejects_transient_excursion() -> None:
    rng = np.random.default_rng(20260914)
    frames = 1104.0 + 16.0 * rng.integers(-2, 3, size=(10, 32, 40))
    expected = np.mean(frames[:, 9, 12])
    frames[8, 9, 12] = 60_000.0
    expected = np.mean(np.delete(frames[:, 9, 12], 8))
    result = sigma_clip_stack(frames, sigma=5.0, iterations=1, chunk_rows=7)
    assert result.pixels[9, 12] == pytest.approx(expected)
    assert result.rejected_high >= 1
    assert result.accepted_count_min == 9


def test_calibration_mean_beats_unrejected_mean_with_sparse_transients() -> None:
    rng = np.random.default_rng(42)
    yy, xx = np.indices((36, 44))
    truth = 1104.0 + 0.5 * xx + 0.25 * yy
    truth[7, 11] = 5000.0  # Repeatable hot pixels are calibration signal.
    noise = 16.0 * np.rint(rng.normal(0.0, 18.0, size=(30, *truth.shape)) / 16.0)
    frames = truth + noise
    frames[3, 13, 17] += 3000.0
    frames[21, 25, 31] -= 3000.0

    result = sigma_clip_stack(frames, sigma=5.0, iterations=1, chunk_rows=5)
    ordinary_error = np.sqrt(np.mean((np.mean(frames, axis=0) - truth) ** 2))
    robust_error = np.sqrt(np.mean((result.pixels - truth) ** 2))

    assert robust_error < ordinary_error
    assert result.pixels[7, 11] == pytest.approx(5000.0, abs=12.0)
    assert result.rejected_low >= 1
    assert result.rejected_high >= 1


def test_chunk_size_does_not_change_integration() -> None:
    rng = np.random.default_rng(7)
    frames = rng.normal(1000.0, 12.0, size=(20, 23, 17))
    frames[4, 8, 9] = 50_000.0
    narrow = sigma_clip_stack(frames, sigma=5.0, iterations=1, chunk_rows=1)
    wide = sigma_clip_stack(frames, sigma=5.0, iterations=1, chunk_rows=23)
    np.testing.assert_array_equal(narrow.pixels, wide.pixels)
    assert narrow.rejected_pixels == wide.rejected_pixels
    assert narrow.pooled_sigma == wide.pooled_sigma
    assert narrow.accepted_count_min == wide.accepted_count_min
    assert narrow.accepted_count_median == wide.accepted_count_median
    assert narrow.accepted_count_max == wide.accepted_count_max


def test_incremental_trim_rejects_single_frame_hot_pixel() -> None:
    integration = IncrementalTrimmedMean((4, 4))
    integration.add(np.full((4, 4), 100.0))
    hot = np.full((4, 4), 100.0)
    hot[2, 1] = 10_000.0
    integration.add(hot)
    integration.add(np.full((4, 4), 100.0))
    np.testing.assert_array_equal(integration.pixels, np.full((4, 4), 100.0))


def test_incremental_group_trim_rejects_a_repeated_five_frame_hot_pixel() -> None:
    integration = IncrementalTrimmedMean((4, 4), group_size=5)
    hot = np.full((4, 4), 100.0)
    hot[2, 2] = 5000.0
    for _ in range(5):
        integration.add(hot)
    for _ in range(10):
        integration.add(np.full((4, 4), 100.0))

    assert integration.frame_count == 15
    np.testing.assert_array_equal(integration.pixels, np.full((4, 4), 100.0))


def test_incremental_group_size_must_be_positive() -> None:
    with pytest.raises(StackIntegrationError, match="group size"):
        IncrementalTrimmedMean((4, 4), group_size=0)


def test_incremental_chunks_preserve_rgb_group_trimming_and_snapshots(monkeypatch) -> None:
    monkeypatch.setattr(IncrementalTrimmedMean, "_CHUNK_ELEMENTS", 7)
    rng = np.random.default_rng(16)
    frames = rng.normal(100, 5, (17, 4, 6, 3))
    frames[::3, 1, 2, 0] = np.nan
    valid = rng.random(frames.shape) > 0.1
    integration = IncrementalTrimmedMean(frames.shape[1:], group_size=5)
    snapshots = []
    expected_snapshots = []
    for index, frame in enumerate(frames):
        # Noncontiguous float64 input must not trigger whole-frame conversion.
        frame = frame[:, ::-1, :]
        mask = valid[index, :, ::-1, :]
        snapshots.append(integration.add(frame, mask))
        masked = np.where(valid[: index + 1], frames[: index + 1], np.nan)
        groups = []
        for first in range(0, index + 1, 5):
            group = masked[first : first + 5]
            counts = np.sum(np.isfinite(group), axis=0)
            mean = np.full(frame.shape, np.nan)
            np.divide(np.nansum(group, axis=0), counts, out=mean, where=counts > 0)
            groups.append(mean)
        group_values = np.array(groups)
        counts = np.sum(np.isfinite(group_values), axis=0)
        total = np.nansum(group_values, axis=0)
        trimmed = counts >= 3
        total[trimmed] -= np.min(np.where(np.isfinite(group_values), group_values, np.inf), axis=0)[
            trimmed
        ]
        total[trimmed] -= np.max(
            np.where(np.isfinite(group_values), group_values, -np.inf), axis=0
        )[trimmed]
        counts[trimmed] -= 2
        expected = np.zeros(frame.shape)
        np.divide(total, counts, out=expected, where=counts > 0)
        expected_snapshots.append(expected[:, ::-1, :])
    for snapshot, expected in zip(snapshots, expected_snapshots, strict=True):
        np.testing.assert_allclose(snapshot, expected, rtol=2e-6)
    np.testing.assert_array_equal(
        integration.coverage, np.sum(valid & np.isfinite(frames), axis=0)[:, ::-1, :]
    )
    assert integration.memory_bytes == np.prod(frames.shape[1:]) * 32


def test_incremental_empty_frame_does_not_advance_group() -> None:
    integration = IncrementalTrimmedMean((3, 3), group_size=2)
    integration.add(np.ones((3, 3)))
    with pytest.raises(StackIntegrationError, match="no valid pixels"):
        integration.add(np.full((3, 3), np.nan))
    assert integration.frame_count == 1
    np.testing.assert_array_equal(integration.add(np.full((3, 3), 3.0)), 2.0)


@pytest.mark.parametrize("lengths", [(6, 6, 6, 6, 6), (3, 3, 3, 3), (5, 2, 4, 1)])
def test_explicit_detector_blocks_reject_repeated_defects_despite_cadence_or_gaps(lengths):
    def integrate():
        integration = IncrementalTrimmedMean((4, 4), group_size=None)
        baseline = np.full((4, 4), 100.0)
        hot = baseline.copy()
        hot[2, 2] = 5000.0
        memory = integration.memory_bytes
        for block, length in enumerate(lengths):
            integration.begin_group()
            # Repeated empty boundaries/rejected exposures do not create samples.
            integration.begin_group()
            for _ in range(length):
                integration.add(hot if block == 0 else baseline)
            assert integration.memory_bytes == memory
        integration.begin_group()
        assert integration.frame_count == sum(lengths)
        np.testing.assert_array_equal(integration.coverage, sum(lengths))
        np.testing.assert_array_equal(integration.pixels, baseline)
        return integration.pixels.tobytes()

    assert integrate() == integrate()


def test_row_reader_preserves_global_sample_grid_and_all_reports() -> None:
    from dataclasses import fields

    from ssc.stacking.integration import sigma_clip_stack_rows

    rng = np.random.default_rng(92)
    # >262144 spatial pixels forces stride two, crossing odd-width row boundaries.
    frames = (1104 + 16 * rng.integers(-2, 3, size=(10, 515, 513))).astype(np.float32)
    frames[:, :60] = 1104  # This region alone has zero temporal noise.
    frames[:4, 4, 7] += 16  # Legitimate noise must inherit the GLOBAL floor.
    frames[2, 210, 12] = np.nan
    frames[8, 240, 9] = 60_000
    frames[1, 330, 6] = -50_000
    expected = sigma_clip_stack(frames, sigma=5, iterations=1)
    requests = []

    def read_rows(start: int, stop: int) -> np.ndarray:
        assert 0 < stop - start <= 7
        requests.append((start, stop))
        return frames[:, start:stop]

    actual = sigma_clip_stack_rows(frames.shape, read_rows, sigma=5, iterations=1, chunk_rows=7)
    np.testing.assert_array_equal(actual.pixels, expected.pixels)
    for field in fields(expected):
        if field.name != "pixels":
            assert getattr(actual, field.name) == getattr(expected, field.name)
    sample = frames.reshape(10, -1)[:, ::2].astype(np.float64)
    differences = np.diff(sample, axis=0)
    differences = differences[np.isfinite(differences)]
    direct_sigma = 1.4826 * np.median(np.abs(differences - np.median(differences))) / np.sqrt(2)
    assert actual.pooled_sigma == direct_sigma
    assert actual.pixels[4, 7] == np.mean(frames[:, 4, 7], dtype=np.float64)
    assert requests[-1][1] == 515
