# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Star-size (HFR/FWHM) quality metric for a captured frame (``ssc-bkv.8``).

Numpy-only: no scipy, no photutils, no sep. None of the three is a current
dependency, and this module does not add one — the bead that spawned it says
to flag the user before reaching for one, and a plain local-maxima detector
plus per-star radial-profile photometry covers what a live "how good is this
frame" indicator needs without the extra weight.

Like the rest of ``ssc.capture`` (see the package docstring), this module
never touches a device, a file, or the network — it is a pure function over
whatever pixel plane it is handed, so it can be verified against a synthetic
star field with no FITS, no camera, no event loop. The FITS/Bayer/header
plumbing that turns a stored frame into that plane lives in
``ssc.api.capture_orchestrator`` (``CaptureService.get_frame_quality``),
which reuses this package's own already-tested :func:`ssc.capture.fits.debayer`
rather than teaching this module anything about mosaics.

**The algorithm, in order:**

1. Robust background/noise via median + MAD*1.4826 (a plain stddev is pulled
   upward by the stars themselves; the median-of-absolute-deviations is not).
2. Peak detection: every pixel that is a local maximum over its own 8
   neighbours *and* above ``background + _DETECT_SIGMA * noise``, found by
   shifted-array comparisons — no ``scipy.ndimage.maximum_filter``/``label``.
3. Greedy deduplication, brightest peak first, dropping any candidate within
   ``_MERGE_RADIUS`` of one already accepted — the cheap substitute for a
   real segmentation/watershed step, adequate for "is this frame in focus",
   not for crowded-field photometry.
4. Per-star cutout photometry around each accepted peak: subtract the robust
   background plus two noise sigma so positive sky-noise pixels are not counted
   as a star's outer wings, re-centroid on the cutout's own intensity-weighted
   centre of mass, then
   two independent size estimates from the same radial-flux profile —
   **HFR** (half-flux radius, the radius enclosing half the cutout's flux,
   found by inverse-interpolating the cumulative radial-flux curve) and
   **FWHM** (the flux-weighted second radial moment converted to the FWHM of
   the Gaussian with that same second moment: ``sigma * 2 * sqrt(2 * ln 2)``).
5. The frame's metric is the *median* HFR/FWHM across up to ``_MAX_STARS``
   brightest successfully-measured stars — a median rather than a mean so one
   cosmic-ray hit or one star that clipped the cutout edge cannot swing the
   whole frame's number.

``None`` on any :class:`FrameQuality` field means unknown, never zero
(CLAUDE.md invariant 7): a starless frame reports ``star_count=0`` and every
size field ``None``, not a fabricated ``0.0`` that would read as "perfectly
sharp".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np
from numpy.typing import NDArray

#: Half-width of the square cutout measured around each accepted peak, in
#: pixels — a 2*_CUTOUT_RADIUS+1 side. Wide enough to hold a visibly bloated
#: (badly focused) star's wings without reaching the next one in a normally
#: star-dense frame; a peak whose cutout would run off the array edge is
#: skipped rather than measured from a partial window (see _measure_star).
_CUTOUT_RADIUS: Final = 12

#: A candidate peak must exceed background + this many noise-sigma to count
#: as a star rather than noise. 5-sigma keeps the false-positive rate low on
#: a read-noise-dominated sub-second exposure without this needing per-camera
#: tuning.
_DETECT_SIGMA: Final = 5.0

#: Flux below background + 2 sigma is statistically compatible with the sky,
#: not evidence of a star's wings. Merely clipping at background counts roughly
#: half of all noise pixels in a 25x25 cutout as positive stellar flux and was
#: measured inflating a live 1.47 px HFR to 2.42 px (2026-09-11).
_PHOTOMETRY_SIGMA: Final = 2.0

#: Minimum separation (px) between two accepted peaks, greedy brightest-first
#: (see module docstring step 3). Smaller than _CUTOUT_RADIUS so two genuinely
#: separate stars whose cutouts would overlap slightly can still both be kept.
_MERGE_RADIUS: Final = 8.0

#: Hard cap on how many local-maxima candidates are even considered for
#: deduplication, taken brightest-first before the O(n^2) greedy pass below —
#: without it a very noisy frame with thousands of sub-threshold-but-still-
#: local-max pixels would make peak dedup itself the bottleneck.
_MAX_CANDIDATES: Final = 500

#: How many of the brightest successfully-measured stars contribute to the
#: frame's median HFR/FWHM. Capped, not "all of them", so a rich star field
#: costs the same as a sparse one.
_MAX_STARS: Final = 40

#: Gaussian FWHM / sigma, i.e. 2 * sqrt(2 * ln 2).
_FWHM_PER_SIGMA: Final = 2.0 * math.sqrt(2.0 * math.log(2.0))


