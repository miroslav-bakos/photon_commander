# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Numbered SQL migrations, applied at startup, forward-only.

Rules this module enforces rather than documents:

* Files are ``NNNN_name.sql`` in :data:`MIGRATIONS_DIR`. The number is the
  version; gaps are an error, not a shrug.
* Applied versions are recorded in ``schema_migrations`` together with the
  SHA-256 of the file. If a file that has already run has changed on disk, the
  startup fails loudly — an edited migration means two installations have
  different schemas under the same version number.
* A database whose version is *ahead* of the shipped migrations is refused
  rather than downgraded. Downgrade is a restore-from-backup operation.
* Nothing here drops or rewrites a column. Forward-only, never destructive.
* Three exact historical checksums remain valid after public documentation
  references changed in SQL comments. Both payload hashes are pinned; arbitrary
  edits remain errors and existing database markers are never rewritten.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from ssc.store.database import Database, StoreError

_log = logging.getLogger(__name__)

MIGRATIONS_DIR: Final = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

# Public-source sanitization changed only docs/COMPLIANCE.md references in
# comments. These exact shipped byte pairs have identical executable SQL.
# Keying on the current checksum prevents this exception accepting later edits.
_HISTORICAL_COMMENT_CHECKSUMS: Final = {
    (5, "cutouts", "0d192bb156206b9fb74418b915246738523efdc8547fe8da49b8749d755b9f34"):
        "fc6ab6f428d998e7b129cb10edac14f58f07907477ba1e7d661003c80fbbb679",
    (6, "bulk_cutouts", "e4dd65677e6b113d02d6bbee02266a3b1a85e251432f129ee51323f51d815f02"):
        "a5e9fb1ce2b850da969c24f4b37ace18cee7aca63519d0f602106812df9674cd",
    (8, "object_info", "246abcb279f1b9483fb3e8e4b404364f2ea5b88ab6ebedc8ee44680dbc0e1783"):
        "7f86028e6a3b918ae46495219dcb62ab0eee4e41a30abe3d42f8511577ee2852",
}

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    checksum   TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
"""


class MigrationError(StoreError):
    """The migration set on disk and the database disagree."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """Read and validate the migration set. Raises on a gap or a bad name."""
    if not directory.is_dir():
        raise MigrationError(f"migrations directory not found: {directory}")

    migrations: list[Migration] = []
    for path in sorted(directory.iterdir()):
        if path.suffix != ".sql":
            continue
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f"migration {path.name!r} is not named NNNN_lower_snake_case.sql")
        migrations.append(
            Migration(
                version=int(match["version"]),
                name=match["name"],
                sql=path.read_text(encoding="utf-8"),
            )
        )

    for expected, migration in enumerate(migrations, start=1):
        if migration.version != expected:
            raise MigrationError(
                f"migration numbering is not contiguous: expected {expected:04d}, "
                f"found {migration.version:04d}_{migration.name}"
            )
    return migrations


async def applied_versions(database: Database) -> dict[int, str]:
    """Version → checksum for everything already applied."""
    await database.executescript(_BOOTSTRAP)
    rows = await database.fetch_all("SELECT version, checksum FROM schema_migrations")
    return {int(row["version"]): str(row["checksum"]) for row in rows}


async def migrate(database: Database, *, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply every pending migration in order. Returns the resulting version."""
    migrations = load_migrations(directory)
    applied = await applied_versions(database)

    known = {migration.version for migration in migrations}
    unknown = sorted(version for version in applied if version not in known)
    if unknown:
        raise MigrationError(
            f"database is at schema version {max(unknown)}, ahead of the {len(migrations)} "
            "migrations shipped with this build. Refusing to run: downgrade is a "
            "restore-from-backup operation, not a migration."
        )

    for migration in migrations:
        recorded = applied.get(migration.version)
        historical = _HISTORICAL_COMMENT_CHECKSUMS.get(
            (migration.version, migration.name, migration.checksum)
        )
        if recorded is not None and recorded != migration.checksum and recorded != historical:
            raise MigrationError(
                f"migration {migration.version:04d}_{migration.name} has changed since "
                "it was applied. Migrations are immutable once shipped — add a new one."
            )
    pending = [migration for migration in migrations if migration.version not in applied]
    backup = await _backup_before_upgrade(database) if pending else None
    for migration in pending:
        _log.info("applying migration %04d_%s", migration.version, migration.name)
        try:
            # executescript commits an existing transaction before executing.
            # Start the transaction *inside* the script and leave it open until
            # its marker is written on the same connection.
            await database.raw.executescript("BEGIN IMMEDIATE;\n" + migration.sql)
            await database.raw.execute(
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.checksum,
                 datetime.now(tz=UTC).isoformat()),
            )
            await database.raw.commit()
        except BaseException as exc:
            await database.raw.rollback()
            if isinstance(exc, sqlite3.Error):
                recovery = f" Restore the consistent pre-upgrade backup at {backup}." if backup else ""
                raise MigrationError(
                    f"Migration {migration.version:04d}_{migration.name} failed; its transaction "
                    "was rolled back. A legacy partial migration must be inspected or restored; "
                    f"no unverified schema was marked as applied. SQLite: {exc}." + recovery
                ) from exc
            raise

    return migrations[-1].version if migrations else 0


async def _backup_before_upgrade(database: Database) -> Path | None:
    """SQLite's online backup includes committed WAL contents in one snapshot."""
    rows = await database.fetch_all("PRAGMA database_list")
    filename = next((str(row["file"]) for row in rows if row["name"] == "main"), "")
    if not filename:
        return None  # SQLite memory databases have no recoverable disk image.
    source = Path(filename)
    backup = source.with_name(f"{source.name}.pre-upgrade-{uuid.uuid4().hex}.sqlite3")
    try:
        # Backups include private settings and sessions, even with a permissive
        # process umask. Create exclusively with owner-only permissions first.
        descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        with closing(sqlite3.connect(backup)) as target:
            await database.raw.backup(target)
    except BaseException:
        backup.unlink(missing_ok=True)
        raise
    _log.info("consistent pre-upgrade database backup: %s", backup)
    return backup


__all__ = ["MIGRATIONS_DIR", "Migration", "MigrationError", "load_migrations", "migrate"]
