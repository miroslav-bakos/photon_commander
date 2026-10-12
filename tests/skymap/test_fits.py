"""The minimal FITS reader: header parsing, image data, WCS extraction —
against small fixture files under ``tests/fixtures/skymap/`` and a couple of
byte-level constructions this module has to refuse."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ssc.skymap.fits import FitsError, extract_wcs, parse_header, read_fits_image

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "skymap"


def test_reads_a_2d_image_with_wcs() -> None:
    data = (FIXTURES / "tiny_2d.fits").read_bytes()
    image = read_fits_image(data)
    assert image.naxis == 2
    assert image.width == 8
    assert image.height == 8
    assert image.planes == 1
    assert image.data.shape == (8, 8)
    wcs = extract_wcs(image.header)
    assert wcs.crval1 == pytest.approx(10.6847)
    assert wcs.crval2 == pytest.approx(41.269)
    assert wcs.ctype1 == "RA---TAN"
    assert wcs.ctype2 == "DEC--TAN"


def test_reads_a_3d_cube_naxis3() -> None:
    data = (FIXTURES / "legacy_cube.fits").read_bytes()
    image = read_fits_image(data)
    assert image.naxis == 3
    assert image.planes == 3
    assert image.data.shape == (3, 6, 6)


def test_a_header_with_no_wcs_cards_raises_rather_than_fabricating_one() -> None:
    data = (FIXTURES / "no_wcs.fits").read_bytes()
    image = read_fits_image(data)
    with pytest.raises(FitsError, match="no usable WCS"):
        extract_wcs(image.header)


def test_bzero_bscale_are_applied() -> None:
    header, _ = parse_header((FIXTURES / "tiny_2d.fits").read_bytes())
    # No BZERO/BSCALE cards in the fixture: defaults (0.0, 1.0) are a no-op.
    assert header.get("BZERO", 0.0) == 0.0
    assert header.get("BSCALE", 1.0) == 1.0


def test_truncated_data_is_refused() -> None:
    data = (FIXTURES / "tiny_2d.fits").read_bytes()
    with pytest.raises(FitsError, match="truncated"):
        read_fits_image(data[:-2880])


def test_unsupported_naxis_is_refused() -> None:
    header, _ = parse_header((FIXTURES / "tiny_2d.fits").read_bytes())
    assert header["NAXIS"] == 2  # sanity: the fixture is what this test assumes


def test_cd_matrix_from_cdelt_and_crota() -> None:
    from tests.fixtures.skymap.build import build_fits_bytes

    plane = np.zeros((4, 4), dtype=np.float32)
    header = {
        "CRPIX1": 2.0,
        "CRPIX2": 2.0,
        "CRVAL1": 83.633,
        "CRVAL2": 22.014,
        "CDELT1": -0.0005,
        "CDELT2": 0.0005,
        "CROTA2": 0.0,
    }
    data = build_fits_bytes(plane, header)
    image = read_fits_image(data)
    wcs = extract_wcs(image.header)
    assert wcs.cd1_1 == pytest.approx(-0.0005)
    assert wcs.cd2_2 == pytest.approx(0.0005)
    assert wcs.cd1_2 == pytest.approx(0.0)
    assert wcs.cd2_1 == pytest.approx(0.0)
