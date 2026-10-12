"""Backend detection: the fix for the invariant-7 gap this file's name refers to.

On a Raspberry Pi running as a headless ``systemd`` *system* service (no user
session bus), ``keyring.get_keyring()`` resolves to the null
``keyring.backends.fail.Keyring`` and every operation would raise
``NoKeyringError`` — but only the first time something actually reads a
secret, hours into an unattended night. :func:`probe_backend` is the fix:
detect it once, at startup, and say so.

A second, worse bug hid behind the first fix and only showed on real hardware
(a Pi 5, gnome-keyring installed but never unlocked — the ordinary state of a
headless box with lingering enabled): bounding the *await* with
``asyncio.wait_for`` around a bare ``asyncio.to_thread`` does not stop the
call still running inside that thread, and the default executor's worker
threads are not daemon threads, so the interpreter refuses to exit at
shutdown. The coroutine returned on time; the process never did. The
in-process tests below only ever checked the coroutine's return value and
would have passed against that bug — the subprocess-based tests further down
assert the thing that actually broke: that the *process* exits.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Iterator

import keyring
import keyring.backend
import keyring.backends.fail
import keyring.errors
import pytest

from ssc.settings.secrets import DEFAULT_PROBE_TIMEOUT_S, probe_backend


@pytest.fixture
def restore_keyring() -> Iterator[None]:
    """Whatever a test installs, put the real one back afterwards."""
    previous = keyring.get_keyring()
    try:
        yield
    finally:
        keyring.set_keyring(previous)


class _WorkingBackend(keyring.backend.KeyringBackend):
    priority = 1.0

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        del self._data[(service, username)]


class _HangingBackend(keyring.backend.KeyringBackend):
    """Simulates a D-Bus call with no daemon listening: it never returns."""

    priority = 1.0

    def get_password(self, service: str, username: str) -> str | None:
        raise AssertionError("get_password should not be reached before set_password blocks")

    def set_password(self, service: str, username: str, password: str) -> None:
        # Long enough to prove the 0.2s timeout below actually bounds the
        # wait, short enough that the leaked background thread (asyncio's
        # ``to_thread`` cannot cancel a running one) does not stall the test
        # process at interpreter shutdown.
        time.sleep(1.5)

    def delete_password(self, service: str, username: str) -> None:
        raise AssertionError("delete_password should not be reached")


class _BrokenBackend(keyring.backend.KeyringBackend):
    """Selected, but every call fails — a locked login keyring, say."""

    priority = 1.0

    def get_password(self, service: str, username: str) -> str | None:
        raise keyring.errors.KeyringLocked("locked")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise keyring.errors.KeyringLocked("locked")

    def delete_password(self, service: str, username: str) -> None:
        raise keyring.errors.KeyringLocked("locked")


class _NativeBackendError(Exception):
    """A platform adapter error outside keyring's documented hierarchy."""


class _MissingLogonSessionBackend(_BrokenBackend):
    """Windows Credential Manager outside an interactive logon session."""

    def set_password(self, service: str, username: str, password: str) -> None:
        raise _NativeBackendError("A specified logon session does not exist")


async def test_the_null_backend_is_reported_unavailable_with_a_reason(
    restore_keyring: None,
) -> None:
    """The exact headless-systemd-service reproduction: no session bus at all."""
    keyring.set_keyring(keyring.backends.fail.Keyring())
    status = await probe_backend()
    assert status.available is False
    assert status.backend == "keyring.backends.fail.Keyring"
    assert status.reason is not None
    assert "no OS keyring backend is available" in status.reason
    # The operator is told what to actually do about it, not left guessing.
    assert "systemd --user" in status.reason
    assert "keyrings.alt" in status.reason


async def test_a_working_backend_reports_available(restore_keyring: None) -> None:
    keyring.set_keyring(_WorkingBackend())
    status = await probe_backend()
    assert status.available is True
    assert status.reason is None
    assert status.backend.endswith("_WorkingBackend")


async def test_a_hanging_backend_is_bounded_not_left_to_hang_startup(
    restore_keyring: None,
) -> None:
    keyring.set_keyring(_HangingBackend())
    started = time.monotonic()
    status = await probe_backend(timeout_s=0.2)
    elapsed = time.monotonic() - started
    assert status.available is False
    assert elapsed < 5.0, "probe_backend must not block on a hung round trip"
    assert status.reason is not None
    assert "did not answer within" in status.reason


async def test_a_backend_that_cannot_complete_a_round_trip_is_unavailable(
    restore_keyring: None,
) -> None:
    keyring.set_keyring(_BrokenBackend())
    status = await probe_backend()
    assert status.available is False
    assert status.reason is not None
    assert "could not complete a round trip" in status.reason


