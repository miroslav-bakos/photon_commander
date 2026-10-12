# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ``plans`` table: a plan document, stored verbatim, with optimistic
concurrency on its own ``revision`` counter.

A plan is the user's portable intent (DESIGN.md 4.2) — it is never normalised
into columns here, only parsed back through :func:`ssc.plan.parse_plan` on the
way out, so a document that fails to parse (an unknown ``schema_version``) is
refused loudly rather than silently coerced, exactly as it would be on load
from a file (CLAUDE.md invariant 7).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

import aiosqlite

from ssc.plan import Plan, parse_plan
from ssc.state.models import PlanSummary
from ssc.store.database import Database


class PlanNotFoundError(LookupError):
    """No plan with that id is stored."""


class PlanAlreadyExistsError(ValueError):
    """A plan with that id has already been created."""


class PlanRevisionConflictError(ValueError):
    """The caller's ``expected_revision`` does not match the stored one.

    Optimistic concurrency (CLAUDE.md invariant 4's "clients render state,
    never own it" applies just as much to a plan document as to the live
    snapshot): a client editing a stale copy must be told to reload, never
    allowed to silently clobber a newer revision.
    """

    def __init__(self, plan_id: str, *, expected: int, actual: int) -> None:
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"plan {plan_id!r} is at revision {actual}, not the expected {expected}; "
            "reload it and reapply the edit"
        )


@dataclass(frozen=True, slots=True)
class PlanRecord:
    """One stored plan: the parsed document plus the store's own bookkeeping.

    ``created_at``/``updated_at`` are this row's own persistence times, not
    the ``created_at``/``updated_at`` the ``document`` itself carries (those
    are the plan author's, and travel with the document verbatim).
    """

    document: Plan
    revision: int
    created_at: datetime
    updated_at: datetime
    night_date: date | None


