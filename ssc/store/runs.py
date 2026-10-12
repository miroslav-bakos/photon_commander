# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ``runs`` and ``run_journal`` tables: the durable half of the executor.

:class:`SqliteJournal` is the ``ssc.scheduler.executor.Journal`` port over SQLite.
Its one job is CLAUDE.md invariant 6 — *durable before acted on* — which here
means :meth:`SqliteJournal.append` does not return until the record has been
committed **and fsynced**: the connection is switched to ``PRAGMA synchronous =
FULL`` before the first append, so in WAL mode every commit syncs the WAL to
disk before ``COMMIT`` returns. Without that, WAL's default ``NORMAL`` only
guarantees durability against an application crash, not against the power
blip DESIGN.md 4.2 explicitly names. The pragma is per-connection and the app
has one connection, so every other write pays the same fsync; that is the
price of a journal the morning can trust, and it is paid deliberately.

Records are stored verbatim as JSON (``at`` and every enum as its string form)
and decoded back into the same frozen :class:`JournalRecord` — round-trip
equality is what ``tests/store/test_runs.py`` asserts, because the executor's
``reduce`` is a fold over exactly these records.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import aiosqlite

from ssc.plan import Plan, parse_plan
from ssc.scheduler import CompiledSchedule
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
)
from ssc.state.models import RunLifecycle
from ssc.store.database import Database, StoreError

#: ``PRAGMA synchronous`` reads back as this integer once set to ``FULL``.
_SYNCHRONOUS_FULL = 2


class RunNotFoundError(LookupError):
    """No run with that id is stored."""


@dataclass(frozen=True, slots=True)
class RunRecord:
    """One stored run: the plan and schedule exactly as they were started, plus
    the summary the run list and the interrupted-run check read."""

    run_id: str
    plan: Plan
    plan_revision: int
    schedule: CompiledSchedule
    status: RunLifecycle
    started_at: datetime
    ended_at: datetime | None
    ended_reason: str | None


@dataclass(frozen=True, slots=True)
class RunHistoryRecord:
    run_id: str
    plan_name: str
    plan_revision: int
    night_date: date
    status: RunLifecycle
    started_at: datetime


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """A journal record with the per-run ``seq`` it was stored under."""

    seq: int
    record: JournalRecord


# --- record <-> JSON --------------------------------------------------------- #


def encode_record(record: JournalRecord) -> str:
    effect = None
    if record.effect is not None:
        effect = {
            "name": record.effect.name.value,
            "args": dict(record.effect.args),
            "completion": record.effect.completion.value,
            "event_kind": record.effect.event_kind,
            "deadline_s": record.effect.deadline_s,
        }
    payload: dict[str, Any] = {
        "at": record.at.isoformat(),
        "run_id": record.run_id,
        "kind": record.kind.value,
        "reason": record.reason,
        "item_id": record.item_id,
        "panel_index": record.panel_index,
        "attempt": record.attempt,
        "step": record.step,
        "status": None if record.status is None else record.status.value,
        "held": record.held,
        "effect": effect,
        "effect_outcome": None if record.effect_outcome is None else record.effect_outcome.value,
        "control": None if record.control is None else record.control.value,
        "run_status": None if record.run_status is None else record.run_status.value,
        "retry_at": None if record.retry_at is None else record.retry_at.isoformat(),
        "recovery_attempts": record.recovery_attempts,
        "focus_conditions": record.focus_conditions,
    }
    return json.dumps(payload, separators=(",", ":"))


def decode_record(text: str) -> JournalRecord:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise StoreError("run_journal.record is not a JSON object")
    effect_payload = payload.get("effect")
    effect = None
    if effect_payload is not None:
        effect = EffectCall(
            name=EffectName(effect_payload["name"]),
            args=dict(effect_payload.get("args") or {}),
            completion=CompletionMode(effect_payload["completion"]),
            event_kind=effect_payload.get("event_kind"),
            deadline_s=effect_payload.get("deadline_s"),
        )
    status = payload.get("status")
    effect_outcome = payload.get("effect_outcome")
    control = payload.get("control")
    run_status = payload.get("run_status")
    return JournalRecord(
        at=datetime.fromisoformat(payload["at"]),
        run_id=payload["run_id"],
        kind=RecordKind(payload["kind"]),
        reason=payload.get("reason", ""),
        item_id=payload.get("item_id"),
        panel_index=payload.get("panel_index"),
        attempt=int(payload.get("attempt", 0)),
        step=int(payload.get("step", 0)),
        status=None if status is None else ItemStatus(status),
        held=payload.get("held"),
        effect=effect,
        effect_outcome=None if effect_outcome is None else EffectOutcome(effect_outcome),
        control=None if control is None else ControlKind(control),
        run_status=None if run_status is None else RunStatus(run_status),
        retry_at=(
            None if payload.get("retry_at") is None else datetime.fromisoformat(payload["retry_at"])
        ),
        recovery_attempts=int(payload.get("recovery_attempts", 0)),
        focus_conditions=bool(payload.get("focus_conditions", False)),
    )


