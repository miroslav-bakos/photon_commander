"""The bundled WHAM H-alpha HiPS: the committed directory the tile proxy
serves, and the build script's geometry (``scripts/build-wham-hips.py``).

The directory is a build artefact committed to the repository; these tests
read it as the proxy would (no network, no rebuild) and check the script's
helpers against skyfield, which is independent of the script's own matrix.
"""

from __future__ import annotations

import importlib.util
import math
import struct
import sys
import zlib
from pathlib import Path
from types import ModuleType

import numpy as np
import pytest
from skyfield.framelib import galactic_frame
from skyfield.positionlib import position_of_radec

from ssc.settings.config import default_bundled_hips_dir
from ssc.skymap import hips
from ssc.skymap.surveys import WHAM_HALPHA

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "build-wham-hips.py"
WHAM_DIR = default_bundled_hips_dir() / WHAM_HALPHA.local_dir  # type: ignore[operator]

#: What ``SkymapService`` / the WWT frame need from ``properties``.
REQUIRED_PROPERTIES = {
    "hips_order",
    "hips_tile_width",
    "hips_tile_format",
    "hips_frame",
    "obs_title",
    "obs_copyright",
    "obs_copyright_url",
    "hips_creator",
    "hips_release_date",
}
CREDIT = (
    "WHAM Sky Survey DR1 — Wisconsin H-Alpha Mapper, funded by the U.S. National Science Foundation"
)


@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_wham_hips", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- the committed directory ----------------------------------------------------- #


def test_properties_has_what_the_proxy_and_the_frame_need() -> None:
    text = (WHAM_DIR / "properties").read_text(encoding="utf-8")
    props = hips.parse_properties(text)
    missing = REQUIRED_PROPERTIES - props.raw.keys()
    assert not missing, f"properties lacks {sorted(missing)}"
    assert props.hips_order == WHAM_HALPHA.max_order
    assert props.hips_tile_width == 512
    assert props.hips_tile_format == ("png",)
    assert props.hips_frame == "equatorial"
    assert props.raw["obs_copyright"] == CREDIT
    assert props.raw["obs_copyright_url"] == WHAM_HALPHA.credit_url
    assert props.raw["hips_creator"] == "SSC build script"
    assert props.raw["ssc_source_version"] == "161116"
    assert props.raw["ssc_source_date"] == "2017-09-12"
    assert props.raw["ssc_source_sha256"] == (
        "7dd5f076e16a99627eb3f4e1f543134321691e8ce2ef0daf826b7e9f18b48c20"
    )


def test_every_tile_up_to_max_order_exists_as_a_png() -> None:
    for order in range(WHAM_HALPHA.max_order + 1):
        for npix in range(hips.tiles_in_order(order)):
            path = WHAM_DIR / hips.tile_path(order, npix, WHAM_HALPHA.tile_format)
            assert path.is_file(), path
            assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n", path


def test_the_pack_is_small_enough_to_commit() -> None:
    total = sum(p.stat().st_size for p in WHAM_DIR.rglob("*") if p.is_file())
    assert total <= 10_000_000, f"{total} bytes"


# -- the script's geometry ------------------------------------------------------- #


def test_registry_max_order_matches_the_scripts_cutoff(script: ModuleType) -> None:
    # Tile pixel 58.63/(2^k*512) deg: 6.87' at order 0, 3.43' at order 1;
    # a quarter of the 15' source pixel is 3.75', so the cutoff is order 1.
    assert script.deepest_useful_order() == 1
    assert WHAM_HALPHA.max_order == 1
    assert script.tile_pixel_deg(1) * 60 < 0.25 * 60 / 4 <= script.tile_pixel_deg(0) * 60


