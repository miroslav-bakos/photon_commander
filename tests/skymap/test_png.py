"""The asinh stretch and the minimal PNG encoder — round-tripped through a
real PNG decoder (``zlib`` + hand-parsed chunks) rather than trusting our own
encoder to check itself."""

from __future__ import annotations

import hashlib
import struct
import zlib

import numpy as np
import pytest

from ssc.skymap.png import (
    asinh_stretch,
    asinh_stretch_reference,
    balance_rgb_for_display,
    describe_stretch,
    encode_grayscale_png,
    encode_rgb_png,
    neutralize_rgb_background,
    nina_autostretch,
    nina_autostretch_unlinked_rgb,
    suppress_excess_green_for_display,
)


def _decode_png(data: bytes) -> tuple[int, int, int, bytes]:
    """Returns (width, height, colour_type, raw filtered scanlines)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    offset = 8
    idat = b""
    width = height = colour_type = 0
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        if kind == b"IHDR":
            width, height, _bit_depth, colour_type = struct.unpack(">IIBB", payload[:10])
        elif kind == b"IDAT":
            idat += payload
        offset += 8 + length + 4  # length + kind + payload + crc
    return width, height, colour_type, zlib.decompress(idat)


def _unfilter(raw: bytes, width: int, height: int, channels: int) -> np.ndarray:
    stride = width * channels
    out = np.zeros((height, stride), dtype=np.uint8)
    pos = 0
    prev = np.zeros(stride, dtype=np.int16)
    for row in range(height):
        filter_type = raw[pos]
        pos += 1
        line = np.frombuffer(raw[pos : pos + stride], dtype=np.uint8).astype(np.int16)
        pos += stride
        if filter_type == 0:
            recon = line
        elif filter_type == 1:
            recon = line.copy()
            for i in range(channels, stride):
                recon[i] = (recon[i] + recon[i - channels]) & 0xFF
        elif filter_type == 2:
            recon = (line + prev) & 0xFF
        else:
            raise AssertionError(f"unexpected filter type {filter_type}")
        out[row] = recon.astype(np.uint8)
        prev = recon
    return out.reshape(height, width, channels) if channels > 1 else out.reshape(height, width)


def test_grayscale_png_round_trips() -> None:
    pixels = np.array([[0, 64, 128, 255], [10, 20, 30, 40]], dtype=np.uint8)
    data = encode_grayscale_png(pixels)
    width, height, colour_type, raw = _decode_png(data)
    assert (width, height, colour_type) == (4, 2, 0)
    decoded = _unfilter(raw, width, height, 1)
    np.testing.assert_array_equal(decoded, pixels)


def test_rgb_png_round_trips() -> None:
    pixels = np.zeros((3, 3, 3), dtype=np.uint8)
    pixels[..., 0] = 10
    pixels[..., 1] = 20
    pixels[..., 2] = 30
    data = encode_rgb_png(pixels)
    width, height, colour_type, raw = _decode_png(data)
    assert (width, height, colour_type) == (3, 3, 2)
    decoded = _unfilter(raw, width, height, 3)
    np.testing.assert_array_equal(decoded, pixels)


def test_rgb_png_sub_filter_uses_the_previous_pixel_not_previous_channel() -> None:
    x = np.arange(256, dtype=np.uint8)
    row = np.stack([x, np.roll(x, 17), np.roll(x, 41)], axis=-1)
    pixels = np.repeat(row[None, ...], 32, axis=0)

    data = encode_rgb_png(pixels)
    width, height, colour_type, raw = _decode_png(data)

    assert (width, height, colour_type) == (256, 32, 2)
    assert raw[0] == 1, "smooth RGB rows should exercise PNG's Sub filter"
    np.testing.assert_array_equal(_unfilter(raw, width, height, 3), pixels)


def test_grayscale_png_refuses_a_non_2d_array() -> None:
    with pytest.raises(ValueError, match="2-D"):
        encode_grayscale_png(np.zeros((2, 2, 3), dtype=np.uint8))


def test_rgb_png_refuses_a_non_hw3_array() -> None:
    with pytest.raises(ValueError, match="RGB"):
        encode_rgb_png(np.zeros((2, 2), dtype=np.uint8))


def test_asinh_stretch_maps_nan_to_zero_not_a_guess() -> None:
    # +/-inf clip to the stretch's extremes like any other out-of-range
    # pixel; only NaN (no ordering to clip to) is zeroed.
    plane = np.array([[1.0, np.nan], [np.inf, -np.inf]], dtype=np.float64)
    out = asinh_stretch(plane)
    assert out[0, 1] == 0
    assert out[1, 0] == 255
    assert out[1, 1] == 0


def test_asinh_stretch_of_an_all_nan_plane_is_all_zero() -> None:
    plane = np.full((3, 3), np.nan, dtype=np.float64)
    out = asinh_stretch(plane)
    assert out.dtype == np.uint8
    assert (out == 0).all()


def test_asinh_stretch_is_monotonic_and_within_range() -> None:
    plane = np.linspace(0.0, 1000.0, 50).reshape(5, 10)
    out = asinh_stretch(plane)
    assert out.min() >= 0
    assert out.max() <= 255
    flat_in = plane.flatten()
    flat_out = out.flatten()
    order = np.argsort(flat_in)
    assert np.all(np.diff(flat_out[order].astype(int)) >= 0)


def test_nina_autostretch_maps_background_median_near_twenty_percent() -> None:
    rng = np.random.default_rng(42)
    sky = rng.normal(1200.0, 80.0, size=(200, 200))
    sky[20, 20] = 30000.0
    sky[80, 140] = 50000.0

    out = nina_autostretch(sky)

    assert abs(float(np.median(out)) / 255.0 - 0.2) < 0.01
    assert out[20, 20] > 240
    assert out[80, 140] > 250


def test_nina_autostretch_uses_one_linked_transform_for_rgb() -> None:
    base = np.linspace(900.0, 1500.0, 100).reshape(10, 10)
    rgb = np.stack([base, base + 100.0, base + 200.0], axis=-1)

    out = nina_autostretch(rgb)

    assert np.all(out[..., 0] <= out[..., 1])
    assert np.all(out[..., 1] <= out[..., 2])
    assert np.any(out[..., 0] != out[..., 2])


def test_nina_autostretch_handles_constant_and_invalid_planes() -> None:
    constant = nina_autostretch(np.full((3, 3), 1000.0))
    invalid = nina_autostretch(np.full((3, 3), np.nan))
    assert (constant == 0).all()
    assert (invalid == 0).all()


def test_rgb_background_neutralization_removes_green_cast_without_erasing_signal() -> None:
    rng = np.random.default_rng(7)
    sky = rng.normal(1000.0, 20.0, size=(100, 100))
    rgb = np.stack([sky, sky + 450.0, sky + 100.0], axis=-1)
    rgb[40:45, 40:45, 1] += 3000.0

    corrected = neutralize_rgb_background(rgb)

    backgrounds = np.median(corrected, axis=(0, 1))
    np.testing.assert_allclose(backgrounds, backgrounds[0], atol=0.1)
    original_green_contrast = rgb[42, 42, 1] - np.median(rgb[..., 1])
    corrected_green_contrast = corrected[42, 42, 1] - np.median(corrected[..., 1])
    assert corrected_green_contrast == pytest.approx(original_green_contrast)
    assert not np.shares_memory(corrected, rgb)


def test_rgb_background_neutralization_leaves_neutral_rgb_unchanged() -> None:
    plane = np.arange(100, dtype=np.float64).reshape(10, 10)
    rgb = np.repeat(plane[..., None], 3, axis=2)
    np.testing.assert_array_equal(neutralize_rgb_background(rgb), rgb)


def test_unlinked_rgb_autostretch_balances_unequal_channel_offsets_and_scales() -> None:
    rng = np.random.default_rng(21)
    sky = rng.normal(1000.0, 25.0, size=(200, 200))
    rgb = np.stack([sky * 0.8, sky * 1.5 + 500.0, sky * 1.1 + 100.0], axis=-1)
    rgb[50:55, 50:55, 1] += 8000.0

    out = nina_autostretch_unlinked_rgb(rgb)

    backgrounds = np.median(out, axis=(0, 1)).astype(np.float64)
    assert np.ptp(backgrounds) <= 1.0
    assert out[52, 52, 1] > backgrounds[1] + 100


def test_unlinked_rgb_autostretch_keeps_neutral_input_neutral() -> None:
    rng = np.random.default_rng(22)
    plane = rng.normal(1200.0, 40.0, size=(50, 50))
    rgb = np.repeat(plane[..., None], 3, axis=2)
    out = nina_autostretch_unlinked_rgb(rgb)
    np.testing.assert_array_equal(out[..., 0], out[..., 1])
    np.testing.assert_array_equal(out[..., 1], out[..., 2])


def test_rgb_display_balance_handles_unequal_background_noise_without_green_cast() -> None:
    rng = np.random.default_rng(23)
    red = rng.normal(580.0, 38.0, size=(300, 300))
    green = rng.normal(260.0, 10.0, size=(300, 300))
    blue = rng.normal(425.0, 36.0, size=(300, 300))
    rgb = np.stack([red, green, blue], axis=-1)
    rgb[100:110, 100:110] += np.array([900.0, 350.0, 550.0])

    balanced = balance_rgb_for_display(rgb, signal_percentile=99.5)
    out = nina_autostretch(balanced)

    backgrounds = np.median(out, axis=(0, 1)).astype(np.float64)
    assert np.ptp(backgrounds) <= 1.0
    highlights = np.percentile(out, 99.5, axis=(0, 1)).astype(np.float64)
    assert np.ptp(highlights) <= 3.0


def test_display_green_suppression_leaves_neutral_sky_and_cyan_signal() -> None:
    pixels = np.array([[[51, 55, 50], [20, 90, 25], [15, 80, 100]]], dtype=np.uint8)

    corrected = suppress_excess_green_for_display(pixels)

    np.testing.assert_array_equal(corrected[0, 0], [51, 55, 50])
    np.testing.assert_array_equal(corrected[0, 1], [20, 25, 25])
    np.testing.assert_array_equal(corrected[0, 2], [15, 80, 100])
    np.testing.assert_array_equal(pixels[0, 1], [20, 90, 25])


# -- asinh_stretch_reference: the tile-pyramid stretch (2026-09-09) -------------- #


def test_reference_stretch_maps_the_same_value_the_same_regardless_of_the_tile() -> None:
    """The whole point of a fixed reference: two "tiles" with wildly
    different content still agree on what one physical value looks like —
    exactly the property per-tile ``asinh_stretch`` does not have, which is
    why a bright tile and a faint neighbour disagreed about a shared pixel
    value (the ``nsns-halpha`` npix 21439/21480 bug)."""
    faint_tile = np.full((4, 4), 50.0, dtype=np.float64)
    bright_tile = np.concatenate([np.full((4, 2), 50.0), np.full((4, 2), 900.0)], axis=1)
    out_faint = asinh_stretch_reference(faint_tile, 0.0, 1000.0)
    out_bright = asinh_stretch_reference(bright_tile, 0.0, 1000.0)
    assert out_faint[0, 0] == out_bright[0, 0], "the same input value must map to the same grey"


def test_reference_stretch_reproduces_the_reported_nsns_inversion_fix() -> None:
    """Regression for the exact bug report: a tile containing the Orion
    Nebula core (huge dynamic range) must no longer render *darker* on
    average than an actually-fainter neighbour once both share one fixed
    reference — the defining symptom of the old per-tile stretch."""
    # Percentile shape lifted from the real tiles (fetched live 2026-09-09):
    # npix 21439 (p1=55, p50=133, p99.5=131667, max=141650 R) has almost all
    # its pixels near 130 R plus a tiny Trapezium-core spike; npix 21480
    # (p1=40, p50=80, p99.5=131, max=177 R) is uniformly fainter throughout.
    bright_core_tile = np.concatenate(
        [np.full(9900, 130.0), np.full(50, 55.0), np.full(50, 140000.0)]
    ).reshape(100, 100)
    fainter_neighbour = np.concatenate(
        [np.full(9800, 80.0), np.full(100, 40.0), np.full(100, 177.0)]
    ).reshape(100, 100)
    with_old_stretch_core = asinh_stretch(bright_core_tile)
    with_old_stretch_neighbour = asinh_stretch(fainter_neighbour)
    assert with_old_stretch_core.mean() < with_old_stretch_neighbour.mean(), (
        "sanity check: the old per-tile stretch really does invert brightness here"
    )

    with_reference_core = asinh_stretch_reference(bright_core_tile, 0.0, 1000.0)
    with_reference_neighbour = asinh_stretch_reference(fainter_neighbour, 0.0, 1000.0)
    assert with_reference_core.mean() > with_reference_neighbour.mean(), (
        "the fixed reference must restore the true ordering: the tile with "
        "more real signal reads brighter, not darker"
    )


def test_reference_stretch_is_monotonic_and_within_range() -> None:
    plane = np.linspace(-50.0, 2000.0, 50).reshape(5, 10)
    out = asinh_stretch_reference(plane, 0.0, 1000.0)
    assert out.min() >= 0
    assert out.max() <= 255
    order = np.argsort(plane.flatten())
    assert np.all(np.diff(out.flatten()[order].astype(int)) >= 0)


def test_reference_stretch_maps_nan_to_zero() -> None:
    plane = np.array([[1.0, np.nan], [np.inf, -np.inf]], dtype=np.float64)
    out = asinh_stretch_reference(plane, 0.0, 1000.0)
    assert out[0, 1] == 0
    assert out[1, 0] == 255
    assert out[1, 1] == 0


def test_reference_stretch_refuses_a_white_point_at_or_below_black() -> None:
    plane = np.zeros((2, 2), dtype=np.float64)
    with pytest.raises(ValueError, match="must exceed"):
        asinh_stretch_reference(plane, 100.0, 100.0)
    with pytest.raises(ValueError, match="must exceed"):
        asinh_stretch_reference(plane, 100.0, 50.0)


def test_describe_stretch_names_its_parameters() -> None:
    text = describe_stretch()
    assert "asinh" in text
    assert "p1" in text or "p1.0" in text or "p1" in text


@pytest.mark.parametrize(("background", "expected"), [
    (5000.0, "5b7bc5720e82e34787a85c6b3cc5e2c7d2ce2567b154d51dbec44473e17e0ea2"),
    (50000.0, "c22c207860020b6a63bc96ce0cf3b49105843113d429b7abeb72efcd4e8851b4"),
])
def test_chunked_linked_stretch_preserves_native_preview_bytes(
    background: float, expected: str
) -> None:
    # Golden bytes from the prior full-plane algorithm, spanning two stripe
    # seams and both the normal and inverted-background stretch branches.
    pixels = np.random.default_rng(29).normal(background, 200, (259, 19, 3))
    pixels[0, 0] = [np.nan, np.inf, -np.inf]
    original = pixels.copy()
    result = nina_autostretch(pixels)
    assert result.shape == pixels.shape
    assert hashlib.sha256(result.tobytes()).hexdigest() == expected
    np.testing.assert_array_equal(pixels, original)
