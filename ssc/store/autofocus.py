# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Retain the latest autofocus presentation, without replaying device actions."""

from __future__ import annotations

from datetime import UTC, datetime

from ssc.state.models import AutofocusState
from ssc.store.database import Database


def restored_state(state: AutofocusState) -> AutofocusState:
    """A saved running measurement is interrupted, never resumed or accepted."""
    if state.status != "running":
        return state
    message = "Autofocus was interrupted by a backend restart; focus position is unverified."
    if state.error:
        message = f"{message} Previous error: {state.error}"
    return state.model_copy(
        update={
            "status": "interrupted",
            "stage": None,
            "final_position": None,
            "completed_at": None,
            "error": message,
        }
    )


class AutofocusRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def get(self) -> AutofocusState | None:
        row = await self._database.fetch_one("SELECT state_json FROM autofocus_latest WHERE id = 1")
        return None if row is None else AutofocusState.model_validate_json(row["state_json"])

    async def save(self, state: AutofocusState) -> None:
        await self._database.execute(
            """INSERT INTO autofocus_latest (id, state_json, updated_at)
               VALUES (1, ?, ?) ON CONFLICT (id) DO UPDATE SET
               state_json = excluded.state_json, updated_at = excluded.updated_at""",
            (state.model_dump_json(), datetime.now(UTC).isoformat()),
        )
