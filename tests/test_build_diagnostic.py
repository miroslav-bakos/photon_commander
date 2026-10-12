"""Diagnostic compiler checks must never leak into default release flags."""
import pytest

from scripts.build_nuitka import nuitka_command


def test_diagnostic_mode_is_opt_in(monkeypatch) -> None:
    monkeypatch.delenv("SSC_BUILD_DIAGNOSTIC", raising=False)
    assert "--debug" not in nuitka_command()
    monkeypatch.setenv("SSC_BUILD_DIAGNOSTIC", "1")
    flags = nuitka_command()
    assert "--debug" in flags
    assert "--python-debug" not in flags
    assert "--no-debug-c-warnings" in flags
    assert "--no-debug-immortal-assumptions" not in flags


def test_unknown_diagnostic_mode_fails_closed(monkeypatch) -> None:
    monkeypatch.setenv("SSC_BUILD_DIAGNOSTIC", "yes")
    with pytest.raises(SystemExit):
        nuitka_command()
