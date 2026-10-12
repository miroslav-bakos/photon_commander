# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Bulk target-field cutout prefetch: a queue of individual per-target
cutout requests (``ssc.skymap.cutouts.CutoutService``), over a *scoped*
subset of the bundled catalogue (``ssc.astro.catalog``), so a user can
download imagery for many objects at once for offline planning rather than
one target at a time.

**Never "the whole catalogue".** ``ssc.astro.catalog`` bundles ~13,324
objects; even respecting the one-request-per-host etiquette
(``THIRD_PARTY_NOTICES.md`` "Sky-survey layers"), fetching all of it against up
to three sources would be gigabytes and could run for many hours, and
SDSS/Legacy Surveys have no documented bulk-use policy (that document's
"Endpoint gate" subsection). So a job's scope is one of exactly two things a
user would plausibly image:

* ``messier`` — the 110 Messier objects, the public-domain core, always offered.
* ``magnitude`` — NGC/IC/Sharpless objects at or brighter than a magnitude
  cutoff, refused outright (never silently truncated) above
  :data:`MAX_MAGNITUDE_SCOPE_OBJECTS`.

Kept out of ``cutouts.py`` (already ~950 lines) as its own module: this is a
queueing/scoping policy layered *on top of* the single-target fetch path,
not another archive client. It never talks to an upstream host itself and
never touches ``skymap_cutouts`` directly — every request, the etiquette
(one in-flight request per host), the cache-hit dedup and the LRU/plan
protection are all :class:`~ssc.skymap.cutouts.CutoutService`'s, reused
through its public ``request``/``wait_for``/``row_by_key``, never
duplicated.

Mirrors the durability and interruption discipline of the tile prefetch
jobs (``ssc.skymap.service``) and the single-target cutout jobs
(``ssc.skymap.cutouts``): a job's row is written before the first pair is
dispatched (CLAUDE.md invariant 6), and a restart marks anything still
``queued``/``running`` as ``interrupted`` — nothing resumes on its own.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

from ssc.astro.catalog import CatalogObject, TargetCatalog
from ssc.skymap.cutouts import (
    CutoutRefusedError,
    CutoutService,
    cutout_cache_key,
    now_utc,
    release_tag,
)
from ssc.skymap.surveys import CutoutSource, find_cutout_source
from ssc.state.models import (
    BulkCutoutJobState,
    BulkCutoutJobStatus,
    BulkCutoutScope,
    CutoutJobStatus,
)
from ssc.store.database import Database

_log = logging.getLogger(__name__)

#: A magnitude cutoff producing more objects than this is refused, with the
#: count in the message, rather than silently truncated to the first N.
MAX_MAGNITUDE_SCOPE_OBJECTS: Final = 500
#: How many bulk jobs, newest first, the snapshot lists.
BULK_JOB_HISTORY: Final = 20
#: Pairs dispatched at once. The one-in-flight-per-host limit is
#: CutoutService's own (a per-host ``asyncio.Lock`` shared across every
#: caller); this only bounds how many concurrent per-target fetches this
#: job juggles across *different* hosts at a time.
_DISPATCH_CONCURRENCY: Final = 6
#: A progress write is never more frequent than this — mirrors
#: ``ssc.skymap.service.PUBLISH_INTERVAL_S``.
_PUBLISH_INTERVAL_S: Final = 0.5

#: The scope kinds a request may name — kept as a plain ``Literal`` here
#: (like ``ssc.skymap.service.PrefetchMode``) rather than importing the
#: pydantic-facing :class:`~ssc.state.models.BulkCutoutScope` into this
#: dataclass; :meth:`BulkCutoutService.start` converts once, at the seam.
BulkScopeKind = Literal["messier", "magnitude"]

Spawn = Callable[[Coroutine[Any, Any, Any], str], "asyncio.Task[Any]"]

_PENDING: Final = frozenset({CutoutJobStatus.QUEUED, CutoutJobStatus.RUNNING})


class BulkCutoutRefusedError(ValueError):
    """The request cannot be started (or even estimated) as asked. The
    message says what to change."""


class BulkJobNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class BulkCutoutRequest:
    scope: BulkScopeKind
    #: Required iff ``scope == "magnitude"``; ignored for ``"messier"``.
    magnitude_lte: float | None
    sources: tuple[str, ...]
    size_deg: float


