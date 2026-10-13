"""Forward-only, immutable once shipped, never destructive."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from ssc.store.database import Database, open_database
from ssc.store.migrations import MIGRATIONS_DIR, MigrationError, load_migrations, migrate


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_shipped_migration_interrupted_before_marker_rolls_back_and_reopens(
    tmp_path: Path, monkeypatch, failure,
) -> None:
    path = tmp_path / "app.sqlite3"
    db = await open_database(path)
    original = db.raw.execute

    def interrupt(sql, parameters=()):
        if sql.startswith("INSERT INTO schema_migrations"):
            raise failure("interrupted after shipped DDL")
        return original(sql, parameters)

    monkeypatch.setattr(db.raw, "execute", interrupt)
    try:
        with pytest.raises(failure):
            await migrate(db)
        assert await db.fetch_one("SELECT name FROM sqlite_master WHERE name = 'config'") is None
        assert await db.fetch_all("SELECT * FROM schema_migrations") == []
    finally:
        await db.aclose()
    reopened = await open_database(path)
    try:
        assert await migrate(reopened) == len(load_migrations())
    finally:
        await reopened.aclose()


async def test_failed_legacy_partial_shipped_migration_is_not_marked_and_backup_has_wal(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy.sqlite3"
    db = await open_database(path)
    try:
        # Reproduce the old runner's interrupted shipped initial DDL, which
        # committed before recording its marker. Include actual user data.
        await db.executescript(load_migrations()[0].sql)
        await db.execute("CREATE TABLE recovery_evidence (value TEXT)")
        await db.execute("INSERT INTO recovery_evidence VALUES ('retained')")
        with pytest.raises(MigrationError, match="legacy partial migration"):
            await migrate(db)
        assert await db.fetch_all("SELECT * FROM schema_migrations") == []
        backups = await asyncio.to_thread(lambda: list(tmp_path.glob("legacy.sqlite3.pre-upgrade-*.sqlite3")))
        assert len(backups) == 1
        assert backups[0].stat().st_mode & 0o777 == 0o600
        with sqlite3.connect(backups[0]) as snapshot:
            assert snapshot.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            assert snapshot.execute("SELECT value FROM recovery_evidence").fetchone() == ("retained",)
            assert snapshot.execute("SELECT * FROM schema_migrations").fetchall() == []
    finally:
        await db.aclose()


async def test_interrupted_shipped_alter_preserves_previous_schema_and_backup(
    tmp_path: Path, monkeypatch,
) -> None:
    directory = tmp_path / "before-0013"
    directory.mkdir()
    for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(migration.name[:4]) < 13:
            shutil.copy(migration, directory / migration.name)
    path = tmp_path / "upgrade.sqlite3"
    db = await open_database(path)
    try:
        await migrate(db, directory=directory)
        original = db.raw.execute

        def interrupt(sql, parameters=()):
            if sql.startswith("INSERT INTO schema_migrations") and parameters[0] == 13:
                raise asyncio.CancelledError("interrupted shipped ALTER after DDL")
            return original(sql, parameters)

        monkeypatch.setattr(db.raw, "execute", interrupt)
        with pytest.raises(asyncio.CancelledError):
            await migrate(db)
        columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(capture_frame)")}
        assert "run_id" not in columns and "block_id" not in columns
        assert (await db.fetch_one("SELECT max(version) FROM schema_migrations"))[0] == 12
        backups = await asyncio.to_thread(lambda: list(tmp_path.glob("upgrade.sqlite3.pre-upgrade-*.sqlite3")))
        versions = []
        for backup in backups:
            with sqlite3.connect(backup) as snapshot:
                versions.append(snapshot.execute("SELECT max(version) FROM schema_migrations").fetchone()[0])
        assert 12 in versions
    finally:
        await db.aclose()
    reopened = await open_database(path)
    try:
        assert await migrate(reopened) == len(load_migrations())
        columns = {row["name"] for row in await reopened.fetch_all("PRAGMA table_info(capture_frame)")}
        assert {"run_id", "block_id"} <= columns
    finally:
        await reopened.aclose()


async def test_migrate_applies_the_shipped_set_and_is_idempotent(database: Database) -> None:
    version = await migrate(database)
    assert version == len(load_migrations())
    assert await migrate(database) == version

    rows = await database.fetch_all("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables = {str(row["name"]) for row in rows}
    assert {"schema_migrations", "config", "device_address", "auth_session"} <= tables


async def test_an_edited_migration_is_refused(database: Database, tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_initial.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    await migrate(database, directory=directory)

    (directory / "0001_initial.sql").write_text("CREATE TABLE b (id INTEGER PRIMARY KEY);")
    with pytest.raises(MigrationError, match="has changed since it was applied"):
        await migrate(database, directory=directory)


async def test_a_database_ahead_of_the_build_is_refused_not_downgraded(
    database: Database, tmp_path: Path
) -> None:
    ahead = tmp_path / "ahead"
    ahead.mkdir()
    (ahead / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    (ahead / "0002_b.sql").write_text("CREATE TABLE b (id INTEGER PRIMARY KEY);")
    await migrate(database, directory=ahead)

    behind = tmp_path / "behind"
    behind.mkdir()
    (behind / "0001_a.sql").write_text("CREATE TABLE a (id INTEGER PRIMARY KEY);")
    with pytest.raises(MigrationError, match="ahead of the 1 migrations"):
        await migrate(database, directory=behind)


def test_a_numbering_gap_is_an_error(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_a.sql").write_text("SELECT 1;")
    (directory / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="not contiguous"):
        load_migrations(directory)


def test_a_badly_named_file_is_an_error(tmp_path: Path) -> None:
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "add-thing.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="NNNN_lower_snake_case"):
        load_migrations(directory)


async def _insert_tile(
    database: Database, *, survey: str, order: int, npix: int, ext: str = "png"
) -> None:
    await database.execute(
        "INSERT INTO skymap_tiles "
        "(survey, tile_order, npix, ext, status, bytes, pinned, fetched_at, last_used) "
        "VALUES (?, ?, ?, ?, 'cached', 100, 0, '2026-09-09T00:00:00+00:00', "
        "'2026-09-09T00:00:00+00:00')",
        (survey, order, npix, ext),
    )


async def test_0010_purges_only_the_nsns_fits_stretch_surveys_cached_tiles(
    database: Database, tmp_path: Path
) -> None:
    """The migration that resets stale per-tile-stretch NSNS tiles must not
    touch any other survey's cache, and must not touch the NSNS surveys'
    own ``properties`` rows (``tile_order = -1``, plain text, never
    stretched at all)."""
    before_0010 = tmp_path / "before"
    before_0010.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        # Everything from 0010 on is excluded here, not just 0010 itself —
        # this fixture is "the schema as it stood right before 0010", and it
        # must stay contiguous regardless of how many migrations have
        # shipped since (architecture policy: migrations are forward-only, so a test
        # pinned to one point in that history should not need editing every
        # time a later one is added).
        if int(path.name[:4]) >= 10:
            continue
        shutil.copy(path, before_0010 / path.name)
    await migrate(database, directory=before_0010)

    await _insert_tile(database, survey="nsns-halpha", order=6, npix=21439)
    await _insert_tile(database, survey="nsns-oiii", order=6, npix=21439)
    await _insert_tile(database, survey="nsns-sii", order=2, npix=83)
    await _insert_tile(database, survey="nsns-halpha", order=-1, npix=0, ext="properties")
    await _insert_tile(database, survey="legacy-surveys-dr11", order=6, npix=1234, ext="jpg")
    await _insert_tile(database, survey="nsns-ohs8", order=6, npix=21439)

    await migrate(database, directory=MIGRATIONS_DIR)

    remaining = {
        (str(row["survey"]), int(row["tile_order"]))
        for row in await database.fetch_all("SELECT survey, tile_order FROM skymap_tiles")
    }
    assert remaining == {
        ("nsns-halpha", -1),  # properties file: never went through either stretch
        ("legacy-surveys-dr11", 6),  # a different survey entirely
        ("nsns-ohs8", 6),  # composited PNG, never had a fits_stretch to fix
    }


async def test_0015_purges_restricted_cutout_rows_and_bulk_jobs(
    database: Database, tmp_path: Path
) -> None:
    before_0015 = tmp_path / "before-0015"
    before_0015.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) >= 15:
            continue
        shutil.copy(path, before_0015 / path.name)
    await migrate(database, directory=before_0015)

    stamp = "2026-09-13T00:00:00+00:00"
    for cutout_id, source in (("retired", "dss2-color"), ("kept", "unwise")):
        await database.execute(
            "INSERT INTO skymap_cutouts "
            "(id, cache_key, source, ra_deg, dec_deg, size_deg, status, bytes, "
            "created_at, updated_at, last_used) VALUES (?, ?, ?, 1, 2, 0.2, "
            "'complete', 10, ?, ?, ?)",
            (cutout_id, f"{source}:key", source, stamp, stamp, stamp),
        )
        await database.execute(
            "INSERT INTO skymap_cutout_pieces (cutout_id, idx, format, path, bytes) "
            "VALUES (?, 0, 'jpg', ?, 10)",
            (cutout_id, f"{source}/{cutout_id}/0.jpg"),
        )
    await database.execute(
        "INSERT INTO skymap_bulk_cutout_jobs "
        "(id, scope, sources, size_deg, status, created_at, updated_at) "
        "VALUES ('retired-job', 'messier', '[\"dss2-color\"]', 0.2, 'complete', ?, ?)",
        (stamp, stamp),
    )

    await migrate(database, directory=MIGRATIONS_DIR)

    rows = await database.fetch_all("SELECT id, source FROM skymap_cutouts")
    assert [(row["id"], row["source"]) for row in rows] == [("kept", "unwise")]
    pieces = await database.fetch_all("SELECT cutout_id FROM skymap_cutout_pieces")
    assert [row["cutout_id"] for row in pieces] == ["kept"]
    assert await database.fetch_all("SELECT id FROM skymap_bulk_cutout_jobs") == []


async def test_0016_dates_existing_named_plans_for_the_calendar(
    database: Database, tmp_path: Path
) -> None:
    before_0016 = tmp_path / "before-0016"
    before_0016.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) >= 16:
            continue
        shutil.copy(path, before_0016 / path.name)
    await migrate(database, directory=before_0016)
    await database.execute(
        "INSERT INTO plans (id, name, document, revision, created_at, updated_at) "
        "VALUES (?, ?, ?, 1, ?, ?)",
        (
            "legacy-night",
            "Plan for 2026-09-24",
            "{}",
            "2026-09-23T12:00:00+00:00",
            "2026-09-23T12:00:00+00:00",
        ),
    )

    await migrate(database, directory=MIGRATIONS_DIR)

    row = await database.fetch_one(
        "SELECT night_date FROM plans WHERE id = ?", ("legacy-night",)
    )
    assert row is not None
    assert row["night_date"] == "2026-09-24"


@pytest.mark.parametrize("historical", [False, True], ids=["public", "private-history"])
async def test_documentation_only_migration_lineages_upgrade_without_rewriting_markers(
    tmp_path: Path, historical: bool,
) -> None:
    initial = tmp_path / "initial"
    initial.mkdir()
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        version = int(path.name[:4])
        if version > 8:
            continue
        sql = path.read_text()
        if historical and version in {5, 6, 8}:
            assert "THIRD_PARTY_NOTICES.md" in sql
            sql = sql.replace("THIRD_PARTY_NOTICES.md", "docs/COMPLIANCE.md")
        (initial / path.name).write_text(sql)
    database_path = tmp_path / "upgrade.sqlite3"
    db = await open_database(database_path)
    try:
        assert await migrate(db, directory=initial) == 8
        await db.execute("CREATE TABLE user_evidence (value TEXT NOT NULL)")
        await db.execute("INSERT INTO user_evidence VALUES ('preserved')")
        markers = [tuple(row) for row in await db.fetch_all(
            "SELECT * FROM schema_migrations ORDER BY version")]
    finally:
        await db.aclose()
    db = await open_database(database_path)
    try:
        assert await migrate(db) == len(load_migrations())
        assert await migrate(db) == len(load_migrations())
        assert [tuple(row) for row in await db.fetch_all(
            "SELECT * FROM schema_migrations WHERE version <= 8 ORDER BY version")] == markers
        assert (await db.fetch_one("SELECT value FROM user_evidence"))[0] == "preserved"
        assert (await db.fetch_one("PRAGMA integrity_check"))[0] == "ok"
    finally:
        await db.aclose()


@pytest.mark.parametrize("version", [5, 6, 8])
async def test_historical_marker_does_not_authorize_changed_current_sql(
    database: Database, tmp_path: Path, version: int,
) -> None:
    await migrate(database)
    migrations = load_migrations()
    migration = migrations[version - 1]
    historical = migration.sql.replace("THIRD_PARTY_NOTICES.md", "docs/COMPLIANCE.md")
    assert historical != migration.sql
    historical_digest = hashlib.sha256(historical.encode()).hexdigest()
    await database.execute("UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                           (historical_digest, version))
    directory = tmp_path / "altered"
    shutil.copytree(MIGRATIONS_DIR, directory)
    path = next(directory.glob(f"{version:04d}_*.sql"))
    path.write_text(path.read_text() + "\nCREATE TABLE unauthorized_change (id INTEGER);\n")
    with pytest.raises(MigrationError, match="has changed since it was applied"):
        await migrate(database, directory=directory)
    assert await database.fetch_one(
        "SELECT name FROM sqlite_master WHERE name = 'unauthorized_change'") is None
    assert (await database.fetch_one(
        "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)))[0] == historical_digest


@pytest.mark.parametrize("version", [5, 6, 8])
async def test_documentation_alias_rejects_unknown_recorded_digest(
    database: Database, version: int,
) -> None:
    await migrate(database)
    await database.execute("UPDATE schema_migrations SET checksum = ? WHERE version = ?",
                           ("0" * 64, version))
    with pytest.raises(MigrationError, match="has changed since it was applied"):
        await migrate(database)
    assert (await database.fetch_one(
        "SELECT checksum FROM schema_migrations WHERE version = ?", (version,)))[0] == "0" * 64
