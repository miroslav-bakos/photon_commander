"""``ssc.store.plans`` — round-trip, revision bump, and optimistic concurrency."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ssc.plan import Plan, ShutdownItem, StartupItem
from ssc.store.database import Database
from ssc.store.plans import (
    PlanAlreadyExistsError,
    PlanNotFoundError,
    PlanRepository,
    PlanRevisionConflictError,
)

_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _plan(plan_id: str = "p1", name: str = "test plan") -> Plan:
    return Plan(
        schema_version=1,
        id=plan_id,
        name=name,
        items=(
            StartupItem(id="startup", start={"kind": "asap"}),
            ShutdownItem(id="shutdown", start={"kind": "relative", "offset_min": 5}),
        ),
        created_at=_NOW,
        updated_at=_NOW,
    )


async def test_create_then_get_round_trips_the_document_verbatim(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    created = await repo.create(_plan())
    assert created.revision == 1

    fetched = await repo.get("p1")
    assert fetched.document == created.document
    assert fetched.revision == 1


async def test_schedule_assigns_a_night_without_rewriting_or_revising_the_plan(
    migrated: Database,
) -> None:
    repo = PlanRepository(migrated)
    created = await repo.create(_plan())

    scheduled = await repo.schedule("p1", night_date=date(2026, 9, 24))

    assert scheduled.night_date == date(2026, 9, 24)
    assert scheduled.document == created.document
    assert scheduled.revision == created.revision
    assert (await repo.list_summaries())[0].night_date == date(2026, 9, 24)


async def test_create_a_duplicate_id_is_refused(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    await repo.create(_plan())
    with pytest.raises(PlanAlreadyExistsError):
        await repo.create(_plan())


async def test_get_an_unknown_plan_raises(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    with pytest.raises(PlanNotFoundError):
        await repo.get("no-such-plan")


async def test_update_bumps_the_revision(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    await repo.create(_plan())
    renamed = _plan(name="renamed")
    updated = await repo.update("p1", renamed, expected_revision=1)
    assert updated.revision == 2
    assert updated.document.name == "renamed"

    fetched = await repo.get("p1")
    assert fetched.revision == 2
    assert fetched.document.name == "renamed"


async def test_update_with_a_stale_revision_is_a_conflict(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    await repo.create(_plan())
    await repo.update("p1", _plan(name="v2"), expected_revision=1)

    with pytest.raises(PlanRevisionConflictError) as excinfo:
        await repo.update("p1", _plan(name="v3-stale"), expected_revision=1)
    assert excinfo.value.expected == 1
    assert excinfo.value.actual == 2

    # The conflicting write must not have landed.
    fetched = await repo.get("p1")
    assert fetched.document.name == "v2"


async def test_update_an_unknown_plan_raises(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    with pytest.raises(PlanNotFoundError):
        await repo.update("no-such-plan", _plan(), expected_revision=1)


async def test_delete_removes_the_row(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    await repo.create(_plan())
    await repo.delete("p1")
    with pytest.raises(PlanNotFoundError):
        await repo.get("p1")


async def test_delete_an_unknown_plan_raises(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    with pytest.raises(PlanNotFoundError):
        await repo.delete("no-such-plan")


async def test_list_summaries_is_newest_edited_first(migrated: Database) -> None:
    repo = PlanRepository(migrated)
    await repo.create(_plan("p1", "first"))
    await repo.create(_plan("p2", "second"))
    await repo.update("p1", _plan("p1", "first-edited"), expected_revision=1)

    summaries = await repo.list_summaries()
    assert [summary.id for summary in summaries] == ["p1", "p2"]
    assert summaries[0].name == "first-edited"
    assert summaries[0].revision == 2
    assert summaries[1].revision == 1


async def test_a_plan_survives_a_fresh_repository_instance(migrated: Database) -> None:
    await PlanRepository(migrated).create(_plan())
    reloaded = PlanRepository(migrated)
    fetched = await reloaded.get("p1")
    assert fetched.document.name == "test plan"
