# SPDX-License-Identifier: GPL-3.0-only

"""Deterministic sigma-clipped integration of registered image planes."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class StackResult:
    """Integrated pixels and the parameters needed to reproduce them."""

    pixels: NDArray[np.float64]
    rejected_pixels: int
    frame_count: int
    sigma: float
    iterations: int
    pooled_sigma: float
    rejected_low: int
    rejected_high: int
    rejected_nonfinite: int
    accepted_count_min: int
    accepted_count_median: float
    accepted_count_max: int


class StackIntegrationError(ValueError):
    """The supplied frame set cannot be integrated safely."""


class IncrementalTrimmedMean:
    """Bounded-memory integration with one high/low *group* rejected.

    ``group_size=1`` is the original per-frame trimmed mean. Live stacking
    may use explicit dither boundaries (``group_size=None``), so a detector
    defect repeated until the next dither contributes one group sample. Once
    three groups exist, the lowest and highest group means are excluded per
    pixel. Memory remains fixed regardless of the run length.
    """

    def __init__(self, shape: tuple[int, ...], *, group_size: int | None = 1) -> None:
        if group_size is not None and group_size < 1:
            raise StackIntegrationError("incremental group size must be at least one")
        self._group_size = group_size
        self._sum = np.zeros(shape, dtype=np.float32)
        self._grouped_count = np.zeros(shape, dtype=np.uint32)
        self._count = np.zeros(shape, dtype=np.uint32)
        self._minimum = np.full(shape, np.inf, dtype=np.float32)
        self._maximum = np.full(shape, -np.inf, dtype=np.float32)
        self._pending_sum = np.zeros(shape, dtype=np.float32)
        self._pending_count = np.zeros(shape, dtype=np.uint32)
        self._pending_frames = 0
        self._pixels = np.zeros(shape, dtype=np.float32)
        self._frame_count = 0

    @property
    def pixels(self) -> NDArray[np.float32]:
        """The last published result; it is replaced, never mutated."""
        return self._pixels

    @property
    def frame_count(self) -> int:
        return self._frame_count

    @property
    def memory_bytes(self) -> int:
        return sum(
            array.nbytes
            for array in (
                self._sum,
                self._grouped_count,
                self._count,
                self._minimum,
                self._maximum,
                self._pending_sum,
                self._pending_count,
                self._pixels,
            )
        )

    @property
    def coverage(self) -> NDArray[np.uint32]:
        """Per-pixel accepted sample count for presentation-time cropping."""
        return self._count

    def begin_group(self) -> None:
        """Finish a detector-position block before accepting the next frame.

        Empty blocks create no sample. A rejected exposure never reaches add(),
        so this boundary remains independent of accepted-frame counts.
        """
        if self._pending_frames:
            self._finish_group()
            self._pixels = self._result_with_pending_group()

    def add(
        self, pixels: NDArray[np.floating], valid: NDArray[np.bool_] | None = None
    ) -> NDArray[np.float32]:
        if pixels.shape != self._sum.shape:
            raise StackIntegrationError(
                f"incremental frame shape {pixels.shape} does not match {self._sum.shape}"
            )
        if valid is not None and valid.shape != pixels.shape:
            raise StackIntegrationError("incremental validity mask shape does not match frame")
        # Keep every scratch array bounded, including float64/noncontiguous input
        # conversion. Published images remain independent full-resolution arrays.
        found_usable = False
        for start, stop in self._chunks():
            values = np.asarray(pixels.flat[start:stop], dtype=np.float32)
            usable = np.isfinite(values)
            if valid is not None:
                usable &= valid.flat[start:stop]
            found_usable |= bool(np.any(usable))
            np.add(
                self._pending_sum.reshape(-1)[start:stop],
                values,
                out=self._pending_sum.reshape(-1)[start:stop],
                where=usable,
            )
            for counts in (self._pending_count, self._count):
                part = counts.reshape(-1)[start:stop]
                np.add(part, 1, out=part, where=usable)
        if not found_usable:
            raise StackIntegrationError("incremental frame contains no valid pixels")
        self._pending_frames += 1
        if self._pending_frames == self._group_size:
            self._finish_group()

        result = self._result_with_pending_group()
        self._pixels = result
        self._frame_count += 1
        return result

    # A fixed element cap also bounds scratch memory for native RGB frames.
    _CHUNK_ELEMENTS = 262_144

    def _chunks(self) -> Iterator[tuple[int, int]]:
        for start in range(0, self._sum.size, self._CHUNK_ELEMENTS):
            yield start, min(start + self._CHUNK_ELEMENTS, self._sum.size)

    def _finish_group(self) -> None:
        for start, stop in self._chunks():
            counts = self._pending_count.reshape(-1)[start:stop]
            usable = counts > 0
            mean = np.zeros(stop - start, dtype=np.float32)
            np.divide(self._pending_sum.reshape(-1)[start:stop], counts, out=mean, where=usable)
            total = self._sum.reshape(-1)[start:stop]
            np.add(total, mean, out=total, where=usable)
            groups = self._grouped_count.reshape(-1)[start:stop]
            np.add(groups, 1, out=groups, where=usable)
            low = self._minimum.reshape(-1)[start:stop]
            high = self._maximum.reshape(-1)[start:stop]
            np.minimum(low, mean, out=low, where=usable)
            np.maximum(high, mean, out=high, where=usable)
        self._pending_sum.fill(0)
        self._pending_count.fill(0)
        self._pending_frames = 0

    def _result_with_pending_group(self) -> NDArray[np.float32]:
        result = np.zeros_like(self._sum)
        for start, stop in self._chunks():
            counts = self._pending_count.reshape(-1)[start:stop]
            pending = counts > 0
            mean = np.zeros(stop - start, dtype=np.float32)
            np.divide(self._pending_sum.reshape(-1)[start:stop], counts, out=mean, where=pending)
            total = self._sum.reshape(-1)[start:stop] + mean
            groups = self._grouped_count.reshape(-1)[start:stop] + pending
            minimum = self._minimum.reshape(-1)[start:stop].copy()
            maximum = self._maximum.reshape(-1)[start:stop].copy()
            np.minimum(minimum, mean, out=minimum, where=pending)
            np.maximum(maximum, mean, out=maximum, where=pending)
            part = result.reshape(-1)[start:stop]
            trimmed = groups >= 3
            ordinary = (groups > 0) & ~trimmed
            np.divide(total, groups, out=part, where=ordinary)
            np.subtract(total, minimum, out=total, where=trimmed)
            np.subtract(total, maximum, out=total, where=trimmed)
            # Subtract only at trimmed pixels to avoid uint32 underflow.
            np.subtract(groups, 2, out=groups, where=trimmed)
            np.divide(total, groups, out=part, where=trimmed)
        return result


def sigma_clip_stack(
    frames: NDArray[np.floating],
    *,
    sigma: float = 3.0,
    iterations: int = 3,
    chunk_rows: int = 64,
) -> StackResult:
    """Quantization-aware sigma clipping followed by per-pixel averaging.

    The input must be ``(frames, height, width)``. Non-finite samples are
    rejected before clipping; no pixel is fabricated when every sample is
    rejected.  A robust temporal-noise estimate pooled across the sensor is a
    lower bound for every per-pixel MAD scale.  This is important for
    quantized cameras: a small pixel stack can have zero MAD even though an
    adjacent ADC code is a legitimate noise sample, not an outlier.

    Work is divided into row chunks so calibration sets at the UI's 100-frame
    bias default do not require several full-size float64 scratch cubes.
    """

    if frames.ndim != 3 or any(dimension == 0 for dimension in frames.shape):
        raise StackIntegrationError("stack needs a non-empty 3-D frame array")
    return sigma_clip_stack_rows(
        frames.shape,
        lambda start, stop: frames[:, start:stop, :],
        sigma=sigma,
        iterations=iterations,
        chunk_rows=chunk_rows,
    )


def sigma_clip_stack_rows(
    shape: tuple[int, int, int],
    read_rows: Callable[[int, int], NDArray[np.floating]],
    *,
    sigma: float = 3.0,
    iterations: int = 3,
    chunk_rows: int = 64,
) -> StackResult:
    """Integrate row sources with the same global estimator as the array API.

    The callback returns all frames for the requested image rows. Its input
    precision must match the array API; source conversion belongs to the caller.
    """
    if len(shape) != 3 or any(dimension <= 0 for dimension in shape):
        raise StackIntegrationError("stack needs a non-empty 3-D frame array")
    if not np.isfinite(sigma) or sigma <= 0 or iterations < 1:
        raise StackIntegrationError("sigma must be positive and iterations must be at least one")
    if chunk_rows < 1:
        raise StackIntegrationError("chunk_rows must be at least one")
    frame_count, height, width = shape
    # Bound the cube scratch as well as row count when many frames are present.
    chunk_rows = min(chunk_rows, max(1, 2_097_152 // (frame_count * width)))
    spatial_pixels = height * width
    stride = max(1, (spatial_pixels + 262_143) // 262_144)
    sample = np.empty((frame_count, (spatial_pixels + stride - 1) // stride), dtype=np.float64)
    for row_start in range(0, height, chunk_rows):
        row_end = min(height, row_start + chunk_rows)
        first = (row_start * width + stride - 1) // stride
        last = (row_end * width + stride - 1) // stride
        if first == last:
            continue
        values = read_rows(row_start, row_end)
        indices = np.arange(first, last) * stride - row_start * width
        sample[:, first:last] = values.reshape(frame_count, -1)[:, indices]
        del values
    differences = np.diff(sample, axis=0)
    del sample
    finite_differences = differences[np.isfinite(differences)]
    del differences
    if finite_differences.size:
        difference_centre = float(np.median(finite_differences, overwrite_input=True))
        np.subtract(finite_differences, difference_centre, out=finite_differences)
        np.abs(finite_differences, out=finite_differences)
        difference_mad = float(np.median(finite_differences, overwrite_input=True))
        pooled_sigma = 1.4826 * difference_mad / np.sqrt(2.0)
    else:
        pooled_sigma = 0.0
    del finite_differences

    result = np.empty((height, width), dtype=np.float64)
    accepted_histogram = np.zeros(frame_count + 1, dtype=np.int64)
    rejected_nonfinite = 0
    rejected_low = 0
    rejected_high = 0
    for row_start in range(0, height, chunk_rows):
        row_end = min(height, row_start + chunk_rows)
        values = read_rows(row_start, row_end).astype(np.float64, copy=False)
        valid = np.isfinite(values)
        rejected_nonfinite += int((~valid).sum())
        if np.any(valid.sum(axis=0) == 0):
            raise StackIntegrationError("a pixel has no valid samples before rejection")

        for _ in range(iterations):
            masked = np.where(valid, values, np.nan)
            centre = np.nanmedian(masked, axis=0)
            signed_deviation = values - centre
            absolute_deviation = np.abs(signed_deviation)
            local_mad = np.nanmedian(np.where(valid, absolute_deviation, np.nan), axis=0)
            scale = np.maximum(1.4826 * local_mad, pooled_sigma)
            # When an otherwise exactly constant synthetic stack contains one
            # excursion, both scale estimates are zero. Preserve the consensus
            # and reject the excursion. Real quantized sensor stacks acquire a
            # non-zero pooled floor from the rest of the detector.
            outlier = valid & (
                ((scale == 0) & (absolute_deviation > 0))
                | ((scale > 0) & (absolute_deviation > sigma * scale))
            )
            if not np.any(outlier):
                break
            rejected_low += int(np.count_nonzero(outlier & (signed_deviation < 0)))
            rejected_high += int(np.count_nonzero(outlier & (signed_deviation > 0)))
            valid[outlier] = False
        count = valid.sum(axis=0)
        if np.any(count == 0):
            raise StackIntegrationError("a pixel has no valid samples after rejection")
        result[row_start:row_end] = np.sum(np.where(valid, values, 0.0), axis=0) / count
        accepted_histogram += np.bincount(count.ravel(), minlength=frame_count + 1)

    populated = np.flatnonzero(accepted_histogram)
    total_pixels = height * width
    cumulative = np.cumsum(accepted_histogram)

    def accepted_at(rank: int) -> int:
        return int(np.searchsorted(cumulative, rank + 1))

    accepted_median = (accepted_at((total_pixels - 1) // 2) + accepted_at(total_pixels // 2)) / 2.0
    rejected = rejected_nonfinite + rejected_low + rejected_high
    return StackResult(
        result,
        rejected,
        int(frame_count),
        float(sigma),
        int(iterations),
        float(pooled_sigma),
        rejected_low,
        rejected_high,
        rejected_nonfinite,
        int(populated[0]),
        accepted_median,
        int(populated[-1]),
    )


__all__ = [
    "IncrementalTrimmedMean",
    "StackIntegrationError",
    "StackResult",
    "sigma_clip_stack",
    "sigma_clip_stack_rows",
]