@dataclass(frozen=True, slots=True)
class BulkCutoutEstimate:
    object_count: int
    #: ``object_count * len(sources)``, before removing already-cached pairs.
    pair_count: int
    already_cached: int
    #: ``pair_count - already_cached`` pairs still to fetch, per source id.
    per_source_request_counts: dict[str, int]
    #: Measured mean bytes of a *complete* cutout, per source id — from what
    #: is already cached, the same honest way the tile-pack estimate works.
    #: ``None`` for a source with nothing cached yet.
    mean_bytes_by_source: dict[str, int | None]
    #: ``None`` whenever any source that still has pairs to fetch has an
    #: unknown mean — a partial sum would read as more certain than it is.
    estimated_bytes: int | None


def _messier_source(catalog: TargetCatalog) -> str:
    """The Messier core's own credit line — ``objects.toml`` is always
    loaded first (``ssc.astro.catalog.default_catalog_paths``) and its 110
    objects are exactly the ones it owns (nothing else contributes a new
    object with that file as owner). Reading it off the catalogue itself,
    rather than hardcoding the string here, means this module never drifts
    from whatever ``objects.toml`` actually declares."""
    return catalog.sources[0]


def select_scope(
    catalog: TargetCatalog, scope: BulkScopeKind, magnitude_lte: float | None
) -> tuple[CatalogObject, ...]:
    """The catalogue objects one scope resolves to. Raises
    :class:`BulkCutoutRefusedError` for a magnitude cutoff over the limit —
    never truncates silently (CLAUDE.md invariant 7)."""
    messier_source = _messier_source(catalog)
    if scope == "messier":
        return tuple(obj for obj in catalog.objects if obj.source == messier_source)
    if magnitude_lte is None:
        raise BulkCutoutRefusedError("scope 'magnitude' requires magnitude_lte")
    # Over the *rest* of the catalogue — a Messier object is already covered
    # by the 'messier' scope, so it is never double-offered here even though
    # some (e.g. M31) are also NGC entries.
    objects = tuple(
        obj
        for obj in catalog.objects
        if obj.source != messier_source
        and obj.magnitude is not None
        and obj.magnitude <= magnitude_lte
    )
    if len(objects) > MAX_MAGNITUDE_SCOPE_OBJECTS:
        raise BulkCutoutRefusedError(
            f"magnitude <= {magnitude_lte} over the NGC/IC/Sharpless catalogue is "
            f"{len(objects)} objects; a bulk prefetch is limited to "
            f"{MAX_MAGNITUDE_SCOPE_OBJECTS}. Lower the cutoff."
        )
    return objects


def _sources(source_ids: tuple[str, ...]) -> tuple[CutoutSource, ...]:
    if not source_ids:
        raise BulkCutoutRefusedError("at least one source is required")
    sources: list[CutoutSource] = []
    seen: set[str] = set()
    for source_id in source_ids:
        if source_id in seen:
            continue
        seen.add(source_id)
        source = find_cutout_source(source_id)
        if source is None:
            raise BulkCutoutRefusedError(f"no cutout source {source_id!r}")
        if not source.bulk_prefetch_allowed:
            # This is the enforcement point for the registry's per-source
            # bulk-use decision, not just descriptive metadata.
            raise BulkCutoutRefusedError(
                f"{source.id.value} is opt-in, per-target only; it cannot be used in a bulk prefetch"
            )
        sources.append(source)
    return tuple(sources)


