# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Typed models for the server-push event stream on ``:4700``.

What is actually known (``docs/M0-FINDINGS.md``, firmware 8.46):

* The server speaks first, is silent for ~9 s after connect, then pushes JSON
  unprompted and **closes the connection**. Short-lived connections are the
  normal shape of this stream, not a fault.
* The envelope uses a **capital-E ``Event``** name key alongside the same
  ``Timestamp`` uptime string the control channel uses::

      {"Event": "PiStatus", "Timestamp": "1654.97", "temp": 37.0}

* Exactly **one** event name has been observed: ``PiStatus``. Nothing else is
  modelled, because nothing else has been seen. Every unrecognised frame becomes
  an :class:`UnknownEvent`, which is surfaced rather than dropped
  (``CLAUDE.md`` invariant 7) and logged once per unseen name so a long capture
  does not flood the log.

Add a typed event by recording a fixture first, then calling
:meth:`EventParser.register`. Do not add one from a guess.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Final

from ssc.seestar.models import PiStatus
from ssc.seestar.rpc import parse_device_uptime
from ssc.seestar.transport import redact

_log = logging.getLogger(__name__)

#: The observed name key. ``method`` is accepted as a fallback because the
#: control channel echoes method names in that member and a stray control-shaped
#: frame on :4700 should still be identifiable rather than anonymous.
EVENT_NAME_KEYS: Final[tuple[str, ...]] = ("Event", "method")

UNNAMED_EVENT: Final = "<unnamed>"


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """The parts of a pushed frame that are common to every event."""

    name: str
    #: Device uptime in seconds. **Not** a wall clock — see ``rpc.parse_device_uptime``.
    device_uptime_s: float | None
    #: When SSC received the frame. This is the only trustworthy wall-clock time.
    received_at: datetime
    raw: Mapping[str, object] = field(default_factory=dict)

    def redacted(self) -> object:
        return redact(self.raw)


@dataclass(frozen=True, slots=True)
class PiStatusEvent:
    """``Event: "PiStatus"`` — thermals and battery telemetry.

    Only ``temp`` has been observed in the *pushed* form. The remaining
    :class:`~ssc.seestar.models.PiStatus` fields parse to ``None`` (unknown) when
    the device does not send them; they are not defaulted to plausible values.
    """

    envelope: EventEnvelope
    status: PiStatus


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    """A well-formed frame whose ``Event`` name has no model yet.

    This is the honest default, not a failure. It carries the full raw payload so
    a capture can be turned into a fixture and then into a typed event.
    """

    envelope: EventEnvelope


@dataclass(frozen=True, slots=True)
class MalformedFrameEvent:
    """A line on :4700 that was not a JSON object."""

    line: str
    reason: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class EventStreamOpened:
    """SSC-generated: the event connection was (re-)established."""

    endpoint: str
    at: datetime


@dataclass(frozen=True, slots=True)
class EventStreamClosed:
    """SSC-generated: the event connection ended.

    ``expected`` is ``True`` for the device's routine push-then-close, which is
    ordinary behaviour and must not be reported as a fault.
    """

    endpoint: str
    reason: str
    expected: bool
    at: datetime


#: Anything the event stream can hand a subscriber. ``UnknownEvent`` and
#: ``PiStatusEvent`` come off the wire; the rest are SSC's own lifecycle events.
SeestarEvent = (
    PiStatusEvent | UnknownEvent | MalformedFrameEvent | EventStreamOpened | EventStreamClosed
)

#: Builds a typed event from a parsed envelope.
EventFactory = Callable[[EventEnvelope], SeestarEvent]


def _event_name(raw: Mapping[str, object]) -> str:
    for key in EVENT_NAME_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and value:
            return value
    return UNNAMED_EVENT


def _pi_status(envelope: EventEnvelope) -> SeestarEvent:
    return PiStatusEvent(envelope=envelope, status=PiStatus.from_payload(envelope.raw))


#: Only observed names appear here.
DEFAULT_EVENT_FACTORIES: Final[Mapping[str, EventFactory]] = {"PiStatus": _pi_status}


class EventParser:
    """Turns raw ``:4700`` lines into typed events.

    Instances keep their own "already warned" set, so tests are isolated from one
    another and a long-running session logs each unseen event name exactly once.
    """

    def __init__(self, factories: Mapping[str, EventFactory] | None = None) -> None:
        self._factories: dict[str, EventFactory] = dict(
            DEFAULT_EVENT_FACTORIES if factories is None else factories
        )
        self._warned: set[str] = set()

    def register(self, name: str, factory: EventFactory) -> None:
        """Add a typed event. Record the fixture before you call this."""
        self._factories[name] = factory

    @property
    def unmodelled_names(self) -> frozenset[str]:
        """Event names seen on the wire with no model — feed these to M0.4."""
        return frozenset(self._warned)

    def parse(self, line: str, *, now: datetime | None = None) -> SeestarEvent | None:
        """Parse one line. Returns ``None`` for a blank keep-alive line."""
        received_at = now or _utcnow()
        if not line.strip():
            return None
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError as exc:
            return MalformedFrameEvent(
                line=line, reason=f"not JSON: {exc}", received_at=received_at
            )
        if not isinstance(decoded, dict):
            return MalformedFrameEvent(
                line=line,
                reason=f"expected an object, got {type(decoded).__name__}",
                received_at=received_at,
            )
        raw: Mapping[str, object] = {str(k): v for k, v in decoded.items()}
        envelope = EventEnvelope(
            name=_event_name(raw),
            device_uptime_s=parse_device_uptime(raw.get("Timestamp")),
            received_at=received_at,
            raw=raw,
        )
        factory = self._factories.get(envelope.name)
        if factory is None:
            if envelope.name not in self._warned:
                self._warned.add(envelope.name)
                _log.info(
                    "unmodelled Seestar event %r — passing through as UnknownEvent (payload keys: %s)",
                    envelope.name,
                    sorted(raw),
                )
            return UnknownEvent(envelope=envelope)
        return factory(envelope)