def _separation_deg(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    a = np.radians([ra1, ra2])
    d = np.radians([dec1, dec2])
    cosine = math.sin(d[0]) * math.sin(d[1]) + math.cos(d[0]) * math.cos(d[1]) * math.cos(
        a[0] - a[1]
    )
    return math.degrees(math.acos(max(-1.0, min(1.0, cosine))))


@pytest.mark.parametrize(
    ("l_deg", "b_deg", "ra_deg", "dec_deg"),
    [
        (0.0, 0.0, (17 + 45.6 / 60) * 15, -(28 + 56 / 60)),  # Galactic centre
        (0.0, 90.0, (12 + 51.4 / 60) * 15, 27 + 8 / 60),  # north Galactic pole
        (0.0, -90.0, (0 + 51.4 / 60) * 15, -(27 + 8 / 60)),  # south Galactic pole
    ],
)
def test_galactic_to_equatorial_matches_known_positions(
    script: ModuleType, l_deg: float, b_deg: float, ra_deg: float, dec_deg: float
) -> None:
    ra, dec = script.galactic_to_equatorial(l_deg, b_deg)
    assert _separation_deg(ra, dec, ra_deg, dec_deg) < 0.01


@pytest.mark.parametrize(
    ("ra_deg", "dec_deg"),
    [(266.405, -28.936), (0.0, 0.0), (83.82, -5.39), (192.86, 27.13), (300.0, -80.0), (45.0, 89.5)],
)
def test_galactic_to_equatorial_inverts_skyfield(
    script: ModuleType, ra_deg: float, dec_deg: float
) -> None:
    lat, lon, _ = position_of_radec(ra_deg / 15.0, dec_deg).frame_latlon(galactic_frame)
    ra, dec = script.galactic_to_equatorial(lon.degrees, lat.degrees)
    # skyfield's matrix carries ~1e-8 rounding; 1e-4 deg is 0.36", far below a source pixel.
    assert _separation_deg(ra, dec, ra_deg, dec_deg) < 1e-4


def test_matrix_agrees_with_skyfields_galactic_frame(script: ModuleType) -> None:
    assert np.abs(script.equatorial_to_galactic_matrix() - galactic_frame._matrix).max() < 1e-6


# -- the tiles' content --------------------------------------------------------------- #


def _decode_palette_png(data: bytes) -> tuple[np.ndarray, np.ndarray]:
    """(pixel indices, alpha per index) of an 8-bit palette PNG with tRNS."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks: dict[bytes, bytes] = {}
    pos = 8
    while pos < len(data):
        length = struct.unpack(">I", data[pos : pos + 4])[0]
        kind = data[pos + 4 : pos + 8]
        chunks[kind] = chunks.get(kind, b"") + data[pos + 8 : pos + 8 + length]
        pos += 12 + length
    width, height, depth, colour = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, colour) == (8, 3)
    rows = np.frombuffer(zlib.decompress(chunks[b"IDAT"]), dtype=np.uint8).reshape(
        height, width + 1
    )
    filters = rows[:, 0]
    pixels = rows[:, 1:].astype(np.int16)
    for y in range(height):
        if filters[y] == 1:
            pixels[y] = np.cumsum(pixels[y]) & 0xFF
        elif filters[y] == 2 and y > 0:
            pixels[y] = (pixels[y] + pixels[y - 1]) & 0xFF
        else:
            assert filters[y] in (0, 2)
    alpha = np.frombuffer(chunks[b"tRNS"], dtype=np.uint8)
    assert len(alpha) == 256 and len(chunks[b"PLTE"]) == 768
    return pixels.astype(np.uint8), alpha


def _mean_alpha(order: int, npix: int) -> float:
    pixels, alpha = _decode_palette_png(
        (WHAM_DIR / hips.tile_path(order, npix, "png")).read_bytes()
    )
    assert pixels.shape == (512, 512)
    return float(alpha[pixels].mean())


def test_galactic_plane_tile_glows_more_than_a_polar_tile(script: ModuleType) -> None:
    gc_ra, gc_dec = script.galactic_to_equatorial(0.0, 0.0)
    ngp_ra, ngp_dec = script.galactic_to_equatorial(0.0, 90.0)
    order = WHAM_HALPHA.max_order
    plane = _mean_alpha(order, hips.ang2pix(gc_ra, gc_dec, order))
    pole = _mean_alpha(order, hips.ang2pix(ngp_ra, ngp_dec, order))
    assert plane > 2 * pole, (plane, pole)
    assert pole > 0, "the faint high-latitude sky is not fully transparent"
    assert alpha_is_transparent_below_floor(script)


def alpha_is_transparent_below_floor(script: ModuleType) -> bool:
    v = script.stretch(
        np.array([-1.0, 0.0, script.FLOOR_R, script.FLOOR_R + 0.01, 1.0, 200.0, 5000.0])
    )
    return bool(v[0] == 0 and v[1] == 0 and v[2] == 0 and 0 < v[3] < v[4] < v[5] == 1.0 == v[6])


def test_tile_pixel_geometry_matches_the_nested_pixel_centres(script: ModuleType) -> None:
    """The four corner pixels of a tile are the tile's HEALPix sub-pixels at
    ``order + 9`` — row = ix, column = iy — and the tile centre is the pixel."""
    order, npix = 1, 28
    vectors = script.tile_pixel_vectors(order, npix)
    sub_order = order + 9
    base = npix << 18
    corner_sub = {
        (0, 0): base,  # ix = iy = 0
        (0, 511): base | (script._spread_bits(np.array([511]))[0] << 1),  # iy = 511
        (511, 0): base | script._spread_bits(np.array([511]))[0],  # ix = 511
        (511, 511): base | (4**9 - 1),
    }
    for (row, col), sub in corner_sub.items():
        expected = hips.pixel_centres(np.array([sub]), sub_order)[0]
        assert np.allclose(vectors[row, col], expected, atol=1e-12)
    centre = hips.pixel_centres(np.array([npix]), order)[0]
    mean = vectors.reshape(-1, 3).mean(axis=0)
    assert math.degrees(math.acos(float(mean @ centre / np.linalg.norm(mean)))) < 0.5
