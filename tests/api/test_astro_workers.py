"""Numerical Planning work must leave the device loop free and bounded."""

import asyncio
import threading

import pytest

from ssc.api.astro_service import AstroService
from ssc.astro.catalog import TargetCatalog


def service() -> AstroService:
    return AstroService(
        catalog=TargetCatalog((), (), {}, {}), profiles={}, ephemeris=None, ephemeris_error="test"
    )


@pytest.mark.asyncio
async def test_numerical_work_does_not_block_loop_and_uses_at_most_two_workers():
    astro = service()
    release = threading.Event()
    two_started = threading.Event()
    lock = threading.Lock()
    running = 0
    peak = 0
    thread_ids = []

    def slow(value):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            thread_ids.append(threading.get_ident())
            if running == 2:
                two_started.set()
        try:
            if not release.wait(3):
                raise TimeoutError("The event loop could not release numerical workers")
            return value
        finally:
            with lock:
                running -= 1

    tasks = [asyncio.create_task(astro.compute(slow, value)) for value in range(4)]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(two_started.wait, 2), timeout=2.5)
        # This turn runs while both numerical workers are deliberately blocked.
        await asyncio.sleep(0)
        assert running == 2
        assert len(thread_ids) == 2
        assert threading.get_ident() not in thread_ids
        release.set()
        assert await asyncio.gather(*tasks) == [0, 1, 2, 3]
        assert peak == 2
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        astro.close()


@pytest.mark.asyncio
async def test_numerical_failure_propagates_and_worker_remains_usable():
    astro = service()

    def fail():
        raise ValueError("invalid calculation inputs")

    try:
        with pytest.raises(ValueError, match="invalid calculation"):
            await astro.compute(fail)
        assert await astro.compute(lambda value: value + 1, 4) == 5
    finally:
        astro.close()
