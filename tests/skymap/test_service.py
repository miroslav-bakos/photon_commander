"""Viewed sky tiles remain available offline; limits stop new writes, not old tiles."""

from __future__ import annotations

import asyncio
import re
import shutil
from collections.abc import AsyncIterator, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pytest
import respx

from ssc.skymap import mercator
from ssc.skymap.cache import TileCache
from ssc.skymap.service import (
    PrefetchRefusedError,
    PrefetchRequest,
    SkymapService,
    build_user_agent,
)
from ssc.skymap.surveys import Survey, SurveyKind, TileScheme
from ssc.state.models import PrefetchJobStatus, StateSnapshot
from ssc.state.store import StateStore
from ssc.store.database import open_database
from ssc.store.migrations import migrate

SURVEY_ID = "legacy-surveys-dr11"
UPSTREAM = re.compile(r"https://www\.legacysurvey\.org/viewer/ls-dr11/1/.*\.jpg")
PNG = b"\xff\xd8\xff" + b"\x00" * 97  # 100 bytes, jpg-ish magic


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[tuple[SkymapService, StateStore]]:
    database = await open_database(tmp_path / "ssc.sqlite3")
    await migrate(database)
    cache = TileCache(database, tmp_path / "tiles")
    # Room for three tiles and a bit; the AppConfig floor of 64 MiB is for
    # real installs, the service itself takes any budget.
    skymap = SkymapService(
        cache,
        max_bytes=3 * len(PNG) + 10,
        user_agent=lambda: build_user_agent("ops@example.org"),
        bundled_root=tmp_path / "bundled",
    )
    initial = await skymap.startup()
    state = StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC), skymap=initial))
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task

    skymap.attach(state, spawn)
    try:
        yield skymap, state
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await skymap.aclose()
        await database.aclose()


async def _prefetch_sky(skymap: SkymapService, max_order: int) -> None:
    job = await skymap.start_prefetch(
        PrefetchRequest(survey=SURVEY_ID, mode="sky", max_order=max_order)
    )
    task = skymap._jobs[job.id]
    await task


def _survey_state(state: StateStore) -> Any:
    return next(s for s in state.snapshot.skymap.surveys if s.id == SURVEY_ID)


async def test_browsing_retains_old_tiles_when_cache_limit_is_reached(
    service: tuple[SkymapService, StateStore],
) -> None:
    skymap, state = service
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        await _prefetch_sky(skymap, 0)  # the one level-0 tile, pinned
        assert _survey_state(state).pack_complete_order == 0

        # Browse five level-3 tiles: the budget holds three tiles in total.
        for x in range(5):
            await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, x, 0), "jpg")

    await skymap.publish()
    survey = _survey_state(state)
    assert survey.cached_tiles == 3, "one pack tile + the first two browsing tiles"
    assert survey.pack_complete_order == 0, "the pack tile survived"
    assert state.snapshot.skymap.cache_bytes <= 3 * len(PNG) + 10
    with respx.mock(assert_all_called=False) as router:
        route = router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        # Both the pack and first browsed tile remain on disk; later tiles
        # were served but not retained after the cap was reached.
        assert (await skymap.get_tile(SURVEY_ID, 0, 0, "jpg")).source == "cache"
        assert (
            await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 0, 0), "jpg")
        ).source == "cache"
        assert route.call_count == 0
        assert (
            await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 4, 0), "jpg")
        ).source == "upstream"
        assert route.call_count == 1


async def test_a_browsed_tile_inside_a_completed_pack_order_is_protected_too(
    service: tuple[SkymapService, StateStore],
) -> None:
    """A tile fetched while browsing, then covered by a pack, stays cached."""
    skymap, state = service
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        await skymap.get_tile(SURVEY_ID, 0, 0, "jpg")  # browsed, not pinned
        await _prefetch_sky(skymap, 0)  # nothing to fetch; the pack is complete
        job = state.snapshot.skymap.jobs[0]
        assert job.status is PrefetchJobStatus.COMPLETE and job.bytes == 0
        assert _survey_state(state).pack_complete_order == 0
        for x in range(4):
            await skymap.get_tile(SURVEY_ID, 2, mercator.pack(2, x, 0), "jpg")
    await skymap.publish()
    assert _survey_state(state).pack_complete_order == 0, "the level-0 tile was not evicted"
    assert _survey_state(state).cached_tiles == 3


