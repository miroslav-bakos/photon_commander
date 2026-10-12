"""Builds a minimal single-HDU FITS file byte-for-byte, the same way the
``tiny_2d.fits``/``legacy_cube.fits``/``no_wcs.fits`` fixtures in this
directory were generated — so a test can also build one on the fly (a mocked
IRSA/SDSS/Legacy Surveys response body, say) without shipping a new binary
fixture for every header variant it needs.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import NDArray

_CARD_SIZE = 80
_BLOCK_SIZE = 2880


def _card(key: str, value: object) -> bytes:
    if isinstance(value, bool):
        text = f"{key:<8}= {'T' if value else 'F'}"
    elif isinstance(value, str):
        text = f"{key:<8}= '{value}'"
    else:
        text = f"{key:<8}= {value!r}"
    return text.ljust(_CARD_SIZE)[:_CARD_SIZE].encode("ascii")


def build_fits_bytes(data: NDArray[np.floating[Any]], header: dict[str, object]) -> bytes:
    """``data`` (2-D or 3-D) plus arbitrary extra header cards -> FITS bytes
    ``ssc.skymap.fits.read_fits_image`` can parse back."""
    big = data.astype(">f4")
    naxis = big.ndim
    cards = [_card("SIMPLE", True), _card("BITPIX", -32), _card("NAXIS", naxis)]
    if naxis == 2:
        height, width = big.shape
        cards += [_card("NAXIS1", width), _card("NAXIS2", height)]
    else:
        planes, height, width = big.shape
        cards += [_card("NAXIS1", width), _card("NAXIS2", height), _card("NAXIS3", planes)]
    for key, value in header.items():
        cards.append(_card(key, value))
    cards.append(b"END".ljust(_CARD_SIZE))
    header_bytes = b"".join(cards)
    header_bytes += b" " * ((-len(header_bytes)) % _BLOCK_SIZE)
    payload = big.tobytes()
    payload += b"\x00" * ((-len(payload)) % _BLOCK_SIZE)
    return header_bytes + payload


__all__ = ["build_fits_bytes"]
