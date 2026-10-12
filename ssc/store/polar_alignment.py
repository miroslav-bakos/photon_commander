# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Complete polar-alignment presentation retained per observing night."""
from __future__ import annotations

from datetime import UTC, date, datetime

from ssc.state.models import PolarAlignmentState
from ssc.store.database import Database


class PolarAlignmentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self, night_date: date, timezone: str) -> PolarAlignmentState | None:
        row = await self._database.fetch_one(
            "SELECT state_json FROM polar_alignment_night WHERE night_date = ? AND timezone = ?",
            (night_date.isoformat(), timezone),
        )
        return None if row is None else PolarAlignmentState.model_validate_json(row["state_json"])

    async def save(self, night_date: date, timezone: str, state: PolarAlignmentState) -> None:
        await self._database.execute(
            """INSERT INTO polar_alignment_night (night_date, timezone, state_json, updated_at)
               VALUES (?, ?, ?, ?) ON CONFLICT (night_date, timezone) DO UPDATE SET
               state_json = excluded.state_json, updated_at = excluded.updated_at""",
            (night_date.isoformat(), timezone, state.model_dump_json(), datetime.now(UTC).isoformat()),
        )
