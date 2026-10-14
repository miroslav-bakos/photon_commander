"""Raw-FITS quality cache persistence, invalidation and bounded CPU admission."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from ssc.api import capture_orchestrator as capture
from ssc.capture.quality import FrameQuality
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.migrations import migrate

QUALITY = FrameQuality(3, 1.5, 3.2, None, None, 100, 2)


@pytest.fixture
async def caches(tmp_path: Path) -> AsyncIterator[tuple[CaptureRepository, list[capture._FrameQualityCache]]]:
    database = await open_database(tmp_path / "cache.sqlite")
    await migrate(database)
    instances: list[capture._FrameQualityCache] = []
    try:
        yield CaptureRepository(database), instances
    finally:
        for instance in instances:
            await instance.aclose()
        await database.aclose()


def cache_for(caches):
    repo, instances = caches
    instance = capture._FrameQualityCache(repo)
    instances.append(instance)
    return instance


async def test_quality_survives_new_instance_and_invalidates_changed_or_missing_file(caches, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_measure_frame_quality", lambda raw: calls.append(raw) or QUALITY)
    path = tmp_path / "frame.fits"
    path.write_bytes(b"original")
    first = cache_for(caches)
    assert await first.get(path) == QUALITY
    assert await first.get(path) == QUALITY
    second = cache_for(caches)
    assert await second.get(path) == QUALITY
    assert calls == [b"original"]
    path.write_bytes(b"replacement")
    assert await second.get(path) == QUALITY
    assert calls == [b"original", b"replacement"]
    path.unlink()
    with pytest.raises(FileNotFoundError):
        await second.get(path)


async def test_algorithm_version_invalidates_persisted_quality(caches, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_measure_frame_quality", lambda raw: calls.append(raw) or QUALITY)
    path = tmp_path / "frame.fits"
    path.write_bytes(b"fits")
    instance = cache_for(caches)
    await instance.get(path)
    monkeypatch.setattr(capture, "_QUALITY_ALGORITHM_VERSION", 2)
    await instance.get(path)
    assert len(calls) == 2


async def test_singleflight_and_cancelled_waiter_keep_worker_occupied(caches, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    def measure(raw):
        calls.append(raw)
        started.set()
        assert release.wait(5)
        return QUALITY

    monkeypatch.setattr(capture, "_measure_frame_quality", measure)
    first_path, second_path = tmp_path / "first.fits", tmp_path / "second.fits"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    instance = cache_for(caches)
    first = asyncio.create_task(instance.get(first_path))
    assert await asyncio.to_thread(started.wait, 5)
    same = asyncio.create_task(instance.get(first_path))
    other = asyncio.create_task(instance.get(second_path))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    # General I/O still progresses while quality CPU work is blocked.
    assert await asyncio.to_thread(lambda: 42) == 42
    await asyncio.sleep(0.03)
    assert calls == [b"first"]
    release.set()
    assert await same == await other == QUALITY
    assert calls == [b"first", b"second"]


async def test_failed_measurement_is_retryable_and_never_cached(caches, tmp_path, monkeypatch):
    calls = []

    def measure(raw):
        calls.append(raw)
        if len(calls) == 1:
            raise ValueError("broken FITS")
        return QUALITY

    monkeypatch.setattr(capture, "_measure_frame_quality", measure)
    path = tmp_path / "frame.fits"
    path.write_bytes(b"fits")
    instance = cache_for(caches)
    with pytest.raises(ValueError, match="broken FITS"):
        await instance.get(path)
    assert await instance.get(path) == QUALITY
    assert len(calls) == 2


async def test_backlog_is_bounded_and_shutdown_drains_active_thread(caches, tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()

    def measure(_raw):
        started.set()
        assert release.wait(5)
        return QUALITY

    monkeypatch.setattr(capture, "_measure_frame_quality", measure)
    monkeypatch.setattr(capture, "_QUALITY_MAX_PENDING", 1)
    path, other = tmp_path / "frame.fits", tmp_path / "other.fits"
    path.write_bytes(b"fits")
    other.write_bytes(b"other")
    instance = cache_for(caches)
    request = asyncio.create_task(instance.get(path))
    assert await asyncio.to_thread(started.wait, 5)
    with pytest.raises(capture.CaptureQualityBusyError):
        await instance.get(other)
    closing = asyncio.create_task(instance.aclose())
    await asyncio.sleep(0.03)
    assert not closing.done()
    release.set()
    await closing
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not instance._pending


async def test_source_change_during_measurement_is_not_returned_or_cached(caches, tmp_path, monkeypatch):
    path = tmp_path / "frame.fits"
    path.write_bytes(b"fits")

    def measure(_raw):
        path.write_bytes(b"changed")
        return QUALITY

    monkeypatch.setattr(capture, "_measure_frame_quality", measure)
    instance = cache_for(caches)
    with pytest.raises(capture.CaptureFrameNotReadyError, match="changed"):
        await instance.get(path)
    assert await caches[0].get_quality_cache(path, capture._quality_identity(path)) is None
