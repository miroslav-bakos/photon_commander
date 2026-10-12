# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Decoding for the Alpaca ``ImageBytes`` binary transfer.

``ImageArray`` requested with ``Accept: application/imagebytes`` is the only
transfer this client uses (``docs/M0-FINDINGS.md`` "Measured: the native
Alpaca camera": 1.6 s / 16.59 MB against 3.7 s / 41.47 MB for the JSON array
on the same frame). This module is the wire-format decoder only — no capture
loop, no FITS, no state.

Checked against the ASCOM Alpaca API Reference, version 12 (23 February 2026),
section 8 "Alpaca Camera ImageBytes Reference" — fetched from
``https://github.com/ASCOMInitiative/ASCOMRemote/raw/main/Documentation/ASCOM%20Alpaca%20API%20Reference.pdf``
— not from memory or from a paraphrase of it. The reference implementation in
``ASCOMInitiative/alpyca`` (``alpaca/camera.py``, functions
``_build_imagedata_array`` and ``_build_imagedata_nested_list_array``) was
used to cross-check the byte offsets and the element-ordering claim.

**Axis order is exactly the spec's, not numpy's or FITS's.** Section 8.8.1
defines a 2-D array as ``Array[NumX, NumY]`` serialised with ``NumY`` (height)
the fastest-changing index — i.e. row-major with *X slowest, Y fastest*. A
plain ``reshape(Dimension1, Dimension2)`` in C order reproduces that exactly,
because numpy's default order also puts the fastest-changing index last. So
:func:`decode_image_bytes` returns an array of shape ``(NumX, NumY)`` —
``array[x, y]`` — with **no transpose applied**. Alpyca's own docs note the
returned array "must be transposed for use with numpy and astropy" for a
FITS-style ``(row, col)`` = ``(y, x)`` layout; that transpose is a consumer
decision, made explicitly by whoever writes the FITS, not a default this
decoder applies. Getting this wrong is not cosmetic: the sensor is a GRBG
Bayer mosaic, and a Bayer pattern's phase is defined by which row is first —
transposing or flipping here would silently turn it into a different,
wrong pattern (GRBG -> BGGR under a row flip) with nothing in a shape check
to catch it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Final

import numpy as np
import numpy.typing as npt

from ssc.client.errors import AlpacaImageBytesError, AlpacaProtocolError, AlpacaRemoteError

#: Bytes 0..43 — see the module docstring for the field-by-field layout.
_HEADER_SIZE: Final = 44

#: The only ``MetadataVersion`` this client understands. A future version may
#: reorder or extend the header; guessing at its layout is exactly the kind
#: of silent-fallback architecture policy invariant 7 forbids, so an unknown version is
#: refused outright rather than parsed as if it were 1.
_SUPPORTED_METADATA_VERSION: Final = 1

#: The only rank this client decodes. The Seestar's cameras are Bayer/mono —
#: `sensortype` 2 (colour-by-mosaic), rank 2 — and 3-plane colour ImageBytes
#: (`Array[NumX, NumY, ColourPlane]`) has never been observed or exercised.
_SUPPORTED_RANK: Final = 2


class ImageArrayElementType(IntEnum):
    """``ImageArrayElementTypes`` from the Alpaca API Reference §8.7.2."""

    UNKNOWN = 0
    INT16 = 1
    INT32 = 2
    DOUBLE = 3
    SINGLE = 4
    UINT64 = 5
    BYTE = 6
    INT64 = 7
    UINT16 = 8
    UINT32 = 9


#: Little-endian dtypes for every element type this client can materialise.
#: ``UNKNOWN`` has no dtype by construction — it is not a data type, it is the
#: absence of one — so it is deliberately excluded rather than mapped to
#: something plausible.
_ELEMENT_DTYPES: Final[dict[int, str]] = {
    ImageArrayElementType.INT16: "<i2",
    ImageArrayElementType.INT32: "<i4",
    ImageArrayElementType.DOUBLE: "<f8",
    ImageArrayElementType.SINGLE: "<f4",
    ImageArrayElementType.UINT64: "<u8",
    ImageArrayElementType.BYTE: "u1",
    ImageArrayElementType.INT64: "<i8",
    ImageArrayElementType.UINT16: "<u2",
    ImageArrayElementType.UINT32: "<u4",
}


@dataclass(frozen=True, slots=True)
class ImageBytesFrame:
    """One decoded ``ImageArray`` ImageBytes response.

    ``array`` has shape ``(Dimension1, Dimension2)`` — ``(NumX, NumY)`` in the
    spec's own naming — see the module docstring for why no transpose is
    applied.
    """

    metadata_version: int
    client_transaction_id: int
    server_transaction_id: int
    image_element_type: ImageArrayElementType
    transmission_element_type: ImageArrayElementType
    array: npt.NDArray[np.generic]


