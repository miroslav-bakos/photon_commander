# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Detection: three distinguishable "not available" reasons, and the
"available" path — none of them ever raise (CLAUDE.md invariant 7: unknown
capability is surfaced, never papered over, never a bare exception)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ssc.platesolve import astap
from ssc.platesolve.base import SolveIssue

_GOOD_BANNER_SCRIPT = """
    import sys
    if "-h" in sys.argv[1:]:
        print("ASTAP astrometric solver, CLI-0.9.581-stack dated 20260101")
        print("(C) 2018, 2021 by Han Kleijn. License LGPL3+, Webpage: www.hnsky.org")
        sys.exit(0)
    sys.exit(1)
"""

_UNRECOGNISED_BANNER_SCRIPT = """
    import sys
    print("some other program, not ASTAP")
    sys.exit(0)
"""

_HANGS_ON_PROBE_SCRIPT = """
    import sys, time
    if "-h" in sys.argv[1:]:
        time.sleep(30)
    sys.exit(0)
"""


async def test_probing_the_real_host_reports_rather_than_raises() -> None:
    """No env override at all — the probe runs against whatever this host
    actually has. It must never raise, and its answer must be internally
    coherent whether or not ASTAP is installed. This deliberately does NOT
    assert that the host lacks a solver: it asserted exactly that until
    2026-09-10, when ASTAP CLI-2026.09.01 was installed here and the test
    began failing for a reason that had nothing to do with the code."""
    availability = await astap.astap_availability()
    assert availability.reason or availability.available
    if availability.available:
        assert availability.issue is None
        assert availability.version
    else:
        assert availability.issue is not None
        assert availability.reason
        assert "astap" in availability.reason.lower()


async def test_no_solver_installed_is_reported_hermetically(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same outcome, but isolated from whatever happens to be on this
    particular machine or in its fallback install directories — so this
    test still means something once ASTAP is eventually installed here."""
    monkeypatch.setattr(astap, "_FALLBACK_DIRS", ())
    availability = await astap.astap_availability(env={"PATH": ""})
    assert availability.available is False
    assert availability.issue is SolveIssue.SOLVER_NOT_FOUND
    assert availability.version is None


async def test_invalid_override_path_names_the_env_var(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    availability = await astap.astap_availability(env={"SSC_ASTAP_CLI": str(missing)})
    assert availability.available is False
    assert availability.issue is SolveIssue.SOLVER_NOT_FOUND
    assert availability.reason is not None
    assert "SSC_ASTAP_CLI" in availability.reason


@pytest.mark.parametrize("key", ["PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"])
def test_windows_uppercase_install_root_is_discovered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    monkeypatch.setattr(astap, "_FALLBACK_DIRS", ())
    binary = tmp_path / "astap" / "astap.exe"
    binary.parent.mkdir()
    binary.write_bytes(b"fixture executable")
    binary.chmod(0o755)
    env = {"PATH": "", key: str(tmp_path)}
    assert astap.resolve_astap_cli(env) == binary
    # An explicit invalid override must not silently select an installed solver.
    assert astap.resolve_astap_cli({**env, "SSC_ASTAP_CLI": str(tmp_path / "missing")}) is None


async def test_unrecognised_binary_is_unsupported_version(fake_astap) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_UNRECOGNISED_BANNER_SCRIPT)
    availability = await astap.astap_availability(env={"SSC_ASTAP_CLI": str(binary)})
    assert availability.available is False
    assert availability.issue is SolveIssue.UNSUPPORTED_VERSION
    assert availability.version is None


async def test_hung_version_probe_is_unsupported_version_not_a_hang(fake_astap) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_HANGS_ON_PROBE_SCRIPT)
    availability = await astap.astap_availability(
        env={"SSC_ASTAP_CLI": str(binary)}, version_probe_timeout_s=0.2
    )
    assert availability.available is False
    assert availability.issue is SolveIssue.UNSUPPORTED_VERSION
    assert availability.reason is not None
    assert "0" in availability.reason  # names the timeout


async def test_recognised_binary_without_database_is_database_unavailable(fake_astap) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_GOOD_BANNER_SCRIPT)
    availability = await astap.astap_availability(env={"SSC_ASTAP_CLI": str(binary)})
    assert availability.available is False
    assert availability.issue is SolveIssue.DATABASE_UNAVAILABLE
    assert availability.version == "CLI-0.9.581-stack"
    assert availability.reason is not None
    assert "database" in availability.reason.lower()


async def test_recognised_binary_with_database_is_available(fake_astap) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_GOOD_BANNER_SCRIPT)
    (binary.parent / "0101.1476").write_bytes(b"")
    availability = await astap.astap_availability(env={"SSC_ASTAP_CLI": str(binary)})
    assert availability.available is True
    assert availability.issue is None
    assert availability.reason is None
    assert availability.version == "CLI-0.9.581-stack"


async def test_database_dir_override_is_respected(fake_astap, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_GOOD_BANNER_SCRIPT)
    data_dir = tmp_path / "elsewhere"
    data_dir.mkdir()
    (data_dir / "0101.1476").write_bytes(b"")
    availability = await astap.astap_availability(
        env={"SSC_ASTAP_CLI": str(binary), "SSC_ASTAP_DATA_DIR": str(data_dir)}
    )
    assert availability.available is True


async def test_raspberry_pi_d20_package_in_opt_is_discovered(
    fake_astap, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    binary = fake_astap(_GOOD_BANNER_SCRIPT)
    opt_astap = tmp_path / "opt" / "astap"
    opt_astap.mkdir(parents=True)
    (opt_astap / "0101.1476").write_bytes(b"")
    monkeypatch.setattr(astap, "_OPT_DATABASE_DIR", opt_astap)

    availability = await astap.astap_availability(env={"SSC_ASTAP_CLI": str(binary)})

    assert availability.available is True
    assert astap.resolve_database_dir(binary, env={}) == opt_astap