@dataclass(frozen=True, slots=True)
class FrameQuality:
    """One frame's star-size quality metric.

    ``star_count`` is how many stars actually contributed to the medians
    below, not how many candidate peaks were seen — a peak whose cutout ran
    off the array edge, or whose local background-subtracted flux was
    non-positive, is dropped rather than counted. When ``star_count`` is 0
    every size field is ``None``: an empty or starless frame reports "unknown
    quality", never a fabricated sharp (or blurry) number.

    ``*_arcsec`` fields are ``None`` whenever the frame's pixel scale is
    unknown — either ``XPIXSZ`` or ``FOCALLEN`` was missing from the FITS
    header the caller read them from — even when the ``*_px`` fields are
    known. ``background``/``noise`` are the same robust estimates the star
    detector itself used, in the frame's native pixel units (ADU or the
    reader's decoded float scale) — useful on their own as a sky-brightness
    read-out even when no star was found.
    """

    star_count: int
    hfr_px: float | None
    fwhm_px: float | None
    hfr_arcsec: float | None
    fwhm_arcsec: float | None
    background: float
    noise: float
    detected_source_count: int = 0
    contrast_snr: float | None = None


def _robust_background(plane: NDArray[np.float64]) -> tuple[float, float]:
    """Median (background) and MAD*1.4826 (noise sigma, the estimator whose
    scaling makes it consistent with a Gaussian stddev). Robust to the stars
    themselves, which a plain mean/stddev is not — a field with a handful of
    bright stars still returns close to the sky-only background and noise."""
    median = float(np.median(plane))
    mad = float(np.median(np.abs(plane - median)))
    return median, mad * 1.4826


def _local_maxima(
    plane: NDArray[np.float64], threshold: float
) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    """Row/col indices of every pixel that is >= all 8 neighbours and above
    ``threshold``, via shifted-array comparisons (no ``scipy.ndimage``).
    Edge-padded so a star sitting at the array's own border can still be a
    local maximum rather than being excluded for lacking a full neighbourhood."""
    padded = np.pad(plane, 1, mode="edge")
    height, width = plane.shape
    is_max = np.ones((height, width), dtype=bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dy == 0 and dx == 0:
                continue
            neighbor = padded[1 + dy : 1 + dy + height, 1 + dx : 1 + dx + width]
            is_max &= plane >= neighbor
    mask = is_max & (plane > threshold)
    ys, xs = np.nonzero(mask)
    return ys, xs


def _merge_close_peaks(
    ys: NDArray[np.intp], xs: NDArray[np.intp], values: NDArray[np.float64]
) -> tuple[NDArray[np.intp], NDArray[np.intp]]:
    """Greedy brightest-first dedup: keep a candidate peak only if it is at
    least ``_MERGE_RADIUS`` from every peak already accepted, stopping once
    ``_MAX_STARS`` are accepted. Considers at most ``_MAX_CANDIDATES`` peaks,
    brightest first, so a noisy frame's candidate count never drives the cost
    of this (otherwise O(n^2)) pass."""
    order = np.argsort(values)[::-1][:_MAX_CANDIDATES]
    accepted_y: list[int] = []
    accepted_x: list[int] = []
    min_sep_sq = _MERGE_RADIUS * _MERGE_RADIUS
    for i in order:
        y, x = int(ys[i]), int(xs[i])
        if any(
            (ay - y) ** 2 + (ax - x) ** 2 < min_sep_sq
            for ay, ax in zip(accepted_y, accepted_x, strict=True)
        ):
            continue
        accepted_y.append(y)
        accepted_x.append(x)
        if len(accepted_y) >= _MAX_STARS:
            break
    return np.array(accepted_y, dtype=np.intp), np.array(accepted_x, dtype=np.intp)


def _measure_star(
    plane: NDArray[np.float64],
    peak_y: int,
    peak_x: int,
    background: float,
    noise: float,
) -> tuple[float, float] | None:
    """HFR and FWHM (both in px) for the star centred near ``(peak_y,
    peak_x)``, or ``None`` if it cannot be measured — the cutout ran off the
    array edge, or the background-subtracted cutout carries no positive flux
    at all (a hot pixel that only just cleared the detection threshold)."""
    y0, y1 = peak_y - _CUTOUT_RADIUS, peak_y + _CUTOUT_RADIUS + 1
    x0, x1 = peak_x - _CUTOUT_RADIUS, peak_x + _CUTOUT_RADIUS + 1
    if y0 < 0 or x0 < 0 or y1 > plane.shape[0] or x1 > plane.shape[1]:
        return None

    cutout = np.maximum(
        plane[y0:y1, x0:x1] - (background + _PHOTOMETRY_SIGMA * noise), 0.0
    )
    total_flux = float(cutout.sum())
    if total_flux <= 0.0:
        return None

    yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float64)
    # Re-centroid on the cutout's own intensity-weighted centre of mass
    # rather than trusting the integer peak pixel as the star's true centre.
    centroid_y = float((cutout * yy).sum() / total_flux)
    centroid_x = float((cutout * xx).sum() / total_flux)
    radius = np.sqrt((yy - centroid_y) ** 2 + (xx - centroid_x) ** 2)

    # FWHM: flux-weighted second radial moment -> Gaussian-equivalent FWHM.
    # For an isotropic 2-D Gaussian, E[r^2] = 2*sigma^2 (the sum of the two
    # per-axis variances), so the raw radial moment is halved before it is
    # treated as the sigma^2 of the (1-D) Gaussian that FWHM is defined on —
    # skipping that halves-to-square-root step would overstate FWHM by sqrt(2).
    radial_moment = float((cutout * radius**2).sum() / total_flux)
    sigma_sq = radial_moment / 2.0
    fwhm = math.sqrt(max(sigma_sq, 0.0)) * _FWHM_PER_SIGMA

    # HFR: inverse-interpolate the cumulative radial-flux profile for the
    # radius enclosing half the cutout's total flux.
    order = np.argsort(radius, axis=None)
    r_sorted = radius.ravel()[order]
    cum_flux = np.cumsum(cutout.ravel()[order])
    half_flux = total_flux / 2.0
    idx = int(np.searchsorted(cum_flux, half_flux))
    if idx <= 0:
        hfr = float(r_sorted[0])
    elif idx >= len(cum_flux):
        hfr = float(r_sorted[-1])
    else:
        c0, c1 = float(cum_flux[idx - 1]), float(cum_flux[idx])
        r0, r1 = float(r_sorted[idx - 1]), float(r_sorted[idx])
        hfr = r0 if c1 == c0 else r0 + (half_flux - c0) / (c1 - c0) * (r1 - r0)

    return hfr, fwhm


