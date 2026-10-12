# SPDX-License-Identifier: GPL-3.0-only
"""File-backed primary-HDU rows for calibration integration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from ssc.skymap.fits import FitsError, parse_header

_DTYPES = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}


@dataclass(frozen=True, slots=True)
class FitsRowSource:
    path: Path
    header: dict[str, object]
    shape: tuple[int, int]
    data_start: int
    dtype: np.dtype

    @classmethod
    def open(cls, path: Path) -> FitsRowSource:
        blocks = bytearray()
        with path.open("rb") as stream:
            while True:
                block = stream.read(2880)
                blocks.extend(block)
                if any(block[i : i + 8] == b"END     " for i in range(0, len(block), 80)):
                    break
                if len(block) < 2880:
                    raise FitsError("no END card before the end of the buffer")
        header, offset = parse_header(bytes(blocks))
        bitpix = header.get("BITPIX")
        if not isinstance(bitpix, int) or bitpix not in _DTYPES:
            raise FitsError(f"unsupported BITPIX {bitpix!r}")
        if header.get("NAXIS") != 2:
            raise FitsError("calibration source must be a 2-D image")
        shape = (int(header["NAXIS2"]), int(header["NAXIS1"]))  # type: ignore[call-overload]
        if any(size <= 0 for size in shape):
            raise FitsError("calibration source must be a non-empty image")
        dtype = np.dtype(_DTYPES[bitpix])
        if path.stat().st_size < offset + shape[0] * shape[1] * dtype.itemsize:
            raise FitsError("truncated FITS data")
        return cls(path, header, shape, offset, dtype)

    def read_rows(self, start: int, stop: int) -> np.ndarray:
        if not 0 <= start < stop <= self.shape[0]:
            raise ValueError("invalid FITS row span")
        count = (stop - start) * self.shape[1]
        with self.path.open("rb") as stream:
            stream.seek(self.data_start + start * self.shape[1] * self.dtype.itemsize)
            raw = stream.read(count * self.dtype.itemsize)
        if len(raw) != count * self.dtype.itemsize:
            raise FitsError("truncated FITS data")
        # Match read_fits_image's float64 scaling before master's float32 cast.
        values = np.frombuffer(raw, dtype=self.dtype).astype(np.float64)
        bzero = float(self.header.get("BZERO", 0.0))  # type: ignore[arg-type]
        bscale = float(self.header.get("BSCALE", 1.0))  # type: ignore[arg-type]
        if bzero or bscale != 1.0:
            values = values * bscale + bzero
        return values.reshape(stop - start, self.shape[1]).astype(np.float32)
