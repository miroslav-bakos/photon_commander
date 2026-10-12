#!/usr/bin/env python3
"""Build the bundled WHAM H-alpha HiPS under ``data/hips/wham/``.

A **developer-time** tool: run by hand, outputs committed, never imported by
``ssc/``. The app serves the committed directory through the tile proxy
(``ssc.skymap.service`` reads ``properties`` and ``Norder*/``) with no network
at all — WHAM redistribution permission has not been recorded; this tool
produces local research data only and its output must not be distributed
(``THIRD_PARTY_NOTICES.md`` "Sky-survey layers", the WHAM rows).

Source
    WHAM-SS DR1 integrated H-alpha image (``VERSION='161116'``, ``DATE=2017-09-12``):
    GLON-CAR plate carree, 1440x720, 0.25 deg/px, Rayleighs, Galactic J2000.
    The Box link is unversioned, so the file's SHA-256 is pinned below: the
    first fetch computed it, every later run refuses a file that differs.

Pipeline, per HiPS tile (nested HEALPix pixel ``npix`` at ``order``)
    1. Every one of the 512x512 tile pixels is a nested HEALPix sub-pixel at
       ``order + 9``; its centre is a unit vector in equatorial J2000.
       Orientation inside the tile follows what the WWT engine renders
       (``HealpixTile._populateVertexList``): image column = HEALPix ``iy``,
       image row (top-down) = HEALPix ``ix``.
    2. Rotate equatorial -> Galactic (IAU 1958 frame at J2000: alpha_NGP =
       192.85948, delta_NGP = 27.12825, l_NCP = 122.93192 deg), sample the map
       bilinearly, wrapping in longitude and clamping in latitude.
    3. Stretch intensity I (Rayleighs) to v in [0, 1]:
           v = asinh((I - FLOOR) / SOFT) / asinh((CEIL - FLOOR) / SOFT)
       with FLOOR = 0.3 R (below the survey's 5th percentile, ~0.35 R; the
       WHAM noise floor is ~0.1 R), SOFT = 1 R (the linear-to-log knee: the
       1-3 R high-latitude emission stays visible), CEIL = 200 R (the 99.9th
       percentile; the Galactic plane and bright HII regions saturate there).
       I <= FLOOR is fully transparent, so the layer blends as a translucent
       glow over optical imagery rather than a black sky.
    4. Write an 8-bit palette PNG: pixel = round(255 v); palette entry v has
       alpha = round(255 v) and colour ramping from H-alpha red (220, 40, 60)
       at v=0 toward pink-white (255, 205, 180) at v=1 (quadratic in v), so
       the brightest emission reads as saturated, not clipped.

Depth
    Tile pixel at order k = 58.63 deg / (2^k * 512): 6.87' at order 0, 3.43' at
    order 1. The source pixel is 15', a quarter of it 3.75', so order 1 is the
    first order whose tile pixel is below that: orders 0..1, 60 tiles.
    Deeper orders would be pure interpolation. ``ssc.skymap.surveys.WHAM_HALPHA``
    carries the same ``max_order``; ``tests/skymap/test_wham_hips.py`` pins it.

Idempotent and deterministic: the same source file and the same recipe give
byte-identical tiles; rerunning overwrites in place.

    uv run python scripts/build-wham-hips.py            # fetch (once), build
    uv run python scripts/build-wham-hips.py --source /path/to/wham-ss-dr1.fits
"""

from __future__ import annotations

import argparse
import hashlib
import math
import struct
import sys
import urllib.request
import zlib
from pathlib import Path
from typing import Final

import numpy as np
from numpy.typing import NDArray

from ssc.skymap import hips

ROOT: Final = Path(__file__).resolve().parents[1]
OUT_DIR: Final = ROOT / "data" / "hips" / "wham"
#: Gitignored (``data/cache/``); the source is never committed or packaged.
CACHE_FILE: Final = ROOT / "data" / "cache" / "wham-ss-dr1-161116-integrated.fits"

# -- source ------------------------------------------------------------------- #

