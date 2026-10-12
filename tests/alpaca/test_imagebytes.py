"""The ImageBytes binary decoder, against payloads built by hand.

No socket, no emulator: every case here constructs the exact bytes the ASCOM
Alpaca API Reference §8 describes and checks the decoder's reaction to it —
including the malformed ones ``ssc-core`` must never see silently patched up.
"""

from __future__ import annotations

import struct

import numpy as np
import pytest

from ssc.client.errors import AlpacaImageBytesError, AlpacaProtocolError, AlpacaRemoteError
from ssc.client.imagebytes import ImageArrayElementType, decode_image_bytes

_HEADER_FORMAT = "<iiIIiiiiiii"  # see ssc.client.imagebytes for the field-by-field layout
_HEADER_SIZE = struct.calcsize(_HEADER_FORMAT)


def _pack_header(
    *,
    metadata_version: int = 1,
    error_number: int = 0,
    client_transaction_id: int = 1,
    server_transaction_id: int = 1,
    data_start: int = _HEADER_SIZE,
    image_element_type: int = ImageArrayElementType.UINT16,
    transmission_element_type: int = ImageArrayElementType.UINT16,
    rank: int = 2,
    dimension1: int = 0,
    dimension2: int = 0,
    dimension3: int = 0,
) -> bytes:
    return struct.pack(
        _HEADER_FORMAT,
        metadata_version,
        error_number,
        client_transaction_id,
        server_transaction_id,
        data_start,
        image_element_type,
        transmission_element_type,
        rank,
        dimension1,
        dimension2,
        dimension3,
    )


def _asymmetric_frame(dimension1: int, dimension2: int) -> np.ndarray:
    """An array whose value at (x, y) differs from (y, x) and from any flip.

    A symmetric or constant test array passes under a transposed or
    vertically-flipped decode just as well as a correct one; this does not.
    Coefficients 3 and 97 are chosen so no (x, y) collides with another
    (x, y) pair after a transpose, a row flip or a column flip.
    """
    xs = np.arange(dimension1, dtype=np.int64).reshape(-1, 1)
    ys = np.arange(dimension2, dtype=np.int64).reshape(1, -1)
    return ((xs * 3 + ys * 97) % 65536).astype("<u2")


def _success_payload(array: np.ndarray, *, client_transaction_id: int = 1) -> bytes:
    dimension1, dimension2 = array.shape
    header = _pack_header(
        client_transaction_id=client_transaction_id,
        dimension1=dimension1,
        dimension2=dimension2,
    )
    return header + array.astype("<u2").tobytes()


def _error_payload(number: int, message: str) -> bytes:
    header = _pack_header(error_number=number, data_start=_HEADER_SIZE)
    return header + message.encode("utf-8")


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def test_round_trip_matches_the_real_cameras_2160x3840_uint16_frame() -> None:
    """Dimension1 is NumX (2160, width), Dimension2 is NumY (3840, height) —
    §8.8.1 of the API Reference. The decoded array is (NumX, NumY), exactly
    as serialised, with no transpose."""
    frame = _asymmetric_frame(2160, 3840)
    payload = _success_payload(frame, client_transaction_id=7)

    decoded = decode_image_bytes(payload, member="camera.imagearray", expected_client_transaction_id=7)

    assert decoded.array.shape == (2160, 3840)
    assert decoded.array.dtype == np.dtype("<u2")
    np.testing.assert_array_equal(decoded.array, frame)
    # Pin the axis order explicitly rather than trust shape equality alone —
    # a transpose of a non-square array would already fail on shape, but a
    # flip along either axis would not.
    assert decoded.array[1, 0] == frame[1, 0] != decoded.array[0, 1]
    assert int(decoded.array[5, 2]) == (5 * 3 + 2 * 97) % 65536
    assert decoded.metadata_version == 1
    assert decoded.client_transaction_id == 7
    assert decoded.image_element_type == ImageArrayElementType.UINT16
    assert decoded.transmission_element_type == ImageArrayElementType.UINT16


