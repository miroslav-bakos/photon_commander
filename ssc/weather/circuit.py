# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""A small circuit breaker, shared by every provider adapter.

Per-provider requirement (weather-providers domain rules / DESIGN.md §6):
stop hammering a provider that is repeatedly failing, and resume trying it
only via a bounded probe rather than either giving up forever or retrying on
every single call. Three states, the standard shape:

* **closed** — requests go through normally; failures accumulate a count.
* **open** — requests are refused outright until ``reset_timeout_s`` has
  elapsed, so a down provider is not hit at full rate.
* **half-open** — the first request after the timeout is let through as a
  probe. Success closes the breaker and clears the count; failure re-opens it
  and restarts the timeout.

The clock is injected (`ssc.weather.base.Clock`, i.e. a zero-arg callable
returning a monotonic-ish float) rather than calling `time.monotonic`
directly, so tests can move time without sleeping.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

from ssc.weather.base import Clock

DEFAULT_FAILURE_THRESHOLD: Final = 3
DEFAULT_RESET_TIMEOUT_S: Final = 60.0


class CircuitState(StrEnum):
    closed = "closed"
    open = "open"
    half_open = "half_open"


@dataclass(slots=True)
class CircuitBreaker:
    """Mutable, per-provider-instance. Not shared across providers."""

    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    reset_timeout_s: float = DEFAULT_RESET_TIMEOUT_S
    clock: Clock = field(default=time.monotonic)

    _state: CircuitState = field(default=CircuitState.closed, init=False)
    _failure_count: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)
    _probe_in_flight: bool = field(default=False, init=False)

    @property
    def state(self) -> CircuitState:
        return self._state

    def allow_request(self) -> bool:
        """Whether a call should be attempted right now.

        Transitions **open -> half_open** here, on the first check after the
        timeout has elapsed, rather than on a timer — this module does no
        background scheduling of its own.
        """
        if self._state == CircuitState.closed:
            return True
        if self._state == CircuitState.open:
            assert self._opened_at is not None
            if self.clock() - self._opened_at >= self.reset_timeout_s:
                self._state = CircuitState.half_open
                self._probe_in_flight = False
                return self._allow_probe()
            return False
        # half_open: exactly one probe in flight at a time.
        return self._allow_probe()

    def _allow_probe(self) -> bool:
        if self._probe_in_flight:
            return False
        self._probe_in_flight = True
        return True

    def record_success(self) -> None:
        self._state = CircuitState.closed
        self._failure_count = 0
        self._opened_at = None
        self._probe_in_flight = False

    def record_failure(self) -> None:
        self._probe_in_flight = False
        if self._state == CircuitState.half_open:
            self._reopen()
            return
        self._failure_count += 1
        if self._failure_count >= self.failure_threshold:
            self._reopen()

    def force_open(self, *, for_s: float) -> None:
        """Open the breaker for at least ``for_s``, regardless of the failure
        count so far. Used when a provider answers ``429`` with a
        ``Retry-After`` longer than the configured reset timeout — honouring
        that header takes priority over the breaker's own default."""
        self.reset_timeout_s = max(self.reset_timeout_s, for_s)
        self._reopen()

    def _reopen(self) -> None:
        self._state = CircuitState.open
        self._opened_at = self.clock()
        self._probe_in_flight = False


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_RESET_TIMEOUT_S",
    "CircuitBreaker",
    "CircuitState",
]