async def test_browsed_tile_remains_cached_after_service_restart(
    service: tuple[SkymapService, StateStore],
    tmp_path: Path,
) -> None:
    skymap, _ = service
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        assert (await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 0, 0), "jpg")).source == "upstream"

    restarted = SkymapService(
        skymap._cache,
        max_bytes=3 * len(PNG) + 10,
        user_agent=lambda: build_user_agent("ops@example.org"),
        bundled_root=tmp_path / "bundled",
    )
    try:
        await restarted.startup()
        with respx.mock(assert_all_called=False) as router:
            route = router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
            assert (await restarted.get_tile(SURVEY_ID, 3, mercator.pack(3, 0, 0), "jpg")).source == "cache"
            assert route.call_count == 0
    finally:
        await restarted.aclose()


async def test_disk_reserve_stops_new_cache_writes_without_evicting_existing_tiles(
    service: tuple[SkymapService, StateStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skymap, _ = service
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 0, 0), "jpg")

    real_usage = shutil.disk_usage
    usage_type = type(real_usage(tmp_path))
    monkeypatch.setattr(
        "ssc.skymap.service.shutil.disk_usage",
        lambda path: usage_type(100 * 1024**3, 91 * 1024**3, 9 * 1024**3),
    )
    with respx.mock(assert_all_called=False) as router:
        route = router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        assert (await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 0, 0), "jpg")).source == "cache"
        assert (await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 1, 0), "jpg")).source == "upstream"
        assert (await skymap.get_tile(SURVEY_ID, 3, mercator.pack(3, 1, 0), "jpg")).source == "upstream"
        assert route.call_count == 2


async def test_prefetch_fails_visibly_when_disk_reserve_is_reached(
    service: tuple[SkymapService, StateStore],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    skymap, state = service
    usage_type = type(shutil.disk_usage(tmp_path))
    monkeypatch.setattr(
        "ssc.skymap.service.shutil.disk_usage",
        lambda path: usage_type(100 * 1024**3, 91 * 1024**3, 9 * 1024**3),
    )
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        await _prefetch_sky(skymap, 0)
    assert state.snapshot.skymap.jobs[0].status is PrefetchJobStatus.FAILED
    assert "free-space reserve" in (state.snapshot.skymap.jobs[0].error or "")


async def test_a_pack_larger_than_the_budget_is_refused_up_front(
    service: tuple[SkymapService, StateStore],
) -> None:
    skymap, _ = service
    with respx.mock() as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        await skymap.get_tile(SURVEY_ID, 0, 0, "jpg")  # measures a tile size

    with pytest.raises(PrefetchRefusedError, match="exceeds the tile cache budget"):
        await skymap.start_prefetch(PrefetchRequest(survey=SURVEY_ID, mode="sky", max_order=2))


async def test_cancelling_a_job_records_cancelled(
    service: tuple[SkymapService, StateStore],
) -> None:
    skymap, state = service
    gate = asyncio.Event()
    asked = asyncio.Event()

    async def slow(request: httpx.Request) -> httpx.Response:
        asked.set()
        await gate.wait()
        return httpx.Response(200, content=PNG)

    # A cancelled request never completes, so respx would not count it as called.
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM).mock(side_effect=slow)
        job = await skymap.start_prefetch(
            PrefetchRequest(survey=SURVEY_ID, mode="sky", max_order=1)
        )
        await asyncio.wait_for(asked.wait(), timeout=2.0)
        await skymap.cancel_prefetch(job.id)
        with pytest.raises(asyncio.CancelledError):
            await skymap._jobs[job.id]
        gate.set()
    recorded = next(j for j in state.snapshot.skymap.jobs if j.id == job.id)
    assert recorded.status is PrefetchJobStatus.CANCELLED
    assert recorded.error == "cancelled by the user"


async def test_cancelling_before_the_first_tile_still_records_cancelled(
    service: tuple[SkymapService, StateStore],
) -> None:
    """The window the test above cannot reach, because it waits for a
    request first: a cancel delivered before `_run` takes its first step
    throws into a coroutine that never starts, so nothing inside it — its
    `try` included — ever runs. The row used to stay `running` with no
    error for the life of the process, invisible to `startup()`'s reaper
    (which only runs at process start) and impossible to cancel again
    (`cancel_prefetch` refuses a task that is already done)."""
    skymap, state = service
    with respx.mock(assert_all_called=False) as router:
        router.get(UPSTREAM).mock(return_value=httpx.Response(200, content=PNG))
        job = await skymap.start_prefetch(
            PrefetchRequest(survey=SURVEY_ID, mode="sky", max_order=1)
        )
        await skymap.cancel_prefetch(job.id)  # deliberately no await in between
        await asyncio.gather(skymap._jobs[job.id], return_exceptions=True)
        for _ in range(200):
            await asyncio.sleep(0.01)
            recorded = next(j for j in state.snapshot.skymap.jobs if j.id == job.id)
            if recorded.status is not PrefetchJobStatus.RUNNING:
                break
    assert recorded.status is PrefetchJobStatus.CANCELLED
    assert recorded.error == "cancelled by the user"