# --- the journal port ---------------------------------------------------------- #


async def ensure_durable_commits(database: Database) -> None:
    """Switch the connection to ``synchronous = FULL`` and verify it took."""
    await database.execute("PRAGMA synchronous = FULL")
    value = await database.pragma("synchronous")
    if int(str(value)) != _SYNCHRONOUS_FULL:
        raise StoreError(
            f"SQLite refused PRAGMA synchronous = FULL (reads back {value!r}); the run "
            "journal cannot promise durability on this connection"
        )


class SqliteJournal:
    """``ssc.scheduler.executor.Journal`` over ``run_journal``. Append-only."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self._durable = False

    async def append(self, record: JournalRecord) -> None:
        if not self._durable:
            await ensure_durable_commits(self._database)
            self._durable = True
        # ``seq`` is allocated inside the INSERT itself, so two appends for the
        # same run can never race for a number even if a caller ever overlaps them.
        await self._database.execute(
            "INSERT INTO run_journal (run_id, seq, at, record) "
            "SELECT ?, COALESCE(MAX(seq), 0) + 1, ?, ? FROM run_journal WHERE run_id = ?",
            (record.run_id, record.at.isoformat(), encode_record(record), record.run_id),
        )

    async def read_all(self, run_id: str) -> Sequence[JournalRecord]:
        return [entry.record for entry in await self.read_entries(run_id)]

    async def read_entries(self, run_id: str) -> list[JournalEntry]:
        rows = await self._database.fetch_all(
            "SELECT seq, record FROM run_journal WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [
            JournalEntry(seq=int(row["seq"]), record=decode_record(str(row["record"])))
            for row in rows
        ]


# --- the runs table --------------------------------------------------------------- #


class RunRepository:
    """Create/get/summarise over ``runs``. The journal itself is :class:`SqliteJournal`."""

    def __init__(self, database: Database) -> None:
        self._database = database
        self.journal = SqliteJournal(database)

    async def record_final_mount(self, run_id: str, *, polled_at: datetime | None,
                                 at_park: bool | None, tracking: bool | None, slewing: bool | None) -> None:
        await self._database.execute(
            "INSERT INTO run_final_mount (run_id, polled_at, at_park, tracking, slewing) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(run_id) DO UPDATE SET polled_at=excluded.polled_at, at_park=excluded.at_park, "
            "tracking=excluded.tracking, slewing=excluded.slewing",
            (run_id, polled_at.isoformat() if polled_at else None, at_park, tracking, slewing),
        )

    async def final_mount(self, run_id: str) -> dict[str, str | bool | None] | None:
        row = await self._database.fetch_one("SELECT * FROM run_final_mount WHERE run_id = ?", (run_id,))
        if row is None:
            return None
        return {"polled_at": row["polled_at"], **{
            key: None if row[key] is None else bool(row[key]) for key in ("at_park", "tracking", "slewing")
        }}

    async def create(
        self,
        *,
        run_id: str,
        plan: Plan,
        plan_revision: int,
        schedule: CompiledSchedule,
        started_at: datetime,
    ) -> RunRecord:
        await self._database.execute(
            "INSERT INTO runs (id, plan_id, plan_name, plan_revision, plan_document, schedule, "
            "status, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                plan.id,
                plan.name,
                plan_revision,
                plan.model_dump_json(),
                schedule.model_dump_json(),
                RunLifecycle.RUNNING.value,
                started_at.isoformat(),
            ),
        )
        return RunRecord(
            run_id=run_id,
            plan=plan,
            plan_revision=plan_revision,
            schedule=schedule,
            status=RunLifecycle.RUNNING,
            started_at=started_at,
            ended_at=None,
            ended_reason=None,
        )

    async def get(self, run_id: str) -> RunRecord:
        row = await self._database.fetch_one(f"{_SELECT} WHERE id = ?", (run_id,))
        if row is None:
            raise RunNotFoundError(f"no run with id {run_id!r}")
        return _record_of(row)

    async def set_status(
        self,
        run_id: str,
        status: RunLifecycle,
        *,
        ended_at: datetime | None = None,
        ended_reason: str | None = None,
    ) -> None:
        await self._database.execute(
            "UPDATE runs SET status = ?, ended_at = ?, ended_reason = ? WHERE id = ?",
            (
                status.value,
                None if ended_at is None else ended_at.isoformat(),
                ended_reason,
                run_id,
            ),
        )

    async def update_plan_snapshot(
        self,
        run_id: str,
        *,
        plan: Plan,
        plan_revision: int,
        schedule: CompiledSchedule,
    ) -> RunRecord:
        """Replace an active run's compatible plan snapshot atomically."""
        await self._database.execute(
            "UPDATE runs SET plan_name = ?, plan_revision = ?, plan_document = ?, schedule = ? "
            "WHERE id = ?",
            (
                plan.name,
                plan_revision,
                plan.model_dump_json(),
                schedule.model_dump_json(),
                run_id,
            ),
        )
        return await self.get(run_id)

    async def list_unfinished(self) -> list[RunRecord]:
        """Runs a previous process left ``running``/``paused`` — what a restart
        must surface as interrupted (DESIGN.md 4.2), newest first."""
        rows = await self._database.fetch_all(
            f"{_SELECT} WHERE status IN (?, ?) ORDER BY started_at DESC",
            (RunLifecycle.RUNNING.value, RunLifecycle.PAUSED.value),
        )
        return [_record_of(row) for row in rows]

    async def list_dawn_safety_pending(self) -> list[RunRecord]:
        """Unverified safety obligations, including runs hidden by a newer run.

        The latest safety stage is authoritative: a failed attempt after an
        earlier confirmation makes the obligation unresolved again.
        """
        rows = await self._database.fetch_all(
            f"{_SELECT} WHERE id IN ("
            "SELECT j.run_id FROM run_journal j "
            "WHERE json_extract(j.record, '$.kind') = 'dawn_safety' "
            "AND j.seq = (SELECT MAX(k.seq) FROM run_journal k "
            "WHERE k.run_id = j.run_id AND json_extract(k.record, '$.kind') = 'dawn_safety') "
            "AND COALESCE(json_extract(j.record, '$.effect.args.stage'), '') != 'completed'"
            ") ORDER BY started_at ASC"
        )
        return [_record_of(row) for row in rows]

    async def latest(self) -> RunRecord | None:
        row = await self._database.fetch_one(f"{_SELECT} ORDER BY started_at DESC LIMIT 1")
        return None if row is None else _record_of(row)

    async def for_night(self, night_date: date) -> list[RunRecord]:
        """Return stored executed documents, independent of current plans."""
        rows = await self._database.fetch_all(
            f"{_SELECT} WHERE json_extract(schedule, '$.night.date') = ? ORDER BY started_at, id",
            (night_date.isoformat(),),
        )
        return [_record_of(row) for row in rows]


    async def history(self, start: date, end: date) -> list[RunHistoryRecord]:
        """Calendar summaries use the executed schedule's night, not current plans."""
        rows = await self._database.fetch_all(
            "SELECT id, plan_name, plan_revision, json_extract(schedule, '$.night.date') AS night_date, "
            "status, started_at FROM runs WHERE json_extract(schedule, '$.night.date') BETWEEN ? AND ? "
            "ORDER BY started_at DESC, id DESC", (start.isoformat(), end.isoformat()),
        )
        return [RunHistoryRecord(run_id=str(row['id']), plan_name=str(row['plan_name']),
            plan_revision=int(row['plan_revision']), night_date=date.fromisoformat(str(row['night_date'])),
            status=RunLifecycle(str(row['status'])), started_at=datetime.fromisoformat(str(row['started_at'])))
            for row in rows]


_SELECT = (
    "SELECT id, plan_revision, plan_document, schedule, status, started_at, ended_at, "
    "ended_reason FROM runs"
)


def _record_of(row: aiosqlite.Row) -> RunRecord:
    ended_at = row["ended_at"]
    return RunRecord(
        run_id=str(row["id"]),
        plan=parse_plan(json.loads(str(row["plan_document"]))),
        plan_revision=int(row["plan_revision"]),
        schedule=CompiledSchedule.model_validate_json(str(row["schedule"])),
        status=RunLifecycle(str(row["status"])),
        started_at=datetime.fromisoformat(str(row["started_at"])),
        ended_at=None if ended_at is None else datetime.fromisoformat(str(ended_at)),
        ended_reason=None if row["ended_reason"] is None else str(row["ended_reason"]),
    )


__all__ = [
    "JournalEntry",
    "RunNotFoundError",
    "RunRecord",
    "RunRepository",
    "SqliteJournal",
    "decode_record",
    "encode_record",
    "ensure_durable_commits",
]
