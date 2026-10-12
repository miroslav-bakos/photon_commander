"""Web Mercator (XYZ) tile addressing, against known values."""

from __future__ import annotations

import math

import pytest

from ssc.skymap import mercator


def test_tiles_in_zoom_is_the_2n_by_2n_grid() -> None:
    assert [mercator.tiles_in_zoom(z) for z in range(4)] == [1, 4, 16, 64]


def test_tiles_in_zoom_refuses_beyond_max_zoom() -> None:
    with pytest.raises(ValueError, match="must be in 0"):
        mercator.tiles_in_zoom(mercator.MAX_ZOOM + 1)


def test_pack_and_unpack_round_trip() -> None:
    for zoom in (0, 1, 2, 5):
        size = 1 << zoom
        for y in range(size):
            for x in range(size):
                npix = mercator.pack(zoom, x, y)
                assert mercator.unpack(zoom, npix) == (x, y)


def test_pack_refuses_coordinates_outside_the_grid() -> None:
    with pytest.raises(ValueError, match="outside"):
        mercator.pack(1, 2, 0)  # x must be 0 or 1 at zoom 1
    with pytest.raises(ValueError, match="outside"):
        mercator.pack(1, 0, -1)


def test_unpack_refuses_an_npix_beyond_the_zoom() -> None:
    with pytest.raises(ValueError, match="out of range"):
        mercator.unpack(1, 4)  # zoom 1 has exactly 4 tiles: npix 0..3


def test_tile_path_matches_legacy_surveys_own_xyz_layout() -> None:
    assert mercator.tile_path(2, 1, 3, "jpg") == "2/1/3.jpg"
    assert mercator.tile_path(0, 0, 0, "jpg") == "0/0/0.jpg"


def test_tile_path_refuses_coordinates_outside_the_grid() -> None:
    with pytest.raises(ValueError, match="outside"):
        mercator.tile_path(1, 5, 0, "jpg")


def test_tile_path_refuses_a_dotted_or_slashed_extension() -> None:
    with pytest.raises(ValueError, match="bare extension"):
        mercator.tile_path(1, 0, 0, ".jpg")
    with pytest.raises(ValueError, match="bare extension"):
        mercator.tile_path(1, 0, 0, "a/b")


# -- ground truth: x is mirrored relative to a naive ra/360 slippy-map formula ---- #
#
# Regression for a real bug (fixed 2026-09-09): `_x_frac` used to compute
# `ra / 360` ("x increasing east", the standard-XYZ assumption) which is
# mirrored relative to Legacy Surveys' own tile builder (`decals-web`'s
# `map/utils.py::get_tile_wcs` computes `rx = zoomscale/2 - x` — the world
# pixel column *falls* as the tile index `x` *rises*). These two tile
# addresses were fetched from the live server and visually confirmed against
# the real target, not derived from this module's own formula — the whole
# point being that a self-consistent test would have missed this exactly as
# the frontend's independent copy of the same bug (`imagerySpike.ts`,
# f1bda71) went unnoticed. Do not "simplify" these back to a round trip.


def test_x_frac_matches_the_live_tile_server_for_m31() -> None:
    """RA 10.6847, Dec +41.269 (M31). At zoom 8, fetching
    `unwise-neo7/1/8/248/95.jpg` from legacysurvey.org returns the galaxy
    filling the frame; `8/7/95.jpg` (the naive `ra/360` formula's tile, one
    tile-row over) is an unrelated star field. Verified 2026-09-09."""
    zoom = 8
    size = 1 << zoom
    x = math.floor(mercator._x_frac(10.6847) * size)
    y = math.floor(mercator._y_frac(41.269) * size)
    assert (x, y) == (248, 95)


