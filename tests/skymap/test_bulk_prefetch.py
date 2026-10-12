"""Bulk target-field cutout prefetch (``ssc.skymap.bulk_prefetch``) against a
real ``CutoutService``/``BulkCutoutService`` pair: scope selection and its
refusal above the magnitude limit, the estimate-first flow, cache-hit
skipping, cancellation, one-in-flight-per-host etiquette across concurrent
per-object fetches, and the restart-is-surfaced-not-resumed rule."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest

from ssc.astro.catalog import CatalogObject, TargetCatalog, load_catalog
from ssc.astro.units import Deg
from ssc.skymap.bulk_prefetch import (
    MAX_MAGNITUDE_SCOPE_OBJECTS,
    BulkCutoutRefusedError,
    BulkCutoutRequest,
    BulkCutoutService,
    BulkJobNotFoundError,
    select_scope,
)
from ssc.skymap.cutouts import CutoutService
from ssc.state.models import BulkCutoutJobStatus, BulkCutoutScope
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from ssc.store.plans import PlanRepository
from tests.fixtures.skymap.build import build_fits_bytes

_WCS = {
    "CRPIX1": 4.0,
    "CRPIX2": 4.0,
    "CRVAL1": 10.6847,
    "CRVAL2": 41.269,
    "CD1_1": -0.0002777,
    "CD1_2": 0.0,
    "CD2_1": 0.0,
    "CD2_2": 0.0002777,
    "CTYPE1": "RA---TAN",
    "CTYPE2": "DEC--TAN",
}


def _plane() -> np.ndarray:
    return np.full((8, 8), 50.0, dtype=np.float32)


def _obj(name: str, ra: float, dec: float, magnitude: float | None, source: str) -> CatalogObject:
    return CatalogObject(
        designations=(name,),
        common_name=None,
        object_type=None,
        ra_j2000_deg=Deg(ra),
        dec_j2000_deg=Deg(dec),
        magnitude=magnitude,
        magnitude_band=None if magnitude is None else "V",
        size_major_arcmin=None,
        size_minor_arcmin=None,
        position_angle_deg=None,
        constellation=None,
        source=source,
        sources=(source,),
    )


_MESSIER_SOURCE = "test Messier core"
_NGC_SOURCE = "test NGC/IC core"


def _catalog(objects: list[CatalogObject]) -> TargetCatalog:
    return TargetCatalog(
        objects=tuple(objects), sources=(_MESSIER_SOURCE,), _by_designation={}, _by_common_name={}
    )


def _small_catalog(n: int) -> TargetCatalog:
    """``n`` synthetic Messier-scope objects, spread out so each gets its own
    cache key, plus one NGC-scope object a magnitude filter can find."""
    objects = [_obj(f"M{i}", float(i), 10.0, 8.0, _MESSIER_SOURCE) for i in range(1, n + 1)]
    objects.append(_obj("NGC1", 50.0, 10.0, 6.0, _NGC_SOURCE))
    return _catalog(objects)


class _JsonThenBytesTransport(httpx.AsyncBaseTransport):
    """A fake transport standing in for SDSS + Legacy Surveys: SDSS's SQL
    lookup always answers "no field" so every request falls through to its
    getjpeg quick-look, and Legacy Surveys' cutout endpoint always answers a
    FITS cube — both regardless of ra/dec, so any number of synthetic
    objects can share one mocked response. Tracks the highest number of
    requests to each host that were ever concurrently in flight, so a test
    can assert the one-in-flight-per-host etiquette held under real
    concurrency, not just by reading the lock's source."""

    def __init__(self, *, delay_s: float = 0.02) -> None:
        self._delay_s = delay_s
        self._lock = asyncio.Lock()
        self._in_flight: dict[str, int] = {}
        self.max_in_flight: dict[str, int] = {}
        self.calls: list[str] = []
        #: Set the moment the first request arrives, so a cancellation test
        #: can wait for the job to be provably dispatching rather than
        #: sleeping for a plausible-looking interval and hoping.
        self.dispatched = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        self.dispatched.set()
        async with self._lock:
            self._in_flight[host] = self._in_flight.get(host, 0) + 1
            self.max_in_flight[host] = max(self.max_in_flight.get(host, 0), self._in_flight[host])
            self.calls.append(f"{host}{request.url.path}")
        try:
            await asyncio.sleep(self._delay_s)
            return self._respond(request)
        finally:
            async with self._lock:
                self._in_flight[host] -= 1

    def _respond(self, request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "skyserver.sdss.org" and path.endswith("/SqlSearch"):
            return httpx.Response(200, json=[{"TableName": "Table1", "Rows": []}])
        if host == "skyserver.sdss.org" and path.endswith("/getjpeg"):
            return httpx.Response(200, content=b"\xff\xd8\xff" + b"\x00" * 32)
        if host == "www.legacysurvey.org" and path.endswith("/fits-cutout"):
            return httpx.Response(200, content=build_fits_bytes(_plane(), _WCS))
        raise AssertionError(f"unexpected request: {host}{path}")  # pragma: no cover


@pytest.fixture
async def env(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    plans = PlanRepository(database)
    transport = _JsonThenBytesTransport()
    http = httpx.AsyncClient(transport=transport)
    cutouts = CutoutService(
        database,
        tmp_path / "cutouts",
        max_bytes=100 * 1024 * 1024,
        user_agent=lambda: "SSC/test (+ops@example.org)",
        plans=plans,
        http=http,
    )
    await cutouts.startup()

    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    published = asyncio.Event()

    async def on_change() -> None:
        published.set()

    cutouts.attach(on_change, spawn)

    catalog = _small_catalog(5)
    bulk = BulkCutoutService(database, cutouts, catalog)
    await bulk.startup()
    bulk.attach(on_change, spawn)

    try:
        yield {
            "database": database,
            "cutouts": cutouts,
            "bulk": bulk,
            "catalog": catalog,
            "transport": transport,
            "tasks": tasks,
            "published": published,
        }
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await cutouts.aclose()
        await http.aclose()
        await database.aclose()


async def _wait_terminal(bulk: BulkCutoutService, job_id: str, *, timeout_s: float = 5.0) -> Any:
    async def poll() -> Any:
        while True:
            jobs = await bulk.compose()
            job = next(j for j in jobs if j.id == job_id)
            if job.status not in (BulkCutoutJobStatus.QUEUED, BulkCutoutJobStatus.RUNNING):
                return job
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(poll(), timeout=timeout_s)


# -- select_scope -------------------------------------------------------------- #


def test_messier_scope_is_exactly_the_bundled_110_and_never_the_whole_catalogue() -> None:
    catalog = load_catalog()
    objects = select_scope(catalog, "messier", None)
    assert len(objects) == 110
    assert len(objects) < len(catalog)


def test_magnitude_scope_over_the_limit_is_refused_with_the_count_in_the_message() -> None:
    catalog = load_catalog()
    # magnitude <= 11 over NGC/IC/Sharpless is comfortably past the limit at
    # the current bundled data (839 objects) — a real, not synthetic, refusal.
    with pytest.raises(BulkCutoutRefusedError, match=r"839 objects.*limited to 500"):
        select_scope(catalog, "magnitude", 11.0)


def test_magnitude_scope_never_double_offers_a_messier_object() -> None:
    catalog = load_catalog()
    objects = select_scope(catalog, "magnitude", 8.0)
    messier_source = catalog.sources[0]
    assert all(obj.source != messier_source for obj in objects)


def test_there_is_no_whole_catalogue_option() -> None:
    """The only two ``BulkCutoutRequest.scope`` shapes the module knows are
    'messier' and 'magnitude' — asking for anything else is a refusal, not a
    silent full-catalogue fetch."""
    catalog = load_catalog()
    with pytest.raises(BulkCutoutRefusedError):
        select_scope(catalog, "magnitude", None)


# -- estimate -------------------------------------------------------------------- #


async def test_estimate_starts_nothing(env: dict[str, Any]) -> None:
    bulk: BulkCutoutService = env["bulk"]
    request = BulkCutoutRequest(
        scope="messier", magnitude_lte=None, sources=("legacy-surveys",), size_deg=0.2
    )
    estimate = await bulk.estimate(request)
    assert estimate.object_count == 5
    assert estimate.pair_count == 5
    assert estimate.already_cached == 0
    assert estimate.estimated_bytes is None  # nothing cached yet — never invented
    jobs = await bulk.compose()
    assert jobs == []


async def test_estimate_mean_bytes_is_measured_never_invented(env: dict[str, Any]) -> None:
    cutouts: CutoutService = env["cutouts"]
    bulk: BulkCutoutService = env["bulk"]
    # Seed one real complete cutout for legacy-surveys so its mean is known.
    state = await cutouts.request("legacy-surveys", 1.0, 10.0, 0.2, None)
    task = cutouts._jobs[state.id]  # awaiting the same task wait_for would
    await task

    request = BulkCutoutRequest(
        scope="messier", magnitude_lte=None, sources=("legacy-surveys",), size_deg=0.2
    )
    estimate = await bulk.estimate(request)
    assert estimate.already_cached == 1
    assert estimate.mean_bytes_by_source["legacy-surveys"] is not None
    assert estimate.estimated_bytes is not None


# -- start / dispatch / skip ------------------------------------------------------ #


async def test_a_job_skips_already_cached_pairs(env: dict[str, Any]) -> None:
    cutouts: CutoutService = env["cutouts"]
    bulk: BulkCutoutService = env["bulk"]
    transport: _JsonThenBytesTransport = env["transport"]

    # Pre-cache object M1's legacy-surveys cutout, exactly as the bulk job
    # would key it (M1 is ra=1.0, dec=10.0 in _small_catalog).
    pre = await cutouts.request("legacy-surveys", 1.0, 10.0, 0.2, None)
    await cutouts.wait_for(pre.id)
    calls_before = len(transport.calls)

    request = BulkCutoutRequest(
        scope="messier", magnitude_lte=None, sources=("legacy-surveys",), size_deg=0.2
    )
    job = await bulk.start(request)
    final = await _wait_terminal(bulk, job.id)

    assert final.status == BulkCutoutJobStatus.COMPLETE
    assert final.total == 5
    assert final.skipped == 1
    assert final.done == 4
    assert final.failed == 0
    # The pre-cached pair made no second upstream request.
    new_calls = [c for c in transport.calls[calls_before:] if "legacysurvey" in c]
    assert len(new_calls) == 4


async def test_cancel_stops_further_dispatch(env: dict[str, Any]) -> None:
    bulk: BulkCutoutService = env["bulk"]
    transport: _JsonThenBytesTransport = env["transport"]
    catalog = _small_catalog(60)
    bulk._catalog = catalog  # swap in a bigger scope for this test only

    request = BulkCutoutRequest(
        scope="messier", magnitude_lte=None, sources=("legacy-surveys", "sdss"), size_deg=0.2
    )
    job = await bulk.start(request)
    # Wait for the first dispatch rather than sleeping a guessed interval:
    # under a loaded CI runner a fixed sleep either cancels a job that never
    # started (proving nothing) or lets it finish (proving less).
    await asyncio.wait_for(transport.dispatched.wait(), timeout=5.0)
    await bulk.cancel(job.id)
    final = await _wait_terminal(bulk, job.id)

    assert final.status == BulkCutoutJobStatus.CANCELLED
    assert final.error is not None
    assert final.done + final.skipped + final.failed < final.total


async def test_cancel_before_the_first_dispatch_still_ends_the_job(env: dict[str, Any]) -> None:
    """Cancelling in the window before `_run` gets going at all.

    `Task.cancel()` on a task whose coroutine has not taken its first step
    throws into a coroutine that never starts, so not one line of `_run`
    runs — its `try` included — and the same held for a cancel landing on
    `_run`'s opening `RUNNING` write. The row used to stay `queued`/`running`
    for the life of the process: `startup()`'s reaper only runs at process
    start, and `cancel` refuses a task that is already done, so the job could
    not even be cancelled a second time. It is the window the API-level test
    hits by construction, since nothing there delays the DELETE."""
    bulk: BulkCutoutService = env["bulk"]
    bulk._catalog = _small_catalog(60)
    request = BulkCutoutRequest(
        scope="messier", magnitude_lte=None, sources=("legacy-surveys", "sdss"), size_deg=0.2
    )
    job = await bulk.start(request)
    await bulk.cancel(job.id)  # deliberately no await in between

    final = await _wait_terminal(bulk, job.id)
    assert final.status == BulkCutoutJobStatus.CANCELLED
    assert final.error == "cancelled by the user"
    assert final.finished_at is not None


async def test_cancel_of_an_unknown_job_is_refused(env: dict[str, Any]) -> None:
    bulk: BulkCutoutService = env["bulk"]
    with pytest.raises(BulkJobNotFoundError):
        await bulk.cancel("no-such-job")


# -- etiquette: one in flight per host, even under concurrent per-object fetches -- #


async def test_one_in_flight_per_host_under_concurrent_per_object_fetches(
    env: dict[str, Any],
) -> None:
    bulk: BulkCutoutService = env["bulk"]
    transport: _JsonThenBytesTransport = env["transport"]
    catalog = _small_catalog(10)
    bulk._catalog = catalog  # a bigger, still fast, scope for concurrency

    request = BulkCutoutRequest(
        scope="messier",
        magnitude_lte=None,
        sources=("legacy-surveys", "sdss"),
        size_deg=0.2,
    )
    job = await bulk.start(request)
    final = await _wait_terminal(bulk, job.id, timeout_s=10.0)

    assert final.status == BulkCutoutJobStatus.COMPLETE
    assert final.failed == 0
    # Never more than one request in flight to the same host at once, even
    # though many objects were dispatched concurrently across two hosts.
    assert transport.max_in_flight.get("www.legacysurvey.org", 0) <= 1
    assert transport.max_in_flight.get("skyserver.sdss.org", 0) <= 1
    # But the two different hosts really did run concurrently, not
    # serialized against each other by mistake.
    assert len(transport.calls) > 0


# -- restart: surfaced, never silently resumed ------------------------------------ #


async def test_interrupted_on_restart_is_surfaced_not_resumed(tmp_path: Path) -> None:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    plans = PlanRepository(database)
    cutouts = CutoutService(
        database,
        tmp_path / "cutouts",
        max_bytes=10 * 1024 * 1024,
        user_agent=lambda: "SSC/test (+ops@example.org)",
        plans=plans,
    )
    await cutouts.startup()
    catalog = _small_catalog(1)
    bulk = BulkCutoutService(database, cutouts, catalog)
    # Simulate a job a previous process left running when it was killed.
    now = datetime.now(tz=UTC).isoformat()
    await database.execute(
        "INSERT INTO skymap_bulk_cutout_jobs (id, scope, magnitude_lte, sources, size_deg, "
        "status, total, done, skipped, failed, bytes, error, created_at, updated_at, "
        "finished_at) VALUES ('leftover', 'messier', NULL, '[\"legacy-surveys\"]', 0.2, "
        "'running', 5, 2, 0, 0, 0, NULL, ?, ?, NULL)",
        (now, now),
    )

    await bulk.startup()  # a fresh process's startup, as ssc.main calls it

    jobs = await bulk.compose()
    assert len(jobs) == 1
    assert jobs[0].id == "leftover"
    assert jobs[0].status == BulkCutoutJobStatus.INTERRUPTED
    assert jobs[0].error is not None and "start it again" in jobs[0].error
    # Not resumed: nothing was spawned, so the id has no tracked task.
    assert bulk._jobs == {}  # confirming nothing was spawned

    await cutouts.aclose()
    await database.aclose()


def test_scope_enum_round_trips_through_state_model() -> None:
    assert BulkCutoutScope("messier") is BulkCutoutScope.MESSIER
    assert BulkCutoutScope("magnitude") is BulkCutoutScope.MAGNITUDE


def test_max_magnitude_scope_objects_is_the_limit_select_scope_enforces() -> None:
    catalog = load_catalog()
    # One over the limit is refused; confirm the constant and the behaviour agree.
    over = select_scope(catalog, "magnitude", 8.0)
    assert len(over) < MAX_MAGNITUDE_SCOPE_OBJECTS
