import numpy as np
import pytest

from ssc.stacking.registration import FrameObservation, RegistrationError, register_translation


def test_known_star_offset_registers_and_preserves_pointing() -> None:
    reference = np.zeros((64, 64), dtype=np.float64)
    reference[12, 19] = 10
    reference[31, 44] = 7
    reference[50, 8] = 5
    candidate = np.roll(reference, (4, -6), axis=(0, 1))
    result = register_translation(
        FrameObservation(reference, 83.1, -5.2),
        FrameObservation(candidate, 83.2, -5.1),
    )
    assert result.shift_y_px == pytest.approx(-4)
    assert result.shift_x_px == pytest.approx(6)
    assert result.reference_ra_deg == 83.1
    assert result.candidate_dec_deg == -5.1
    assert result.correlation_peak > 0.9
    assert result.peak_ratio > 10

    corrected = np.roll(
        candidate, (round(result.shift_y_px), round(result.shift_x_px)), axis=(0, 1)
    )
    assert np.array_equal(corrected, reference)


def test_registration_rejects_mismatched_frames() -> None:
    with pytest.raises(RegistrationError, match="identical dimensions"):
        register_translation(
            FrameObservation(np.zeros((8, 8)), None, None),
            FrameObservation(np.zeros((9, 8)), None, None),
        )


def test_noisy_star_field_uses_feature_correlation_when_phase_peak_is_ambiguous() -> None:
    rng = np.random.default_rng(42)
    stars = np.zeros((128, 128), dtype=np.float64)
    ys = rng.integers(8, 120, 100)
    xs = rng.integers(8, 120, 100)
    stars[ys, xs] = rng.uniform(50, 150, 100)
    reference = stars + rng.normal(0, 50, stars.shape)
    candidate = np.roll(stars, (8, 20), axis=(0, 1)) + rng.normal(0, 50, stars.shape)

    result = register_translation(
        FrameObservation(reference, None, None),
        FrameObservation(candidate, None, None),
    )

    assert result.shift_y_px == -8
    assert result.shift_x_px == -20
    assert result.peak_ratio > 1.2


def test_dense_star_field_recovers_rotation_and_subpixel_translation() -> None:
    rng = np.random.default_rng(20260914)
    shape = (540, 960)
    reference = np.full(shape, 1000.0)
    candidate = np.full(shape, 1000.0)
    reference_stars = np.column_stack(
        (rng.uniform(60, 480, 35), rng.uniform(80, 880, 35))
    )
    expected_rotation_deg = 0.37
    expected_shift = np.asarray((5.25, -8.5))
    angle = np.radians(expected_rotation_deg)
    matrix = np.asarray(
        ((np.cos(angle), -np.sin(angle)), (np.sin(angle), np.cos(angle)))
    )
    candidate_stars = (reference_stars - expected_shift) @ matrix.T
    yy, xx = np.mgrid[: shape[0], : shape[1]]
    for index, (reference_star, candidate_star) in enumerate(
        zip(reference_stars, candidate_stars, strict=True)
    ):
        amplitude = 10_000.0 + index * 300.0
        reference += amplitude * np.exp(
            -((yy - reference_star[0]) ** 2 + (xx - reference_star[1]) ** 2) / 2.0
        )
        candidate += amplitude * np.exp(
            -((yy - candidate_star[0]) ** 2 + (xx - candidate_star[1]) ** 2) / 2.0
        )
    reference += rng.normal(0.0, 20.0, shape)
    candidate += rng.normal(0.0, 20.0, shape)

    result = register_translation(
        FrameObservation(reference, None, None),
        FrameObservation(candidate, None, None),
    )

    assert result.rotation_deg == pytest.approx(expected_rotation_deg, abs=0.03)
    assert result.shift_y_px == pytest.approx(expected_shift[0], abs=0.35)
    assert result.shift_x_px == pytest.approx(expected_shift[1], abs=0.35)
    assert result.matched_star_count is not None and result.matched_star_count >= 30
    assert result.star_match_fraction is not None and result.star_match_fraction >= 0.8
    assert result.star_rms_px is not None and result.star_rms_px < 0.5