#: "FITS Image [4.0 MB]" under "Integrated Survey (DR1-v161116-170912)" on
#: https://www.astro.wisc.edu/research/research-areas/galactic-astronomy/wham/wham-sky-survey/wham-ss-data-release/
SOURCE_URL: Final = "https://uwmadison.box.com/shared/static/p0o200zyen2ittuhop9o8ugpxli2isre.fits"
#: Computed on the first fetch (2026-09-07). A different hash means a different
#: file behind the unversioned link: stop and re-read the header, do not build.
SOURCE_SHA256: Final = "7dd5f076e16a99627eb3f4e1f543134321691e8ce2ef0daf826b7e9f18b48c20"
#: Header facts the build relies on (THIRD_PARTY_NOTICES.md endpoint gate, WHAM row).
EXPECTED_HEADER: Final[dict[str, object]] = {
    "NAXIS1": 1440,
    "NAXIS2": 720,
    "CTYPE1": "GLON-CAR",
    "CTYPE2": "GLAT-CAR",
    "BUNIT": "R",
    "VERSION": "161116",
    "TAG": "DR1",
}
SOURCE_PIXEL_DEG: Final = 0.25
USER_AGENT: Final = "SSC build script (https://github.com/coditsro/ssc; maintainer@example.org)"

CREDIT: Final = (
    "WHAM Sky Survey DR1 — Wisconsin H-Alpha Mapper, funded by the U.S. National Science Foundation"
)
CREDIT_URL: Final = "https://www.astro.wisc.edu/research/research-areas/galactic-astronomy/wham/"
#: Bump with the recipe (stretch, palette, depth); keeps rebuilds deterministic.
RECIPE_VERSION: Final = "1"
HIPS_RELEASE_DATE: Final = "2026-09-07T00:00Z"

# -- geometry ----------------------------------------------------------------- #

TILE_WIDTH: Final = 512
TILE_ORDER_BITS: Final = 9  # log2(TILE_WIDTH)
#: Side of one of the 12 base HEALPix pixels, sqrt(4 pi / 12) in degrees.
BASE_PIXEL_SIDE_DEG: Final = math.degrees(math.sqrt(4.0 * math.pi / 12.0))

#: IAU 1958 Galactic frame expressed in J2000 (Reid & Brunthaler 2004, as
#: used by skyfield's ``galactic_frame`` and astropy's ``Galactic``).
RA_NGP_DEG: Final = 192.85948
DEC_NGP_DEG: Final = 27.12825
L_NCP_DEG: Final = 122.93192

# -- stretch and palette --------------------------------------------------------- #

FLOOR_R: Final = 0.3
SOFT_R: Final = 1.0
CEIL_R: Final = 200.0
COLOUR_FAINT: Final = (220, 40, 60)
COLOUR_BRIGHT: Final = (255, 205, 180)


# -- Galactic <-> equatorial --------------------------------------------------------- #


def _rot_z(angle_deg: float) -> NDArray[np.float64]:
    """Passive rotation about z: components of a fixed vector in a frame turned by ``angle``."""
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])


def _rot_y(angle_deg: float) -> NDArray[np.float64]:
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    return np.array([[c, 0.0, -s], [0.0, 1.0, 0.0], [s, 0.0, c]])


def equatorial_to_galactic_matrix() -> NDArray[np.float64]:
    """3x3 matrix taking J2000 equatorial unit vectors to Galactic ones."""
    return _rot_z(180.0 - L_NCP_DEG) @ _rot_y(90.0 - DEC_NGP_DEG) @ _rot_z(RA_NGP_DEG)


def unit_vectors(lon_deg: NDArray[np.float64], lat_deg: NDArray[np.float64]) -> NDArray[np.float64]:
    lon = np.radians(lon_deg)
    lat = np.radians(lat_deg)
    return np.stack([np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)], axis=-1)


def lon_lat(vectors: NDArray[np.float64]) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """(longitude in [0, 360), latitude in [-90, 90]) of unit vectors (N, 3), degrees."""
    lon = np.degrees(np.arctan2(vectors[..., 1], vectors[..., 0])) % 360.0
    lat = np.degrees(np.arcsin(np.clip(vectors[..., 2], -1.0, 1.0)))
    return lon, lat


def galactic_to_equatorial(l_deg: float, b_deg: float) -> tuple[float, float]:
    """(RA, Dec) J2000 in degrees, RA in [0, 360), of Galactic (l, b) in degrees."""
    vec = unit_vectors(np.array([l_deg]), np.array([b_deg]))
    eq = vec @ equatorial_to_galactic_matrix()  # v_eq = R^T v_gal  ==  v_gal @ R
    ra, dec = lon_lat(eq)
    return float(ra[0]), float(dec[0])


# -- HiPS tile geometry ------------------------------------------------------------- #