def test_decoded_array_is_independent_of_the_source_buffer() -> None:
    frame = _asymmetric_frame(4, 3)
    payload = bytearray(_success_payload(frame))
    decoded = decode_image_bytes(bytes(payload), member="camera.imagearray")
    payload[:] = b"\x00" * len(payload)  # the response buffer is gone; the array must not be
    assert decoded.array[2, 1] == frame[2, 1]


# --------------------------------------------------------------------------- #
# Refusals
# --------------------------------------------------------------------------- #


def test_a_truncated_header_is_refused() -> None:
    with pytest.raises(AlpacaImageBytesError, match="truncated"):
        decode_image_bytes(b"\x01\x00\x00\x00", member="camera.imagearray")


def test_a_declared_size_that_disagrees_with_the_bytes_received_is_refused() -> None:
    frame = _asymmetric_frame(10, 8)
    payload = _success_payload(frame)
    short = payload[:-5]  # header still declares 10x8 uint16, data is short
    with pytest.raises(AlpacaImageBytesError, match="data bytes were received"):
        decode_image_bytes(short, member="camera.imagearray")


def test_an_unsupported_rank_is_refused() -> None:
    header = _pack_header(rank=3, dimension1=2, dimension2=2, dimension3=3)
    payload = header + bytes(2 * 2 * 3 * 2)
    with pytest.raises(AlpacaImageBytesError, match="rank 3"):
        decode_image_bytes(payload, member="camera.imagearray")


def test_an_unknown_element_type_is_refused() -> None:
    header = _pack_header(
        transmission_element_type=42, dimension1=2, dimension2=2
    )
    payload = header + bytes(16)
    with pytest.raises(AlpacaImageBytesError, match="TransmissionElementType 42"):
        decode_image_bytes(payload, member="camera.imagearray")


def test_an_unsupported_metadata_version_is_refused() -> None:
    header = _pack_header(metadata_version=2, dimension1=2, dimension2=2)
    payload = header + bytes(8)
    with pytest.raises(AlpacaImageBytesError, match="MetadataVersion 2"):
        decode_image_bytes(payload, member="camera.imagearray")


def test_a_non_positive_dimension_is_refused() -> None:
    header = _pack_header(dimension1=0, dimension2=5)
    with pytest.raises(AlpacaImageBytesError, match="non-positive dimension"):
        decode_image_bytes(header, member="camera.imagearray")


def test_a_rank_2_array_with_a_nonzero_dimension3_is_refused() -> None:
    header = _pack_header(dimension1=2, dimension2=2, dimension3=1)
    payload = header + bytes(8)
    with pytest.raises(AlpacaImageBytesError, match="non-zero Dimension3"):
        decode_image_bytes(payload, member="camera.imagearray")


# --------------------------------------------------------------------------- #
# The error path is not the happy path with a flag
# --------------------------------------------------------------------------- #


def test_an_alpaca_error_carried_in_the_header_is_a_remote_error() -> None:
    payload = _error_payload(0x401, "BinX must be 1 for this camera")
    with pytest.raises(AlpacaRemoteError) as caught:
        decode_image_bytes(payload, member="camera.imagearray")
    assert caught.value.number == 0x401
    assert caught.value.message == "BinX must be 1 for this camera"


def test_an_error_number_without_a_message_is_a_protocol_violation() -> None:
    payload = _error_payload(0x400, "")
    with pytest.raises(AlpacaProtocolError, match="empty ErrorMessage"):
        decode_image_bytes(payload, member="camera.imagearray")


def test_a_mis_echoed_transaction_id_is_refused() -> None:
    frame = _asymmetric_frame(2, 2)
    payload = _success_payload(frame, client_transaction_id=5)
    with pytest.raises(AlpacaProtocolError, match="echoed ClientTransactionID"):
        decode_image_bytes(payload, member="camera.imagearray", expected_client_transaction_id=6)
