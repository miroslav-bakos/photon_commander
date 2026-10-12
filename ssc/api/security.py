# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Pairing, sessions, PIN gating and rate limiting (DESIGN.md §9).

The threat model is modest and stated: this is a LAN tool. What it must stop is
another device on the same Wi-Fi driving the mount, and a stuck client retry
loop hammering the scope. It is not an internet service and must not be turned
into one without redesigning §9.

How pairing works:

* The process holds one **pairing token**, printed in the QR code the desktop
  shell shows (``http://<lan-ip>:7777/?t=<token>``).
* A browser exchanges it once for a session cookie: ``HttpOnly``,
  ``SameSite=Lax``, path ``/``. The token itself is never stored in the browser.
* Only the SHA-256 of the cookie value is persisted, so a copied database is not
  a copied session.
* If a control PIN is configured the paired session starts at ``view`` scope and
  must be upgraded; without a PIN, pairing grants ``control`` directly.

Rate limiting is a per-key token bucket, applied to the endpoints that reach the
device or the keyring. It is deliberately in-process: there is one process.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

from ssc.settings.secrets import CONTROL_PIN_NAME, SecretStore
from ssc.settings.store import SettingsStore
from ssc.store.database import Database

_log = logging.getLogger(__name__)

SESSION_COOKIE: Final = "ssc_session"
PAIRING_QUERY_PARAM: Final = "t"

#: Long enough that a phone paired at dusk is still paired at dawn, short enough
#: that a guest device does not stay authorised forever.
DEFAULT_SESSION_TTL = timedelta(days=30)

_MIN_PIN_LENGTH: Final = 4


class Scope(StrEnum):
    VIEW = "view"
    CONTROL = "control"


class AuthError(Exception):
    """Authentication or authorisation failed. Carries no detail for the client."""


@dataclass(frozen=True, slots=True)
class Session:
    scope: Scope
    label: str | None
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A freshly minted session. ``token`` is the only time the value exists."""

    token: str
    session: Session


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class AuthService:
    """Pairing tokens in, sessions out."""

    def __init__(
        self,
        database: Database,
        settings: SettingsStore,
        secrets_store: SecretStore,
        *,
        ttl: timedelta = DEFAULT_SESSION_TTL,
    ) -> None:
        self._database = database
        self._settings = settings
        self._secrets = secrets_store
        self._ttl = ttl
        self._pairing_token = secrets.token_urlsafe(32)

    @property
    def pairing_token(self) -> str:
        return self._pairing_token

    def rotate_pairing_token(self) -> str:
        """Invalidate the QR code. Existing sessions are unaffected."""
        self._pairing_token = secrets.token_urlsafe(32)
        return self._pairing_token

    def pairing_url(self, origin: str) -> str:
        return f"{origin.rstrip('/')}/?{PAIRING_QUERY_PARAM}={self._pairing_token}"

    async def pair(self, token: str, *, label: str | None = None) -> IssuedSession:
        """Exchange a pairing token for a session. Raises :class:`AuthError`."""
        if not hmac.compare_digest(token, self._pairing_token):
            _log.warning("rejected a pairing attempt with an invalid token")
            raise AuthError("invalid pairing token")

        scope = Scope.VIEW if self._settings.security.control_pin_set else Scope.CONTROL
        return await self._issue(scope=scope, label=label)

    async def bootstrap_session(self, *, label: str | None = None) -> IssuedSession:
        """Mint a session with no pairing token at all.

        Used exactly once, from ``PUT /api/settings/security/auth-required``:
        when a caller who currently has no session (because auth was off)
        switches it on, they must not be locked out of their own change —
        every *other*, not-yet-paired device is the one that now has to pair.
        Scope follows the same rule ``pair`` already uses: view-only if a
        control PIN is configured, control otherwise.
        """
        scope = Scope.VIEW if self._settings.security.control_pin_set else Scope.CONTROL
        return await self._issue(scope=scope, label=label)

    async def _issue(self, *, scope: Scope, label: str | None) -> IssuedSession:
        value = secrets.token_urlsafe(32)
        now = datetime.now(tz=UTC)
        session = Session(scope=scope, label=label, created_at=now, expires_at=now + self._ttl)
        await self._database.execute(
            "INSERT INTO auth_session (token_sha256, scope, label, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                _hash(value),
                session.scope.value,
                session.label,
                session.created_at.isoformat(),
                session.expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        return IssuedSession(token=value, session=session)

    async def resolve(self, token: str | None) -> Session | None:
        """The session behind a cookie value, or ``None``. Expired is ``None``."""
        if not token:
            return None
        row = await self._database.fetch_one(
            "SELECT scope, label, created_at, expires_at FROM auth_session WHERE token_sha256 = ?",
            (_hash(token),),
        )
        if row is None:
            return None
        expires_at = datetime.fromisoformat(str(row["expires_at"]))
        if expires_at <= datetime.now(tz=UTC):
            await self.revoke(token)
            return None
        await self._database.execute(
            "UPDATE auth_session SET last_seen_at = ? WHERE token_sha256 = ?",
            (datetime.now(tz=UTC).isoformat(), _hash(token)),
        )
        return Session(
            scope=Scope(str(row["scope"])),
            label=None if row["label"] is None else str(row["label"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            expires_at=expires_at,
        )

    async def revoke(self, token: str) -> None:
        await self._database.execute(
            "DELETE FROM auth_session WHERE token_sha256 = ?", (_hash(token),)
        )

    async def elevate(self, token: str, pin: str) -> Session:
        """Upgrade a view session to control with the PIN. Raises on mismatch."""
        session = await self.resolve(token)
        if session is None:
            raise AuthError("no active session")
        expected = await self._secrets.get(CONTROL_PIN_NAME)
        if expected is None:
            raise AuthError("no control PIN is configured")
        if not hmac.compare_digest(pin, expected):
            _log.warning("rejected a control PIN attempt")
            raise AuthError("incorrect PIN")
        await self._database.execute(
            "UPDATE auth_session SET scope = ? WHERE token_sha256 = ?",
            (Scope.CONTROL.value, _hash(token)),
        )
        return Session(
            scope=Scope.CONTROL,
            label=session.label,
            created_at=session.created_at,
            expires_at=session.expires_at,
        )

    async def set_control_pin(self, pin: str | None) -> bool:
        """Set or clear the control PIN. Returns whether a PIN is now required."""
        if pin is None:
            await self._secrets.delete(CONTROL_PIN_NAME)
            await self._settings.set_control_pin_set(False)
            return False
        if len(pin) < _MIN_PIN_LENGTH or not pin.isdigit():
            raise ValueError(f"the control PIN must be at least {_MIN_PIN_LENGTH} digits")
        await self._secrets.set(CONTROL_PIN_NAME, pin)
        await self._settings.set_control_pin_set(True)
        # Existing control sessions drop back to view: a new PIN means the
        # already-paired tablet in the shed must prove it knows the new one.
        await self._database.execute("UPDATE auth_session SET scope = ?", (Scope.VIEW.value,))
        return True

    async def purge_expired(self) -> int:
        rows = await self._database.fetch_all(
            "DELETE FROM auth_session WHERE expires_at <= ? RETURNING token_sha256",
            (datetime.now(tz=UTC).isoformat(),),
        )
        return len(rows)


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float


class RateLimiter:
    """Token bucket per key. Keeps a stuck retry loop off the mount."""

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        if rate_per_second <= 0 or burst < 1:
            raise ValueError("rate_per_second must be > 0 and burst >= 1")
        self._rate = rate_per_second
        self._burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._clock: Callable[[], float] = time.monotonic if monotonic is None else monotonic

    def allow(self, key: str) -> bool:
        now = self._clock()
        bucket = self._buckets.get(key)
        if bucket is None:
            self._buckets[key] = _Bucket(tokens=self._burst - 1.0, updated_at=now)
            return True
        bucket.tokens = min(self._burst, bucket.tokens + (now - bucket.updated_at) * self._rate)
        bucket.updated_at = now
        if bucket.tokens < 1.0:
            return False
        bucket.tokens -= 1.0
        return True

    def retry_after(self, key: str) -> float:
        bucket = self._buckets.get(key)
        if bucket is None or bucket.tokens >= 1.0:
            return 0.0
        return (1.0 - bucket.tokens) / self._rate


__all__ = [
    "DEFAULT_SESSION_TTL",
    "PAIRING_QUERY_PARAM",
    "SESSION_COOKIE",
    "AuthError",
    "AuthService",
    "IssuedSession",
    "RateLimiter",
    "Scope",
    "Session",
]
