"""Public WHAM distribution boundary and independent build-script numerics.

Survey pixels are excluded until redistribution permission is recorded. Geometry
and stretch helpers remain testable with synthetic inputs, without survey data.
"""

from __future__ import annotations

import importlib.util
import math
import sys
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
@pytest.fixture(scope="module")
def script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_wham_hips", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# -- public distribution boundary ------------------------------------------------ #


def test_wham_is_disabled_without_recorded_distribution_rights() -> None:
    assert WHAM_HALPHA.enabled is False
    assert WHAM_HALPHA.bundle_allowed is False
    assert WHAM_HALPHA.production_cleared is False
    assert WHAM_HALPHA.local_dir is None
    assert WHAM_HALPHA.pending_clearance


def test_public_tree_contains_no_bundled_wham_pixels() -> None:
    for wham_dir in (ROOT / "data" / "hips" / "wham", default_bundled_hips_dir() / "wham"):
        assert not any(path.is_file() for path in wham_dir.rglob("*")), wham_dir


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


# -- synthetic stretch ----------------------------------------------------------- #


def test_stretch_is_transparent_below_floor_and_monotonic(script: ModuleType) -> None:
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
