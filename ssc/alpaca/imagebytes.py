# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Encoding ``Camera.ImageArray`` as the ImageBytes binary transfer.

This is the server-side mirror of ``ssc/client/imagebytes.py``'s
``decode_image_bytes`` — not a second decoder, and it shares that module's
verified header layout (``ssc/client/imagebytes.py``'s module docstring:
checked against the ASCOM Alpaca API Reference v12 §8 and cross-checked
against ``alpyca``) rather than re-deriving it.

Two shapes this driver needs, both built here:

* **A successful frame**, re-encoded with *this driver's own* transaction
  ids. The frame was already decoded off the device's own response
  (``SeestarBackend.native_image_bytes``), whose ``ClientTransactionID`` is
  this driver's *internal* client talking to the device — not the id the
  external caller (ConformU, NINA, ``ssc-core``) sent. Serving the device's
  own header back verbatim would echo the wrong id, which is a ConformU
  failure in its own right.
* **An error**, in the same binary shape. A caller that asked for
  ``Accept: application/imagebytes`` and gets back a JSON error body has had
  its Accept header ignored — and ``ssc/client/imagebytes.py``'s own reader
  refuses exactly that mismatch (``AlpacaClient.image_array`` checks
  ``Content-Type`` before decoding). So a failure on this path must still be
  ImageBytes, with the message carried the way §8.9 defines: after the same
  44-byte header, starting at ``DataStart``.
"""

from __future__ import annotations

import struct
from typing import Final

from ssc.client.imagebytes import ImageBytesFrame

#: Matches ``ssc/client/imagebytes.py``'s ``_HEADER_SIZE`` — the two are the
#: same wire format, read from one side and written from the other.
_HEADER_SIZE: Final = 44

#: The only ``MetadataVersion`` this driver writes — the one it (and its own
#: client) is verified to decode.
_METADATA_VERSION: Final = 1

#: ``MetadataVersion, ErrorNumber, ClientTransactionID, ServerTransactionID,
#: DataStart, ImageElementType, TransmissionElementType, Rank, Dimension1,
#: Dimension2, Dimension3`` — 11 little-endian fields, 44 bytes total.
_HEADER_FORMAT: Final = "<iiIIiiiiiii"


def encode_image_bytes_frame(
    frame: ImageBytesFrame, *, client_transaction_id: int, server_transaction_id: int
) -> bytes:
    """A successful ``ImageArray`` response, as ImageBytes.

    ``frame``'s array, element types and rank are carried through exactly as
    decoded — nothing here reinterprets a dimension or an element type, only
    the two transaction ids change. ``frame.array`` must be rank 2 (the only
    rank :func:`~ssc.client.imagebytes.decode_image_bytes` ever produces).
    """
    array = frame.array
    if array.ndim != 2:
        raise ValueError(
            f"encode_image_bytes_frame: array has rank {array.ndim}, not the 2 this "
            "driver has ever decoded off the device"
        )
    dimension1, dimension2 = array.shape
    header = struct.pack(
        _HEADER_FORMAT,
        _METADATA_VERSION,
        0,
        client_transaction_id,
        server_transaction_id,
        _HEADER_SIZE,
        int(frame.image_element_type),
        int(frame.transmission_element_type),
        2,
        dimension1,
        dimension2,
        0,
    )
    return header + array.tobytes()


def encode_image_bytes_error(
    error_number: int, message: str, *, client_transaction_id: int, server_transaction_id: int
) -> bytes:
    """An ``ImageArray`` failure, in the same binary shape a success would use.

    The image-shape fields (``ImageElementType``, ``Rank``, dimensions) carry
    no meaning on an error and are written as ``0`` — a reader must stop at
    ``ErrorNumber`` (``ssc.client.imagebytes.decode_image_bytes`` does), so
    nothing downstream reads them.
    """
    header = struct.pack(
        _HEADER_FORMAT,
        _METADATA_VERSION,
        error_number,
        client_transaction_id,
        server_transaction_id,
        _HEADER_SIZE,
        0,
        0,
        0,
        0,
        0,
        0,
    )
    return header + message.encode("utf-8")


__all__ = ["encode_image_bytes_error", "encode_image_bytes_frame"]
