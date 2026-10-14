"""Paired light capture durability and physical completion barriers, without hardware."""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from ssc.api import dual_capture
from ssc.api.capture_service import ExposureContext
from ssc.skymap.fits import read_fits_image
from ssc.store.capture import CaptureRepository
from ssc.store.database import open_database
from ssc.store.migrations import migrate
from tests.api.test_capture_routes import _camera, _filterwheel, _telescope


@pytest.fixture
async def repo(tmp_path: Path) -> AsyncIterator[CaptureRepository]:
    database = await open_database(tmp_path / "dual.sqlite")
    await migrate(database)
    try:
        yield CaptureRepository(database)
    finally:
        await database.aclose()


def camera():
    result = _camera(properties={"camerastate": 0, "exposuremax": 60, "lastexposureduration": 0.01})
    original = result.put
    async def put(member, **params):
        if member == "gain":
            result.properties["gain"] = params["Gain"]
        return await original(member, **params)
    result.put = put
    return result


async def pair(repo, tmp_path, tele, wide, index=0):
    telescope = _telescope()
    telescope.properties.setdefault("slewing", False)
    return await dual_capture.capture_pair(tele=tele, wide=wide, telescope=telescope,
        filterwheel=_filterwheel(), focuser=None, repo=repo, run_id="run", block_id="lights",
        frame_index=index, duration_s=0.01, gain=150,
        context=ExposureContext(object_name="M42", focal_length_mm=None, telescope_name="scope", ssc_version="test", device_firmware="test"),
        folder=tmp_path, filename_template="frame_%SEQUENCE%", tz=ZoneInfo("UTC"))


async def test_pair_saves_distinct_optics_and_counts_one_logical_frame(repo, tmp_path):
    tele, wide = camera(), camera()
    await pair(repo, tmp_path, tele, wide)
    records = await repo.list_complete()
    assert len(records) == 2
    assert {record.path.parent.name for record in records} == {"tele", "wide"}
    assert all(record.path.parent.parent.parent.name == "M42" for record in records)
    assert tele.start_exposure_calls == wide.start_exposure_calls == [(0.01, True)]
    assert ("gain", {"Gain": 150}) in wide.put_calls
    assert await repo.count_committed(run_id="run", block_id="lights") == 1
    assert len(await repo.list_block_frames("run", "lights")) == 1
    assert (await repo.run_summary("run")).saved_frames == 1
    wide_record = await repo.dual_member("run", "lights", 0, "wide")
    wide_header = read_fits_image(wide_record.path.read_bytes()).header
    assert wide_header.get("FILTER") is None
    assert wide_header.get("FOCALLEN") is None
    assert wide_header["GAIN"] == 150
    assert not await repo.pending_dual_owned("run", "wide")


async def test_failed_wide_preserves_tele_and_restart_captures_only_missing_member(repo, tmp_path):
    tele, wide = camera(), camera()
    async def failed_readout():
        raise RuntimeError("Wide download failed")
    wide.image_array = failed_readout
    with pytest.raises(dual_capture.DualCaptureError, match="Wide download failed"):
        await pair(repo, tmp_path, tele, wide)
    assert len(await repo.list_complete()) == 1
    assert await repo.count_committed(run_id="run", block_id="lights") == 0
    assert ("abortexposure", {}) in wide.put_calls
    restarted_database = await open_database(repo._database.path)
    restarted_repo = CaptureRepository(restarted_database)
    retry_tele, retry_wide = camera(), camera()
    try:
        await pair(restarted_repo, tmp_path, retry_tele, retry_wide)
    finally:
        await restarted_database.aclose()
    assert retry_tele.start_exposure_calls == []
    assert retry_wide.start_exposure_calls == [(0.01, True)]
    assert len(await repo.list_complete()) == 2
    assert await repo.count_committed(run_id="run", block_id="lights") == 1


