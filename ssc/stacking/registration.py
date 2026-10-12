# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic rigid (rotation + translation) registration of star fields."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

MIN_UNAMBIGUOUS_PEAK_RATIO = 1.2
MIN_RIGID_MATCHED_STARS = 8
MIN_RIGID_MATCH_FRACTION = 0.35
MAX_RIGID_RMS_PX = 2.0
_MAX_REGISTRATION_STARS = 60
_REGISTRATION_DETECT_SIGMA = 7.0
_REGISTRATION_MAX_DIMENSION = 960


@dataclass(frozen=True, slots=True)
class FrameObservation:
    """One frame and the pointing recorded with its exposure."""

    pixels: NDArray[np.floating]
    ra_deg: float | None
    dec_deg: float | None


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    """The corrective rigid transform that aligns candidate coordinates."""

    shift_y_px: float
    shift_x_px: float
    reference_ra_deg: float | None
    reference_dec_deg: float | None
    candidate_ra_deg: float | None
    candidate_dec_deg: float | None
    correlation_peak: float
    correlation_sidelobe: float
    peak_ratio: float
    rotation_deg: float = 0.0
    matched_star_count: int | None = None
    star_match_fraction: float | None = None
    star_rms_px: float | None = None


class RegistrationError(ValueError):
    """Frames cannot be registered without comparable 2-D pixel data."""


def _wrapped(value: int, size: int) -> int:
    return value if value <= size // 2 else value - size


def _peak_metrics(
    correlation: NDArray[np.floating], *, suppression_radius: int
) -> tuple[tuple[int, int], float, float]:
    raw_peak = np.unravel_index(int(np.argmax(correlation)), correlation.shape)
    peak = (int(raw_peak[0]), int(raw_peak[1]))
    suppressed = correlation.copy()
    for offset_y in range(-suppression_radius, suppression_radius + 1):
        for offset_x in range(-suppression_radius, suppression_radius + 1):
            suppressed[
                (peak[0] + offset_y) % correlation.shape[0],
                (peak[1] + offset_x) % correlation.shape[1],
            ] = 0.0
    return peak, float(correlation[peak]), float(np.max(suppressed))


def _feature_signal(pixels: NDArray[np.float64]) -> NDArray[np.float32]:
    """Keep stars and other high-contrast structure while discarding sky noise."""
    median = float(np.median(pixels))
    noise = float(np.median(np.abs(pixels - median))) * 1.4826
    signal = pixels.astype(np.float32, copy=True)
    signal -= np.float32(median + 2.0 * noise)
    np.maximum(signal, np.float32(0.0), out=signal)
    return signal


def _feature_correlation(
    reference: NDArray[np.float64], candidate: NDArray[np.float64]
) -> tuple[tuple[int, int], float, float] | None:
    """Cross-correlate robust high-signal structure as a noisy-frame fallback."""
    ref_signal = _feature_signal(reference)
    cur_signal = _feature_signal(candidate)
    ref_power = float(np.sum(ref_signal * ref_signal, dtype=np.float64))
    cur_power = float(np.sum(cur_signal * cur_signal, dtype=np.float64))
    if ref_power <= 0.0 or cur_power <= 0.0:
        return None
    correlation = np.fft.irfft2(
        np.fft.rfft2(ref_signal) * np.conj(np.fft.rfft2(cur_signal)),
        s=ref_signal.shape,
    )
    correlation /= np.sqrt(ref_power * cur_power)
    return _peak_metrics(correlation, suppression_radius=3)


