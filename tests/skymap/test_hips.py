"""HiPS addressing and the HEALPix cone search, against known values and
against each other."""

from __future__ import annotations

import math

import numpy as np
import pytest

from ssc.skymap import hips


def test_tile_path_matches_the_hips_layout() -> None:
    assert hips.tile_path(3, 123, "png") == "Norder3/Dir0/Npix123.png"
    assert hips.tile_path(6, 12345, "jpg") == "Norder6/Dir10000/Npix12345.jpg"
    assert hips.tile_path(9, 2_999_999, "png") == "Norder9/Dir2990000/Npix2999999.png"


def test_tile_path_refuses_an_npix_beyond_the_order() -> None:
    with pytest.raises(ValueError, match="out of range"):
        hips.tile_path(3, 12345, "png")


def test_tiles_in_order_is_twelve_times_four_to_the_order() -> None:
    assert [hips.tiles_in_order(o) for o in range(4)] == [12, 48, 192, 768]


def test_nested_pixel_centres_round_trip_through_ang2pix() -> None:
    """Every pixel's centre must map back to that pixel — the two halves of
    the transcription agree, at every face and both caps."""
    for order in (0, 1, 2, 4):
        count = hips.tiles_in_order(order)
        centres = hips.pixel_centres(np.arange(count, dtype=np.int64), order)
        for npix, (x, y, z) in enumerate(centres):
            ra = math.degrees(math.atan2(y, x)) % 360.0
            dec = math.degrees(math.asin(z))
            assert hips.ang2pix(ra, dec, order) == npix, (order, npix)


def test_ang2pix_known_base_pixels() -> None:
    # The HEALPix base pixels: 0-3 around the north pole, 4-7 on the
    # equator (4 centred on RA 0), 8-11 around the south pole.
    assert hips.ang2pix(0.0, 0.0, 0) == 4
    assert hips.ang2pix(0.0, 89.9, 0) == 0
    assert hips.ang2pix(0.0, -89.9, 0) == 8
    # Order-1 children of base pixel 4 are 16..19.
    assert 16 <= hips.ang2pix(0.0, 0.0, 1) <= 19


def test_equatorial_to_galactic_uses_the_iau_galactic_centre_anchor() -> None:
    lon, lat = hips.equatorial_to_galactic(266.405, -28.936175)
    assert min(lon, 360.0 - lon) == pytest.approx(0.0, abs=1e-3)
    assert lat == pytest.approx(0.0, abs=1e-3)


def test_max_pixel_radius_matches_the_reference_at_nside_1() -> None:
    assert math.degrees(hips.max_pixel_radius_rad(0)) == pytest.approx(48.1897, abs=1e-3)


def test_cone_coverage_contains_the_centre_tile_at_every_order() -> None:
    ra, dec = 83.82, -5.39  # M42
    by_order = hips.cone_tiles_by_order(ra, dec, 0.5, 7)
    for order, tiles in by_order.items():
        assert hips.ang2pix(ra, dec, order) in tiles, order


def test_a_larger_cone_is_a_superset_of_a_smaller_one() -> None:
    small = set(hips.cone_tiles(83.82, -5.39, 0.5, 7))
    large = set(hips.cone_tiles(83.82, -5.39, 2.0, 7))
    assert small <= large
    assert len(large) > len(small)


def test_a_cone_near_a_pole_and_across_ra_zero_is_still_covered() -> None:
    for ra, dec in ((0.0, 89.5), (359.9, 0.0), (0.1, -89.9)):
        for order in (2, 5):
            assert hips.ang2pix(ra, dec, order) in hips.cone_tiles(ra, dec, 1.0, order)


def test_a_whole_sky_cone_is_every_tile() -> None:
    assert hips.cone_tiles(10.0, 10.0, 180.0, 3) == list(range(768))


def test_cone_coverage_is_inclusive_but_not_the_whole_sky() -> None:
    """The bound is generous, not useless: a 1 degree cone at order 6 (pixel
    side ~0.9 degrees) must be a handful of tiles, not hundreds."""
    tiles = hips.cone_tiles(83.82, -5.39, 1.0, 6)
    assert 4 <= len(tiles) <= 40


def test_properties_parsing() -> None:
    text = """
# a comment
hips_order = 11
hips_tile_format = jpeg png
hips_tile_width = 512
hips_frame = equatorial
creator_did = ivo://example/P/test
"""
    props = hips.parse_properties(text)
    assert props.hips_order == 11
    assert props.hips_tile_format == ("jpeg", "png")
    assert props.hips_tile_width == 512
    assert props.hips_frame == "equatorial"
    assert props.raw["creator_did"] == "ivo://example/P/test"


def test_properties_missing_fields_are_none_not_defaults() -> None:
    props = hips.parse_properties("hips_tile_format = png\n")
    assert props.hips_order is None
    assert props.hips_tile_width is None


def test_properties_non_integer_order_is_an_error() -> None:
    with pytest.raises(ValueError, match="hips_order"):
        hips.parse_properties("hips_order = deep\n")
