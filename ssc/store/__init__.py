# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""SQLite persistence: connection pragmas, numbered migrations."""

from ssc.store.database import (
    MEMORY_URI,
    Database,
    StoreError,
    database,
    open_database,
)
from ssc.store.migrations import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    load_migrations,
    migrate,
)

__all__ = [
    "MEMORY_URI",
    "MIGRATIONS_DIR",
    "Database",
    "Migration",
    "MigrationError",
    "StoreError",
    "database",
    "load_migrations",
    "migrate",
    "open_database",
]