def _star_centroids(pixels: NDArray[np.float64]) -> NDArray[np.float64]:
    """Return bright, separated stellar centroids in source-pixel coordinates."""
    height, width = pixels.shape
    factor = max(1, int(np.ceil(max(height, width) / _REGISTRATION_MAX_DIMENSION)))
    used_height = height // factor * factor
    used_width = width // factor * factor
    reduced = pixels[:used_height, :used_width]
    if factor > 1:
        reduced = reduced.reshape(
            used_height // factor, factor, used_width // factor, factor
        ).mean(axis=(1, 3))
    background = float(np.median(reduced))
    noise = 1.4826 * float(np.median(np.abs(reduced - background)))
    if not np.isfinite(noise) or noise <= 0.0:
        return np.empty((0, 2), dtype=np.float64)
    padded = np.pad(reduced, 1, mode="edge")
    is_peak = reduced > background + _REGISTRATION_DETECT_SIGMA * noise
    small_height, small_width = reduced.shape
    for offset_y in (-1, 0, 1):
        for offset_x in (-1, 0, 1):
            if offset_y == 0 and offset_x == 0:
                continue
            is_peak &= reduced >= padded[
                1 + offset_y : 1 + offset_y + small_height,
                1 + offset_x : 1 + offset_x + small_width,
            ]
    ys, xs = np.nonzero(is_peak)
    if ys.size == 0:
        return np.empty((0, 2), dtype=np.float64)
    order = np.argsort(reduced[ys, xs])[::-1][:1000]
    accepted: list[tuple[float, float]] = []
    integer_peaks: list[tuple[int, int]] = []
    for index in order:
        y, x = int(ys[index]), int(xs[index])
        if y < 3 or x < 3 or y >= small_height - 3 or x >= small_width - 3:
            continue
        if any((y - old_y) ** 2 + (x - old_x) ** 2 < 25 for old_y, old_x in integer_peaks):
            continue
        cutout = np.maximum(
            reduced[y - 2 : y + 3, x - 2 : x + 3] - (background + 2.0 * noise),
            0.0,
        )
        flux = float(cutout.sum())
        if flux <= 0.0:
            continue
        grid_y, grid_x = np.mgrid[y - 2 : y + 3, x - 2 : x + 3]
        centre_offset = (factor - 1) / 2.0
        accepted.append(
            (
                float((cutout * grid_y).sum() / flux) * factor + centre_offset,
                float((cutout * grid_x).sum() / flux) * factor + centre_offset,
            )
        )
        integer_peaks.append((y, x))
        if len(accepted) >= _MAX_REGISTRATION_STARS:
            break
    return np.asarray(accepted, dtype=np.float64).reshape((-1, 2))


def _score_transform(
    reference: NDArray[np.float64],
    candidate: NDArray[np.float64],
    matrix: NDArray[np.float64],
    translation: NDArray[np.float64],
) -> tuple[int, float, NDArray[np.intp], NDArray[np.intp]]:
    transformed = candidate @ matrix + translation
    distances = np.sqrt(
        np.sum((transformed[:, None, :] - reference[None, :, :]) ** 2, axis=2)
    )
    nearest = np.argmin(distances, axis=1)
    nearest_distance = distances[np.arange(len(candidate)), nearest]
    # Greedily keep a one-to-one correspondence, shortest residual first.
    candidate_indices: list[int] = []
    reference_indices: list[int] = []
    used_reference: set[int] = set()
    for candidate_index in np.argsort(nearest_distance):
        reference_index = int(nearest[candidate_index])
        if nearest_distance[candidate_index] > 3.0 or reference_index in used_reference:
            continue
        candidate_indices.append(int(candidate_index))
        reference_indices.append(reference_index)
        used_reference.add(reference_index)
    if not candidate_indices:
        return 0, float("inf"), np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp)
    residuals = nearest_distance[np.asarray(candidate_indices, dtype=np.intp)]
    return (
        len(candidate_indices),
        float(np.sqrt(np.mean(residuals**2))),
        np.asarray(reference_indices, dtype=np.intp),
        np.asarray(candidate_indices, dtype=np.intp),
    )


