# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The ``capture_frame`` table: one row per FITS file SSC writes.

Mirrors ``ssc.store.runs``'s durability discipline: the row is written
*before* the thing it describes is acted on (architecture policy invariant 6). Here
"acted on" is "the FITS bytes are written" — :meth:`CaptureRepository.begin_frame`
inserts a ``'writing'`` row first; only after the file has been fsynced and
renamed into its final path does :meth:`CaptureRepository.complete_frame` flip
it to ``'complete'``. A row still ``'writing'`` after a restart describes a
frame a killed process never finished; :meth:`CaptureRepository.discard_frame`
is the recovery path, not a general delete.

Session grouping is deliberately not a column here — DESIGN.md §9.5.4 wants
it derived purely from ``object``/``date_obs`` on read
(``ssc.capture.catalogue.derive_sessions``), so nothing about this table's
row order or a user's later reorganising of files on disk can desynchronise it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import aiosqlite

from ssc.store.database import Database

_SELECT = (
    "SELECT id, path, object, filter, frame_type, date_obs, exposure_s, gain, ccd_temp_c, "
    "width, height, status, created_at FROM capture_frame"
)


class CaptureFrameNotFoundError(LookupError):
    """No captured frame with that id is stored."""


@dataclass(frozen=True, slots=True)
class CaptureFrameRecord:
    """One catalogued frame, as stored — the read side of the table above.

    ``frame_type`` is the raw ``'light'``/``'dark'``/``'flat'``/``'bias'``
    string — never ``ssc.capture.frame.FrameType`` — for the same reason
    ``status`` is a plain string rather than an enum here: this module sits
    *below* ``ssc.capture`` (``ssc.capture.pipeline`` imports this module,
    not the other way round), so depending on ``ssc.capture.frame`` here
    would import the whole ``ssc.capture`` package back into itself and
    cycle. The SQL ``CHECK`` constraint is what actually bounds this to the
    four known values; a caller that wants the typed enum converts at the
    boundary (``ssc.api.routes.capture``).
    """

    id: int
    path: Path
    object_name: str | None
    filter_name: str | None
    frame_type: str
    date_obs: datetime
    exposure_s: float | None
    gain: int | None
    ccd_temp_c: float | None
    width: int
    height: int
    status: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RunCaptureSummary:
    saved_frames: int
    pending_frames: int
    captured_integration_s: float
    unknown_exposure_frames: int
    accepted_frames: int
    rejected_frames: int
    stack_error_frames: int
    stack_unmeasured_frames: int
    accepted_integration_s: float | None


@dataclass(frozen=True, slots=True)
class HistoricalCaptureFrame:
    """Retained catalogue evidence without filesystem paths or current state."""

    id: int
    run_id: str
    block_id: str | None
    object_name: str | None
    frame_type: str
    status: str
    exposure_s: float | None
    outcome: str | None