class BulkCutoutService:
    """Scopes a catalogue subset, estimates its cost, and — on confirmation
    — runs it as a durable queue of :meth:`CutoutService.request` calls."""

    def __init__(
        self,
        database: Database,
        cutouts: CutoutService,
        catalog: TargetCatalog,
        *,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self._db = database
        self._cutouts = cutouts
        self._catalog = catalog
        self._clock = clock
        self._now = now
        self._jobs: dict[str, asyncio.Task[Any]] = {}
        self._cancel_requested: set[str] = set()
        self._on_change: Callable[[], Coroutine[Any, Any, None]] | None = None
        self._spawn: Spawn | None = None

    def attach(self, on_change: Callable[[], Coroutine[Any, Any, None]], spawn: Spawn) -> None:
        self._on_change = on_change
        self._spawn = spawn

    async def startup(self) -> None:
        """A restart marks anything still ``queued``/``running`` as
        ``interrupted`` (CLAUDE.md invariant 6) — mirrors
        ``CutoutService.startup``."""
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_bulk_cutout_jobs SET status = ?, error = ?, updated_at = ? "
            "WHERE status IN ('queued', 'running')",
            (
                BulkCutoutJobStatus.INTERRUPTED,
                "ssc-core restarted while this bulk prefetch was running; start it again",
                stamp,
            ),
        )

    async def _publish(self) -> None:
        if self._on_change is not None:
            await self._on_change()

    # -- the snapshot section -------------------------------------------------- #

    async def compose(self) -> list[BulkCutoutJobState]:
        rows = await self._db.fetch_all(
            "SELECT * FROM skymap_bulk_cutout_jobs ORDER BY created_at DESC, id LIMIT ?",
            (BULK_JOB_HISTORY,),
        )
        return [_job_of(row) for row in rows]

    # -- estimate ---------------------------------------------------------------- #

    async def _mean_bytes(self, source_id: str) -> int | None:
        row = await self._db.fetch_one(
            "SELECT AVG(bytes) AS mean FROM skymap_cutouts WHERE status = 'complete' AND source = ?",
            (source_id,),
        )
        if row is None or row["mean"] is None:
            return None
        return int(row["mean"])

    async def estimate(self, request: BulkCutoutRequest) -> BulkCutoutEstimate:
        objects = select_scope(self._catalog, request.scope, request.magnitude_lte)
        sources = _sources(request.sources)
        per_source_counts: dict[str, int] = {}
        mean_by_source: dict[str, int | None] = {}
        already_cached = 0
        estimated_bytes: int | None = 0
        for source in sources:
            need = 0
            for obj in objects:
                ra_deg, dec_deg = float(obj.ra_j2000_deg), float(obj.dec_j2000_deg)
                key = cutout_cache_key(
                    source.id, release_tag(source, dec_deg), ra_deg, dec_deg, request.size_deg
                )
                if await self._cutouts.row_by_key(key) is not None:
                    already_cached += 1
                else:
                    need += 1
            per_source_counts[source.id.value] = need
            mean = await self._mean_bytes(source.id.value)
            mean_by_source[source.id.value] = mean
            if need > 0:
                estimated_bytes = (
                    None
                    if mean is None or estimated_bytes is None
                    else estimated_bytes + mean * need
                )
        return BulkCutoutEstimate(
            object_count=len(objects),
            pair_count=len(objects) * len(sources),
            already_cached=already_cached,
            per_source_request_counts=per_source_counts,
            mean_bytes_by_source=mean_by_source,
            estimated_bytes=estimated_bytes,
        )

    # -- starting / cancelling a job ---------------------------------------------- #

    async def start(self, request: BulkCutoutRequest) -> BulkCutoutJobState:
        if self._spawn is None:
            raise RuntimeError("BulkCutoutService.attach() was not called")
        objects = select_scope(self._catalog, request.scope, request.magnitude_lte)
        sources = _sources(request.sources)
        if any(not task.done() for task in self._jobs.values()):
            raise BulkCutoutRefusedError(
                "a bulk prefetch is already running; wait for it or cancel it"
            )
        pairs: list[tuple[CatalogObject, CutoutSource]] = [
            (obj, source) for obj in objects for source in sources
        ]
        now = self._now()
        job_id = uuid.uuid4().hex[:12]
        scope = BulkCutoutScope(request.scope)
        source_ids = tuple(source.id.value for source in sources)
        job = BulkCutoutJobState(
            id=job_id,
            scope=scope,
            magnitude_lte=request.magnitude_lte,
            sources=source_ids,
            size_deg=request.size_deg,
            status=BulkCutoutJobStatus.QUEUED,
            total=len(pairs),
            created_at=now,
            updated_at=now,
        )
        # Durable before acted on (invariant 6): the row exists before the
        # first pair is dispatched.
        await self._db.execute(
            "INSERT INTO skymap_bulk_cutout_jobs (id, scope, magnitude_lte, sources, size_deg, "
            "status, total, done, skipped, failed, bytes, error, created_at, updated_at, "
            "finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 0, NULL, ?, ?, NULL)",
            (
                job_id,
                scope.value,
                request.magnitude_lte,
                json.dumps(list(source_ids)),
                request.size_deg,
                job.status.value,
                job.total,
                now.isoformat(),
                now.isoformat(),
            ),
        )
        task = self._spawn(self._run(job_id, pairs, request.size_deg), f"bulk-cutout-{job_id}")
        task.add_done_callback(lambda finished: self._reap(job_id, finished))
        self._jobs[job_id] = task
        await self._publish()
        return job

    async def cancel(self, job_id: str) -> None:
        task = self._jobs.get(job_id)
        if task is None or task.done():
            raise BulkJobNotFoundError(f"no running bulk prefetch {job_id!r}")
        self._cancel_requested.add(job_id)
        task.cancel()

    def _reap(self, job_id: str, task: asyncio.Task[Any]) -> None:
        """Give the row a terminal status however the task ended, including
        the ways ``_run``'s own handlers cannot see.

        ``Task.cancel()`` on a task whose coroutine has not taken its first
        step throws into a coroutine that never started, so *no* line of
        ``_run`` runs — its ``try`` included — and the row stays ``queued``
        for the life of the process: ``startup()``'s reaper only runs at
        process start, and :meth:`cancel` refuses a task that is already
        done, so the job can never be cancelled again either. The same held
        for a cancel landing on the ``RUNNING`` write itself before that
        write moved inside the ``try`` below, and it still holds for any
        exception ``_run`` does not catch. A task done-callback fires in all
        of those cases, so reconciliation hangs off that rather than off any
        one of them."""
        if self._spawn is None:  # pragma: no cover — attach() precedes start()
            return
        if task.cancelled():
            requested = job_id in self._cancel_requested
            self._cancel_requested.discard(job_id)
            status = BulkCutoutJobStatus.CANCELLED if requested else BulkCutoutJobStatus.INTERRUPTED
            error = (
                "cancelled by the user"
                if requested
                else "ssc-core shut down while this bulk prefetch was running; start it again"
            )
        else:
            exc = task.exception()
            if exc is None:
                return
            status = BulkCutoutJobStatus.FAILED
            error = f"unexpected error while prefetching: {exc}"
        self._spawn(self._settle(job_id, status, error), f"bulk-cutout-settle-{job_id}")

    async def _settle(self, job_id: str, status: BulkCutoutJobStatus, error: str) -> None:
        """Write ``status`` only if the row is still non-terminal — whenever
        ``_run`` did reach its own handler it has already written a truer
        one, counters and all, and this must not overwrite it. Progress
        counters are left as last published: this knows the job ended, not
        how far it got."""
        row = await self._db.fetch_one(
            "SELECT status FROM skymap_bulk_cutout_jobs WHERE id = ?", (job_id,)
        )
        if row is None or str(row["status"]) not in ("queued", "running"):
            return
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_bulk_cutout_jobs SET status = ?, error = ?, updated_at = ?, "
            "finished_at = ? WHERE id = ?",
            (status.value, error, stamp, stamp, job_id),
        )
        _log.info("bulk cutout %s reaped as %s: %s", job_id, status.value, error)
        await self._publish()

    # -- running the queue --------------------------------------------------------- #

    async def _one(
        self, job_id: str, obj: CatalogObject, source: CutoutSource, size_deg: float
    ) -> tuple[Literal["done", "skipped", "failed"], int]:
        ra_deg, dec_deg = float(obj.ra_j2000_deg), float(obj.dec_j2000_deg)
        key = cutout_cache_key(source.id, release_tag(source, dec_deg), ra_deg, dec_deg, size_deg)
        existing = await self._cutouts.row_by_key(key)
        if existing is not None:
            outcome: Literal["done", "skipped", "failed"] = (
                "skipped" if existing.status == CutoutJobStatus.COMPLETE else "failed"
            )
            _log.info(
                "bulk cutout %s: %s/%s already resolved (%s) -> %s",
                job_id,
                obj.name,
                source.id.value,
                existing.status.value,
                outcome,
            )
            return outcome, 0
        try:
            state = await self._cutouts.request(source.id.value, ra_deg, dec_deg, size_deg, None)
        except CutoutRefusedError as exc:
            _log.warning(
                "bulk cutout %s: %s/%s refused: %s", job_id, obj.name, source.id.value, exc
            )
            return "failed", 0
        final = state if state.status not in _PENDING else await self._cutouts.wait_for(state.id)
        if final.status == CutoutJobStatus.COMPLETE:
            _log.info(
                "bulk cutout %s: %s/%s fetched, %d bytes",
                job_id,
                obj.name,
                source.id.value,
                final.bytes,
            )
            return "done", final.bytes
        _log.info(
            "bulk cutout %s: %s/%s ended %s: %s",
            job_id,
            obj.name,
            source.id.value,
            final.status.value,
            final.error,
        )
        return "failed", 0

    async def _run(
        self, job_id: str, pairs: list[tuple[CatalogObject, CutoutSource]], size_deg: float
    ) -> None:
        done = skipped = failed = 0
        total_bytes = 0
        status = BulkCutoutJobStatus.RUNNING
        error: str | None = None
        last_publish = self._clock()

        def tally(outcome: Literal["done", "skipped", "failed"], size: int) -> None:
            nonlocal done, skipped, failed, total_bytes
            if outcome == "done":
                done += 1
                total_bytes += size
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1

        try:
            # Inside the try, not before it: a cancel delivered while this
            # first write/publish was in flight used to escape `_run`
            # entirely and leave the row at `running` for good.
            await self._write_progress(
                job_id, status, done, skipped, failed, total_bytes, None, finished=False
            )
            await self._publish()
            batch: list[tuple[CatalogObject, CutoutSource]] = []
            for pair in pairs:
                batch.append(pair)
                if len(batch) < _DISPATCH_CONCURRENCY:
                    continue
                for outcome, size in await asyncio.gather(
                    *(self._one(job_id, obj, source, size_deg) for obj, source in batch)
                ):
                    tally(outcome, size)
                batch.clear()
                now_clock = self._clock()
                if now_clock - last_publish >= _PUBLISH_INTERVAL_S:
                    last_publish = now_clock
                    await self._write_progress(
                        job_id, status, done, skipped, failed, total_bytes, None, finished=False
                    )
                    await self._publish()
            if batch:
                for outcome, size in await asyncio.gather(
                    *(self._one(job_id, obj, source, size_deg) for obj, source in batch)
                ):
                    tally(outcome, size)
            status = BulkCutoutJobStatus.COMPLETE
        except asyncio.CancelledError:
            if job_id in self._cancel_requested:
                self._cancel_requested.discard(job_id)
                status = BulkCutoutJobStatus.CANCELLED
                error = "cancelled by the user"
            else:
                status = BulkCutoutJobStatus.INTERRUPTED
                error = "ssc-core shut down while this bulk prefetch was running; start it again"
            await asyncio.shield(
                self._finish(job_id, status, done, skipped, failed, total_bytes, error)
            )
            raise
        await self._finish(job_id, status, done, skipped, failed, total_bytes, error)

    async def _finish(
        self,
        job_id: str,
        status: BulkCutoutJobStatus,
        done: int,
        skipped: int,
        failed: int,
        size: int,
        error: str | None,
    ) -> None:
        await self._write_progress(
            job_id, status, done, skipped, failed, size, error, finished=True
        )
        await self._publish()

    async def _write_progress(
        self,
        job_id: str,
        status: BulkCutoutJobStatus,
        done: int,
        skipped: int,
        failed: int,
        size: int,
        error: str | None,
        *,
        finished: bool,
    ) -> None:
        stamp = self._now().isoformat()
        await self._db.execute(
            "UPDATE skymap_bulk_cutout_jobs SET status = ?, done = ?, skipped = ?, failed = ?, "
            "bytes = ?, error = ?, updated_at = ?, finished_at = ? WHERE id = ?",
            (
                status.value,
                done,
                skipped,
                failed,
                size,
                error,
                stamp,
                stamp if finished else None,
                job_id,
            ),
        )


