"""Real SQLite restart retention and presentation-only autofocus recovery."""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from ssc.state.models import (
    AutofocusCurvePoint,
    AutofocusSampleState,
    AutofocusState,
    AutofocusStatus,
)
from ssc.store.autofocus import AutofocusRepository, restored_state
from ssc.store.database import Database, database
from ssc.store.migrations import migrate


def measured_state(status: AutofocusStatus = "completed") -> AutofocusState:
    return AutofocusState(
        status=status,
        stage="fine" if status == "running" else None,
        samples=(AutofocusSampleState(position=1200, hfr_px=1.4, star_count=30, stage="fine"),),
        curve=(AutofocusCurvePoint(position=1200.0, hfr_px=1.4),),
        candidate_position=1200,
        final_position=1200,
        started_at=datetime(2026, 10, 11, 20, tzinfo=UTC),
        completed_at=datetime(2026, 10, 11, 20, 2, tzinfo=UTC),
    )


async def test_latest_measurements_survive_file_database_reopen(tmp_path: Path) -> None:
    path = tmp_path / "autofocus.sqlite"
    original = measured_state()
    async with database(path) as handle:
        await migrate(handle)
        repo = AutofocusRepository(handle)
        assert await repo.get() is None
        await repo.save(original)
    async with database(path) as handle:
        await migrate(handle)
        assert await AutofocusRepository(handle).get() == original


async def test_latest_run_replaces_previous_samples(migrated: Database) -> None:
    repo = AutofocusRepository(migrated)
    await repo.save(measured_state())
    replacement = AutofocusState(status="failed", error="No measurable stars")
    await repo.save(replacement)
    assert await repo.get() == replacement
    assert len(await migrated.fetch_all("SELECT * FROM autofocus_latest")) == 1


async def test_running_recovery_is_pure_and_persistable(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "interrupted.sqlite"
    original = measured_state("running").model_copy(update={"error": "Transport retry"})
    async with database(path) as handle:
        await migrate(handle)
        await AutofocusRepository(handle).save(original)
    async with database(path) as handle:
        await migrate(handle)
        repo = AutofocusRepository(handle)
        saved = await repo.get()
        assert saved is not None
        with monkeypatch.context() as guard:

            def forbid_io(*_args, **_kwargs):
                pytest.fail("Recovery must not perform I/O")

            guard.setattr(Database, "execute", forbid_io)
            guard.setattr(Database, "fetch_one", forbid_io)
            guard.setattr("builtins.open", forbid_io)
            guard.setattr("socket.socket", forbid_io)
            interrupted = restored_state(saved)
        assert interrupted.status == "interrupted"
        assert interrupted.samples == original.samples
        assert interrupted.curve == original.curve
        assert interrupted.started_at == original.started_at
        assert interrupted.candidate_position == original.candidate_position
        assert interrupted.stage is None
        assert interrupted.final_position is None
        assert interrupted.completed_at is None
        assert interrupted.error is not None
        assert "unverified" in interrupted.error
        assert "Transport retry" in interrupted.error
        assert restored_state(interrupted) is interrupted
        await repo.save(interrupted)
    async with database(path) as handle:
        assert await AutofocusRepository(handle).get() == interrupted


@pytest.mark.parametrize("status", ["idle", "completed", "failed"])
def test_terminal_recovery_preserves_original_state(status: AutofocusStatus) -> None:
    original = measured_state(status)
    assert restored_state(original) is original


async def test_malformed_retained_record_raises(migrated: Database) -> None:
    await migrated.execute(
        "INSERT INTO autofocus_latest VALUES (1, ?, ?)",
        ('{"status":"unknown"}', datetime.now(UTC).isoformat()),
    )
    with pytest.raises(ValidationError):
        await AutofocusRepository(migrated).get()