class PlanRepository:
    """Async list/get/upsert/delete over the ``plans`` table.

    One writer, like every other store in this app: callers do not need their
    own locking, but two concurrent writers racing the same plan id will still
    only have the *last* write's optimistic-concurrency check actually mean
    anything — acceptable here because ``ssc-core`` itself is the only writer
    (CLAUDE.md: "One writer").
    """

    def __init__(self, database: Database) -> None:
        self._database = database

    async def list_summaries(self) -> list[PlanSummary]:
        """Every stored plan's identity, newest-edited first — what the
        snapshot's ``plans`` section and ``GET /api/plans`` both want, without
        parsing a single document."""
        rows = await self._database.fetch_all(
            "SELECT id, name, revision, updated_at, night_date FROM plans "
            "ORDER BY updated_at DESC, id"
        )
        return [
            PlanSummary(
                id=str(row["id"]),
                name=str(row["name"]),
                revision=int(row["revision"]),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                night_date=(date.fromisoformat(str(row["night_date"])) if row["night_date"] else None),
            )
            for row in rows
        ]

    async def get(self, plan_id: str) -> PlanRecord:
        row = await self._database.fetch_one(
            "SELECT document, revision, created_at, updated_at, night_date FROM plans WHERE id = ?",
            (plan_id,),
        )
        if row is None:
            raise PlanNotFoundError(f"no plan with id {plan_id!r}")
        return _record_of(row)

    async def create(self, document: Plan, *, night_date: date | None = None) -> PlanRecord:
        """Store a new plan at revision 1. Refuses to overwrite an existing id
        — that is what :meth:`update` is for."""
        existing = await self._database.fetch_one(
            "SELECT id FROM plans WHERE id = ?", (document.id,)
        )
        if existing is not None:
            raise PlanAlreadyExistsError(
                f"a plan with id {document.id!r} already exists; use PUT to update it"
            )
        moment = datetime.now(tz=UTC)
        await self._database.execute(
            "INSERT INTO plans (id, name, document, revision, created_at, updated_at, night_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                document.id,
                document.name,
                document.model_dump_json(),
                1,
                moment.isoformat(),
                moment.isoformat(),
                night_date.isoformat() if night_date is not None else None,
            ),
        )
        return PlanRecord(
            document=document,
            revision=1,
            created_at=moment,
            updated_at=moment,
            night_date=night_date,
        )

    async def update(self, plan_id: str, document: Plan, *, expected_revision: int) -> PlanRecord:
        """Replace the stored document, bumping ``revision`` by one — but only
        if ``expected_revision`` still matches what is stored (optimistic
        concurrency; raises :class:`PlanRevisionConflictError` otherwise)."""
        row = await self._database.fetch_one(
            "SELECT revision, created_at, night_date FROM plans WHERE id = ?", (plan_id,)
        )
        if row is None:
            raise PlanNotFoundError(f"no plan with id {plan_id!r}")
        actual_revision = int(row["revision"])
        if actual_revision != expected_revision:
            raise PlanRevisionConflictError(
                plan_id, expected=expected_revision, actual=actual_revision
            )
        moment = datetime.now(tz=UTC)
        new_revision = actual_revision + 1
        await self._database.execute(
            "UPDATE plans SET name = ?, document = ?, revision = ?, updated_at = ? WHERE id = ?",
            (document.name, document.model_dump_json(), new_revision, moment.isoformat(), plan_id),
        )
        return PlanRecord(
            document=document,
            revision=new_revision,
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=moment,
            night_date=(date.fromisoformat(str(row["night_date"])) if row["night_date"] else None),
        )

    async def replace_for_night(
        self, plan_id: str, document: Plan, *, expected_revision: int, night_date: date
    ) -> PlanRecord:
        """Atomically replace the document and night with a revision guard."""
        moment = datetime.now(tz=UTC)
        row = await self._database.fetch_one(
            "UPDATE plans SET document = ?, night_date = ?, revision = revision + 1, "
            "updated_at = ? WHERE id = ? AND revision = ? "
            "RETURNING document, revision, created_at, updated_at, night_date",
            (document.model_dump_json(), night_date.isoformat(), moment.isoformat(),
             plan_id, expected_revision),
        )
        await self._database.raw.commit()
        if row is None:
            current = await self.get(plan_id)
            raise PlanRevisionConflictError(
                plan_id, expected=expected_revision, actual=current.revision
            )
        return _record_of(row)

    async def schedule(self, plan_id: str, *, night_date: date | None) -> PlanRecord:
        """Assign a plan to a local observing night without rewriting its document."""
        row = await self._database.fetch_one("SELECT id FROM plans WHERE id = ?", (plan_id,))
        if row is None:
            raise PlanNotFoundError(f"no plan with id {plan_id!r}")
        moment = datetime.now(tz=UTC)
        await self._database.execute(
            "UPDATE plans SET night_date = ?, updated_at = ? WHERE id = ?",
            (night_date.isoformat() if night_date is not None else None, moment.isoformat(), plan_id),
        )
        return await self.get(plan_id)

    async def delete(self, plan_id: str) -> None:
        row = await self._database.fetch_one("SELECT id FROM plans WHERE id = ?", (plan_id,))
        if row is None:
            raise PlanNotFoundError(f"no plan with id {plan_id!r}")
        await self._database.execute("DELETE FROM plans WHERE id = ?", (plan_id,))

    async def target_positions(self) -> list[tuple[float, float]]:
        """``(ra_deg, dec_deg)`` of every item across every stored plan that
        names a target — used by ``ssc.skymap.cutouts`` so a cutout a saved
        plan item still points at is never evicted (M11 spec). RA is stored
        on plan items in hours (DESIGN.md 3.4); converted to degrees here so
        the caller compares like with like against a cutout's ``ra_deg``.
        """
        rows = await self._database.fetch_all("SELECT document FROM plans")
        positions: list[tuple[float, float]] = []
        for row in rows:
            document = parse_plan(json.loads(str(row["document"])))
            for item in document.items:
                ra_h = getattr(item, "ra_j2000_h", None)
                dec_deg = getattr(item, "dec_j2000_deg", None)
                if ra_h is None or dec_deg is None:
                    continue
                positions.append((ra_h * 15.0, dec_deg))
        return positions


def _record_of(row: aiosqlite.Row) -> PlanRecord:
    document = parse_plan(json.loads(str(row["document"])))
    return PlanRecord(
        document=document,
        revision=int(row["revision"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        night_date=(date.fromisoformat(str(row["night_date"])) if row["night_date"] else None),
    )


__all__ = [
    "PlanAlreadyExistsError",
    "PlanNotFoundError",
    "PlanRecord",
    "PlanRepository",
    "PlanRevisionConflictError",
]