def _job_of(row: Any) -> BulkCutoutJobState:
    finished = row["finished_at"]
    return BulkCutoutJobState(
        id=str(row["id"]),
        scope=BulkCutoutScope(str(row["scope"])),
        magnitude_lte=None if row["magnitude_lte"] is None else float(row["magnitude_lte"]),
        sources=tuple(json.loads(str(row["sources"]))),
        size_deg=float(row["size_deg"]),
        status=BulkCutoutJobStatus(str(row["status"])),
        total=int(row["total"]),
        done=int(row["done"]),
        skipped=int(row["skipped"]),
        failed=int(row["failed"]),
        bytes=int(row["bytes"]),
        error=None if row["error"] is None else str(row["error"]),
        created_at=datetime.fromisoformat(str(row["created_at"])),
        updated_at=datetime.fromisoformat(str(row["updated_at"])),
        finished_at=None if finished is None else datetime.fromisoformat(str(finished)),
    )


__all__ = [
    "BULK_JOB_HISTORY",
    "MAX_MAGNITUDE_SCOPE_OBJECTS",
    "BulkCutoutEstimate",
    "BulkCutoutRefusedError",
    "BulkCutoutRequest",
    "BulkCutoutService",
    "BulkJobNotFoundError",
    "select_scope",
]