def test_cone_prefetch_refuses_toast(service: tuple[SkymapService, StateStore]) -> None:
    """No registry entry uses TOAST any more (Pan-STARRS DR1, removed
    2026-09-08, was the one that did), so this exercises `_plan`'s own
    refusal directly against a synthetic TOAST survey rather than over HTTP
    (`tests/api/test_skymap.py` used to pin this down against Pan-STARRS)."""
    skymap, _ = service
    toast_survey = Survey(
        id="synthetic-toast",
        label="Synthetic TOAST survey (test only)",
        kind=SurveyKind.OPTICAL,
        scheme=TileScheme.TOAST,
        upstream_base_url="https://example.org/toast/demo",
        tile_format="png",
        max_order=3,
        credit_label="Test fixture",
        credit_url="https://example.org",
    )
    with pytest.raises(PrefetchRefusedError, match="TOAST"):
        skymap._plan(
            toast_survey,
            PrefetchRequest(
                survey=toast_survey.id, mode="cone", max_order=3, ra_deg=1, dec_deg=2, radius_deg=1
            ),
        )


async def test_fits_stretch_survey_serves_a_converted_png_not_raw_fits(
    service: tuple[SkymapService, StateStore],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NSNS's individual bands (`Survey.fits_stretch=True`) are the first
    fetch-through HiPS this registry serves as raw FITS upstream — WWT's
    engine cannot draw a FITS tile (it decodes an imageset tile as an
    ordinary bitmap), so the tile proxy must convert before caching/serving,
    never pass the archive's own bytes straight through."""
    from ssc.skymap import service as service_module
    from ssc.skymap.surveys import NSNS_HALPHA
    from tests.fixtures.skymap.build import build_fits_bytes

    skymap, _ = service
    synthetic_enabled = NSNS_HALPHA.model_copy(
        update={"enabled": True, "pending_clearance": None}
    )
    monkeypatch.setattr(
        service_module,
        "find_survey",
        lambda survey_id: synthetic_enabled if survey_id == synthetic_enabled.id else None,
    )
    plane = np.linspace(0.0, 500.0, 64, dtype=np.float32).reshape(8, 8)
    fits_bytes = build_fits_bytes(plane, {})
    upstream = re.compile(r"http://www\.simg\.de/nebulae3/dr0_2/halpha/Norder0/Dir0/Npix0\.fits")
    with respx.mock() as router:
        router.get(upstream).mock(return_value=httpx.Response(200, content=fits_bytes))
        result = await skymap.get_tile("nsns-halpha", 0, 0, "png")
    assert result.media_type == "image/png"
    assert result.data.startswith(b"\x89PNG\r\n\x1a\n"), "served bytes must be a real PNG, not FITS"
    assert result.data != fits_bytes

    # Cached under the client-facing "png" ext, not "fits" — a second
    # request must hit the cache and never touch the network again.
    with respx.mock(assert_all_called=False) as router:
        route = router.get(upstream).mock(return_value=httpx.Response(200, content=fits_bytes))
        cached = await skymap.get_tile("nsns-halpha", 0, 0, "png")
        assert cached.source == "cache"
        assert route.call_count == 0
    assert cached.data == result.data


async def test_cone_prefetch_plans_a_mercator_survey(
    service: tuple[SkymapService, StateStore],
) -> None:
    """Deliverable 4: cone prefetch (per-target detail) now has an address
    space for Mercator, not just HiPS -- `_plan` dispatches to
    `mercator.cone_tiles_by_zoom` rather than refusing outright."""
    from ssc.skymap.surveys import find_survey

    skymap, _ = service
    survey = find_survey(SURVEY_ID)
    assert survey is not None
    plan = skymap._plan(
        survey,
        PrefetchRequest(
            survey=SURVEY_ID, mode="cone", max_order=6, ra_deg=180.0, dec_deg=0.0, radius_deg=3.0
        ),
    )
    assert plan.total > 0
    addresses = list(plan.addresses())
    assert len(addresses) == plan.total
    assert all(0 <= order <= 6 for order, _npix in addresses)


def test_user_agent_carries_version_and_contact() -> None:
    assert re.fullmatch(r"SSC/\S+ \(\+ops@example\.org\)", build_user_agent("ops@example.org"))
    assert re.fullmatch(r"SSC/\S+", build_user_agent(None))
    assert re.fullmatch(r"SSC/\S+", build_user_agent("  "))
