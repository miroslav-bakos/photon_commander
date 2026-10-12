# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Keyring-backed secrets. Values enter the process and never leave it.

CLAUDE.md invariant 8: API keys live in the OS keyring, never in a config file,
never in a response body, never in a log line. Two consequences implemented here:

* The store returns a value only to server-side code (a weather provider
  building a request). The API layer can ask *whether* a name is set and can
  set or delete one; it can never read one back.
* ``keyring`` is synchronous and can block for a long time — a locked login
  keyring prompts, or (the ordinary state of a headless Pi with lingering
  enabled but gnome-keyring never unlocked) silently hangs inside a D-Bus call
  that nothing ever answers. Every blocking call in this module runs in a
  ``daemon=True`` thread it owns and is bounded by a wall-clock timeout, via
  :func:`_bounded` — never bare ``asyncio.to_thread``, whose default-executor
  worker threads are *not* daemon threads and would strand the process at
  interpreter shutdown if the call inside never returns (see ``_bounded``'s
  own docstring for the failure this replaced).

``keyring`` cannot enumerate the entries of a service, so the *names* we have
stored are tracked in the config table. Names are not secret; values are.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import keyring
import keyring.backend
import keyring.backends.fail

_log = logging.getLogger(__name__)

#: The keyring service name. One namespace for the whole app.
SERVICE_NAME: Final = "ssc-seestar-commander"

#: Reserved name for the optional control PIN (DESIGN.md §9).
CONTROL_PIN_NAME: Final = "control_pin"

_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")

#: A separate service namespace so the probe's canary entry can never collide
#: with, or show up alongside, a real secret name a caller has stored.
_PROBE_SERVICE: Final = f"{SERVICE_NAME}-backend-probe"
_PROBE_NAME: Final = "probe"
_PROBE_VALUE: Final = "probe"

#: Bounds the round-trip below. A backend that needs a session D-Bus (the
#: SecretService keyring, GNOME's, KDE's) can *block* rather than fail fast
#: when no daemon answers, and a headless systemd service has no daemon —
#: this is what keeps a stuck D-Bus call from hanging startup forever.
DEFAULT_PROBE_TIMEOUT_S: Final = 2.0

#: Bounds every live get/set/delete. Longer than the startup probe — this is
#: a user-triggered operation, not a boot gate — but still bounded: architecture policy's
#: async discipline forbids an unbounded blocking call inside a handler, and an
#: unlocked-but-unresponsive keyring must not be able to hang a request (or,
#: via the same non-daemon-thread hazard :func:`_bounded` exists to close,
#: hang process shutdown) forever.
DEFAULT_OPERATION_TIMEOUT_S: Final = 5.0

#: What an operator on a headless host can actually do about it. Repeated in
#: both the state notice and ``--check-config`` so it is never left as an
#: exercise (architecture policy invariant 7). ``keyring`` never gets a silent,
#: SSC-chosen fallback (invariant 8) — the recommended fix keeps secrets in a
#: real OS keyring by giving the service account an actual session to use it
#: with, which is why it is listed first.
BACKEND_UNAVAILABLE_HELP: Final = (
    "run ssc-core as a systemd --user service under `loginctl enable-linger` "
    "with a real D-Bus session and gnome-keyring unlocked at login, "
    "or configure a file-backed `keyrings.alt` backend yourself "
    "(a keyringrc.cfg pointing at an EncryptedKeyring, its passphrase supplied "
    "via a systemd credential) if no user session is possible on this host"
)


class SecretError(Exception):
    """The OS keyring refused the operation. Never silently swallowed."""


@dataclass(frozen=True, slots=True)
class BackendStatus:
    """Whether ``keyring`` actually has somewhere to put a secret on this host.

    Detected once, at startup (see :func:`probe_backend`) — not on every
    operation, so a transiently locked keyring is not confused with "no
    backend at all".
    """

    available: bool
    #: The backend class ``keyring`` resolved to, e.g.
    #: ``keyring.backends.SecretService.Keyring``. Always set, even when
    #: unavailable, so a log line or notice can say what was actually tried.
    backend: str
    #: Why it is unusable. ``None`` iff ``available`` is ``True``.
    reason: str | None = None