def _rigid_star_registration(
    reference: NDArray[np.float64], candidate: NDArray[np.float64]
) -> tuple[float, float, float, int, float, float] | None:
    """Match invariant star-pair geometry and return a refined rigid transform."""
    reference_stars = _star_centroids(reference)
    candidate_stars = _star_centroids(candidate)
    minimum_available = min(len(reference_stars), len(candidate_stars))
    if minimum_available < MIN_RIGID_MATCHED_STARS:
        return None

    candidate_pairs: list[tuple[float, int, int]] = []
    for first in range(len(candidate_stars)):
        offsets = candidate_stars[first + 1 :] - candidate_stars[first]
        for relative, distance in enumerate(np.linalg.norm(offsets, axis=1)):
            candidate_pairs.append((float(distance), first, first + 1 + relative))
    candidate_pairs.sort(key=lambda item: item[0])
    candidate_distances = np.asarray([item[0] for item in candidate_pairs])
    minimum_baseline = 0.2 * min(reference.shape)
    best: tuple[int, float, NDArray[np.float64], NDArray[np.float64]] | None = None
    for first in range(len(reference_stars)):
        for second in range(first + 1, len(reference_stars)):
            reference_vector = reference_stars[second] - reference_stars[first]
            distance = float(np.linalg.norm(reference_vector))
            if distance < minimum_baseline:
                continue
            tolerance = max(2.5, distance * 0.003)
            start = int(np.searchsorted(candidate_distances, distance - tolerance))
            stop = int(np.searchsorted(candidate_distances, distance + tolerance, side="right"))
            for _candidate_distance, candidate_first, candidate_second in candidate_pairs[start:stop]:
                for swap in (False, True):
                    source_first, source_second = (
                        (candidate_second, candidate_first)
                        if swap
                        else (candidate_first, candidate_second)
                    )
                    candidate_vector = (
                        candidate_stars[source_second] - candidate_stars[source_first]
                    )
                    angle = np.arctan2(reference_vector[0], reference_vector[1]) - np.arctan2(
                        candidate_vector[0], candidate_vector[1]
                    )
                    cosine, sine = float(np.cos(angle)), float(np.sin(angle))
                    matrix = np.asarray(((cosine, -sine), (sine, cosine)))
                    translation = (
                        reference_stars[first] - candidate_stars[source_first] @ matrix
                    )
                    score, rms, reference_indices, candidate_indices = _score_transform(
                        reference_stars, candidate_stars, matrix, translation
                    )
                    if best is not None and (score, -rms) <= (best[0], -best[1]):
                        continue
                    if score >= MIN_RIGID_MATCHED_STARS:
                        # Orthogonal Procrustes refinement over all unique matches.
                        source = candidate_stars[candidate_indices]
                        target = reference_stars[reference_indices]
                        source_centre = source.mean(axis=0)
                        target_centre = target.mean(axis=0)
                        left, _singular, right_t = np.linalg.svd(
                            (source - source_centre).T @ (target - target_centre)
                        )
                        refined_matrix = left @ right_t
                        if np.linalg.det(refined_matrix) < 0:
                            left[:, -1] *= -1
                            refined_matrix = left @ right_t
                        refined_translation = target_centre - source_centre @ refined_matrix
                        score, rms, _reference_indices, _candidate_indices = _score_transform(
                            reference_stars,
                            candidate_stars,
                            refined_matrix,
                            refined_translation,
                        )
                        matrix, translation = refined_matrix, refined_translation
                    best = (score, rms, matrix, translation)
                    if score >= max(20, int(np.ceil(minimum_available * 0.7))):
                        break
                if best is not None and best[0] >= max(
                    20, int(np.ceil(minimum_available * 0.7))
                ):
                    break
            if best is not None and best[0] >= max(20, int(np.ceil(minimum_available * 0.7))):
                break
        if best is not None and best[0] >= max(20, int(np.ceil(minimum_available * 0.7))):
            break
    if best is None:
        raise RegistrationError("star geometry could not be matched")
    score, rms, matrix, translation = best
    fraction = score / minimum_available
    rotation_deg = float(np.degrees(np.arctan2(matrix[1, 0], matrix[0, 0])))
    return (
        float(translation[0]),
        float(translation[1]),
        rotation_deg,
        score,
        fraction,
        rms,
    )