def decode_image_bytes(
    payload: bytes, *, member: str, expected_client_transaction_id: int | None = None
) -> ImageBytesFrame:
    """Decode one ``ImageArray`` ImageBytes response.

    ``member`` names the call for every error message, the same convention
    :class:`~ssc.client.alpaca.AlpacaClient` uses for its JSON path.
    ``expected_client_transaction_id``, when given, is checked against the
    header's echo exactly as the JSON path checks it — a driver that
    misroutes a binary response is exactly as serious a bug as one that
    misroutes a JSON one.

    Raises :class:`AlpacaRemoteError` for an error the device reported inside
    the header (§8.9), :class:`AlpacaProtocolError` for a mis-echoed
    transaction id, and :class:`AlpacaImageBytesError` for anything about the
    binary format itself this client will not guess at: an unsupported
    ``MetadataVersion``, a rank other than 2, an element type with no
    representation here, a payload too short to hold what it declares, or a
    data length that disagrees with ``Dimension1 x Dimension2``.
    """
    if len(payload) < _HEADER_SIZE:
        raise AlpacaImageBytesError(
            f"{member}: truncated ImageBytes payload — header needs {_HEADER_SIZE} bytes, "
            f"got {len(payload)}"
        )

    (metadata_version,) = struct.unpack_from("<i", payload, 0)
    if metadata_version != _SUPPORTED_METADATA_VERSION:
        raise AlpacaImageBytesError(
            f"{member}: unsupported ImageBytes MetadataVersion {metadata_version} "
            f"(this client only decodes version {_SUPPORTED_METADATA_VERSION})"
        )

    error_number: int
    client_transaction_id: int
    server_transaction_id: int
    data_start: int
    error_number, client_transaction_id, server_transaction_id, data_start = struct.unpack_from(
        "<iIIi", payload, 4
    )

    if (
        expected_client_transaction_id is not None
        and client_transaction_id != expected_client_transaction_id
    ):
        raise AlpacaProtocolError(
            f"{member}: driver echoed ClientTransactionID {client_transaction_id!r} in the "
            f"ImageBytes header, expected {expected_client_transaction_id}"
        )

    if error_number != 0:
        if data_start < _HEADER_SIZE or data_start > len(payload):
            raise AlpacaImageBytesError(
                f"{member}: ImageBytes error header declares DataStart {data_start}, which is "
                f"outside the {len(payload)}-byte payload"
            )
        try:
            message = payload[data_start:].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AlpacaImageBytesError(
                f"{member}: ImageBytes ErrorNumber {error_number:#05x} arrived with a message "
                "that was not valid UTF-8"
            ) from exc
        if not message.strip():
            raise AlpacaProtocolError(
                f"{member}: ImageBytes ErrorNumber {error_number:#05x} arrived with an empty "
                "ErrorMessage"
            )
        raise AlpacaRemoteError(error_number, message, member=member)

    if data_start < _HEADER_SIZE or data_start > len(payload):
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes DataStart is {data_start}, which is outside the "
            f"{len(payload)}-byte payload (header alone needs {_HEADER_SIZE})"
        )

    (
        image_element_type_raw,
        transmission_element_type_raw,
        rank,
        dimension1,
        dimension2,
        dimension3,
    ) = struct.unpack_from("<iiiiii", payload, 20)

    if rank != _SUPPORTED_RANK:
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes rank {rank} is not supported (this client decodes rank "
            f"{_SUPPORTED_RANK} only)"
        )
    if dimension3 != 0:
        raise AlpacaImageBytesError(
            f"{member}: a rank-2 ImageBytes array declared a non-zero Dimension3 ({dimension3})"
        )
    if dimension1 <= 0 or dimension2 <= 0:
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes declared a non-positive dimension "
            f"(Dimension1={dimension1}, Dimension2={dimension2})"
        )
    if transmission_element_type_raw not in _ELEMENT_DTYPES:
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes TransmissionElementType {transmission_element_type_raw} has "
            "no representation this client can decode"
        )
    if image_element_type_raw not in ImageArrayElementType._value2member_map_:
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes ImageElementType {image_element_type_raw} is not a known "
            "ImageArrayElementTypes value"
        )

    transmission_element_type = ImageArrayElementType(transmission_element_type_raw)
    dtype = np.dtype(_ELEMENT_DTYPES[transmission_element_type])
    element_count = dimension1 * dimension2
    expected_bytes = element_count * dtype.itemsize
    actual_bytes = len(payload) - data_start
    if actual_bytes != expected_bytes:
        raise AlpacaImageBytesError(
            f"{member}: ImageBytes declares {dimension1}x{dimension2} "
            f"{transmission_element_type.name} elements ({expected_bytes} data bytes) but "
            f"{actual_bytes} data bytes were received"
        )

    array = np.frombuffer(payload, dtype=dtype, count=element_count, offset=data_start).reshape(
        dimension1, dimension2
    )

    return ImageBytesFrame(
        metadata_version=metadata_version,
        client_transaction_id=client_transaction_id,
        server_transaction_id=server_transaction_id,
        image_element_type=ImageArrayElementType(image_element_type_raw),
        transmission_element_type=transmission_element_type,
        array=array.copy(),
    )


__all__ = ["ImageArrayElementType", "ImageBytesFrame", "decode_image_bytes"]