def test_x_frac_matches_the_live_tile_server_for_m42() -> None:
    """RA 83.8208, Dec -5.3833 (M42, the Orion Nebula). At zoom 8,
    `unwise-neo7/1/8/196/131.jpg` shows the nebula in frame; `8/59/131.jpg`
    (the naive `ra/360` formula's tile) does not. Verified 2026-09-09 —
    a second, independent point so the fix is pinned to the *direction* of
    the mirror, not a coincidence at one field."""
    zoom = 8
    size = 1 << zoom
    x = math.floor(mercator._x_frac(83.8208) * size)
    y = math.floor(mercator._y_frac(-5.3833) * size)
    assert (x, y) == (196, 131)


def test_cone_tiles_by_zoom_includes_the_live_verified_m31_tile() -> None:
    """`cone_tiles_by_zoom` (the cone-prefetch entry point, `service.py`)
    must resolve a cone around M31 to the same tile confirmed live above,
    not just to whatever `_x_frac` itself says — this is the path a real
    prefetch job actually calls."""
    by_zoom = mercator.cone_tiles_by_zoom(10.6847, 41.269, 0.1, 8)
    assert mercator.pack(8, 248, 95) in by_zoom[8]


# -- cone_tiles_by_zoom ------------------------------------------------------------ #


def test_cone_tiles_by_zoom_refuses_bad_input() -> None:
    with pytest.raises(ValueError, match="must be in 0"):
        mercator.cone_tiles_by_zoom(180.0, 0.0, 1.0, mercator.MAX_ZOOM + 1)
    with pytest.raises(ValueError, match="not be negative"):
        mercator.cone_tiles_by_zoom(180.0, 0.0, -1.0, 5)
    with pytest.raises(ValueError, match=r"-90\.\.90"):
        mercator.cone_tiles_by_zoom(180.0, 95.0, 1.0, 5)


def test_cone_tiles_by_zoom_includes_the_centre_tile_at_every_zoom() -> None:
    by_zoom = mercator.cone_tiles_by_zoom(180.0, 0.0, 3.0, 8)
    for zoom, tiles in by_zoom.items():
        size = 1 << zoom
        cx = int(mercator._x_frac(180.0) * size) % size
        cy = int(mercator._y_frac(0.0) * size)
        assert mercator.pack(zoom, cx, cy) in tiles


def test_cone_tiles_by_zoom_is_monotone_in_radius() -> None:
    small = set(mercator.cone_tiles_by_zoom(180.0, 0.0, 1.0, 9)[9])
    big = set(mercator.cone_tiles_by_zoom(180.0, 0.0, 3.0, 9)[9])
    assert small <= big


def test_cone_tiles_by_zoom_matches_the_documented_sizing_reference() -> None:
    """`ssc/skymap/service.py`'s `PREFETCH_MAX_TILES` docstring and
    `CLAUDE.md`'s task brief both quote "a 6-degree region at zoom 11 is
    about 1,296 tiles" as the sizing reference for a per-target prefetch.
    The padded bounding-box approach here is a conservative superset, not an
    exact count, so this pins it to the same order of magnitude rather than
    the literal number."""
    total = len(mercator.cone_tiles_by_zoom(180.0, 0.0, 3.0, 11)[11])
    assert 1_000 <= total <= 2_000


def test_cone_tiles_by_zoom_handles_ra_wrap_at_the_seam() -> None:
    # A cone straddling RA 0h/24h must not raise, and must still cover a
    # tile on both sides of the seam.
    by_zoom = mercator.cone_tiles_by_zoom(0.5, 10.0, 3.0, 6)
    size = 1 << 6
    xs = {mercator.unpack(6, npix)[0] for npix in by_zoom[6]}
    assert 0 in xs
    assert size - 1 in xs


def test_cone_tiles_by_zoom_near_a_pole_covers_the_full_ra_row() -> None:
    by_zoom = mercator.cone_tiles_by_zoom(0.0, 89.9, 2.0, 4)
    size = 1 << 4
    assert len(by_zoom[4]) == size  # the whole top row, every column


def test_cone_tiles_by_zoom_at_zoom_zero_is_the_single_all_sky_tile() -> None:
    assert mercator.cone_tiles_by_zoom(10.0, 5.0, 0.5, 0) == {0: [0]}
