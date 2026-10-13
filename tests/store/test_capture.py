"""``ssc.store.capture``: the durable row-before-bytes lifecycle (CLAUDE.md
invariant 6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ssc.store.capture import CaptureFrameNotFoundError, CaptureRepository
from ssc.store.database import Database

DATE_OBS = datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC)


@pytest.fixture
def repo(migrated: Database) -> CaptureRepository:
    return CaptureRepository(migrated)


async def test_begin_frame_starts_writing(repo: CaptureRepository, tmp_path: Path) -> None:
    frame_id = await repo.begin_frame(
        path=tmp_path / "a.fits",
        date_obs=DATE_OBS,
        object_name="M42",
        filter_name="LP",
        frame_type="light",
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=3840,
        height=2160,
    )
    writing = await repo.list_writing()
    assert [record.id for record in writing] == [frame_id]
    assert writing[0].status == "writing"
    assert await repo.list_complete() == []


async def test_complete_frame_moves_it_out_of_writing(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await repo.begin_frame(
        path=tmp_path / "a.fits",
        date_obs=DATE_OBS,
        object_name="M42",
        filter_name="LP",
        frame_type="light",
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=3840,
        height=2160,
    )
    await repo.complete_frame(frame_id)
    assert await repo.list_writing() == []
    complete = await repo.list_complete()
    assert len(complete) == 1
    assert complete[0].status == "complete"
    assert complete[0].object_name == "M42"
    assert complete[0].frame_type == "light"
    assert complete[0].date_obs == DATE_OBS


async def test_begin_frame_refuses_an_unknown_frame_type(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    """The table's own CHECK constraint — the actual guard, since this layer
    stores the raw string rather than the enum (``CaptureFrameRecord``'s own
    docstring)."""
    with pytest.raises(Exception, match="CHECK constraint failed"):
        await repo.begin_frame(
            path=tmp_path / "bogus.fits",
            date_obs=DATE_OBS,
            object_name="M42",
            filter_name="LP",
            frame_type="bogus",
            exposure_s=30.0,
            gain=200,
            ccd_temp_c=20.0,
            width=3840,
            height=2160,
        )


async def test_complete_frame_unknown_id_raises(repo: CaptureRepository) -> None:
    with pytest.raises(CaptureFrameNotFoundError):
        await repo.complete_frame(999)


async def test_get_frame_returns_the_row_whatever_its_status(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    path = tmp_path / "a.fits"
    frame_id = await repo.begin_frame(
        path=path,
        date_obs=DATE_OBS,
        object_name="M42",
        filter_name="LP",
        frame_type="light",
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=3840,
        height=2160,
    )
    still_writing = await repo.get_frame(frame_id)
    assert still_writing.status == "writing"
    assert still_writing.path == path

    await repo.complete_frame(frame_id)
    now_complete = await repo.get_frame(frame_id)
    assert now_complete.status == "complete"
    assert now_complete.object_name == "M42"


async def test_get_frame_unknown_id_raises(repo: CaptureRepository) -> None:
    with pytest.raises(CaptureFrameNotFoundError):
        await repo.get_frame(999)


async def test_discard_frame_removes_a_writing_row_and_returns_its_path(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    path = tmp_path / "orphan.fits"
    frame_id = await repo.begin_frame(
        path=path,
        date_obs=DATE_OBS,
        object_name=None,
        filter_name=None,
        frame_type="light",
        exposure_s=None,
        gain=None,
        ccd_temp_c=None,
        width=3840,
        height=2160,
    )
    returned = await repo.discard_frame(frame_id)
    assert returned == path
    assert await repo.list_writing() == []
    with pytest.raises(CaptureFrameNotFoundError):
        await repo.discard_frame(frame_id)


async def test_discard_frame_refuses_a_completed_row(
    repo: CaptureRepository, tmp_path: Path
) -> None:
    frame_id = await repo.begin_frame(
        path=tmp_path / "a.fits",
        date_obs=DATE_OBS,
        object_name=None,
        filter_name=None,
        frame_type="light",
        exposure_s=None,
        gain=None,
        ccd_temp_c=None,
        width=3840,
        height=2160,
    )
    await repo.complete_frame(frame_id)
    with pytest.raises(ValueError, match="not 'writing'"):
        await repo.discard_frame(frame_id)


async def test_a_killed_process_leaves_the_row_writing_on_reopen(tmp_path: Path) -> None:
    """The literal durability gate: a row inserted before the process dies
    is still there, still 'writing', when a fresh connection opens the same
    database file — nothing about the crash silently completed it."""
    from ssc.store.database import open_database
    from ssc.store.migrations import migrate

    db_path = tmp_path / "ssc.sqlite3"
    db = await open_database(db_path)
    await migrate(db)
    repo = CaptureRepository(db)
    frame_id = await repo.begin_frame(
        path=tmp_path / "capture" / "a.fits",
        date_obs=DATE_OBS,
        object_name="M42",
        filter_name="LP",
        frame_type="light",
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=3840,
        height=2160,
    )
    await db.aclose()  # simulates the process dying before completion

    reopened = await open_database(db_path)
    try:
        reopened_repo = CaptureRepository(reopened)
        writing = await reopened_repo.list_writing()
        assert [record.id for record in writing] == [frame_id]
        assert await reopened_repo.list_complete() == []
    finally:
        await reopened.aclose()


async def test_run_summary_is_complete_and_isolated_after_reopen(repo, migrated, tmp_path):
    from ssc.store.database import open_database

    for index in range(64):
        frame = await repo.begin_frame(
            path=tmp_path / f"{index}.fits", date_obs=DATE_OBS, object_name="private target",
            filter_name="LP", frame_type="light", exposure_s=30.0, gain=200,
            ccd_temp_c=20.0, width=4, height=4,
            run_id="night" if index < 61 else "other" if index == 61 else None,
        )
        if index != 60:
            await repo.complete_frame(frame)
    # A separately opened real SQLite connection must give the same durable result.
    reopened = await open_database(tmp_path / "ssc.sqlite3")
    try:
        summary = await CaptureRepository(reopened).run_summary("night")
    finally:
        await reopened.aclose()
    assert summary.saved_frames == 60
    assert summary.pending_frames == 1
    assert summary.captured_integration_s == 1800
    assert summary.stack_unmeasured_frames == 60
    assert summary.accepted_integration_s is None


async def test_stack_evidence_survives_replay_and_intentional_raw_removal(repo, tmp_path):
    frame = await repo.begin_frame(path=tmp_path / "rejected.fits", date_obs=DATE_OBS,
        object_name=None, filter_name=None, frame_type="light", exposure_s=30,
        gain=None, ccd_temp_c=None, width=4, height=4, run_id="night")
    await repo.complete_frame(frame)
    await repo.record_stack_outcome(frame, "rejected")
    await repo.record_stack_outcome(frame, "rejected")
    await repo.delete_complete_frame(frame)
    summary = await repo.run_summary("night")
    assert summary.saved_frames == 0
    assert summary.rejected_frames == 1


async def test_begin_frame_keeps_its_insert_id_when_another_table_inserts(
    repo: CaptureRepository, migrated: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    await migrated.execute("CREATE TABLE competing_writer (id INTEGER PRIMARY KEY)")
    await migrated.execute("INSERT INTO competing_writer (id) VALUES (40)")
    inserted = asyncio.Event()
    competing_done = asyncio.Event()
    commit = migrated.raw.commit

    async def interleaved_commit() -> None:
        await commit()
        inserted.set()
        await competing_done.wait()

    async def competing_insert() -> None:
        await inserted.wait()
        await migrated.raw.execute("INSERT INTO competing_writer DEFAULT VALUES")
        await commit()
        competing_done.set()

    monkeypatch.setattr(migrated.raw, "commit", interleaved_commit)
    writer = asyncio.create_task(competing_insert())
    frame_id = await repo.begin_frame(
        path=tmp_path / "own.fits", date_obs=DATE_OBS, object_name="M42", filter_name="LP",
        frame_type="light", exposure_s=30, gain=200, ccd_temp_c=20, width=3840, height=2160,
    )
    await writer
    own_row = await migrated.fetch_one("SELECT id FROM capture_frame WHERE path = ?", (str(tmp_path / "own.fits"),))
    assert own_row is not None
    assert frame_id == own_row["id"]
    await repo.complete_frame(frame_id)
    assert (await repo.get_frame(frame_id)).status == "complete"
