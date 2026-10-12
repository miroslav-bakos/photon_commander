"""Sequencing, replay windows and subscriber back-pressure."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ssc.state.events import LinkChanged, NoticeCleared, NoticeRaised, StateEvent
from ssc.state.models import (
    MAX_NOTICES,
    LinkState,
    LinkStatus,
    Notice,
    NoticeLevel,
    StateSnapshot,
)
from ssc.state.reducer import replay
from ssc.state.store import Gap, Replay, StateStore

MOMENT = datetime(2026, 8, 28, 21, 0, tzinfo=UTC)


def snapshot() -> StateSnapshot:
    return StateSnapshot(server_started_at=MOMENT)


def link(status: LinkStatus) -> StateEvent:
    return LinkChanged(link=LinkState(status=status, changed_at=MOMENT))


def notice(index: int) -> StateEvent:
    return NoticeRaised(
        notice=Notice(
            id=f"n{index}",
            level=NoticeLevel.WARNING,
            code="test",
            message=f"notice {index}",
            raised_at=MOMENT,
        )
    )


def test_seq_starts_at_zero_and_increments_once_per_event() -> None:
    store = StateStore(snapshot())
    assert store.seq == 0
    assert store.apply(link(LinkStatus.CONNECTING)) == 1
    assert store.apply(link(LinkStatus.READY)) == 2
    assert store.read().snapshot.link.status is LinkStatus.READY


def test_since_returns_only_what_the_client_missed() -> None:
    store = StateStore(snapshot())
    for _ in range(5):
        store.apply(link(LinkStatus.READY))
    outcome = store.since(3)
    assert isinstance(outcome, Replay)
    assert [item.seq for item in outcome.events] == [4, 5]
    assert isinstance(store.since(5), Replay)
    assert store.since(5) == Replay(events=[])


def test_a_seq_older_than_the_ring_is_a_gap_not_a_silent_truncation() -> None:
    store = StateStore(snapshot(), buffer_size=4)
    for _ in range(10):
        store.apply(link(LinkStatus.READY))
    outcome = store.since(2)
    assert isinstance(outcome, Gap)
    assert outcome.requested_seq == 2
    assert outcome.oldest_seq == 6
    assert outcome.current_seq == 10


def test_a_client_ahead_of_the_server_is_a_gap() -> None:
    store = StateStore(snapshot())
    store.apply(link(LinkStatus.READY))
    outcome = store.since(99)
    assert isinstance(outcome, Gap)
    assert outcome.current_seq == 1


def test_replaying_the_ring_reproduces_the_authoritative_snapshot() -> None:
    store = StateStore(snapshot(), buffer_size=64)
    base = store.read().snapshot
    events = [
        link(LinkStatus.CONNECTING),
        notice(1),
        link(LinkStatus.READY),
        NoticeCleared(notice_id="n1"),
    ]
    for event in events:
        store.apply(event)
    assert replay(base, events) == store.read().snapshot


def test_notices_are_bounded_deterministically() -> None:
    store = StateStore(snapshot(), buffer_size=MAX_NOTICES * 4)
    for index in range(MAX_NOTICES + 10):
        store.apply(notice(index))
    notices = store.read().snapshot.notices
    assert len(notices) == MAX_NOTICES
    assert notices[0].id == f"n{10}"


async def test_a_subscriber_receives_every_event_in_order() -> None:
    store = StateStore(snapshot())
    subscription = store.subscribe()
    store.apply(link(LinkStatus.CONNECTING))
    store.apply(link(LinkStatus.READY))
    seen = [subscription.queue.get_nowait() for _ in range(2)]
    assert [item.seq for item in seen] == [1, 2]


async def test_a_slow_subscriber_is_flagged_rather_than_silently_skipped() -> None:
    store = StateStore(snapshot(), queue_size=2)
    subscription = store.subscribe()
    for _ in range(5):
        store.apply(link(LinkStatus.READY))
    assert subscription.overflowed is True
    assert subscription.queue.qsize() == 2


async def test_close_releases_every_subscriber() -> None:
    store = StateStore(snapshot())
    subscription = store.subscribe()
    assert store.subscriber_count == 1
    store.close()
    assert store.subscriber_count == 0
    await asyncio.wait_for(subscription.closed.wait(), timeout=1.0)


def test_a_negative_seq_is_a_programming_error() -> None:
    store = StateStore(snapshot())
    with pytest.raises(ValueError, match="negative"):
        store.since(-1)
