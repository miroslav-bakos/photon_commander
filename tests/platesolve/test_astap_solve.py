# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Invocation, against a fake astap_cli emitting the real ``.ini`` shape
(``ssc/platesolve/astap.py``'s module docstring: read live from Han Kleijn's
own source on 2026-09-10, not from memory) — success, no-solution, a hang,
malformed output, and the two reactive database exit codes."""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ssc.astro.frames import MountFrame, j2000_coord, to_mount_frame
from ssc.astro.units import Deg
from ssc.platesolve import astap
from ssc.platesolve.base import (
    InvalidPlateSolveInputError,
    MalformedSolverOutputError,
    PlateSolveError,
    SolveFailedError,
    SolveIssue,
    SolverUnavailableError,
    SolveTimedOutError,
)

_SUCCESS_SCRIPT = """
    import sys
    argv = sys.argv[1:]
    out_base = argv[argv.index("-o") + 1]
    with open(out_base + ".ini", "w") as f:
        f.write("PLTSOLVD=T\\n")
        f.write("CRPIX1=4.0000000000000E+000\\n")
        f.write("CRPIX2=4.0000000000000E+000\\n")
        f.write("CRVAL1=8.3822000000000E+001\\n")
        f.write("CRVAL2=-5.3910000000000E+000\\n")
        f.write("CDELT1=1.0000000000000E-003\\n")
        f.write("CDELT2=1.0000000000000E-003\\n")
        f.write("CROTA1=0.0000000000000E+000\\n")
        f.write("CROTA2=1.5000000000000E+000\\n")
        f.write("CD1_1=1.0000000000000E-003\\n")
        f.write("CD1_2=0.0000000000000E+000\\n")
        f.write("CD2_1=0.0000000000000E+000\\n")
        f.write("CD2_2=1.0000000000000E-003\\n")
        f.write("CMDLINE=" + " ".join(argv) + "\\n")
    sys.exit(0)
"""

_NOT_ENOUGH_STARS_SCRIPT = """
    import sys
    argv = sys.argv[1:]
    out_base = argv[argv.index("-o") + 1]
    with open(out_base + ".ini", "w") as f:
        f.write("PLTSOLVD=F\\n")
        f.write("CMDLINE=" + " ".join(argv) + "\\n")
        f.write("ERROR=Not enough stars.\\n")
    sys.exit(2)
"""

_HANGS_SCRIPT = """
    import sys, time
    time.sleep(30)
    sys.exit(0)
"""

_MALFORMED_SCRIPT = """
    import sys
    argv = sys.argv[1:]
    out_base = argv[argv.index("-o") + 1]
    with open(out_base + ".ini", "w") as f:
        f.write("PLTSOLVD=T\\n")
        f.write("CRVAL1=not-a-number\\n")
    sys.exit(0)
"""

_IMAGE_UNREADABLE_SCRIPT = """
    import sys
    argv = sys.argv[1:]
    out_base = argv[argv.index("-o") + 1]
    with open(out_base + ".ini", "w") as f:
        f.write("PLTSOLVD=F\\n")
        f.write("ERROR=Error reading image file.\\n")
    sys.exit(16)
"""

_NO_DATABASE_SCRIPT = """
    import sys
    argv = sys.argv[1:]
    out_base = argv[argv.index("-o") + 1]
    with open(out_base + ".ini", "w") as f:
        f.write("PLTSOLVD=F\\n")
        f.write("ERROR=No star database found.\\n")
    sys.exit(32)
"""

_UNEXPECTED_EXIT_SCRIPT = """
    import sys
    print("garbled internal error", file=sys.stderr)
    sys.exit(255)
"""


async def test_solve_success_returns_j2000_result(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_SUCCESS_SCRIPT)
    fits_path = fits_factory(date_obs=datetime(2026, 9, 10, 21, 15, 0, tzinfo=UTC))

    result = await astap.astap_solve(fits_path, cli_path=binary, solver_version="CLI-0.9.581-stack")

    assert result.coord.frame is MountFrame.J2000
    assert result.coord == j2000_coord(Deg(83.822), Deg(-5.391))
    assert result.date_obs == datetime(2026, 9, 10, 21, 15, 0, tzinfo=UTC)
    assert result.pixel_scale_arcsec == pytest.approx(3.6)
    assert result.rotation_deg == pytest.approx(1.5)
    assert result.solver == "astap"
    assert result.solver_version == "CLI-0.9.581-stack"


async def test_solve_passes_discovered_opt_database(
    fake_astap, fits_factory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    opt_astap = tmp_path / "opt" / "astap"
    opt_astap.mkdir(parents=True)
    (opt_astap / "0101.1476").write_bytes(b"")
    monkeypatch.setattr(astap, "_OPT_DATABASE_DIR", opt_astap)
    script = f"""
import sys
argv = sys.argv[1:]
if "-d" not in argv or argv[argv.index("-d") + 1] != {str(opt_astap)!r}:
    sys.exit(55)
out_base = argv[argv.index("-o") + 1]
with open(out_base + ".ini", "w") as f:
    f.write("PLTSOLVD=T\\nCRVAL1=83.822\\nCRVAL2=-5.391\\n")
    f.write("CDELT1=0.001\\nCDELT2=0.001\\nCROTA2=1.5\\n")
