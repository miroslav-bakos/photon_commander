# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Credential scrubbing for anything that leaves the process.

``ssc/seestar`` already redacts at the wire layer, so nothing credential-bearing
should reach here. This is the second gate, on the way *out*, and for one path —
``EventEnvelope.raw`` on its way into ``StateSnapshot.last_device_event`` via
``ssc.main._event_record`` — it is the *only* gate before ``GET /api/state`` and
the WS ``device.event`` frame reach the browser. The API must not surface
``ap.passwd`` or ``setting.password`` even if a future device-state key, a
vendor action echo or a log-shaped payload smuggles one up
(``docs/M0-FINDINGS.md``, "Second credential exposure").

Matching is by **substring**, not whole key name, at any depth and
case-insensitively — the same shape as the wire layer's own gate
(``ssc.seestar.transport.is_sensitive_key``). An exact-name gate here would be
*weaker* than the one it is meant to back up: ``ap_passwd``, ``wifi_password``,
``access_token`` and ``auth_challenge`` all carry a secret and none of them is
the bare word ``passwd`` or ``token``. Firmware 8.46 only pushes ``PiStatus``
today, so none of these are reachable yet — but ``session.py``'s auth-challenge
seam is exactly where a future firmware would first send one, and this gate
must already be strong enough when it does.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

REDACTED: Final = "***redacted***"

#: Matched case-insensitively as a substring of a key name, at any depth.
#: Deliberately not a bare ``"key"``: ``key_mgmt`` (WPA2-PSK vs open) is not a
#: secret and must survive, mirroring ``ssc.seestar.transport``'s own choice.
SECRET_KEY_MARKERS: Final[tuple[str, ...]] = (
    "passw",  # passwd, password, ap_passwd, wifi_password, ...
    "pwd",
    "psk",
    "token",
    "secret",
    "challenge",  # auth_challenge and whatever comes after it
    "credential",
    "api_key",
    "apikey",
)


def is_sensitive_key(key: str) -> bool:
    """Whether ``key`` names, or contains the name of, a credential."""
    lowered = key.lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def redact(value: object) -> object:
    """Return ``value`` with every credential-named member replaced."""
    if isinstance(value, Mapping):
        return {
            str(key): (REDACTED if is_sensitive_key(str(key)) else redact(member))
            for key, member in value.items()
        }
    if isinstance(value, str | bytes):
        return value
    if isinstance(value, Sequence):
        return [redact(member) for member in value]
    return value


def redact_mapping(value: Mapping[str, object]) -> dict[str, object]:
    """:func:`redact` narrowed to the mapping case, for typed call sites."""
    redacted = redact(value)
    if not isinstance(redacted, dict):  # pragma: no cover - redact preserves mappings
        raise TypeError(f"redact returned {type(redacted).__name__}, expected dict")
    return redacted


__all__ = ["REDACTED", "SECRET_KEY_MARKERS", "is_sensitive_key", "redact", "redact_mapping"]