def register_translation(
    reference: FrameObservation, candidate: FrameObservation
) -> RegistrationResult:
    """Estimate a star-pattern translation using phase correlation.

    The FFT correlation is translation-only and preserves the supplied RA/DEC
    values verbatim for the stack manifest. It does not fabricate sky
    coordinates when either exposure lacks them.
    """

    if reference.pixels.ndim != 2 or candidate.pixels.ndim != 2:
        raise RegistrationError("registration needs one 2-D image plane per frame")
    if reference.pixels.shape != candidate.pixels.shape:
        raise RegistrationError("registration frames must have identical dimensions")
    if not np.isfinite(reference.pixels).all() or not np.isfinite(candidate.pixels).all():
        raise RegistrationError("registration frames contain non-finite pixels")

    ref = reference.pixels.astype(np.float64, copy=True)
    cur = candidate.pixels.astype(np.float64, copy=True)
    rigid = _rigid_star_registration(ref, cur)
    if rigid is not None:
        shift_y, shift_x, rotation_deg, matched, match_fraction, rms = rigid
        return RegistrationResult(
            shift_y_px=shift_y,
            shift_x_px=shift_x,
            reference_ra_deg=reference.ra_deg,
            reference_dec_deg=reference.dec_deg,
            candidate_ra_deg=candidate.ra_deg,
            candidate_dec_deg=candidate.dec_deg,
            correlation_peak=match_fraction,
            correlation_sidelobe=rms,
            peak_ratio=match_fraction / max(1.0 - match_fraction, 1e-12),
            rotation_deg=rotation_deg,
            matched_star_count=matched,
            star_match_fraction=match_fraction,
            star_rms_px=rms,
        )
    ref -= ref.mean()
    cur -= cur.mean()
    spectrum = np.fft.fft2(ref) * np.conj(np.fft.fft2(cur))
    magnitude = np.abs(spectrum)
    if not np.any(magnitude > 0):
        raise RegistrationError("registration frames have no signal")
    correlation = np.abs(np.fft.ifft2(spectrum / np.maximum(magnitude, 1e-12)))
    peak, peak_value, sidelobe = _peak_metrics(correlation, suppression_radius=1)
    peak_ratio = peak_value / max(sidelobe, 1e-12)
    # Phase correlation gives every spatial frequency equal weight. That is
    # excellent for clean synthetic translations, but on real low-SNR star
    # fields it can amplify sensor/sky noise until several unrelated bins tie.
    # Retry those cases using only robust high-contrast structure. A periodic
    # texture still remains ambiguous, while ordinary good subs recover their
    # star-pattern displacement instead of starting a rejection cascade.
    if peak_ratio < MIN_UNAMBIGUOUS_PEAK_RATIO:
        feature = _feature_correlation(ref, cur)
        if feature is not None:
            feature_peak, feature_value, feature_sidelobe = feature
            feature_ratio = feature_value / max(feature_sidelobe, 1e-12)
            if feature_ratio > peak_ratio:
                peak = feature_peak
                peak_value = feature_value
                sidelobe = feature_sidelobe
                peak_ratio = feature_ratio
    # The phase-correlation peak is already the translation to apply to the
    # candidate.  Negating it would describe how the candidate moved from the
    # reference and applying that value would double a dither instead of
    # cancelling it.
    shift_y = float(_wrapped(int(peak[0]), correlation.shape[0]))
    shift_x = float(_wrapped(int(peak[1]), correlation.shape[1]))
    return RegistrationResult(
        shift_y_px=shift_y,
        shift_x_px=shift_x,
        reference_ra_deg=reference.ra_deg,
        reference_dec_deg=reference.dec_deg,
        candidate_ra_deg=candidate.ra_deg,
        candidate_dec_deg=candidate.dec_deg,
        correlation_peak=peak_value,
        correlation_sidelobe=sidelobe,
        peak_ratio=peak_ratio,
    )


__all__ = [
    "MAX_RIGID_RMS_PX",
    "MIN_RIGID_MATCHED_STARS",
    "MIN_RIGID_MATCH_FRACTION",
    "MIN_UNAMBIGUOUS_PEAK_RATIO",
    "FrameObservation",
    "RegistrationError",
    "RegistrationResult",
    "register_translation",
]
