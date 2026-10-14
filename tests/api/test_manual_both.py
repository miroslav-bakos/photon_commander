"""Manual Both jobs use real SQLite/FITS and fake independent cameras."""
from __future__ import annotations

import asyncio

import numpy as np
import pytest

from ssc.capture.frame import FrameType
from ssc.skymap.fits import read_fits_image
from ssc.store.capture import CaptureRepository
from tests.api.test_capture_routes import (
    _camera,
    _configure_folder,
    _filterwheel,
    _install_capture_service,
    _telescope,
)


def setup(paired, tmp_path):
    _configure_folder(paired, tmp_path)
    tele = _camera(properties={"camerastate": 0})
    wide = _camera(properties={"camerastate": 0, "exposuremax": 60})
    wide.frame.array[:] += 100
    telescope = _telescope()
    telescope.properties["slewing"] = False
    services, service = _install_capture_service(paired, camera=tele, wide_camera=wide,
                                                telescope=telescope, filterwheel=_filterwheel())
    return services, service, tele, wide


async def wait_until(predicate):
    async with asyncio.timeout(5):
        while not predicate():  # noqa: ASYNC110 - observe authoritative job state
            await asyncio.sleep(0.01)


def request(paired, **extra):
    return paired.post("/api/capture/exposure", json={"duration_s": 0.01, "gain": None, "object": "M42", "frame_type": "light", "optic_id": "both", **extra})