async def test_a_windows_backend_without_a_logon_session_is_unavailable(
    restore_keyring: None,
) -> None:
    keyring.set_keyring(_MissingLogonSessionBackend())
    status = await probe_backend()
    assert status.available is False
    assert status.reason is not None
    assert "specified logon session does not exist" in status.reason


async def test_the_default_timeout_is_short_enough_to_never_hang_a_check_config_run() -> None:
    # Not "instant", but a bind check or a --check-config run must not appear
    # to hang for tens of seconds waiting on a dead D-Bus socket.
    assert DEFAULT_PROBE_TIMEOUT_S <= 5.0


# --------------------------------------------------------------------------- #
# Process-exit regression tests: a backend that never returns at all.
#
# These run in a real subprocess, on purpose. A same-process ``asyncio``-level
# check can only ever observe that the *coroutine* returned in time — it
# cannot observe a stranded non-daemon thread refusing to let the interpreter
# exit, because pytest's own process keeps running regardless. Only "does the
# child process actually terminate" catches that class of bug, which is
# exactly the one that got through the first time.
# --------------------------------------------------------------------------- #

#: Generous relative to the 0.2-0.3s probe timeouts used below: this is the
#: ceiling for the *whole subprocess* (import, probe, bounded wait, process
#: teardown), not the probe itself. If the fix regresses to the bare
#: ``asyncio.wait_for``/``asyncio.to_thread`` pattern, the child hangs forever
#: and this bound turns that into a normal, loud test failure instead of a
#: wedged CI job.
_SUBPROCESS_TIMEOUT_S = 20.0

_NEVER_RETURNS_BACKEND = '''
import keyring
import keyring.backend
import threading


class NeverReturnsBackend(keyring.backend.KeyringBackend):
    """The real hazard, faithfully reproduced: a call that genuinely never
    comes back, the same shape as a D-Bus round trip against gnome-keyring
    that is installed but was never unlocked."""

    priority = 1.0

    def get_password(self, service, username):
        raise AssertionError("get_password should not be reached")

    def set_password(self, service, username, password):
        threading.Event().wait()  # blocks forever; no timeout at all

    def delete_password(self, service, username):
        raise AssertionError("delete_password should not be reached")


keyring.set_keyring(NeverReturnsBackend())
'''


def _run_child(script: str) -> subprocess.CompletedProcess[str]:
    """Run ``script`` as a fresh interpreter, killing it if it outlives the bound.

    A raised ``subprocess.TimeoutExpired`` here *is* the failure this test
    exists to catch — a child that outlives ``_SUBPROCESS_TIMEOUT_S`` is
    exactly the "process never exits" bug, not a flake to retry past.
    """
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=_SUBPROCESS_TIMEOUT_S,
    )


def test_probe_backend_lets_the_process_exit_even_when_the_round_trip_never_returns() -> None:
    script = (
        _NEVER_RETURNS_BACKEND
        + """
import asyncio
from ssc.settings.secrets import probe_backend

async def main():
    status = await probe_backend(timeout_s=0.2)
    print(f"available={status.available}")

asyncio.run(main())
print("PROCESS_REACHED_EXIT")
"""
    )
    result = _run_child(script)
    assert result.returncode == 0, result.stderr
    assert "available=False" in result.stdout
    assert "PROCESS_REACHED_EXIT" in result.stdout, (
        "the coroutine returning is not enough — the process must actually exit"
    )


def test_check_config_exits_even_when_the_secrets_round_trip_never_returns(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """End-to-end: the exact operator-facing command that must never hang."""
    data_dir = tmp_path_factory.mktemp("ssc-check-config")
    script = (
        _NEVER_RETURNS_BACKEND
        + f"""
import sys
from ssc.main import run

sys.exit(run(["--host", "127.0.0.1", "--data-dir", {str(data_dir)!r}, "--no-mdns", "--check-config"]))
"""
    )
    result = _run_child(script)
    assert result.returncode == 0, result.stderr
    assert "secrets     UNAVAILABLE" in result.stdout


def test_secret_store_set_lets_the_process_exit_even_when_the_keyring_never_returns() -> None:
    """The coordinator's explicit re-check: get/set/delete share the same
    bounded-daemon-thread path as the probe, not the bare ``to_thread`` one.
    ``NeverReturnsBackend.set_password`` is the one that hangs; ``get`` and
    ``delete`` raise ``AssertionError`` immediately by design above, so
    ``set`` is the one that actually exercises the bound."""
    script = (
        _NEVER_RETURNS_BACKEND
        + """
import asyncio
from ssc.settings.secrets import SecretError, SecretStore

async def main():
    store = SecretStore()
    try:
        await store.set("owm", "k3y")
    except SecretError as exc:
        print(f"raised={exc}")

asyncio.run(main())
print("PROCESS_REACHED_EXIT")
"""
    )
    result = _run_child(script)
    assert result.returncode == 0, result.stderr
    assert "raised=" in result.stdout
    assert "PROCESS_REACHED_EXIT" in result.stdout
