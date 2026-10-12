# SPDX-License-Identifier: GPL-3.0-only
"""Conservative intra-exposure motion checks; saved lights are never removed."""

import numpy as np

from ssc.capture.quality import _local_maxima


def frame_motion_reason(plane: np.ndarray) -> str | None:
    """Find repeated star fields and coherent elongated stars, not image offsets.

    Work at half resolution and remove broad background structure. A ghost
    must have a strong noncentral autocorrelation peak supported by multiple
    stars. One double star or a translated, otherwise sharp frame is valid.
    """
    if plane.ndim != 2 or min(plane.shape) < 64:
        return None
    image = np.asarray(plane[::2, ::2], dtype=np.float64)
    background = sum(np.roll(image, (dy, dx), (0, 1))
                     for dy, dx in ((-4, 0), (4, 0), (0, -4), (0, 4))) / 4
    high = np.maximum(image - background, 0)
    high = np.maximum(high - np.percentile(high, 98), 0)
    ys, xs = _local_maxima(high, 0)
    order = np.argsort(high[ys, xs])[::-1]
    peaks: list[tuple[int, int]] = []
    for index in order:
        y, x = int(ys[index]), int(xs[index])
        if any((y - py) ** 2 + (x - px) ** 2 < 16 for py, px in peaks):
            continue
        peaks.append((y, x))
        if len(peaks) >= 150:
            break
    if len(peaks) < 12:
        return None
    spectrum = np.fft.rfft2(high)
    correlation = np.fft.irfft2(spectrum * spectrum.conj(), s=high.shape).real
    power = float(correlation[0, 0])
    if power <= 0 or not np.isfinite(power):
        return None
    yy, xx = np.indices(high.shape)
    dy = np.minimum(yy, high.shape[0] - yy)
    dx = np.minimum(xx, high.shape[1] - xx)
    distance_sq = dy * dy + dx * dx
    correlation[(distance_sq <= 12 ** 2) | (distance_sq >= 160 ** 2)] = 0
    peak_y, peak_x = np.unravel_index(int(correlation.argmax()), correlation.shape)
    y, x = int(peak_y), int(peak_x)
    strength = float(correlation[y, x]) / power
    shift_y = int(y) if y <= high.shape[0] // 2 else int(y) - high.shape[0]
    shift_x = int(x) if x <= high.shape[1] // 2 else int(x) - high.shape[1]
    positions = np.asarray(peaks)
    shifted = positions + np.asarray((shift_y, shift_x))
    distances = ((shifted[:, None, :] - positions[None, :, :]) ** 2).sum(axis=2)
    matched = int(np.count_nonzero(np.min(distances, axis=1) <= 2 ** 2))
    if strength >= 0.12 and matched >= 8 and matched / len(peaks) >= 0.15:
        return (f"motion quality gate: repeated star field separated by "
                f"{2 * np.hypot(shift_y, shift_x):.1f}px "
                f"({matched} matched stars; correlation {strength:.2f})")

    elongated = 0
    measured = 0
    orientations: list[complex] = []
    for y, x in peaks[:40]:
        if y < 8 or x < 8 or y + 9 > high.shape[0] or x + 9 > high.shape[1]:
            continue
        patch = high[y-8:y+9, x-8:x+9]
        flux = float(patch.sum())
        if flux <= 0:
            continue
        py, px = np.mgrid[-8:9, -8:9]
        py = py - (patch * py).sum() / flux
        px = px - (patch * px).sum() / flux
        covariance = np.array([[(patch * px * px).sum(), (patch * px * py).sum()],
                               [(patch * px * py).sum(), (patch * py * py).sum()]]) / flux
        values, vectors = np.linalg.eigh(covariance)
        if values[0] < 0.2:
            continue
        measured += 1
        if values[1] / values[0] >= 4:
            elongated += 1
            vx, vy = vectors[:, 1]
            orientations.append(complex(vx, vy) ** 2)
    if measured >= 12 and elongated / measured >= 0.6 and abs(sum(orientations)) / elongated >= 0.8:
        return f"motion quality gate: {elongated}/{measured} stars are coherently elongated"
    return None