def _spread_bits(values: NDArray[np.int64]) -> NDArray[np.int64]:
    """Bit *i* of each value moves to bit *2i* (nested HEALPix interleave)."""
    v = values.astype(np.int64) & 0xFFFF
    v = (v | (v << 8)) & 0x00FF00FF
    v = (v | (v << 4)) & 0x0F0F0F0F
    v = (v | (v << 2)) & 0x33333333
    v = (v | (v << 1)) & 0x55555555
    return v


def tile_pixel_vectors(order: int, npix: int) -> NDArray[np.float64]:
    """Equatorial unit vectors (TILE_WIDTH, TILE_WIDTH, 3) of the tile's pixel
    centres, indexed ``[row, column]`` with row 0 at the top of the image.

    Row = HEALPix ``ix``, column = HEALPix ``iy`` of the sub-pixel at
    ``order + TILE_ORDER_BITS`` — the WWT engine's texture mapping.
    """
    axis = np.arange(TILE_WIDTH, dtype=np.int64)
    spread = _spread_bits(axis)
    sub = (npix << (2 * TILE_ORDER_BITS)) + spread[:, None] + (spread << 1)[None, :]
    centres = hips.pixel_centres(sub.ravel(), order + TILE_ORDER_BITS)
    return centres.reshape(TILE_WIDTH, TILE_WIDTH, 3)


def tile_pixel_deg(order: int) -> float:
    """Angular size of one tile pixel at ``order`` (the base pixel side / (2^k * width))."""
    return BASE_PIXEL_SIDE_DEG / ((1 << order) * TILE_WIDTH)


def deepest_useful_order() -> int:
    """The first order whose tile pixel is below a quarter of the source pixel."""
    order = 0
    while tile_pixel_deg(order) >= SOURCE_PIXEL_DEG / 4.0:
        order += 1
    return order


# -- FITS ------------------------------------------------------------------------- #


class SourceMap:
    """The WHAM plate carree image plus the WCS needed to sample it."""

    def __init__(self, data: NDArray[np.float64], header: dict[str, object]) -> None:
        self.data = data
        self.header = header
        self.crpix1 = float(header["CRPIX1"])  # type: ignore[arg-type]
        self.crpix2 = float(header["CRPIX2"])  # type: ignore[arg-type]
        self.crval1 = float(header["CRVAL1"])  # type: ignore[arg-type]
        self.crval2 = float(header["CRVAL2"])  # type: ignore[arg-type]
        self.cd1_1 = float(header["CD1_1"])  # type: ignore[arg-type]
        self.cd2_2 = float(header["CD2_2"])  # type: ignore[arg-type]
        self.width = data.shape[1]
        self.height = data.shape[0]

    def sample(self, l_deg: NDArray[np.float64], b_deg: NDArray[np.float64]) -> NDArray[np.float64]:
        """Bilinear intensity at Galactic (l, b): periodic in l, clamped in b."""
        # FITS pixel coordinates are 1-based; 0-based array index = p - 1.
        x = (self.crpix1 - 1.0) + ((l_deg - self.crval1 + 180.0) % 360.0 - 180.0) / self.cd1_1
        y = (self.crpix2 - 1.0) + (b_deg - self.crval2) / self.cd2_2
        x = np.mod(x, self.width)
        y = np.clip(y, 0.0, self.height - 1.0)
        x0 = np.floor(x).astype(np.int64)
        y0 = np.minimum(np.floor(y).astype(np.int64), self.height - 2)
        fx = x - x0
        fy = y - y0
        x1 = (x0 + 1) % self.width
        y1 = y0 + 1
        top = self.data[y0, x0] * (1.0 - fx) + self.data[y0, x1] * fx
        bottom = self.data[y1, x0] * (1.0 - fx) + self.data[y1, x1] * fx
        return top * (1.0 - fy) + bottom * fy


def _parse_card_value(raw: str) -> object:
    value = raw.split(" /", 1)[0].strip() if not raw.lstrip().startswith("'") else raw
    if value.startswith("'"):
        end = value.find("'", 1)
        return value[1:end].strip()
    if value in {"T", "F"}:
        return value == "T"
    try:
        return int(value)
    except ValueError:
        return float(value)


