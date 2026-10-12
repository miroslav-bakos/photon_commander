# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest

from ssc.api.autofocus_service import (
    AutofocusAlreadyRunningError,
    AutofocusConditionsError,
    AutofocusError,
    AutofocusRestoreError,
    AutofocusService,
    FocusSample,
    _fitted_minimum,
)
from ssc.api.capture_service import ExposureTimeoutError
from ssc.state.models import AutofocusState, StateSnapshot
from ssc.state.store import StateStore


def test_focus_fit_ignores_an_attractive_measurement_with_too_few_stars() -> None:
    samples = [
        FocusSample(1180, 3.0, 40),
        FocusSample(1190, 2.0, 42),
        FocusSample(1200, 1.5, 44),
        FocusSample(1210, 2.0, 41),
        FocusSample(1220, 0.4, 1),
    ]

    assert _fitted_minimum(samples) == pytest.approx(1200, abs=2)


class FakeFocuser:
    def __init__(self, position: int = 1200, maximum: int = 2600) -> None:
        self.position = position
        self.maximum = maximum
        self.connected = True
        self.moves: list[int] = []

    async def is_connected(self) -> bool:
        return self.connected

    async def connect(self) -> None:
        self.connected = True

    async def get(self, member: str, **params: object) -> object:
        del params
        return {"position": self.position, "maxstep": self.maximum, "ismoving": False}[member]

    async def put(self, member: str, **params: object) -> object:
        assert member == "move"
        position = params["Position"]
        assert isinstance(position, int)
        self.position = position
        self.moves.append(self.position)
        return None


@pytest.mark.asyncio
async def test_retention_coalesces_slow_writes_and_flushes_authoritative_terminal_state() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    saved: list[AutofocusState] = []
    state = StateStore(StateSnapshot(server_started_at=datetime.now(UTC)))

    async def persist(section: AutofocusState) -> None:
        entered.set()
        await release.wait()
        saved.append(section)

    service = AutofocusService(
        focuser=FakeFocuser(), camera=SimpleNamespace(), state_store=state, persist=persist
    )
    service._set_section(AutofocusState(status="running", started_at=datetime.now(UTC)))
    await entered.wait()
    service._publish(candidate_position=1210)
    service._publish(status="completed", final_position=1210, completed_at=datetime.now(UTC))
    assert service.state is state.snapshot.autofocus
    release.set()
    await service.flush_persistence()
    assert len(saved) == 2
    assert saved[-1] == state.snapshot.autofocus
    assert saved[-1].status == "completed"


@pytest.mark.asyncio
async def test_retention_failure_is_visible_and_does_not_discard_measurements() -> None:
    state = StateStore(StateSnapshot(server_started_at=datetime.now(UTC)))

    async def persist(_section: AutofocusState) -> None:
        raise OSError("private filesystem detail must not enter notice")

    service = AutofocusService(
        focuser=FakeFocuser(), camera=SimpleNamespace(), state_store=state, persist=persist
    )
    service._set_section(AutofocusState(status="running", started_at=datetime.now(UTC)))
    service._record_sample(FocusSample(1200, 1.5, 20), stage="baseline")
    await service.flush_persistence()
    assert len(state.snapshot.autofocus.samples) == 1
    notice = next(notice for notice in state.snapshot.notices if notice.id == "autofocus-retention")
    assert notice.code == "autofocus_retention_failed"
    assert "private filesystem" not in notice.message


@pytest.mark.asyncio
async def test_autofocus_measures_curve_and_finishes_near_its_minimum() -> None:
    focuser = FakeFocuser()
    sleeps: list[float] = []

    async def sample() -> tuple[float, int]:
        return 1.5 + ((focuser.position - 1212) / 10) ** 2, 25

    async def no_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = AutofocusService(focuser=focuser, camera=None, sample_quality=sample, sleep=no_sleep)
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.start_position == 1200
    assert result.final_position == pytest.approx(1212, abs=1)
    assert focuser.position == result.final_position
    assert len(result.samples) == 13
    assert len([sample for sample in service.state.samples if sample.stage == "fine"]) == 3
    assert sleeps == [2.0] * 13
    assert service.state.started_at is not None
    assert service.state.completed_at is not None
    assert service.state.completed_at >= service.state.started_at