def _backend_name(backend: keyring.backend.KeyringBackend) -> str:
    cls = type(backend)
    return f"{cls.__module__}.{cls.__qualname__}"


async def _bounded[T](func: Callable[[], T], *, timeout_s: float, what: str) -> T:
    """Run a blocking ``keyring`` call with a hard wall-clock bound.

    ``asyncio.wait_for`` wrapped around a bare ``asyncio.to_thread`` bounds
    only the *await*: ``to_thread`` schedules onto the default executor, whose
    worker threads are **not** daemon threads, so abandoning the await when
    the timeout fires does not stop the call still running inside that thread.
    A stuck non-daemon thread is exactly what makes the interpreter refuse to
    exit at shutdown — it joins every non-daemon thread there. A keyring that
    is installed but never unlocked (the ordinary state of a headless Pi with
    lingering enabled but no login) blocks inside its D-Bus call forever
    rather than erroring, which turned what looked like a 2-second bound into
    a process that never exits — ``--check-config`` hanging forever, and a
    stuck thread stranding graceful shutdown at runtime, both observed on real
    hardware.

    The fix: the blocking call itself runs in a thread *this function starts*
    with ``daemon=True``, so if it never returns, it can never block process
    exit either — the interpreter does not wait for daemon threads. The
    *wait* for it still goes through ``asyncio.to_thread``, but waiting on
    ``threading.Event.wait(timeout)`` is itself bounded by ``timeout_s``
    regardless of whether the event is ever set, so that pool thread always
    returns promptly and is never the one left stranded.
    """
    done = threading.Event()
    outcome: list[T | Exception] = []

    def _run() -> None:
        try:
            outcome.append(func())
        except Exception as exc:  # re-raised on the caller's side, verbatim
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=_run, daemon=True, name=f"ssc-secrets:{what}").start()
    finished = await asyncio.to_thread(done.wait, timeout_s)
    if not finished:
        _log.warning(
            "%s did not complete within %gs; abandoning it to a daemon thread so the "
            "process can still exit — it may still be blocked inside the OS keyring",
            what,
            timeout_s,
        )
        raise TimeoutError(f"{what} did not complete within {timeout_s:g}s")
    result = outcome[0]
    if isinstance(result, Exception):
        raise result
    return result


def _round_trip(backend: keyring.backend.KeyringBackend) -> None:
    """Synchronous; run off the event loop, inside :func:`_bounded`'s thread."""
    backend.set_password(_PROBE_SERVICE, _PROBE_NAME, _PROBE_VALUE)
    try:
        read_back = backend.get_password(_PROBE_SERVICE, _PROBE_NAME)
        if read_back != _PROBE_VALUE:
            raise SecretError(f"backend round-trip read back {read_back!r}, not what was written")
    finally:
        try:
            backend.delete_password(_PROBE_SERVICE, _PROBE_NAME)
        except Exception:
            # Cleanup failing does not change the verdict already reached above;
            # a leaked canary entry is a nuisance, not a reason to call a
            # backend that just answered a write and a read "unavailable".
            _log.warning("could not clean up the keyring backend probe entry")