def read_fits_image(path: Path) -> SourceMap:
    """Read a single-HDU 2-D FITS image: BITPIX -32 only, which is what WHAM ships."""
    raw = path.read_bytes()
    header: dict[str, object] = {}
    offset = 0
    while True:
        card = raw[offset : offset + 80].decode("ascii")
        offset += 80
        key = card[:8].strip()
        if key == "END":
            break
        if key in {"COMMENT", "HISTORY", ""} or card[8:10] != "= ":
            continue
        header[key] = _parse_card_value(card[10:])
        if offset >= len(raw):
            raise ValueError(f"{path}: no END card")
    data_start = math.ceil(offset / 2880) * 2880
    if header.get("BITPIX") != -32 or header.get("NAXIS") != 2:
        raise ValueError(f"{path}: expected BITPIX=-32 NAXIS=2, got {header}")
    width, height = int(header["NAXIS1"]), int(header["NAXIS2"])  # type: ignore[arg-type]
    count = width * height
    data = np.frombuffer(raw, dtype=">f4", count=count, offset=data_start).astype(np.float64)
    bzero = float(header.get("BZERO", 0.0))  # type: ignore[arg-type]
    bscale = float(header.get("BSCALE", 1.0))  # type: ignore[arg-type]
    data = data.reshape(height, width) * bscale + bzero
    return SourceMap(data, header)


def verify_header(header: dict[str, object]) -> None:
    for key, expected in EXPECTED_HEADER.items():
        if header.get(key) != expected:
            raise ValueError(f"source header {key}={header.get(key)!r}, expected {expected!r}")
    for key, expected in {"CD1_1": -SOURCE_PIXEL_DEG, "CD2_2": SOURCE_PIXEL_DEG}.items():
        if not math.isclose(float(header[key]), expected):  # type: ignore[arg-type]
            raise ValueError(f"source header {key}={header[key]!r}, expected {expected}")


# -- fetch and pin --------------------------------------------------------------------- #


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def obtain_source(source: Path | None) -> Path:
    """The pinned source file: a path given on the command line, the cached
    download, or a fresh download. Whatever it is, the SHA-256 must match."""
    if source is None:
        source = CACHE_FILE
        if not source.exists():
            print(f"fetching {SOURCE_URL}")
            source.parent.mkdir(parents=True, exist_ok=True)
            request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read()
            partial = source.with_suffix(".part")
            partial.write_bytes(body)
            partial.replace(source)
    digest = sha256_of(source)
    if digest != SOURCE_SHA256:
        raise SystemExit(
            f"refusing to build: {source} has SHA-256 {digest}, pinned {SOURCE_SHA256}. "
            "The unversioned Box link now serves a different file — re-read its header "
            "(THIRD_PARTY_NOTICES.md endpoint gate, WHAM row) before updating the pin."
        )
    return source


# -- stretch, palette, PNG ------------------------------------------------------------ #


def stretch(intensity_r: NDArray[np.float64]) -> NDArray[np.float64]:
    """Rayleighs -> [0, 1] (asinh with the documented floor, knee and ceiling)."""
    scaled = np.arcsinh(np.clip(intensity_r - FLOOR_R, 0.0, CEIL_R - FLOOR_R) / SOFT_R)
    v = scaled / math.asinh((CEIL_R - FLOOR_R) / SOFT_R)
    return np.nan_to_num(np.clip(v, 0.0, 1.0), nan=0.0)


def palette() -> tuple[bytes, bytes]:
    """256 RGB entries and their alphas, both a function of v = index / 255."""
    rgb = bytearray()
    alpha = bytearray()
    for index in range(256):
        v = index / 255.0
        w = v * v
        for faint, bright in zip(COLOUR_FAINT, COLOUR_BRIGHT, strict=True):
            rgb.append(round(faint + (bright - faint) * w))
        alpha.append(index)
    return bytes(rgb), bytes(alpha)


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _filtered_rows(pixels: NDArray[np.uint8], filter_type: int) -> bytes:
    if filter_type == 0:
        delta = pixels
    elif filter_type == 1:  # Sub
        delta = pixels.copy()
        delta[:, 1:] = (pixels[:, 1:].astype(np.int16) - pixels[:, :-1]) & 0xFF
    elif filter_type == 2:  # Up
        delta = pixels.copy()
        delta[1:, :] = (pixels[1:, :].astype(np.int16) - pixels[:-1, :]) & 0xFF
    else:
        raise ValueError(filter_type)
    tag = np.full((pixels.shape[0], 1), filter_type, dtype=np.uint8)
    return np.concatenate([tag, delta.astype(np.uint8)], axis=1).tobytes()