async def test_wide_complete_is_preserved_when_tele_readout_fails(repo, tmp_path):
    tele, wide = camera(), camera()
    async def failed_readout():
        raise RuntimeError("Tele download failed")
    tele.image_array = failed_readout
    with pytest.raises(dual_capture.DualCaptureError, match="Tele download failed"):
        await pair(repo, tmp_path, tele, wide)
    retry_tele, retry_wide = camera(), camera()
    await pair(repo, tmp_path, retry_tele, retry_wide)
    assert retry_tele.start_exposure_calls == [(0.01, True)]
    assert retry_wide.start_exposure_calls == []


async def test_pair_intent_is_durable_before_start_and_restart_option_is_frozen(repo, tmp_path):
    assert await repo.freeze_run_companion("run", True)
    assert await CaptureRepository(repo._database).freeze_run_companion("run", False)
    assert not await repo.freeze_run_companion("other", False)
    assert not await repo.freeze_run_companion("other", True)
    tele, wide = camera(), camera()
    for role, member in (("tele", tele), ("wide", wide)):
        original = member.start_exposure
        async def started(duration_s, *, light=True, role=role, original=original):
            assert await repo.dual_issued("run", "lights", 0, role)
            await original(duration_s, light=light)
        member.start_exposure = started
    await pair(repo, tmp_path, tele, wide)


async def test_cancel_waits_for_owned_camera_abort_and_keeps_complete_member(repo, tmp_path):
    tele, wide = camera(), camera()
    wide_reading = asyncio.Event()
    cleanup_started, cleanup_release = asyncio.Event(), asyncio.Event()
    async def blocked_readout():
        wide_reading.set()
        await asyncio.Event().wait()
    original_put = wide.put
    async def blocked_abort(member, **params):
        if member == "abortexposure":
            cleanup_started.set()
            await cleanup_release.wait()
        return await original_put(member, **params)
    wide.image_array = blocked_readout
    wide.put = blocked_abort
    task = asyncio.create_task(pair(repo, tmp_path, tele, wide))
    await asyncio.wait_for(wide_reading.wait(), 3)
    async def tele_complete():
        while True:
            record = await repo.dual_member("run", "lights", 0, "tele")
            if record is not None and record.status == "complete":
                return
            await asyncio.sleep(0.01)
    await asyncio.wait_for(tele_complete(), 3)
    task.cancel()
    await asyncio.wait_for(cleanup_started.wait(), 3)
    assert not task.done()
    cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await repo.pending_dual_owned("run", "wide")
    assert (await repo.dual_member("run", "lights", 0, "tele")).status == "complete"


async def test_unsupported_duration_starts_neither_camera(repo, tmp_path):
    tele, wide = camera(), camera()
    wide.properties["exposuremax"] = 0.001
    with pytest.raises(dual_capture.DualCaptureError, match="same exposure"):
        await pair(repo, tmp_path, tele, wide)
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


async def test_recovery_drains_only_issued_owned_camera_and_blocks_failed_cleanup(repo):
    tele, wide = camera(), camera()
    await repo.begin_dual_pair("run", "lights", 0, "intent")
    await repo.dual_issued("run", "lights", 0, "wide", mark=True)
    await dual_capture.recover_dual_ownership(repo=repo, run_id="run", tele=tele, wide=wide)
    assert tele.put_calls == []
    assert ("abortexposure", {}) in wide.put_calls
    await repo.dual_issued("run", "lights", 0, "wide", mark=True)
    async def failed_abort(_member, **_params):
        raise RuntimeError("abort transport failed")
    wide.put = failed_abort
    with pytest.raises(RuntimeError, match="abort transport failed"):
        await dual_capture.recover_dual_ownership(repo=repo, run_id="run", tele=tele, wide=wide)
    assert await repo.pending_dual_owned("run", "wide")