def _pixel_scale_arcsec(pixel_size_um: float | None, focal_length_mm: float | None) -> float | None:
    """Plate scale in arcsec/px, or ``None`` if either input is unknown or
    the focal length is non-positive. ``206.265 * pixel_size_um /
    focal_length_mm``: the standard arcsec-per-mm-of-focal-length constant
    (``206265`` arcsec/radian) applied to a pixel size already in micrometres
    (hence the ``/ 1000`` folded into the ``206.265`` rather than ``206265``)."""
    if pixel_size_um is None or focal_length_mm is None or focal_length_mm <= 0:
        return None
    return 206.265 * pixel_size_um / focal_length_mm


def measure_frame_quality(
    plane: NDArray[np.float64],
    *,
    pixel_size_um: float | None,
    focal_length_mm: float | None,
) -> FrameQuality:
    """Detect stars in ``plane`` (a single 2-D luminance plane — mono, or
    already debayered/channel-reduced by the caller) and report the frame's
    HFR/FWHM quality metric. Never raises on a starless or flat field: it
    reports ``star_count=0`` and ``None`` size fields instead (invariant 7)."""
    background, noise = _robust_background(plane)
    if noise <= 0.0:
        # A perfectly flat plane (synthetic all-zero frame, or a saturated
        # frame with zero spread) has no threshold that means anything —
        # reporting "no stars found" is honest; inventing a threshold from
        # nothing is not.
        return FrameQuality(
            star_count=0, hfr_px=None, fwhm_px=None, hfr_arcsec=None, fwhm_arcsec=None,
            background=background, noise=noise,
        )

    threshold = background + _DETECT_SIGMA * noise
    ys, xs = _local_maxima(plane, threshold)
    detected_source_count = len(ys)
    if len(ys) == 0:
        return FrameQuality(
            star_count=0, hfr_px=None, fwhm_px=None, hfr_arcsec=None, fwhm_arcsec=None,
            background=background, noise=noise,
            detected_source_count=0, contrast_snr=0.0,
        )

    values = plane[ys, xs]
    peak_ys, peak_xs = _merge_close_peaks(ys, xs, values)

    hfr_values: list[float] = []
    fwhm_values: list[float] = []
    for peak_y, peak_x in zip(peak_ys, peak_xs, strict=True):
        measured = _measure_star(plane, int(peak_y), int(peak_x), background, noise)
        if measured is None:
            continue
        hfr, fwhm = measured
        hfr_values.append(hfr)
        fwhm_values.append(fwhm)

    if not hfr_values:
        return FrameQuality(
            star_count=0, hfr_px=None, fwhm_px=None, hfr_arcsec=None, fwhm_arcsec=None,
            background=background, noise=noise,
        )

    hfr_px = float(np.median(hfr_values))
    fwhm_px = float(np.median(fwhm_values))
    scale = _pixel_scale_arcsec(pixel_size_um, focal_length_mm)
    hfr_arcsec = hfr_px * scale if scale is not None else None
    fwhm_arcsec = fwhm_px * scale if scale is not None else None

    signal = float(np.percentile(plane, 99.9)) - background
    contrast_snr = max(0.0, signal / noise) if np.isfinite(signal) else None

    return FrameQuality(
        star_count=len(hfr_values),
        hfr_px=hfr_px,
        fwhm_px=fwhm_px,
        hfr_arcsec=hfr_arcsec,
        fwhm_arcsec=fwhm_arcsec,
        background=background,
        noise=noise,
        detected_source_count=detected_source_count,
        contrast_snr=contrast_snr,
    )


__all__ = ["FrameQuality", "measure_frame_quality"]