def encode_png(pixels: NDArray[np.uint8], rgb: bytes, alpha: bytes) -> bytes:
    """8-bit palette PNG with a tRNS alpha table; the smallest of three row filters."""
    height, width = pixels.shape
    header = struct.pack(">IIBBBBB", width, height, 8, 3, 0, 0, 0)
    best = min(
        (zlib.compress(_filtered_rows(pixels, f), 9) for f in (0, 1, 2)),
        key=len,
    )
    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _png_chunk(b"IHDR", header),
            _png_chunk(b"PLTE", rgb),
            _png_chunk(b"tRNS", alpha),
            _png_chunk(b"IDAT", best),
            _png_chunk(b"IEND", b""),
        ]
    )


# -- build ------------------------------------------------------------------------ #


def render_tile(
    source: SourceMap, eq_to_gal: NDArray[np.float64], order: int, npix: int
) -> NDArray[np.uint8]:
    vectors = tile_pixel_vectors(order, npix) @ eq_to_gal.T
    l_deg, b_deg = lon_lat(vectors)
    v = stretch(source.sample(l_deg, b_deg))
    return np.round(v * 255.0).astype(np.uint8)


def properties_text(source: SourceMap, max_order: int) -> str:
    lines = [
        "# Built by scripts/build-wham-hips.py — do not edit by hand.",
        "hips_version = 1.4",
        "hips_creator = SSC build script",
        f"hips_release_date = {HIPS_RELEASE_DATE}",
        "hips_status = private master unclonable",
        "dataproduct_type = image",
        "hips_frame = equatorial",
        f"hips_order = {max_order}",
        f"hips_tile_width = {TILE_WIDTH}",
        "hips_tile_format = png",
        f"hips_pixel_scale = {tile_pixel_deg(max_order):.6f}",
        "hips_initial_ra = 266.405",
        "hips_initial_dec = -28.936",
        "hips_initial_fov = 120",
        "obs_title = WHAM Sky Survey DR1 — integrated H-alpha intensity",
        "obs_collection = WHAM-SS DR1",
        "obs_regime = Optical",
        "obs_description = Wisconsin H-Alpha Mapper Sky Survey DR1, H-alpha intensity "
        "integrated -80..+80 km/s (LSR), 1-degree beams on a 0.25-degree grid; "
        "rendered as a translucent glow (asinh stretch, transparent below "
        f"{FLOOR_R} R, saturated at {CEIL_R:.0f} R).",
        f"obs_copyright = {CREDIT}",
        f"obs_copyright_url = {CREDIT_URL}",
        f"ssc_recipe_version = {RECIPE_VERSION}",
        f"ssc_source_url = {SOURCE_URL}",
        f"ssc_source_sha256 = {SOURCE_SHA256}",
        f"ssc_source_version = {source.header['VERSION']}",
        f"ssc_source_date = {source.header['DATE']}",
        f"ssc_source_object = {source.header['OBJECT']}",
    ]
    return "\n".join(lines) + "\n"


def build(source_path: Path, out_dir: Path) -> tuple[int, int]:
    """Write every tile and ``properties``; returns (tile count, bytes written)."""
    source = read_fits_image(source_path)
    verify_header(source.header)
    max_order = deepest_useful_order()
    eq_to_gal = equatorial_to_galactic_matrix()
    rgb, alpha = palette()

    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = 0
    total = 0
    for order in range(max_order + 1):
        for npix in range(hips.tiles_in_order(order)):
            png = encode_png(render_tile(source, eq_to_gal, order, npix), rgb, alpha)
            path = out_dir / hips.tile_path(order, npix, "png")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(png)
            tiles += 1
            total += len(png)
    props = properties_text(source, max_order).encode("utf-8")
    (out_dir / "properties").write_bytes(props)
    total += len(props)
    return tiles, total


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=None,
        help=f"an already-downloaded copy of the WHAM FITS (default: fetch into {CACHE_FILE})",
    )
    parser.add_argument("--out", type=Path, default=OUT_DIR, help=f"HiPS root (default: {OUT_DIR})")
    args = parser.parse_args(argv)

    source_path = obtain_source(args.source)
    print(f"source {source_path}")
    print(f"sha256 {SOURCE_SHA256} (verified)")
    max_order = deepest_useful_order()
    print(
        f"orders 0..{max_order}: tile pixel {tile_pixel_deg(max_order) * 60:.2f}' < "
        f"{SOURCE_PIXEL_DEG * 60 / 4:.2f}' (a quarter of the {SOURCE_PIXEL_DEG * 60:.0f}' source pixel)"
    )
    tiles, total = build(source_path, args.out)
    print(
        f"wrote {tiles} tiles + properties, {total} bytes ({total / 1e6:.2f} MB) under {args.out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