@pytest.mark.asyncio
@pytest.mark.parametrize("weak", [False, True])
async def test_uncertain_fine_scan_remeasures_monotonically_without_duplicate_fit_points(
    weak: bool,
) -> None:
    focuser = FakeFocuser()

    async def sample() -> tuple[float, int]:
        # A flat curve or weak center must not use the three-point shortcut.
        return (1.5 + ((focuser.position - 1200) / 2) ** 2 if weak else 1.5), (
            3 if weak and focuser.position == 1200 else 40
        )

    async def no_sleep(_seconds: float) -> None:
        pass

    service = AutofocusService(focuser=focuser, camera=None, sample_quality=sample, sleep=no_sleep)
    coarse = [FocusSample(position, 1.5, 40) for position in range(1170, 1231, 10)]
    measured = await service._measure_fine_curve(
        1200,
        2,
        2600,
        coarse,
        settle_s=2.0,
        exposure_s=2.0,
    )
    assert focuser.moves == [1202, 1200, 1198, 1204, 1202, 1200, 1198, 1196]
    assert [point.position for point in measured] == [1204, 1202, 1200, 1198, 1196]


@pytest.mark.asyncio
async def test_autofocus_retries_a_transient_exposure_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    focuser = FakeFocuser()
    camera = SimpleNamespace(connect_calls=0)

    async def connect() -> None:
        camera.connect_calls += 1

    camera.connect = connect
    attempts = 0

    async def expose(**_kwargs: object) -> tuple[object, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ExposureTimeoutError("temporary ImageReady timeout")
        metadata = SimpleNamespace(bayer_offset_x=None, bayer_offset_y=None)
        return SimpleNamespace(pixels=np.ones((16, 16)), metadata=metadata), object()

    monkeypatch.setattr("ssc.api.autofocus_service.drive_exposure", expose)
    monkeypatch.setattr(AutofocusService, "_measure", staticmethod(lambda *_args: (1.4, 30)))
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    service = AutofocusService(focuser=focuser, camera=camera, sleep=sleep)  # type: ignore[arg-type]

    assert await service._sample_quality(1.0) == (1.4, 30)
    assert attempts == 2
    assert camera.connect_calls == 1
    assert sleeps == [2.0]


@pytest.mark.asyncio
async def test_autofocus_validates_interior_best_on_a_flat_fine_plateau() -> None:
    """Regression from the Pi5 run on 2026-09-15.

    The coarse curve is strongly bracketed, but seeing noise tilts the narrow
    fine quadratic until its fitted vertex clips to the sampled edge. The
    sharp interior measurement is still safe to try because two fresh frames
    decide whether it or the starting position is finally retained.
    """
    focuser = FakeFocuser(position=1210)
    samples_by_position = {
        1240: [2.148487664661091],
        1230: [1.872423929416502],
        1220: [1.3979123465616672],
        1210: [
            1.320926115577729,
            1.3224806194039116,
            1.3271984358150437,
            1.34,
        ],
        1208: [1.3891274119446404],
        1206: [1.3598776127537513],
        1204: [1.3012313636794843, 1.29],
        1202: [1.3791705540334203],
        1200: [1.3252303099031648],
        1190: [1.5397126785952182],
        1180: [1.8754573216746002],
    }

    # Replay the recorded fine values for the preliminary triplet; the
    # complete fallback and fresh validation/control retain their original values.
    for position in (1208, 1206, 1204):
        samples_by_position[position].insert(0, samples_by_position[position][0])

    async def sample() -> tuple[float, int]:
        return samples_by_position[focuser.position].pop(0), 40

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser,
        camera=None,
        sample_quality=sample,
        sleep=no_sleep,
    )
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.final_position == 1204
    assert result.hfr_px == 1.29
    assert focuser.position == 1204
    assert service.state.status == "completed"


