# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The authoritative snapshot, the sequenced log, and the fan-out to clients.

Design in three sentences:

* One in-memory :class:`~ssc.state.models.StateSnapshot`, and a monotonic
  ``seq`` that increments once per applied event. ``seq`` starts at 0, meaning
  "nothing has happened yet"; the first event is ``seq == 1``.
* A bounded ring of the most recent ``(seq, event)`` pairs. A reconnecting
  client asks for everything after its last ``seq``; if that is older than the
  ring, it gets a :class:`Gap` and the caller must resnapshot **and say so**.
  A client must never render a stale snapshot as live.
* Subscribers get a bounded queue each. A subscriber that cannot keep up is
  marked ``overflowed`` and told to resnapshot — it is never silently skipped
  past missing events.

Mutation is synchronous and single-threaded: every writer runs on the event
loop, so there is no lock and no await between "compute the successor" and
"publish it". A writer on another thread uses
:meth:`StateStore.apply_threadsafe`, which hops to the loop rather than racing.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Final

from ssc.state.events import StateEvent
from ssc.state.models import StateSnapshot
from ssc.state.reducer import reduce

_log = logging.getLogger(__name__)

#: How many events stay replayable. A phone that sleeps for a few minutes
#: reconnects inside this window; a laptop that slept overnight resnapshots.
DEFAULT_BUFFER_SIZE: Final = 512

#: Per-subscriber backlog. Beyond this the subscriber is resnapshotted rather
#: than blocking the writer or dropping events behind its back.
DEFAULT_QUEUE_SIZE: Final = 256


@dataclass(frozen=True, slots=True)
class StateView:
    """A snapshot and the ``seq`` it is current as of. Always read together."""

    seq: int
    snapshot: StateSnapshot


@dataclass(frozen=True, slots=True)
class SequencedEvent:
    seq: int
    event: StateEvent


@dataclass(frozen=True, slots=True)
class Replay:
    """Every event after the requested ``seq``, in order."""

    events: list[SequencedEvent]


@dataclass(frozen=True, slots=True)
class Gap:
    """The requested ``seq`` fell out of the ring. The client must resnapshot."""

    requested_seq: int
    oldest_seq: int
    current_seq: int


@dataclass(eq=False)
class Subscription:
    """One client's feed. Closed by the store on shutdown, never left dangling."""

    queue: asyncio.Queue[SequencedEvent] = field(default_factory=asyncio.Queue)
    #: Set when the queue overflowed: the client has missed events and must
    #: resnapshot. Surfaced rather than papered over (architecture policy invariant 7).
    overflowed: bool = False
    closed: asyncio.Event = field(default_factory=asyncio.Event)


class StateStore:
    """The single mutation path for application state (CLAUDE.md invariant 4).

    ``seq`` alone does not identify *which* process produced it: a restarted
    server also starts its ``seq`` back at 0, and a freshly-started process
    that has emitted a handful of events has a ``seq`` a resuming client can
    entirely plausibly have seen before, from the process that ran before it.
    ``epoch`` is the per-process identity that closes that hole — a random id
    minted once per :class:`StateStore` instance. A client resuming with
    ``?seq=N`` must also present the ``epoch`` it last saw; if it does not match
    this instance's, the ``seq`` it is holding could belong to a different
    process's history and must not be trusted to stitch onto this one's.
    """

    def __init__(
        self,
        snapshot: StateSnapshot,
        *,
        buffer_size: int = DEFAULT_BUFFER_SIZE,
        queue_size: int = DEFAULT_QUEUE_SIZE,
    ) -> None:
        if buffer_size < 1:
            raise ValueError("buffer_size must be at least 1")
        self._snapshot = snapshot
        self._seq = 0
        self._epoch = uuid.uuid4().hex
        self._buffer: deque[SequencedEvent] = deque(maxlen=buffer_size)
        self._queue_size = queue_size
        self._subscribers: set[Subscription] = set()

    # -- reading ----------------------------------------------------------- #

    @property
    def epoch(self) -> str:
        """This process's identity. Distinct every time a :class:`StateStore` is built."""
        return self._epoch

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def snapshot(self) -> StateSnapshot:
        return self._snapshot

    def read(self) -> StateView:
        """The snapshot and its ``seq``, captured together and never apart."""
        return StateView(seq=self._seq, snapshot=self._snapshot)

    @property
    def oldest_replayable_seq(self) -> int:
        """The lowest ``seq`` a client may ask to replay *from*."""
        if not self._buffer:
            return self._seq
        return self._buffer[0].seq - 1

    def since(self, seq: int) -> Replay | Gap:
        """Everything after ``seq``, or a :class:`Gap` if it is too far back."""
        if seq < 0:
            raise ValueError("seq must not be negative")
        if seq > self._seq:
            # A client ahead of the server means a restarted server with a reset
            # counter. Treat it as a gap so the client resets to the truth.
            return Gap(
                requested_seq=seq, oldest_seq=self.oldest_replayable_seq, current_seq=self._seq
            )
        if seq == self._seq:
            return Replay(events=[])
        if seq < self.oldest_replayable_seq:
            return Gap(
                requested_seq=seq, oldest_seq=self.oldest_replayable_seq, current_seq=self._seq
            )
        return Replay(events=[item for item in self._buffer if item.seq > seq])

    # -- writing ----------------------------------------------------------- #

    def apply(self, event: StateEvent) -> int:
        """Reduce, record, publish. Returns the new ``seq``."""
        _log.debug("state transition", extra={"diagnostic_event":
            hashlib.sha256(f"state:{type(event).__name__}".encode()).hexdigest()[:16]})
        self._snapshot = reduce(self._snapshot, event)
        self._seq += 1
        item = SequencedEvent(seq=self._seq, event=event)
        self._buffer.append(item)
        for subscription in self._subscribers:
            self._offer(subscription, item)
        return self._seq

    def apply_threadsafe(self, loop: asyncio.AbstractEventLoop, event: StateEvent) -> None:
        """Apply from another thread by hopping onto the owning loop."""
        loop.call_soon_threadsafe(self.apply, event)

    def _offer(self, subscription: Subscription, item: SequencedEvent) -> None:
        if subscription.overflowed:
            return
        try:
            subscription.queue.put_nowait(item)
        except asyncio.QueueFull:
            subscription.overflowed = True
            _log.warning(
                "state subscriber fell behind at seq=%d; it will be resnapshotted", item.seq
            )

    # -- subscribing ------------------------------------------------------- #

    def subscribe(self) -> Subscription:
        subscription = Subscription(queue=asyncio.Queue(maxsize=self._queue_size))
        self._subscribers.add(subscription)
        return subscription

    def unsubscribe(self, subscription: Subscription) -> None:
        self._subscribers.discard(subscription)
        subscription.closed.set()

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def close(self) -> None:
        """Release every subscriber. Called from the lifespan shutdown."""
        for subscription in list(self._subscribers):
            self.unsubscribe(subscription)

    def __iter__(self) -> Iterator[SequencedEvent]:
        return iter(list(self._buffer))


__all__ = [
    "DEFAULT_BUFFER_SIZE",
    "DEFAULT_QUEUE_SIZE",
    "Gap",
    "Replay",
    "SequencedEvent",
    "StateStore",
    "StateView",
    "Subscription",
]