@pytest.mark.parametrize("method,args", [("park", {}), ("find_home", {}), ("slew_to", {"ra_j2000_h": 1, "dec_j2000_deg": 2}), ("shutdown", {}), ("set_filter", {"position": "Dark"})])
async def test_unverified_companion_cleanup_blocks_device_motion(method, args):
    from types import SimpleNamespace

    from ssc.api.device_commands import AlpacaDeviceCommands

    @asynccontextmanager
    async def blocked():
        raise dual_capture.DualCaptureError("Wide cleanup unverified")
        yield  # pragma: no cover - context entry must fail
    commands = AlpacaDeviceCommands(SimpleNamespace(device=lambda _name: None), state=None, astro=None, before_motion=blocked)
    with pytest.raises(dual_capture.DualCaptureError, match="Wide cleanup unverified"):
        await getattr(commands, method)(**args)


async def test_other_repository_recovery_waits_until_current_pair_finishes(repo, tmp_path):
    tele, wide = camera(), camera()
    reading, release = asyncio.Event(), asyncio.Event()
    original = wide.image_array
    async def delayed():
        reading.set()
        await release.wait()
        return await original()
    wide.image_array = delayed
    capture = asyncio.create_task(pair(repo, tmp_path, tele, wide))
    await asyncio.wait_for(reading.wait(), 3)
    recovery = asyncio.create_task(dual_capture.recover_dual_ownership(repo=CaptureRepository(repo._database), run_id="run", tele=tele, wide=wide))
    await asyncio.sleep(0.02)
    assert not recovery.done()
    assert not any(member == "abortexposure" for member, _ in wide.put_calls)
    release.set()
    await capture
    await recovery
    assert not await repo.pending_dual_runs()


async def test_pair_cannot_finish_while_wide_still_reports_readout_busy(repo, tmp_path):
    tele, wide = camera(), camera()
    readout = asyncio.Event()
    original = wide.image_array
    async def image():
        result = await original()
        wide.properties["camerastate"] = 4
        readout.set()
        return result
    wide.image_array = image
    task = asyncio.create_task(pair(repo, tmp_path, tele, wide))
    await asyncio.wait_for(readout.wait(), 3)
    await asyncio.sleep(0.03)
    assert not task.done()
    wide.properties["camerastate"] = 0
    await asyncio.wait_for(task, 3)


async def test_restart_recovers_a_written_final_without_reexposing(repo, tmp_path, monkeypatch):
    original = repo.complete_frame
    cut = False
    async def power_loss(frame_id):
        nonlocal cut
        record = await repo.get_frame(frame_id)
        if record.path.parent.name == "tele" and not cut:
            cut = True
            raise RuntimeError("power cut after final FITS rename")
        await original(frame_id)
    monkeypatch.setattr(repo, "complete_frame", power_loss)
    with pytest.raises(dual_capture.DualCaptureError, match="power cut"):
        await pair(repo, tmp_path, camera(), camera())
    retry_tele, retry_wide = camera(), camera()
    await pair(CaptureRepository(repo._database), tmp_path, retry_tele, retry_wide)
    assert retry_tele.start_exposure_calls == retry_wide.start_exposure_calls == []
    assert await repo.count_committed(run_id="run", block_id="lights") == 1


async def test_atomic_motion_guard_prevents_pair_start_in_dispatch_gap(repo, tmp_path):
    tele, wide = camera(), camera()
    async with dual_capture.dual_motion_guard(repo=CaptureRepository(repo._database), tele=tele, wide=wide):
        acquisition = asyncio.create_task(pair(repo, tmp_path, tele, wide))
        await asyncio.sleep(0.03)
        assert tele.start_exposure_calls == wide.start_exposure_calls == []
    await acquisition


async def test_pair_rejects_asynchronous_slew_still_in_progress(repo, tmp_path, monkeypatch):
    from tests.api import test_capture_routes
    original = test_capture_routes._telescope
    def slewing():
        telescope = original()
        telescope.properties["slewing"] = True
        return telescope
    monkeypatch.setattr("tests.api.test_dual_capture._telescope", slewing)
    tele, wide = camera(), camera()
    with pytest.raises(dual_capture.DualCaptureError, match=r"Telescope\.Slewing"):
        await pair(repo, tmp_path, tele, wide)
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


