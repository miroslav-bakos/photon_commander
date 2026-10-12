"""``ssc.store.runs``: the executor's journal on SQLite (CLAUDE.md invariant 6).

Three properties matter. Records come back in ``seq`` order and *equal* to what
went in — ``executor.reduce`` folds these, so a lossy round trip is a wrong
resume position. An append is durable before it returns: the connection is on
``synchronous = FULL`` and a reopened database has every record. And the
journal is append-only: nothing here can update or delete a row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ssc.scheduler.executor import (
    CompletionMode,
    ControlKind,
    EffectCall,
    EffectName,
    EffectOutcome,
    ItemStatus,
    JournalRecord,
    RecordKind,
    RunStatus,
    reduce,
)
from ssc.state.models import RunLifecycle
from ssc.store.database import Database, open_database
from ssc.store.migrations import migrate
from ssc.store.runs import (
    RunNotFoundError,
    RunRepository,
    SqliteJournal,
    decode_record,
    encode_record,
)
from tests.scheduler.schedules import BASE_UTC, build_plan, build_schedule

RUN_ID = "run-1"


def _records(run_id: str = RUN_ID) -> list[JournalRecord]:
    """One record of every shape the executor writes."""
    at = datetime(2026, 1, 1, 20, 0, 0, 123456, tzinfo=UTC)
    slew = EffectCall(
        name=EffectName.SLEW_TO,
        args={"ra_j2000_h": 5.5, "dec_j2000_deg": -5.4},
        completion=CompletionMode.DEVICE_EVENT,
        event_kind="slew_complete",
        deadline_s=120.0,
    )
    return [
        JournalRecord(at=at, run_id=run_id, kind=RecordKind.RUN_STARTED, reason="run started"),
        JournalRecord(
            at=at + timedelta(seconds=1),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="goto",
            panel_index=None,
            attempt=1,
            step=1,
            status=ItemStatus.RUNNING,
            effect=slew,
            reason="issuing slew_to (attempt 1)",
        ),
        JournalRecord(
            at=at + timedelta(seconds=2),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="goto",
            attempt=1,
            step=1,
            status=ItemStatus.RUNNING,
            effect=slew,
            effect_outcome=EffectOutcome.FAILED,
            retry_at=at + timedelta(seconds=7),
            recovery_attempts=1,
            focus_conditions=True,
            reason="automatic recovery scheduled",
        ),
        JournalRecord(
            at=at + timedelta(seconds=2),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="goto",
            attempt=1,
            step=2,
            effect=slew,
            effect_outcome=EffectOutcome.COMPLETED,
            reason="slew_to completed",
        ),
        JournalRecord(
            at=at + timedelta(seconds=3),
            run_id=run_id,
            kind=RecordKind.ITEM_TRANSITION,
            item_id="goto",
            panel_index=2,
            attempt=1,
            step=2,
            held=True,
            effect=EffectCall(name=EffectName.GATE_PARK),
            reason="cloud > 60% sustained",
        ),
        JournalRecord(
            at=at + timedelta(seconds=4),
            run_id=run_id,
            kind=RecordKind.CONTROL,
            control=ControlKind.SKIP,
            item_id="goto",
            reason="skipped by the user",
        ),
        JournalRecord(
            at=at + timedelta(seconds=5),
            run_id=run_id,
            kind=RecordKind.CONTROL,
            control=ControlKind.STOP,
            run_status=RunStatus.STOPPED,
            effect=EffectCall(name=EffectName.GATE_PARK),
            effect_outcome=EffectOutcome.FAILED,
            reason="not implemented: Telescope.Park",
        ),
        JournalRecord(
            at=at + timedelta(seconds=6),
            run_id=run_id,
            kind=RecordKind.RUN_ENDED,
            run_status=RunStatus.STOPPED,
            reason="stopped by control",
        ),
    ]


async def _create_run(database: Database, run_id: str = RUN_ID) -> RunRepository:
    repo = RunRepository(database)
    plan = build_plan()
    await repo.create(
        run_id=run_id,
        plan=plan,
        plan_revision=3,
        schedule=build_schedule(plan),
        started_at=BASE_UTC,
    )
    return repo


def test_every_record_shape_round_trips_through_json() -> None:
    for record in _records():
        assert decode_record(encode_record(record)) == record


async def test_append_then_read_all_returns_records_in_seq_order(migrated: Database) -> None:
    repo = await _create_run(migrated)
    journal: SqliteJournal = repo.journal
    records = _records()
    for record in records:
        await journal.append(record)

    assert list(await journal.read_all(RUN_ID)) == records
    entries = await journal.read_entries(RUN_ID)
    assert [entry.seq for entry in entries] == list(range(1, len(records) + 1))
    # The executor's own fold over what came back reaches the same position.
    assert reduce(await journal.read_all(RUN_ID)) == reduce(records)


async def test_journals_of_different_runs_do_not_mix(migrated: Database) -> None:
    repo = await _create_run(migrated, "a")
    await _create_run(migrated, "b")
    await repo.journal.append(_records("a")[0])
    await repo.journal.append(_records("b")[0])
    await repo.journal.append(_records("a")[1])
    assert [r.run_id for r in await repo.journal.read_all("a")] == ["a", "a"]
    assert [r.run_id for r in await repo.journal.read_all("b")] == ["b"]
    assert await repo.journal.read_all("never") == []


async def test_append_is_durable_before_it_returns(tmp_path: Path) -> None:
    """Reopen the file after every append has returned: nothing is missing,
    and the connection that wrote it was on ``synchronous = FULL``."""
    path = tmp_path / "runs.sqlite3"
    first = await open_database(path)
    try:
        await migrate(first)
        repo = await _create_run(first)
        for record in _records():
            await repo.journal.append(record)
        assert int(str(await first.pragma("synchronous"))) == 2
    finally:
        await first.aclose()

    second = await open_database(path)
    try:
        assert list(await SqliteJournal(second).read_all(RUN_ID)) == _records()
        stored = await RunRepository(second).get(RUN_ID)
        assert stored.plan == build_plan()
        assert stored.schedule == build_schedule(build_plan())
        assert stored.plan_revision == 3
        assert stored.status is RunLifecycle.RUNNING
    finally:
        await second.aclose()


async def test_a_journal_row_needs_its_run_row(migrated: Database) -> None:
    """``run_journal.run_id`` is a real foreign key, enforced."""
    import aiosqlite

    journal = SqliteJournal(migrated)
    with pytest.raises(aiosqlite.IntegrityError):
        await journal.append(_records("no-such-run")[0])


async def test_status_summary_and_the_unfinished_query(migrated: Database) -> None:
    repo = await _create_run(migrated, "old")
    await _create_run(migrated, "new")
    await repo.set_status(
        "old", RunLifecycle.COMPLETED, ended_at=BASE_UTC, ended_reason="all items settled"
    )
    await repo.set_status("new", RunLifecycle.PAUSED)

    unfinished = await repo.list_unfinished()
    assert [record.run_id for record in unfinished] == ["new"]
    old = await repo.get("old")
    assert old.status is RunLifecycle.COMPLETED
    assert old.ended_at == BASE_UTC
    assert old.ended_reason == "all items settled"
    with pytest.raises(RunNotFoundError):
        await repo.get("missing")