async def probe_backend(*, timeout_s: float = DEFAULT_PROBE_TIMEOUT_S) -> BackendStatus:
    """Classify the backend ``keyring`` resolved to: usable, or not.

    Two checks, because either alone is insufficient:

    * ``keyring.get_keyring()`` returning the null ``fail.Keyring`` is the
      unambiguous "nothing is configured" signal — exactly what a systemd
      *system* service sees, with no session bus at all. Caught first and
      fast, with no I/O attempted.
    * Anything else still has to *prove* it works with one real
      set/get/delete round trip, bounded by ``timeout_s`` via :func:`_bounded`
      — a backend can be selected and still not actually answer (a locked
      login keyring, a D-Bus call with a stale ``DBUS_SESSION_BUS_ADDRESS``
      and no daemon listening on it, or one that is simply never unlocked).
    """
    backend = keyring.get_keyring()
    name = _backend_name(backend)
    if isinstance(backend, keyring.backends.fail.Keyring):
        return BackendStatus(
            available=False,
            backend=name,
            reason=f"no OS keyring backend is available on this host ({BACKEND_UNAVAILABLE_HELP})",
        )
    try:
        await _bounded(
            lambda: _round_trip(backend), timeout_s=timeout_s, what=f"{name} backend round trip"
        )
    except TimeoutError:
        return BackendStatus(
            available=False,
            backend=name,
            reason=(
                f"the {name} backend did not answer within {timeout_s:g}s "
                f"({BACKEND_UNAVAILABLE_HELP})"
            ),
        )
    except Exception as exc:
        # Backends are platform adapters and do not consistently wrap native
        # failures in KeyringError. Windows Credential Manager, for example,
        # raises win32ctypes.pywin32.pywintypes.error when no interactive
        # logon session exists. The probe boundary must classify any backend
        # exception as unavailable rather than aborting application startup.
        return BackendStatus(
            available=False,
            backend=name,
            reason=f"the {name} backend could not complete a round trip: {exc} ({BACKEND_UNAVAILABLE_HELP})",
        )
    return BackendStatus(available=True, backend=name)


def validate_name(name: str) -> str:
    if not _VALID_NAME.match(name):
        raise ValueError(
            f"secret name {name!r} must be lower-case alphanumeric with . _ -, 1-64 chars"
        )
    return name


class SecretStore:
    """Async facade over ``keyring`` for one service namespace."""

    def __init__(self, *, service: str = SERVICE_NAME) -> None:
        self._service = service

    @property
    def service(self) -> str:
        return self._service

    async def get(self, name: str) -> str | None:
        """The secret value, or ``None`` if it is not set. Server-side use only."""
        validate_name(name)
        try:
            return await _bounded(
                lambda: keyring.get_password(self._service, name),
                timeout_s=DEFAULT_OPERATION_TIMEOUT_S,
                what=f"reading secret {name!r}",
            )
        except TimeoutError as exc:
            raise SecretError(f"reading secret {name!r} from the OS keyring: {exc}") from exc
        except Exception as exc:
            raise SecretError(f"could not read secret {name!r} from the OS keyring: {exc}") from exc

    async def set(self, name: str, value: str) -> None:
        validate_name(name)
        if not value:
            raise ValueError("refusing to store an empty secret; delete it instead")
        try:
            await _bounded(
                lambda: keyring.set_password(self._service, name, value),
                timeout_s=DEFAULT_OPERATION_TIMEOUT_S,
                what=f"storing secret {name!r}",
            )
        except TimeoutError as exc:
            raise SecretError(f"storing secret {name!r} in the OS keyring: {exc}") from exc
        except Exception as exc:
            raise SecretError(f"could not store secret {name!r} in the OS keyring: {exc}") from exc
        # Deliberately no value, and no length, in the log line.
        _log.info("stored secret %r in the OS keyring", name)

    async def delete(self, name: str) -> bool:
        """Remove a secret. Returns whether one was there to remove."""
        validate_name(name)
        try:
            await _bounded(
                lambda: keyring.delete_password(self._service, name),
                timeout_s=DEFAULT_OPERATION_TIMEOUT_S,
                what=f"deleting secret {name!r}",
            )
        except keyring.errors.PasswordDeleteError:
            return False
        except TimeoutError as exc:
            raise SecretError(f"deleting secret {name!r} from the OS keyring: {exc}") from exc
        except Exception as exc:
            raise SecretError(
                f"could not delete secret {name!r} from the OS keyring: {exc}"
            ) from exc
        _log.info("deleted secret %r from the OS keyring", name)
        return True

    async def has(self, name: str) -> bool:
        return await self.get(name) is not None


__all__ = [
    "BACKEND_UNAVAILABLE_HELP",
    "CONTROL_PIN_NAME",
    "DEFAULT_OPERATION_TIMEOUT_S",
    "DEFAULT_PROBE_TIMEOUT_S",
    "SERVICE_NAME",
    "BackendStatus",
    "SecretError",
    "SecretStore",
    "probe_backend",
    "validate_name",
]