@pytest.mark.asyncio
async def test_autofocus_validates_coarse_candidate_when_fine_best_is_at_edge() -> None:
    """A noisy fine refinement must not abort a schedule after a valid coarse fit."""
    focuser = FakeFocuser(position=1210)
    samples_by_position = {
        1240: [2.147478650115217],
        1230: [2.2571200170721792],
        1220: [1.5208610057443037],
        1210: [1.3771034709389702, 1.3336996136275017, 1.34],
        1209: [1.2936256572191853],
        1207: [1.3364212554875614],
        1205: [1.3932282162325802, 1.28],
        1203: [1.3392758566947385],
        1201: [1.33012338525005],
        1200: [1.4255154438737305],
        1190: [1.549575932414474],
        1180: [1.9775002620487694],
    }

    # Replay the recorded fine values for the preliminary triplet; the
    # complete fallback and fresh validation/control retain their original values.
    for position in (1207, 1205, 1203):
        samples_by_position[position].insert(0, samples_by_position[position][0])

    async def sample() -> tuple[float, int]:
        return samples_by_position[focuser.position].pop(0), 38

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser,
        camera=None,
        sample_quality=sample,
        sleep=no_sleep,
    )
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.final_position == 1205
    assert result.hfr_px == 1.28
    assert focuser.position == 1205
    assert service.state.status == "completed"


@pytest.mark.asyncio
async def test_autofocus_accepts_modest_real_improvement_over_fresh_control() -> None:
    """Regression from the Pi5 Sh2-131 run on 2026-09-18.

    The fitted curve selected 1205 and the validation frame was visibly better
    than a fresh control at 1210, but the old 2% margin kept the stale focus.
    """
    focuser = FakeFocuser(position=1210)
    samples_by_position = {
        1240: [2.146],
        1230: [1.885],
        1220: [1.479],
        1210: [1.341, 1.340, 1.301],
        1209: [1.269],
        1207: [1.293],
        1205: [1.340, 1.279],
        1203: [1.328],
        1201: [1.408],
        1200: [1.316],
        1190: [1.486],
        1180: [1.824],
    }

    # Replay the recorded fine values for the preliminary triplet; the
    # complete fallback and fresh validation/control retain their original values.
    for position in (1207, 1205, 1203):
        samples_by_position[position].insert(0, samples_by_position[position][0])

    async def sample() -> tuple[float, int]:
        return samples_by_position[focuser.position].pop(0), 40

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser,
        camera=None,
        sample_quality=sample,
        sleep=no_sleep,
    )
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.final_position == 1205
    assert result.hfr_px == 1.279
    assert focuser.position == 1205
    assert service.state.status == "completed"


@pytest.mark.asyncio
async def test_autofocus_extends_coarse_sweep_when_best_point_is_at_edge() -> None:
    """A clear basin just outside the initial span should be bracketed, not refused."""
    focuser = FakeFocuser(position=1230)
    samples_by_position = {
        1260: [3.637],
        1250: [3.229],
        1240: [2.496],
        1230: [2.140, 2.1, 2.1],
        1220: [1.531],
        1210: [1.419],
        1200: [1.374],
        1203: [1.34],
        1201: [1.19],
        1199: [1.12],
        1198: [1.07],
        1197: [1.10],
        1195: [1.165, 1.09],
        1193: [1.112, 1.08],
        1191: [1.101],
        1190: [1.18],
        1189: [1.104],
        1187: [1.146],
        1180: [1.75],
    }

    # Replay the recorded fine values for the preliminary triplet; the
    # complete fallback and fresh validation/control retain their original values.
    for position in (1201, 1199, 1197):
        samples_by_position[position].insert(0, samples_by_position[position][0])

    async def sample() -> tuple[float, int]:
        assert samples_by_position[focuser.position], (focuser.position, focuser.moves)
        return samples_by_position[focuser.position].pop(0), 39

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser,
        camera=None,
        sample_quality=sample,
        sleep=no_sleep,
    )
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.final_position == 1198
    assert result.hfr_px == 1.07
    assert focuser.moves[:9] == [1260, 1250, 1240, 1230, 1220, 1210, 1200, 1190, 1180]
    assert service.state.status == "completed"


@pytest.mark.asyncio
async def test_autofocus_enforces_two_second_minimum_settle() -> None:
    focuser = FakeFocuser()

    async def sample() -> tuple[float, int]:
        return 2.0, 10

    service = AutofocusService(focuser=focuser, camera=None, sample_quality=sample)
    with pytest.raises(AutofocusError, match="at least 2s"):
        await service.run(step=10, settle_s=1.9)


@pytest.mark.asyncio
async def test_autofocus_requires_fine_step_smaller_than_coarse_step() -> None:
    focuser = FakeFocuser()

    async def sample() -> tuple[float, int]:
        return 2.0, 10

    service = AutofocusService(focuser=focuser, camera=None, sample_quality=sample)
    with pytest.raises(AutofocusError, match="smaller than AF step"):
        await service.run(step=2, fine_step=2)


