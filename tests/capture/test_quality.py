"""``ssc.capture.quality``'s numpy-only HFR/FWHM star metric (``ssc-bkv.8``),
verified against synthetic star fields built here rather than any captured
FITS — the algorithm is a pure function over a pixel plane and has no other
dependency to fake."""

from __future__ import annotations

import numpy as np
import pytest

from ssc.capture.quality import measure_frame_quality


def _gaussian_star(height: int, width: int, cy: float, cx: float, sigma: float, flux: float) -> np.ndarray:
    """A Gaussian blob carrying ``flux`` total counts, peak = flux /
    (2*pi*sigma^2) — flux-preserving, not peak-preserving, because that is
    what a real defocus does: the same star's photons spread thinner, the
    peak drops, rather than the peak staying fixed while the star grows."""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float64)
    r2 = (yy - cy) ** 2 + (xx - cx) ** 2
    peak = flux / (2.0 * np.pi * sigma**2)
    return peak * np.exp(-r2 / (2.0 * sigma**2))


def _synthetic_field(
    *,
    height: int = 128,
    width: int = 128,
    stars: list[tuple[float, float, float, float]],
    background: float = 100.0,
    noise_sigma: float = 2.0,
    seed: int = 1,
) -> np.ndarray:
    """``stars`` is a list of ``(cy, cx, sigma, flux)``. Gaussian read noise
    on top of a flat background — an idealised but non-trivial frame: real
    read/shot noise is not exactly Gaussian, but it is close enough that a
    detector too fragile to handle this would also fail on a real frame."""
    rng = np.random.default_rng(seed)
    plane = np.full((height, width), background, dtype=np.float64)
    plane += rng.normal(0.0, noise_sigma, size=(height, width))
    for cy, cx, sigma, flux in stars:
        plane += _gaussian_star(height, width, cy, cx, sigma, flux)
    return plane


def test_starless_field_reports_zero_stars_and_no_size() -> None:
    """A flat sky with only read noise must never report a fabricated size —
    ``star_count=0`` and every ``*_px``/``*_arcsec`` field ``None``
    (CLAUDE.md invariant 7)."""
    plane = _synthetic_field(stars=[])

    quality = measure_frame_quality(plane, pixel_size_um=2.9, focal_length_mm=250.0)

    assert quality.star_count == 0
    assert quality.hfr_px is None
    assert quality.fwhm_px is None
    assert quality.hfr_arcsec is None
    assert quality.fwhm_arcsec is None
    assert quality.background == pytest.approx(100.0, abs=1.0)


def test_perfectly_flat_plane_reports_zero_stars_without_dividing_by_zero() -> None:
    """A plane with literally zero spread (noise sigma computes to exactly
    0) must not raise or invent a detection threshold from nothing."""
    plane = np.full((32, 32), 500.0, dtype=np.float64)

    quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=None)

    assert quality.star_count == 0
    assert quality.hfr_px is None
    assert quality.noise == 0.0


def test_sharp_star_measures_a_smaller_hfr_than_a_bloated_one() -> None:
    """The core claim of the whole metric: a tighter PSF must score a
    smaller HFR/FWHM than a wider one, all else equal — this is what the
    bottom-right overlay is for (telling the owner focus got worse)."""
    # Same total flux for both — a real defocus spreads the same star's
    # photons thinner, it does not also make the star brighter.
    flux = 56549.0
    sharp = _synthetic_field(stars=[(64.0, 64.0, 1.5, flux)], seed=7)
    bloated = _synthetic_field(stars=[(64.0, 64.0, 4.5, flux)], seed=7)

    sharp_quality = measure_frame_quality(sharp, pixel_size_um=None, focal_length_mm=None)
    bloated_quality = measure_frame_quality(bloated, pixel_size_um=None, focal_length_mm=None)

    assert sharp_quality.star_count == 1
    assert bloated_quality.star_count == 1
    assert sharp_quality.hfr_px is not None
    assert bloated_quality.hfr_px is not None
    assert sharp_quality.hfr_px < bloated_quality.hfr_px
    assert sharp_quality.fwhm_px is not None
    assert bloated_quality.fwhm_px is not None
    assert sharp_quality.fwhm_px < bloated_quality.fwhm_px

    # A Gaussian's FWHM = sigma * 2*sqrt(2*ln2) is a known, checkable answer —
    # not just "smaller than the other one" but numerically close to truth.
    expected_fwhm = 1.5 * 2.0 * np.sqrt(2.0 * np.log(2.0))
    assert sharp_quality.fwhm_px == pytest.approx(expected_fwhm, rel=0.25)


def test_multiple_stars_reports_the_median_across_them() -> None:
    # Fluxes chosen so every star's *peak* clears the detection threshold by
    # a healthy margin (~3000 counts) despite the wide outlier's flux being
    # spread over a much larger area (flux = peak * 2*pi*sigma^2).
    stars = [
        (20.0, 20.0, 1.5, 42412.0),
        (20.0, 100.0, 1.5, 42412.0),
        (100.0, 20.0, 1.5, 42412.0),
        (100.0, 100.0, 3.0, 169646.0),  # one deliberately wider outlier
        (64.0, 64.0, 1.5, 42412.0),
    ]
    plane = _synthetic_field(height=128, width=128, stars=stars, seed=3)

    quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=None)

    assert quality.star_count == 5
    # The median of four ~1.5-sigma stars and one ~3.0-sigma outlier should
    # sit close to the tight stars' size, not be dragged toward the outlier.
    assert quality.hfr_px is not None
    assert quality.hfr_px < 3.0


def test_close_double_star_is_deduplicated_to_one_peak() -> None:
    """Two peaks within the merge radius must collapse to one accepted
    star (greedy brightest-first dedup), not be double-counted."""
    plane = _synthetic_field(
        stars=[(64.0, 64.0, 1.5, 56549.0), (64.0, 68.0, 1.5, 42412.0)], seed=9
    )

    quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=None)

    assert quality.star_count == 1


def test_star_too_close_to_edge_is_skipped_not_measured_partially() -> None:
    """A peak whose cutout would run off the array is dropped rather than
    measured from a truncated window that would bias its size downward."""
    plane = _synthetic_field(height=64, width=64, stars=[(2.0, 2.0, 1.5, 56549.0)], seed=5)

    quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=None)

    assert quality.star_count == 0
    assert quality.hfr_px is None


def test_arcsec_conversion_uses_pixel_scale_when_known() -> None:
    plane = _synthetic_field(stars=[(64.0, 64.0, 1.5, 56549.0)], seed=11)

    quality = measure_frame_quality(plane, pixel_size_um=2.9, focal_length_mm=250.0)

    assert quality.hfr_px is not None
    assert quality.hfr_arcsec is not None
    expected_scale = 206.265 * 2.9 / 250.0
    assert quality.hfr_arcsec == pytest.approx(quality.hfr_px * expected_scale)


def test_arcsec_is_none_when_pixel_scale_is_unknown() -> None:
    plane = _synthetic_field(stars=[(64.0, 64.0, 1.5, 56549.0)], seed=11)

    quality = measure_frame_quality(plane, pixel_size_um=None, focal_length_mm=250.0)

    assert quality.hfr_px is not None
    assert quality.hfr_arcsec is None
    assert quality.fwhm_arcsec is None