async def test_motion_guard_allows_nested_and_autofocus_child_dispatch():
    from types import SimpleNamespace

    from ssc.api.device_commands import AlpacaDeviceCommands

    entries = []
    @asynccontextmanager
    async def guard():
        entries.append("enter")
        yield
        entries.append("exit")
    commands = AlpacaDeviceCommands(SimpleNamespace(device=lambda _name: None), state=None, astro=None, before_motion=guard)
    calls = []
    async def put(_device, member, **_params):
        calls.append(member)
    commands._put = put
    async def runner():
        await commands.find_home()
        await commands.unpark()
    commands.set_autofocus_runner(runner)
    await asyncio.wait_for(commands.autofocus(), 1)
    assert entries == ["enter", "exit"]
    assert calls == ["findhome", "unpark"]


async def test_child_task_cannot_keep_motion_guard_after_parent_scope_exits():
    from types import SimpleNamespace

    from ssc.api.device_commands import AlpacaDeviceCommands

    lock = asyncio.Lock()
    child_release, child_created = asyncio.Event(), asyncio.Event()
    tasks = []
    @asynccontextmanager
    async def guard():
        async with lock:
            yield
    commands = AlpacaDeviceCommands(SimpleNamespace(device=lambda _name: None), state=None, astro=None, before_motion=guard)
    calls = []
    async def put(_device, member, **_params):
        calls.append(member)
    commands._put = put
    async def action(_name, _params):
        async def delayed():
            child_created.set()
            await child_release.wait()
            await commands.find_home()
        tasks.append(asyncio.create_task(delayed()))
        await child_created.wait()
    commands._action = action
    await commands.startup()
    async with lock:
        child_release.set()
        await asyncio.sleep(0.02)
        assert calls == []
    await tasks[0]
    assert calls == ["findhome"]


async def test_unstructured_child_entering_before_parent_exit_waits_for_own_guard():
    from types import SimpleNamespace

    from ssc.api.device_commands import AlpacaDeviceCommands

    lock = asyncio.Lock()
    entered = asyncio.Event()
    tasks, calls, guards = [], [], []
    @asynccontextmanager
    async def guard():
        async with lock:
            guards.append("enter")
            yield
            guards.append("exit")
    commands = AlpacaDeviceCommands(SimpleNamespace(device=lambda _name: None), state=None, astro=None, before_motion=guard)
    async def put(_device, member, **_params):
        calls.append(member)
    commands._put = put
    async def action(_name, _params):
        async def child():
            entered.set()
            await commands.find_home()
        tasks.append(asyncio.create_task(child()))
        await entered.wait()
        await asyncio.sleep(0.02)
        assert calls == []
    commands._action = action
    await asyncio.wait_for(commands.startup(), 1)
    await asyncio.wait_for(tasks[0], 1)
    assert calls == ["findhome"]
    assert guards == ["enter", "exit", "enter", "exit"]


async def test_polar_alignment_background_gateway_motion_can_acquire_guard():
    from types import SimpleNamespace

    from ssc.api.device_commands import AlpacaDeviceCommands

    lock = asyncio.Lock()
    @asynccontextmanager
    async def guard():
        async with lock:
            yield
    commands = AlpacaDeviceCommands(SimpleNamespace(device=lambda _name: None), state=None, astro=None, before_motion=guard)
    async def put(*_args, **_params):
        return None
    commands._put = put
    async def background_runner(**_kwargs):
        # PA's actual gateway obtains its own global guard in a spawned task.
        async def gateway_motion():
            async with guard():
                return None
        await asyncio.create_task(gateway_motion())
    commands.set_polar_alignment_runner(background_runner)
    await asyncio.wait_for(commands.polar_alignment(), 1)
    commands.set_calibration_runner(background_runner)
    await asyncio.wait_for(commands.capture_calibration(), 1)