def test_manual_both_single_saves_independent_pixels_roles_and_preparing_until_both_ack(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    release = paired.portal.call(asyncio.Event)
    original = wide.start_exposure
    async def delayed(duration_s, *, light=True):
        await release.wait()
        await original(duration_s, light=light)
    wide.start_exposure = delayed
    response = request(paired)
    assert response.status_code == 202, response.text
    assert response.json()["stage"] is None
    assert response.json()["started_at"] is None
    paired.portal.call(wait_until, lambda: bool(tele.start_exposure_calls))
    assert service.section.stage is None
    assert service._wide_stream_reserved()
    paired.portal.call(release.set)
    paired.portal.call(wait_until, lambda: not service.section.in_progress)
    records = paired.portal.call(CaptureRepository(services.database).list_complete)
    assert len(records) == 2
    by_role = {record.path.parent.name: record for record in records}
    assert set(by_role) == {"tele", "wide"}
    tele_pixels = read_fits_image(by_role["tele"].path.read_bytes()).data
    wide_pixels = read_fits_image(by_role["wide"].path.read_bytes()).data
    np.testing.assert_array_equal(wide_pixels, tele_pixels + 100)
    assert tele.start_exposure_calls == wide.start_exposure_calls == [(0.01, True)]
    assert ("gain", {"Gain": 100}) in wide.put_calls
    assert not service._settings.capture.wide_companion_enabled
    assert not service._wide_stream_reserved()


@pytest.mark.parametrize("duration", [61, 60.01])
def test_both_rejects_over_wide_max_before_gain_dither_or_start(paired, tmp_path, duration):
    _services, _service, tele, wide = setup(paired, tmp_path)
    response = request(paired, duration_s=duration)
    assert response.status_code == 422, response.text
    assert tele.start_exposure_calls == wide.start_exposure_calls == []
    assert tele.put_calls == wide.put_calls == []


def test_both_missing_wide_rejects_without_tele_start(paired, tmp_path):
    _configure_folder(paired, tmp_path)
    tele = _camera()
    _install_capture_service(paired, camera=tele)
    response = request(paired)
    assert response.status_code == 503, response.text
    assert tele.start_exposure_calls == []


@pytest.mark.parametrize("kind,duration", [("dark", 1), ("bias", 0), ("flat", 1)])
def test_both_exposure_is_lights_only(paired, tmp_path, kind, duration):
    _services, _service, tele, wide = setup(paired, tmp_path)
    response = request(paired, frame_type=kind, duration_s=duration, flat_source="panel")
    assert response.status_code == 422
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


def test_finite_both_loop_counts_pairs_and_finishes_both_files(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    response = request(paired, capture_mode="loop", frame_limit=2)
    assert response.status_code == 202, response.text
    paired.portal.call(wait_until, lambda: not service.section.loop_active and not service.section.in_progress)
    assert service.section.loop_completed_frames == 2
    records = paired.portal.call(CaptureRepository(services.database).list_complete)
    assert len(records) == 4
    assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 2


def test_stopping_unbounded_both_loop_finishes_current_pair_and_no_next(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    reading, release = paired.portal.call(asyncio.Event), paired.portal.call(asyncio.Event)
    original = wide.image_array
    async def delayed():
        reading.set()
        await release.wait()
        return await original()
    wide.image_array = delayed
    response = request(paired, capture_mode="loop")
    assert response.status_code == 202, response.text
    paired.portal.call(reading.wait)
    stop = paired.post("/api/capture/loop/stop")
    assert stop.status_code == 200, stop.text
    assert service.section.in_progress
    paired.portal.call(release.set)
    paired.portal.call(wait_until, lambda: not service.section.in_progress)
    assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 1
    assert len(paired.portal.call(CaptureRepository(services.database).list_complete)) == 2


def test_failed_both_does_not_restart_manual_shooting_and_preserves_saved_tele(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    async def broken():
        raise RuntimeError("Wide readout broke")
    wide.image_array = broken
    assert request(paired, capture_mode="loop").status_code == 202
    paired.portal.call(wait_until, lambda: service.section.last_error is not None)
    assert not service.section.loop_active
    records = paired.portal.call(CaptureRepository(services.database).list_complete)
    assert len(records) == 1 and records[0].path.parent.name == "tele"
    assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 1


def test_next_manual_admission_drains_orphan_wide_intent_before_start(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    repo = CaptureRepository(services.database)
    async def plant():
        await repo.begin_dual_pair("manual-dual-old", "manual-light", 1, "intent")
        await repo.dual_issued("manual-dual-old", "manual-light", 1, "wide", mark=True)
    paired.portal.call(plant)
    response = paired.post("/api/capture/exposure", json={"duration_s": 0.01, "frame_type": "light", "optic_id": "tele"})
    assert response.status_code == 202, response.text
    paired.portal.call(wait_until, lambda: not service.section.in_progress)
    assert ("abortexposure", {}) in wide.put_calls
    assert not paired.portal.call(repo.pending_dual_runs)
    assert len(tele.start_exposure_calls) == 1 and wide.start_exposure_calls == []


@pytest.mark.parametrize("status", ["running", "paused", "interrupted"])
def test_both_refuses_observing_plan_ownership_between_frames(paired, tmp_path, status):
    from datetime import UTC, date, datetime

    from ssc.state.events import RunChanged
    from ssc.state.models import RunState

    services, _service, tele, wide = setup(paired, tmp_path)
    run = RunState(run_id="active", plan_id="plan", plan_name="Plan", plan_revision=1,
                   night_date=date(2026, 10, 14), status=status, started_at=datetime.now(UTC))
    paired.portal.call(services.state.apply, RunChanged(run=run))
    response = request(paired)
    assert response.status_code == 409, response.text
    assert "Stop the active observing plan" in response.text
    assert tele.put_calls == wide.put_calls == []
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


@pytest.mark.parametrize("loop", [False, True])
def test_cancel_during_preparing_gain_rolls_back_without_starting(paired, tmp_path, loop, monkeypatch):
    _services, service, tele, wide = setup(paired, tmp_path)
    rearmed = []
    monkeypatch.setattr(service, "_ensure_wide_rtsp_polling", lambda: rearmed.append(True))
    async def exercise():
        entered = asyncio.Event()
        original = tele.get
        async def delayed(member, **params):
            if member == "gain":
                entered.set()
                await asyncio.Event().wait()
            return await original(member, **params)
        tele.get = delayed
        method = service.start_capture_loop if loop else service.request_exposure
        task = asyncio.create_task(method(duration_s=0.01, gain=None, object_name="M42", frame_type=FrameType.LIGHT, optic_id="both"))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    paired.portal.call(exercise)
    assert not service.section.in_progress
    assert not service.section.loop_active
    assert service._capture_loop is None
    assert not service._wide_stream_reserved()
    assert rearmed
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


@pytest.mark.parametrize("fail", [False, True])
def test_single_both_completion_or_failure_rearms_explicit_wide_stream(paired, tmp_path, fail, monkeypatch):
    _services, service, _tele, wide = setup(paired, tmp_path)
    attempts = []
    monkeypatch.setattr(service, "_ensure_wide_rtsp_polling", lambda: attempts.append(True))
    if fail:
        async def broken():
            raise RuntimeError("Wide readout failed")
        wide.image_array = broken
    assert request(paired).status_code == 202
    paired.portal.call(wait_until, lambda: bool(attempts))
    assert not service._wide_stream_reserved()
    assert not service.section.in_progress
    assert not service._settings.capture.wide_companion_enabled


def test_dither_waits_for_both_previous_images_before_next_pair(paired, tmp_path, monkeypatch):
    services, service, tele, wide = setup(paired, tmp_path)
    repo = CaptureRepository(services.database)
    seen = []
    async def dither(_ra, _dec):
        records = await repo.list_complete()
        assert len(records) == 2
        assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 1
        seen.append(True)
    async def settle(**_kwargs):
        return None
    service._dither = dither
    monkeypatch.setattr(service, "_next_dither_target", lambda **_kwargs: (1, 2))
    monkeypatch.setattr(service, "_remember_dither_center", lambda _name: None)
    monkeypatch.setattr(service, "_wait_for_dither_settle", settle)
    async def configure():
        await services.settings.set_capture_dither(enabled=True, every_n_frames=1, min_pixels=2, max_pixels=2)
    paired.portal.call(configure)
    assert request(paired, capture_mode="loop", frame_limit=2).status_code == 202
    paired.portal.call(wait_until, lambda: not service.section.loop_active and not service.section.in_progress)
    assert seen == [True]
    assert service.section.loop_completed_frames == 2


def test_unverified_pair_cleanup_remains_blocked_until_orphan_recovery(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    original_put = wide.put
    async def broken_image():
        wide.properties["camerastate"] = 2
        raise RuntimeError("readout failed")
    async def broken_abort(member, **params):
        if member == "abortexposure":
            raise RuntimeError("Wide abort unavailable")
        return await original_put(member, **params)
    wide.image_array, wide.put = broken_image, broken_abort
    assert request(paired).status_code == 202
    paired.portal.call(wait_until, lambda: service.section.last_error is not None)
    assert service.section.in_progress
    assert "unconfirmed" in service.section.last_error
    assert request(paired).status_code == 409
    assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 1
    wide.put = original_put
    wide.properties["camerastate"] = 0
    response = paired.post("/api/capture/exposure", json={"duration_s": 0.01, "frame_type": "light", "optic_id": "tele"})
    assert response.status_code == 202, response.text
    paired.portal.call(wait_until, lambda: not service.section.in_progress)
    assert not paired.portal.call(CaptureRepository(services.database).pending_dual_runs)


def test_cancelling_owned_both_job_drains_wide_and_keeps_saved_tele(paired, tmp_path):
    services, service, tele, wide = setup(paired, tmp_path)
    reading = paired.portal.call(asyncio.Event)
    async def blocked_image():
        reading.set()
        await asyncio.Event().wait()
    wide.image_array = blocked_image
    assert request(paired).status_code == 202
    paired.portal.call(reading.wait)
    async def cancel():
        repo = CaptureRepository(services.database)
        async with asyncio.timeout(3):
            while not await repo.list_complete():  # noqa: ASYNC110 - observe durable save
                await asyncio.sleep(0.01)
        await services.tasks.cancel_all()
    paired.portal.call(cancel)
    assert not service.section.in_progress
    assert not service.section.loop_active
    assert not service._wide_stream_reserved()
    assert ("abortexposure", {}) in wide.put_calls
    records = paired.portal.call(CaptureRepository(services.database).list_complete)
    assert len(records) == 1 and records[0].path.parent.name == "tele"
    assert len(tele.start_exposure_calls) == len(wide.start_exposure_calls) == 1


def test_stop_loop_during_preflight_prevents_unstarted_pair(paired, tmp_path):
    _services, service, tele, wide = setup(paired, tmp_path)
    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()
        original = tele.get
        async def delayed(member, **params):
            if member == "gain":
                entered.set()
                await release.wait()
            return await original(member, **params)
        tele.get = delayed
        task = asyncio.create_task(service.start_capture_loop(duration_s=0.01, gain=None,
                                  object_name="M42", frame_type=FrameType.LIGHT, optic_id="both"))
        await asyncio.wait_for(entered.wait(), 3)
        service.stop_capture_loop()
        release.set()
        await task
    paired.portal.call(exercise)
    assert not service.section.in_progress and not service.section.loop_active
    assert not service._wide_stream_reserved()
    assert tele.start_exposure_calls == wide.start_exposure_calls == []


def test_cancelled_both_preparation_dither_requires_verified_stopped_mount(paired, tmp_path, monkeypatch):
    services, service, tele, wide = setup(paired, tmp_path)
    async def exercise():
        entered = asyncio.Event()
        await services.settings.set_capture_dither(enabled=True, every_n_frames=1, min_pixels=2, max_pixels=2)
        service._light_sequence = 1
        async def moving(_ra, _dec):
            service._telescope.properties["slewing"] = True
            entered.set()
            await asyncio.Event().wait()
        async def unconfirmed_abort():
            return None  # Acknowledgement alone is not stopped telemetry.
        service._dither = moving
        service._dither_abort = unconfirmed_abort
        task = asyncio.create_task(service.start_capture_loop(duration_s=0.01, gain=None,
                                  object_name="M42", frame_type=FrameType.LIGHT, optic_id="both"))
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    monkeypatch.setattr(service, "_next_dither_target", lambda **_kwargs: (1, 2))
    monkeypatch.setattr(service, "_remember_dither_center", lambda _name: None)
    paired.portal.call(exercise)
    assert service._manual_both_motion_error is not None
    assert not service._wide_stream_reserved()
    response = request(paired)
    assert response.status_code == 409, response.text
    assert tele.start_exposure_calls == wide.start_exposure_calls == []
    service._telescope.properties["slewing"] = False
    assert request(paired).status_code == 202
    paired.portal.call(wait_until, lambda: not service.section.in_progress)