class CaptureRepository:
    """Create/complete/discard/list over ``capture_frame``."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def path_exists(self, path: Path) -> bool:
        row = await self._database.fetch_one(
            "SELECT 1 FROM capture_frame WHERE path = ? LIMIT 1", (str(path),)
        )
        return row is not None

    async def begin_frame(
        self,
        *,
        path: Path,
        date_obs: datetime,
        object_name: str | None,
        filter_name: str | None,
        frame_type: str,
        exposure_s: float | None,
        gain: int | None,
        ccd_temp_c: float | None,
        width: int,
        height: int,
        run_id: str | None = None,
        block_id: str | None = None,
        now: datetime | None = None,
    ) -> int:
        """Insert the durable placeholder row. Returns its id.

        ``date_obs`` is SSC's own record of when the exposure started — the
        caller commanded ``StartExposure`` and knows this regardless of
        whether the device's own ``LastExposureStartTime`` property could
        later be read (that fallibility only affects the FITS header card
        SSC authors from it, ``ssc.capture.frame.FrameMetadata.date_obs`` —
        see that module). The catalogue's own timestamp is not allowed to be
        unknown, because every frame must be orderable and groupable into a
        session; the header card and the catalogue's clock are independent
        for exactly this reason.

        ``frame_type`` is the raw ``'light'``/``'dark'``/``'flat'``/``'bias'``
        string (see :class:`CaptureFrameRecord`'s docstring for why this
        layer never imports the enum). The table's own ``CHECK`` constraint
        is the actual guard against anything else reaching this column.

        ``run_id``/``block_id`` tag a frame shot by a scheduled ``take_exposures``
        block (``ssc.scheduler.executor.CapturePort``) with the run and plan
        item it belongs to — ``None``/``None`` for a manual capture, which has
        neither. Set here, in the same durable-before-acted-on insert as
        everything else on this row (CLAUDE.md invariant 6), because
        :meth:`count_committed` — the executor's resume truth — has to be able
        to trust it from the moment the row exists.
        """
        moment = (now or datetime.now(tz=UTC)).astimezone(UTC)
        await self._database.execute(
            "INSERT INTO capture_frame (path, object, filter, frame_type, date_obs, "
            "exposure_s, gain, ccd_temp_c, width, height, run_id, block_id, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'writing', ?)",
            (
                str(path),
                object_name,
                filter_name,
                frame_type,
                date_obs.astimezone(UTC).isoformat(),
                exposure_s,
                gain,
                ccd_temp_c,
                width,
                height,
                run_id,
                block_id,
                moment.isoformat(),
            ),
        )
        row = await self._database.fetch_one("SELECT last_insert_rowid() AS id")
        assert row is not None, "INSERT without a following last_insert_rowid()"
        return int(row["id"])

    async def complete_frame(self, frame_id: int) -> None:
        """Mark a frame's FITS bytes as fsynced and in their final place."""
        row = await self._database.fetch_one(
            "SELECT status FROM capture_frame WHERE id = ?", (frame_id,)
        )
        if row is None:
            raise CaptureFrameNotFoundError(f"no capture frame with id {frame_id}")
        await self._database.execute(
            "UPDATE capture_frame SET status = 'complete' WHERE id = ?", (frame_id,)
        )

    async def discard_frame(self, frame_id: int) -> Path:
        """Remove a row a killed process left ``'writing'`` (the invariant 6
        resume path), returning its path so the caller can also remove
        whatever bytes made it to disk. Refuses a ``'complete'`` row — this
        is crash recovery, not a general delete."""
        row = await self._database.fetch_one(
            "SELECT path, status FROM capture_frame WHERE id = ?", (frame_id,)
        )
        if row is None:
            raise CaptureFrameNotFoundError(f"no capture frame with id {frame_id}")
        if str(row["status"]) != "writing":
            raise ValueError(
                f"capture frame {frame_id} is {row['status']!r}, not 'writing'; "
                "refusing to discard a completed frame"
            )
        await self._database.execute("DELETE FROM capture_frame WHERE id = ?", (frame_id,))
        return Path(str(row["path"]))

    async def delete_complete_frame(self, frame_id: int) -> Path:
        """Delete a completed frame row, returning its path for byte cleanup."""
        row = await self._database.fetch_one(
            "SELECT path, status FROM capture_frame WHERE id = ?", (frame_id,)
        )
        if row is None:
            raise CaptureFrameNotFoundError(f"no capture frame with id {frame_id}")
        if str(row["status"]) != "complete":
            raise ValueError(f"capture frame {frame_id} is not complete")
        await self._database.execute("DELETE FROM capture_frame WHERE id = ?", (frame_id,))
        return Path(str(row["path"]))

    async def move_complete_frame(self, frame_id: int, destination: Path) -> None:
        """Update the path of a completed frame after its bytes were moved."""
        row = await self._database.fetch_one(
            "SELECT status FROM capture_frame WHERE id = ?", (frame_id,)
        )
        if row is None:
            raise CaptureFrameNotFoundError(f"no capture frame with id {frame_id}")
        if str(row["status"]) != "complete":
            raise ValueError(f"capture frame {frame_id} is not complete")
        await self._database.execute(
            "UPDATE capture_frame SET path = ? WHERE id = ?", (str(destination), frame_id)
        )

    async def get_frame(self, frame_id: int) -> CaptureFrameRecord:
        """One frame by id, whatever its status — a plain accessor. Deciding
        whether a ``'writing'`` row is safe to act on (e.g. serve its bytes)
        is the caller's concern, not this repository's."""
        row = await self._database.fetch_one(f"{_SELECT} WHERE id = ?", (frame_id,))
        if row is None:
            raise CaptureFrameNotFoundError(f"no capture frame with id {frame_id}")
        return _record_of(row)

    async def list_writing(self) -> list[CaptureFrameRecord]:
        """Rows a previous process left mid-write — what a restart's
        reconciliation pass must resolve before the catalogue is trusted."""
        rows = await self._database.fetch_all(f"{_SELECT} WHERE status = 'writing'")
        return [_record_of(row) for row in rows]

    async def list_block_frames(
        self, run_id: str, block_id: str, *, include_writing: bool = True
    ) -> list[CaptureFrameRecord]:
        """Read a persisted item in acquisition order without mixing other runs."""
        statuses = "('complete', 'writing')" if include_writing else "('complete')"
        rows = await self._database.fetch_all(
            f"{_SELECT} WHERE run_id = ? AND block_id = ? AND status IN {statuses} "
            "ORDER BY date_obs, id", (run_id, block_id),
        )
        return [_record_of(row) for row in rows]

    async def list_complete(self, *, object_name: str | None = None) -> list[CaptureFrameRecord]:
        if object_name is None:
            rows = await self._database.fetch_all(f"{_SELECT} WHERE status = 'complete'")
        else:
            rows = await self._database.fetch_all(
                f"{_SELECT} WHERE status = 'complete' AND object = ? ORDER BY date_obs",
                (object_name,),
            )
        return [_record_of(row) for row in rows]

    async def list_recent(self, *, limit: int) -> list[CaptureFrameRecord]:
        """The most recently started complete frames, newest first — what
        the capture catalogue's own list view wants (``GET
        /api/capture/frames``), as opposed to :meth:`list_complete`'s
        unordered whole-catalogue read that session derivation
        (``ssc.capture.catalogue.derive_sessions``) sorts for itself."""
        rows = await self._database.fetch_all(
            f"{_SELECT} WHERE status = 'complete' ORDER BY date_obs DESC LIMIT ?",
            (limit,),
        )
        return [_record_of(row) for row in rows]

    async def record_stack_outcome(
        self, frame_id: int, outcome: Literal["accepted", "rejected", "error"]
    ) -> None:
        # Replaying the same saved frame after restart replaces evidence, never doubles it.
        await self._database.execute(
            "INSERT INTO capture_stack_outcome (frame_id, run_id, exposure_s, outcome) "
            "SELECT id, run_id, exposure_s, ? FROM capture_frame WHERE id = ? "
            "ON CONFLICT(frame_id) DO UPDATE SET outcome = excluded.outcome",
            (outcome, frame_id),
        )

    async def historical_frames(self, run_ids: Sequence[str]) -> list[HistoricalCaptureFrame]:
        """Read each retained frame once; removed outcomes cannot inflate totals."""
        if not run_ids:
            return []
        placeholders = ",".join("?" for _ in run_ids)
        rows = await self._database.fetch_all(
            "SELECT f.id, f.run_id, f.block_id, f.object, f.frame_type, f.status, f.exposure_s, s.outcome "
            "FROM capture_frame f LEFT JOIN capture_stack_outcome s ON s.frame_id = f.id "
            f"WHERE f.run_id IN ({placeholders}) ORDER BY f.id",
            tuple(run_ids),
        )
        return [
            HistoricalCaptureFrame(
                id=int(row["id"]),
                run_id=str(row["run_id"]),
                block_id=row["block_id"],
                object_name=row["object"],
                frame_type=str(row["frame_type"]),
                status=str(row["status"]),
                exposure_s=None if row["exposure_s"] is None else float(row["exposure_s"]),
                outcome=row["outcome"],
            )
            for row in rows
        ]


    async def run_summary(self, run_id: str) -> RunCaptureSummary:
        row = await self._database.fetch_one(
            "SELECT COUNT(CASE WHEN f.status = 'complete' THEN 1 END) AS saved, "
            "COUNT(CASE WHEN f.status = 'writing' THEN 1 END) AS pending, "
            "COALESCE(SUM(CASE WHEN f.status = 'complete' AND f.frame_type = 'light' THEN f.exposure_s END), 0) AS integration, "
            "COUNT(CASE WHEN f.status = 'complete' AND f.frame_type = 'light' AND f.exposure_s IS NULL THEN 1 END) AS unknown_exposure, "
            "COUNT(CASE WHEN f.status = 'complete' AND s.outcome = 'accepted' THEN 1 END) AS accepted, "
            "COUNT(CASE WHEN f.status = 'complete' AND s.outcome = 'rejected' THEN 1 END) AS rejected, "
            "COUNT(CASE WHEN f.status = 'complete' AND s.outcome = 'error' THEN 1 END) AS errors, "
            "COUNT(CASE WHEN f.status = 'complete' AND f.frame_type = 'light' AND s.outcome IS NULL THEN 1 END) AS unmeasured, "
            "SUM(CASE WHEN f.status = 'complete' AND s.outcome = 'accepted' THEN f.exposure_s END) AS accepted_integration "
            "FROM capture_frame f LEFT JOIN capture_stack_outcome s ON s.frame_id = f.id WHERE f.run_id = ?",
            (run_id,),
        )
        assert row is not None
        outcomes = await self._database.fetch_one(
            "SELECT COUNT(CASE WHEN outcome = 'accepted' THEN 1 END) AS accepted, "
            "COUNT(CASE WHEN outcome = 'rejected' THEN 1 END) AS rejected, "
            "COUNT(CASE WHEN outcome = 'error' THEN 1 END) AS errors, "
            "SUM(CASE WHEN outcome = 'accepted' THEN exposure_s END) AS integration "
            "FROM capture_stack_outcome WHERE run_id = ?", (run_id,),
        )
        assert outcomes is not None
        return RunCaptureSummary(
            saved_frames=int(row["saved"]), pending_frames=int(row["pending"]),
            captured_integration_s=float(row["integration"]), unknown_exposure_frames=int(row["unknown_exposure"]),
            accepted_frames=int(outcomes["accepted"]), rejected_frames=int(outcomes["rejected"]),
            stack_error_frames=int(outcomes["errors"]), stack_unmeasured_frames=int(row["unmeasured"]),
            accepted_integration_s=None if outcomes["integration"] is None else float(outcomes["integration"]),
        )

    async def count_committed(self, *, run_id: str, block_id: str) -> int:
        """How many of this block's frames are durably catalogued —
        ``ssc.scheduler.executor.CapturePort.frames_captured``'s own contract:
        the count a resumed run trusts is the rows on disk, never a counter
        anything kept in memory. A ``'writing'`` row (a frame mid-exposure, or
        one a killed process never finished) never counts; only ``'complete'``
        does."""
        row = await self._database.fetch_one(
            "SELECT COUNT(*) AS n FROM capture_frame WHERE run_id = ? AND block_id = ? "
            "AND status = 'complete'",
            (run_id, block_id),
        )
        assert row is not None, "COUNT(*) always returns exactly one row"
        return int(row["n"])


def _record_of(row: aiosqlite.Row) -> CaptureFrameRecord:
    return CaptureFrameRecord(
        id=int(row["id"]),
        path=Path(str(row["path"])),
        object_name=None if row["object"] is None else str(row["object"]),
        filter_name=None if row["filter"] is None else str(row["filter"]),
        frame_type=str(row["frame_type"]),
        date_obs=datetime.fromisoformat(str(row["date_obs"])),
        exposure_s=None if row["exposure_s"] is None else float(row["exposure_s"]),
        gain=None if row["gain"] is None else int(row["gain"]),
        ccd_temp_c=None if row["ccd_temp_c"] is None else float(row["ccd_temp_c"]),
        width=int(row["width"]),
        height=int(row["height"]),
        status=str(row["status"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
    )


__all__ = ["CaptureFrameNotFoundError", "CaptureFrameRecord", "CaptureRepository"]