@pytest.mark.asyncio
async def test_autofocus_restores_start_when_too_few_frames_have_stars() -> None:
    focuser = FakeFocuser()

    async def starless() -> tuple[None, int]:
        return None, 0

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser, camera=None, sample_quality=starless, sleep=no_sleep
    )
    with pytest.raises(AutofocusError, match="no measurable stars"):
        await service.run()
    assert focuser.position == 1200


async def test_autofocus_rejects_tiny_star_sample_as_temporary_conditions() -> None:
    focuser = FakeFocuser()

    async def sparse():
        return 0.1, 1

    async def no_sleep(_seconds):
        pass

    service = AutofocusService(focuser=focuser, camera=None, sample_quality=sparse, sleep=no_sleep)
    with pytest.raises(AutofocusConditionsError):
        await service.run()
    assert focuser.position == 1200


async def test_failed_focus_restore_is_hardware_error_not_cloud_condition() -> None:
    class BrokenFocuser(FakeFocuser):
        async def put(self, member, **params):
            raise RuntimeError("motor unavailable")

    async def starless():
        return None, 0

    service = AutofocusService(focuser=BrokenFocuser(), camera=None, sample_quality=starless)
    with pytest.raises(AutofocusRestoreError, match="could not be restored"):
        await service.run()


@pytest.mark.asyncio
async def test_autofocus_rejects_a_concurrent_run() -> None:
    focuser = FakeFocuser()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_sample() -> tuple[float, int]:
        entered.set()
        await release.wait()
        return 1.5 + ((focuser.position - 1212) / 10) ** 2, 10

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser, camera=None, sample_quality=blocked_sample, sleep=no_sleep
    )
    first = asyncio.create_task(service.run())
    await entered.wait()
    with pytest.raises(AutofocusAlreadyRunningError):
        await service.run()
    release.set()
    await first


@pytest.mark.asyncio
async def test_autofocus_restores_start_when_minimum_is_not_bracketed() -> None:
    focuser = FakeFocuser()

    async def falling_curve() -> tuple[float, int]:
        return 20.0 - focuser.position / 100.0, 12

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser, camera=None, sample_quality=falling_curve, sleep=no_sleep
    )
    with pytest.raises(AutofocusError, match="minimum lies at the edge"):
        await service.run()
    assert focuser.position == 1200


@pytest.mark.asyncio
async def test_autofocus_keeps_start_when_candidate_does_not_beat_fresh_control() -> None:
    focuser = FakeFocuser()
    sample_count = 0

    async def drifting_curve() -> tuple[float, int]:
        nonlocal sample_count
        sample_count += 1
        curve = 1.5 + ((focuser.position - 1212) / 10) ** 2
        # After the baseline/coarse sweep, the fresh start-position control
        # improves with seeing; its identity must not depend on sweep length.
        if sample_count > 8 and focuser.position == 1200:
            return 1.0, 20
        return curve, 20

    async def no_sleep(_seconds: float) -> None:
        return None

    service = AutofocusService(
        focuser=focuser, camera=None, sample_quality=drifting_curve, sleep=no_sleep
    )
    result = await service.run(step=10, fine_step=2, settle_s=2.0)

    assert result.final_position == 1200
    assert result.hfr_px == 1.0
    assert focuser.position == 1200


async def test_cancelled_autofocus_aborts_camera_halts_and_restores_focus() -> None:
    from unittest.mock import AsyncMock

    class HaltableFocuser(FakeFocuser):
        async def put(self, member: str, **params: object) -> object:
            if member == "halt":
                calls.append("halt")
                return None
            return await super().put(member, **params)

    calls: list[str] = []
    focuser = HaltableFocuser()
    entered = asyncio.Event()

    async def sample() -> tuple[float, int]:
        entered.set()
        await asyncio.Event().wait()
        return 2, 10

    async def put(member: str) -> None:
        calls.append(member)

    async def no_sleep(_: float) -> None:
        pass

    service = AutofocusService(
        focuser=focuser,
        camera=SimpleNamespace(put=AsyncMock(side_effect=put)),
        sample_quality=sample,
        sleep=no_sleep,
    )
    task = asyncio.create_task(service.run())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["abortexposure", "halt"]
    assert focuser.position == 1200
    assert service.state.status == "failed"
