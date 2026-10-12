# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Fixtures shared by the platesolve tests: a real minimal FITS (written with
``ssc.capture.fits``, the same writer a real capture uses) and a helper that
builds a fake ``astap_cli`` — a small Python script standing in for the real
binary, emitting astap_cli's own ``-h`` banner and ``.ini`` output shapes
(``ssc/platesolve/astap.py``'s module docstring records exactly where those
shapes were read from)."""

from __future__ import annotations

import stat
import textwrap
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from ssc.capture.fits import write_fits_bytes
from ssc.capture.frame import FrameMetadata, FrameType

FakeAstapBuilder = Callable[[str], Path]


def _frame_metadata(**overrides: object) -> FrameMetadata:
    defaults: dict[str, object] = {
        "date_obs": datetime(2026, 9, 10, 21, 15, 0, tzinfo=UTC),
        "exposure_s": 10.0,
        "gain": 100,
        "ccd_temp_c": 12.3,
        "bayer_offset_x": 1,
        "bayer_offset_y": 0,
        "pixel_size_um": 2.9,
        "ra_deg": 83.822,
        "dec_deg": -5.391,
        "site_lat_deg": 51.5,
        "site_lon_deg": -0.1,
        "filter_name": None,
        "object_name": "M42",
        "frame_type": FrameType.LIGHT,
        "focal_length_mm": 160.0,
        "telescope_name": "Seestar S30 Pro",
        "ssc_version": "test",
        "device_firmware": "9.31",
    }
    defaults.update(overrides)
    return FrameMetadata(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def fits_factory(tmp_path: Path) -> Callable[..., Path]:
    """Write a small real FITS file — a genuine ``ssc.capture.fits`` output,
    the shape a captured frame actually is — and return its path."""

    counter = iter(range(10_000))

    def make(**metadata_overrides: object) -> Path:
        pixels = np.zeros((8, 8), dtype=np.uint16)
        metadata = _frame_metadata(**metadata_overrides)
        payload = write_fits_bytes(pixels, metadata)
        path = tmp_path / f"frame_{next(counter)}.fits"
        path.write_bytes(payload)
        return path

    return make


@pytest.fixture
def fake_astap(tmp_path: Path) -> FakeAstapBuilder:
    """Build an executable standing in for ``astap_cli`` from a Python
    script body. The body sees ``argv`` (its own ``sys.argv[1:]``) and must
    itself decide what to print/write/exit with — each test supplies the
    body for the exact astap_cli behaviour it is exercising."""

    def build(script_body: str) -> Path:
        script_dir = tmp_path / "bin"
        script_dir.mkdir(exist_ok=True)
        script_path = script_dir / "astap_cli"
        script_path.write_text(
            "#!/usr/bin/env python3\n" + textwrap.dedent(script_body), encoding="ascii"
        )
        mode = script_path.stat().st_mode
        script_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return script_path

    return build


@pytest.fixture
def empty_path_env() -> dict[str, str]:
    """An environment with no PATH entries that could accidentally contain a
    real astap_cli — the "nothing installed" state this repo is actually in
    today (THIRD_PARTY_NOTICES.md's gate: astap/astap_cli/solve-field all
    absent)."""
    return {"PATH": ""}