sys.exit(0)
"""
    binary = fake_astap(script)

    result = await astap.astap_solve(fits_factory(), cli_path=binary, env={})

    assert result.coord == j2000_coord(Deg(83.822), Deg(-5.391))


async def test_solve_date_obs_is_the_exposure_not_the_solve_time(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    """Requirement 4: the result carries the frame's own DATE-OBS, not
    ``datetime.now()`` at solve completion."""
    binary = fake_astap(_SUCCESS_SCRIPT)
    exposed_at = datetime(2020, 1, 1, 0, 0, 0, tzinfo=UTC)  # far from "now"
    fits_path = fits_factory(date_obs=exposed_at)

    result = await astap.astap_solve(fits_path, cli_path=binary)

    assert result.date_obs == exposed_at


async def test_a_mislabelled_result_is_refused_by_the_frame_boundary(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    """Requirement 3: a caller cannot mistake J2000 for the mount's frame.
    Simulates the exact mistake the seam exists to prevent — carrying a
    solved position under the mount's topocentric tag — and shows
    ``ssc.astro.frames`` refuses to convert it further."""
    binary = fake_astap(_SUCCESS_SCRIPT)
    fits_path = fits_factory()
    result = await astap.astap_solve(fits_path, cli_path=binary)

    mislabelled = dataclasses.replace(result.coord, frame=MountFrame.TOPOCENTRIC)
    with pytest.raises(ValueError, match="J2000"):
        to_mount_frame(
            ephemeris=object(),  # type: ignore[arg-type]
            site=object(),  # type: ignore[arg-type]
            coord=mislabelled,
            when=datetime(2026, 9, 10, tzinfo=UTC),
            mount_frame=MountFrame.TOPOCENTRIC,
        )
    # The correctly tagged result does not trip that same guard.
    assert result.coord.frame is MountFrame.J2000


async def test_no_solution_raises_solve_failed(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_NOT_ENOUGH_STARS_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(SolveFailedError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.NOT_SOLVED
    assert "star" in str(excinfo.value).lower()


async def test_hang_times_out_instead_of_blocking_forever(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_HANGS_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(SolveTimedOutError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary, timeout_s=0.2)
    assert excinfo.value.issue is SolveIssue.TIMED_OUT


async def test_malformed_output_is_refused_not_parsed_optimistically(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_MALFORMED_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(MalformedSolverOutputError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.MALFORMED_OUTPUT


async def test_unreadable_image_raises_invalid_input(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_IMAGE_UNREADABLE_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(InvalidPlateSolveInputError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.INVALID_INPUT


async def test_reactive_missing_database_raises_solver_unavailable(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    """Exit 32 can happen even after a prior availability check passed — a
    database deleted between the check and the solve. Caught here too, not
    just in ``astap_availability``."""
    binary = fake_astap(_NO_DATABASE_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(SolverUnavailableError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.DATABASE_UNAVAILABLE


async def test_unrecognised_exit_code_is_refused_with_detail(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_UNEXPECTED_EXIT_SCRIPT)
    fits_path = fits_factory()

    with pytest.raises(PlateSolveError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.INVOCATION_FAILED
    assert "255" in str(excinfo.value)


async def test_missing_date_obs_is_refused_before_invoking_the_solver(
    fake_astap, fits_factory, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    """A frame with no DATE-OBS cannot become a timestamped measurement — a
    solve is never even attempted."""
    import numpy as np

    from ssc.capture.fits import write_fits_bytes
    from tests.platesolve.conftest import _frame_metadata

    metadata = _frame_metadata(date_obs=None)
    payload = write_fits_bytes(np.zeros((8, 8), dtype=np.uint16), metadata)
    fits_path = tmp_path / "no-date-obs.fits"
    fits_path.write_bytes(payload)

    binary = fake_astap(_SUCCESS_SCRIPT)
    with pytest.raises(InvalidPlateSolveInputError) as excinfo:
        await astap.astap_solve(fits_path, cli_path=binary)
    assert excinfo.value.issue is SolveIssue.INVALID_INPUT
    assert "DATE-OBS" in str(excinfo.value)


async def test_naive_date_obs_is_read_as_utc(fake_astap, fits_factory) -> None:  # type: ignore[no-untyped-def]
    """A DATE-OBS with no timezone means UTC — FITS 4.0 section 4.4.2.1 —
    and writers omit the zone rather than spell out "+00:00".

    This is a regression test for a live failure on 2026-09-10: the seam
    refused every one of the owner's own Seestar frames as "not timezone-
    aware", which would have made every plate solve fail in the field. No
    test caught it because every fixture here wrote an aware timestamp,
    which no real camera does.
    """
    binary = fake_astap(_SUCCESS_SCRIPT)
    fits_path = fits_factory(date_obs=datetime(2026, 9, 10, 21, 15, 0, tzinfo=UTC))

    raw = fits_path.read_bytes()
    aware = b"'2026-09-10T21:15:00+00:00'"
    naive = b"'2026-09-10T21:15:00'"
    assert aware in raw, "fixture no longer writes an offset; update this test"
    fits_path.write_bytes(raw.replace(aware, naive + b" " * (len(aware) - len(naive))))

    result = await astap.astap_solve(fits_path, cli_path=binary)

    assert result.date_obs == datetime(2026, 9, 10, 21, 15, 0, tzinfo=UTC)


async def test_solver_hints_use_hours_south_pole_distance_and_image_height(fake_astap, fits_factory) -> None:
    script = _SUCCESS_SCRIPT.replace("argv = sys.argv[1:]", """argv = sys.argv[1:]
    assert float(argv[argv.index('-ra') + 1]) == 23.5
    assert float(argv[argv.index('-spd') + 1]) == 60
    assert float(argv[argv.index('-fov') + 1]) == 2.1
    assert '-r' not in argv
    """)
    result = await astap.astap_solve(fits_factory(), cli_path=fake_astap(script),
                                    position_hint=(23.5, -30), field_height_deg=2.1)
    assert result.coord.frame is MountFrame.J2000
