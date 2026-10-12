#!/usr/bin/env python
"""Generate Photon Commander's opaque application and favicon PNG set.

Run from ``web/`` with ``uv run --with pillow python scripts/generate-icons.py``.
The simple photon-star and orbital targeting ring mirror the canonical SVG mark
in ``src/assets/brand/app-icon.svg``. Installed icons keep the complete mark
inside launcher-safe padding while favicons use the larger mark needed at 16px.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).resolve().parent.parent / "public" / "icons"
BRAND_DIR = Path(__file__).resolve().parent.parent / "src" / "assets" / "brand"
MASTER = 1024
BG = "#06112f"
CYAN = "#35cfee"
CYAN_LIGHT = "#8af5ff"
WHITE = "#f8fdff"
INSTALLED_ARTWORK_SCALE = 0.76
# A 56% square fits inside the maskable specification's central 80%-diameter
# safe circle even at its corners: 0.56 * sqrt(2) < 0.80.
MASKABLE_ARTWORK_SCALE = 0.56


def draw_star(draw: ImageDraw.ImageDraw, cx: float, cy: float, radius: float) -> None:
    """Four-point photon sparkle that survives favicon downsampling."""
    points: list[tuple[float, float]] = []
    for index in range(8):
        angle = math.pi / 4 * index
        length = radius if index % 2 == 0 else radius * 0.22
        points.append((cx + length * math.cos(angle), cy + length * math.sin(angle)))
    draw.polygon(points, fill=WHITE)


def render_background() -> Image.Image:
    return Image.new("RGB", (MASTER, MASTER), BG)


def render_artwork() -> Image.Image:
    image = Image.new("RGBA", (MASTER, MASTER), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    ring_box = (218, 218, 806, 806)
    draw.arc(ring_box, 8, 286, fill=CYAN, width=54)
    draw.arc(ring_box, 326, 368, fill=CYAN, width=54)
    draw.ellipse((676, 260, 760, 344), fill=CYAN_LIGHT)
    draw_star(draw, 512, 512, 260)
    draw.ellipse((484, 484, 540, 540), fill=CYAN)
    return image


def render_master(artwork_scale: float = 1.0) -> Image.Image:
    image = render_background()
    artwork = render_artwork()
    if artwork_scale != 1.0:
        size = round(MASTER * artwork_scale)
        artwork = artwork.resize((size, size), Image.Resampling.LANCZOS)
        inset = (MASTER - size) // 2
        image.paste(artwork, (inset, inset), artwork)
    else:
        image.paste(artwork, (0, 0), artwork)
    return image


def save_all() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    favicon_master = render_master()
    installed_master = render_master(INSTALLED_ARTWORK_SCALE)
    maskable_master = render_master(MASKABLE_ARTWORK_SCALE)

    def resized(image: Image.Image, size: int) -> Image.Image:
        return image.resize((size, size), Image.Resampling.LANCZOS)

    for size in (192, 512):
        resized(installed_master, size).save(OUT_DIR / f"icon-{size}.png")
        resized(maskable_master, size).save(OUT_DIR / f"icon-maskable-{size}.png")
        resized(installed_master, size).save(BRAND_DIR / f"app-icon-{size}.png")
    resized(installed_master, 180).convert("RGB").save(OUT_DIR / "apple-touch-icon.png")
    resized(installed_master, 180).convert("RGB").save(BRAND_DIR / "apple-touch-icon-180.png")
    installed_master.save(BRAND_DIR / "app-icon-1024.png")
    for size in (16, 32, 48):
        resized(favicon_master, size).save(OUT_DIR / f"favicon-{size}.png")
        resized(favicon_master, size).save(BRAND_DIR / f"favicon-{size}.png")
    print(f"Wrote icons to {OUT_DIR}")


if __name__ == "__main__":
    save_all()
