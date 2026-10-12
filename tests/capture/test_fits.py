"""``ssc.capture.fits``'s writer, round-tripped through ``ssc.skymap.fits``'s
independent reader — the two halves are never allowed to only agree with
themselves."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import numpy as np
import pytest

from ssc.capture.fits import ROW_ORDER, FitsWriteError, bayer_pattern, debayer, write_fits_bytes
from ssc.capture.frame import FrameMetadata, FrameType
from ssc.skymap.fits import read_fits_image

FULL_METADATA = FrameMetadata(
    date_obs=datetime(2026, 9, 10, 3, 30, 0, tzinfo=UTC),
    exposure_s=30.0,
    gain=200,
    ccd_temp_c=31.8,
    bayer_offset_x=1,
    bayer_offset_y=0,
    pixel_size_um=2.9,
    ra_deg=83.822,
    dec_deg=-5.391,
    site_lat_deg=48.2082,
    site_lon_deg=16.3738,
    filter_name="LP",
    object_name="M42",
    frame_type=FrameType.LIGHT,
    focal_length_mm=250.0,
    telescope_name="Seestar S30 Pro",
    ssc_version="0.1.0",
    device_firmware="9.16",
    optic_id="tele",
    flat_source="panel",
    date_end=datetime(2026, 9, 10, 3, 30, 30, tzinfo=UTC),
    bin_x=1,
    bin_y=1,
    subframe_x=0,
    subframe_y=0,
    pixel_size_y_um=2.9,
    sensor_name="IMX585",
    camera_offset=1108,
    max_adu=65535,
    readout_mode=0,
    focuser_position=1210,
    focuser_temperature_c=31.3125,
    alignment_mode=1,
    aperture_diameter_mm=30.0,
    focal_ratio=250.0 / 30.0,
    wide_camera=False,
    stack_count=5,
    total_exposure_s=150.0,
)


def test_full_frame_round_trips_byte_identical_with_every_header_card() -> None:
    rng = np.random.default_rng(42)
    pixels = rng.integers(0, 65536, size=(3840, 2160), dtype=np.uint16)

    raw = write_fits_bytes(pixels, FULL_METADATA)
    image = read_fits_image(raw)

    assert image.data.shape == (3840, 2160)
    assert np.array_equal(image.data, pixels.astype(np.float64))

    header = image.header
    assert header["DATE-OBS"] == "2026-09-10T03:30:00+00:00"
    assert header["DATE-EXP"] == "2026-09-10T03:30:30+00:00"
    assert header["EXPOSURE"] == pytest.approx(30.0)
    assert header["IMAGETYP"] == "light"
    assert header["OPTICID"] == "tele"
    assert header["FLATSRC"] == "panel"
    assert header["GAIN"] == 200
    assert header["CCD-TEMP"] == pytest.approx(31.8)
    assert header["BAYERPAT"] == "GRBG"
    assert header["ROWORDER"] == ROW_ORDER
    assert header["XPIXSZ"] == pytest.approx(2.9)
    assert header["YPIXSZ"] == pytest.approx(2.9)
    assert header["XORGSUBF"] == 0
    assert header["YORGSUBF"] == 0
    assert header["XBINNING"] == 1
    assert header["YBINNING"] == 1
    assert header["CCDXBIN"] == 1
    assert header["CCDYBIN"] == 1
    assert header["RA"] == pytest.approx(83.822)
    assert header["DEC"] == pytest.approx(-5.391)
    assert header["RADESYS"] == "ICRS"
    assert header["EQUINOX"] == pytest.approx(2000.0)
    assert header["SITELAT"] == pytest.approx(48.2082)
    assert header["SITELONG"] == pytest.approx(16.3738)
    assert header["FILTER"] == "LP"
    assert header["OBJECT"] == "M42"
    assert header["FOCALLEN"] == pytest.approx(250.0)
    assert header["APTDIA"] == pytest.approx(30.0)
    assert header["APERTURE"] == pytest.approx(250.0 / 30.0)
    assert header["EQMODE"] == 1
    assert header["WIDECAM"] == 0
    assert header["INSTRUME"] == "IMX585"
    assert header["OFFSET"] == 1108
    assert header["MAXADU"] == pytest.approx(65535)
    assert header["READOUTM"] == 0
    assert header["FOCUSPOS"] == 1210
    assert header["FOCTEMP"] == pytest.approx(31.3125)
    assert header["STACKCNT"] == 5
    assert header["TOTALEXP"] == pytest.approx(150.0)
    assert header["TELESCOP"] == "Seestar S30 Pro"
    assert header["PROGRAM"] == "Photon Commander 0.1.0 / firmware 9.16"


def test_a_field_passed_as_none_is_absent_not_zero() -> None:
    metadata = FrameMetadata(
        date_obs=None,
        exposure_s=None,
        gain=None,
        ccd_temp_c=None,
        bayer_offset_x=None,
        bayer_offset_y=None,
        pixel_size_um=None,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=None,
        object_name=None,
        frame_type=FrameType.LIGHT,
        focal_length_mm=None,
        telescope_name=None,
        ssc_version="0.1.0",
        device_firmware=None,
    )
    pixels = np.zeros((4, 4), dtype=np.uint16)
    image = read_fits_image(write_fits_bytes(pixels, metadata))

    for card in (
        "DATE-OBS",
        "EXPOSURE",
        "GAIN",
        "CCD-TEMP",
        "BAYERPAT",
        "XPIXSZ",
        "RA",
        "DEC",
        "RADESYS",
        "EQUINOX",
        "SITELAT",
        "SITELONG",
        "FILTER",
        "OBJECT",
        "FOCALLEN",
        "TELESCOP",
        "DATE-EXP",
        "YPIXSZ",
        "XORGSUBF",
        "YORGSUBF",
        "XBINNING",
        "YBINNING",
        "CCDXBIN",
        "CCDYBIN",
        "INSTRUME",
        "OFFSET",
        "MAXADU",
        "READOUTM",
        "FOCUSPOS",
        "FOCTEMP",
        "EQMODE",
        "WIDECAM",
        "APTDIA",
        "APERTURE",
        "STACKCNT",
        "TOTALEXP",
    ):
        assert card not in image.header, f"{card} should be absent, not defaulted"
    # PROGRAM is always written — Photon Commander's own version is always
    # known — but the firmware half is simply missing, not present as a lie.
    assert image.header["PROGRAM"] == "Photon Commander 0.1.0"
    # ROWORDER is a Photon Commander authoring choice, not a device read: always present.
    assert image.header["ROWORDER"] == ROW_ORDER


def test_unicode_string_metadata_is_transliterated_only_in_the_fits_header() -> None:
    metadata = replace(
        FULL_METADATA,
        object_name="RA 0.717h / Dec 41.229\N{DEGREE SIGN} \N{EM DASH} Pléiades",
        filter_name="H\N{GREEK SMALL LETTER ALPHA} 7 nm",
        telescope_name="Seestar\N{TRADE MARK SIGN}",
    )

    raw = write_fits_bytes(np.zeros((4, 4), dtype=np.uint16), metadata)
    image = read_fits_image(raw)

    assert image.header["OBJECT"] == "RA 0.717h / Dec 41.229 deg - Pleiades"
    assert image.header["FILTER"] == "Halpha 7 nm"
    assert image.header["TELESCOP"] == "SeestarTM"
    assert metadata.object_name == "RA 0.717h / Dec 41.229\N{DEGREE SIGN} \N{EM DASH} Pléiades"
    assert raw[:2880].isascii()


def test_bayer_pattern_matches_the_measured_seestar_sensor() -> None:
    # docs/M0-FINDINGS.md: get_camera_info reports debayer_pattern "GR",
    # corroborating GRBG at offset (1, 0).
    assert bayer_pattern(1, 0) == "GRBG"
    assert bayer_pattern(0, 0) == "RGGB"
    assert bayer_pattern(0, 1) == "GBRG"
    assert bayer_pattern(1, 1) == "BGGR"


def test_bayer_pattern_is_absent_if_only_one_offset_is_known() -> None:
    assert bayer_pattern(1, None) is None
    assert bayer_pattern(None, 0) is None
    assert bayer_pattern(None, None) is None


def test_bayer_phase_matches_the_live_sub_lattice_measurement() -> None:
    """2026-09-10, firmware's native Alpaca on :32323, telephoto camera/0: a
    0.1 s dark's sub-lattice means, expressed in the transposed FITS
    ``pixels[y, x]`` orientation, were

        row even, col even: 1086.70   row even, col odd: 1073.59
        row odd,  col even: 1040.91   row odd,  col odd:  1086.80

    The diagonal pair agreeing to 0.1 ADU while the off-diagonal pair
    differs by 33 puts green at (0, 0) and (1, 1); measured
    ``BayerOffsetX=1, BayerOffsetY=0`` places red at (0, 1), making this GRBG
    (the device, owner, and Seestar app FITS agree). This test reproduces that arrangement,
    scaled down to a tileable array, and checks the writer reports GRBG for
    it — not just for a hand-picked offset pair.
    """
    green_diag_a, red, blue, green_diag_b = 1087, 1074, 1041, 1087
    pixels = np.array(
        [
            [green_diag_a, red, green_diag_a, red],
            [blue, green_diag_b, blue, green_diag_b],
            [green_diag_a, red, green_diag_a, red],
            [blue, green_diag_b, blue, green_diag_b],
        ],
        dtype=np.uint16,
    )
    metadata = FrameMetadata(
        date_obs=None,
        exposure_s=0.1,
        gain=None,
        ccd_temp_c=26.6875,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=2.9,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=None,
        object_name=None,
        frame_type=FrameType.LIGHT,
        focal_length_mm=None,
        telescope_name=None,
        ssc_version="0.1.0",
        device_firmware="8.46",
    )
    image = read_fits_image(write_fits_bytes(pixels, metadata))
    assert image.header["BAYERPAT"] == "GRBG"
    assert image.data[0, 0] == green_diag_a
    assert image.data[1, 1] == green_diag_b
    assert image.data[0, 1] == red
    assert image.data[1, 0] == blue


def test_bayer_phase_and_row_order_are_consistent_a_vertical_flip_fails_this() -> None:
    """A 2x2-tiled GRBG mosaic at offset (1, 0): row 0 is G R, row 1 is B G.
    Written with ``ROWORDER = TOP-DOWN`` and no vertical flip, the array's
    own row 0 must land in the FITS file's row 0 unchanged. If a future
    change flipped the array before writing, row 0 of the read-back image
    would instead hold what is currently row 1 (300, 400) — asserting the
    literal values here, not just the BAYERPAT string, is what makes that
    regression fail instead of silently passing.
    """
    green1, red, blue, green2 = 200, 100, 300, 400
    pixels = np.array(
        [
            [green1, red, green1, red],
            [blue, green2, blue, green2],
            [green1, red, green1, red],
            [blue, green2, blue, green2],
        ],
        dtype=np.uint16,
    )
    metadata = FrameMetadata(
        date_obs=None,
        exposure_s=None,
        gain=None,
        ccd_temp_c=None,
        bayer_offset_x=1,
        bayer_offset_y=0,
        pixel_size_um=None,
        ra_deg=None,
        dec_deg=None,
        site_lat_deg=None,
        site_lon_deg=None,
        filter_name=None,
        object_name=None,
        frame_type=FrameType.LIGHT,
        focal_length_mm=None,
        telescope_name=None,
        ssc_version="0.1.0",
        device_firmware=None,
    )
    image = read_fits_image(write_fits_bytes(pixels, metadata))
    assert image.header["BAYERPAT"] == "GRBG"
    assert image.header["ROWORDER"] == "TOP-DOWN"
    assert image.data[0, 0] == green1
    assert image.data[0, 1] == red
    assert image.data[1, 0] == blue
    assert image.data[1, 1] == green2


def test_non_2d_pixels_are_refused() -> None:
    pixels = np.zeros((2, 4, 4), dtype=np.uint16)
    with pytest.raises(FitsWriteError, match="2-D"):
        write_fits_bytes(pixels, FULL_METADATA)


def test_unsupported_dtype_is_refused() -> None:
    pixels = np.zeros((4, 4), dtype=np.int64)
    with pytest.raises(FitsWriteError, match="dtype"):
        write_fits_bytes(pixels, FULL_METADATA)


@pytest.mark.parametrize(
    ("pattern", "tile"),
    [
        ("RGGB", (("R", "G"), ("G", "B"))),
        ("GRBG", (("G", "R"), ("B", "G"))),
        ("GBRG", (("G", "B"), ("R", "G"))),
        ("BGGR", (("B", "G"), ("G", "R"))),
    ],
)
def test_debayer_reconstructs_a_uniform_field_exactly(
    pattern: str, tile: tuple[tuple[str, str], tuple[str, str]]
) -> None:
    """A mosaic tiled from one constant value per channel is a degenerate but
    exact case: every 3x3 neighbourhood's weighted average of a channel is
    that channel's own constant, at every pixel including the border (the
    same count of that channel replicates under edge-padding on both the sum
    and the weight, so the ratio is untouched) — this isolates the channel-
    assignment logic from the box filter's own weighting, which a
    photographic test image would not."""
    values = {"R": 100.0, "G": 150.0, "B": 200.0}
    unit = np.array(
        [[values[tile[0][0]], values[tile[0][1]]], [values[tile[1][0]], values[tile[1][1]]]]
    )
    mosaic = np.tile(unit, (4, 4))  # 8x8, four full 2x2 tiles each way

    rgb = debayer(mosaic, pattern)

    assert rgb.shape == (8, 8, 3)
    assert np.allclose(rgb[..., 0], values["R"])
    assert np.allclose(rgb[..., 1], values["G"])
    assert np.allclose(rgb[..., 2], values["B"])


def test_debayer_rejects_an_unknown_pattern() -> None:
    with pytest.raises(FitsWriteError, match="pattern"):
        debayer(np.zeros((4, 4)), "XYZW")


def test_debayer_rejects_a_non_2d_mosaic() -> None:
    with pytest.raises(FitsWriteError, match="2-D"):
        debayer(np.zeros((2, 4, 4)), "GRBG")
