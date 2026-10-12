# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from ssc.api import capture_orchestrator


def test_colour_capture_preview_balances_rgb_before_autostretch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mosaic = np.arange(16, dtype=np.uint16).reshape(4, 4)
    rgb = np.stack((mosaic, mosaic + 100, mosaic + 200), axis=-1).astype(np.float64)
    balanced = rgb + 42
    calls: list[str] = []

    monkeypatch.setattr(
        capture_orchestrator,
        "read_fits_image",
        lambda _raw: SimpleNamespace(data=mosaic, header={"BAYERPAT": "GRBG"}),
    )
    monkeypatch.setattr(capture_orchestrator, "debayer", lambda _plane, _pattern: rgb)

    def balance(pixels: np.ndarray) -> np.ndarray:
        calls.append("balance")
        np.testing.assert_array_equal(pixels, rgb)
        return balanced

    def stretch(pixels: np.ndarray) -> np.ndarray:
        calls.append("stretch")
        np.testing.assert_array_equal(pixels, balanced)
        return np.zeros((4, 4, 3), dtype=np.uint8)

    monkeypatch.setattr(capture_orchestrator, "balance_rgb_for_display", balance)
    monkeypatch.setattr(capture_orchestrator, "nina_autostretch", stretch)

    rendered = capture_orchestrator._render_frame_png(b"fits", thumbnail=False)

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert calls == ["balance", "stretch"]


def test_rgb_fits_archive_preview_keeps_three_channels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planes = np.stack([np.full((4, 4), value) for value in (10, 20, 30)])
    monkeypatch.setattr(
        capture_orchestrator,
        "read_fits_image",
        lambda _raw: SimpleNamespace(data=planes, header={}),
    )
    seen: list[np.ndarray] = []

    def balance(rgb: np.ndarray) -> np.ndarray:
        seen.append(rgb.copy())
        return rgb

    monkeypatch.setattr(capture_orchestrator, "balance_rgb_for_display", balance)
    rendered = capture_orchestrator._render_frame_png(b"fits", thumbnail=False)

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    np.testing.assert_array_equal(seen[0][0, 0], [10, 20, 30])


def test_large_bayer_thumbnail_reduces_mosaic_before_colour_render(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mosaic = np.arange(640 * 640, dtype=np.uint16).reshape(640, 640)
    monkeypatch.setattr(
        capture_orchestrator,
        "read_fits_image",
        lambda _raw: SimpleNamespace(data=mosaic, header={"BAYERPAT": "GRBG"}),
    )
    monkeypatch.setattr(
        capture_orchestrator,
        "debayer",
        lambda *_args: pytest.fail("thumbnail must not debayer the full sensor plane"),
    )
    seen: list[np.ndarray] = []

    def balance(rgb: np.ndarray) -> np.ndarray:
        seen.append(rgb.copy())
        return rgb

    monkeypatch.setattr(capture_orchestrator, "balance_rgb_for_display", balance)

    rendered = capture_orchestrator._render_frame_png(b"fits", thumbnail=True)

    assert rendered.startswith(b"\x89PNG\r\n\x1a\n")
    assert seen[0].shape == (320, 320, 3)
    np.testing.assert_allclose(
        seen[0][0, 0],
        [float(mosaic[0, 1]), (float(mosaic[0, 0]) + float(mosaic[1, 1])) / 2, float(mosaic[1, 0])],
    )


def test_rgb_fits_archive_quality_uses_green_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planes = np.stack([np.full((4, 4), value) for value in (10, 20, 30)])
    monkeypatch.setattr(
        capture_orchestrator,
        "read_fits_image",
        lambda _raw: SimpleNamespace(data=planes, header={}),
    )
    seen: list[np.ndarray] = []

    def measure(plane: np.ndarray, **_kwargs: object) -> object:
        seen.append(plane.copy())
        return object()

    monkeypatch.setattr(capture_orchestrator, "measure_frame_quality", measure)
    capture_orchestrator._measure_frame_quality(b"fits")

    np.testing.assert_array_equal(seen[0], planes[1])
