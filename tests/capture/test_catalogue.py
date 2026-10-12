"""Session derivation (DESIGN.md §9.5.4): purely from ``object``/``date_obs``,
never from insertion order or path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from ssc.capture.catalogue import SESSION_GAP, derive_sessions
from ssc.store.capture import CaptureFrameRecord

_BASE = datetime(2026, 9, 10, 22, 0, 0, tzinfo=UTC)


def _frame(
    *,
    id_: int,
    object_name: str | None,
    at: datetime,
    filter_name: str | None = "LP",
) -> CaptureFrameRecord:
    return CaptureFrameRecord(
        id=id_,
        path=Path(f"/capture/{id_}.fits"),
        object_name=object_name,
        filter_name=filter_name,
        frame_type="light",
        date_obs=at,
        exposure_s=30.0,
        gain=200,
        ccd_temp_c=20.0,
        width=3840,
        height=2160,
        status="complete",
        created_at=at,
    )


def test_two_frames_minutes_apart_on_one_target_are_one_session() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE),
        _frame(id_=2, object_name="M42", at=_BASE + timedelta(minutes=5)),
    ]
    sessions = derive_sessions(frames)
    assert len(sessions) == 1
    assert sessions[0].object_name == "M42"
    assert len(sessions[0].frames) == 2


def test_same_target_two_nights_apart_is_two_sessions() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE),
        _frame(id_=2, object_name="M42", at=_BASE + timedelta(days=1)),
    ]
    sessions = derive_sessions(frames)
    assert len(sessions) == 2
    assert all(session.object_name == "M42" for session in sessions)


def test_two_targets_interleaved_are_two_sessions() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE),
        _frame(id_=2, object_name="M31", at=_BASE + timedelta(minutes=1)),
        _frame(id_=3, object_name="M42", at=_BASE + timedelta(minutes=2)),
        _frame(id_=4, object_name="M31", at=_BASE + timedelta(minutes=3)),
    ]
    sessions = derive_sessions(frames)
    assert len(sessions) == 2
    by_object = {session.object_name: session for session in sessions}
    assert set(by_object) == {"M42", "M31"}
    assert len(by_object["M42"].frames) == 2
    assert len(by_object["M31"].frames) == 2


def test_gap_exactly_at_the_threshold_does_not_split() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE),
        _frame(id_=2, object_name="M42", at=_BASE + SESSION_GAP),
    ]
    assert len(derive_sessions(frames)) == 1


def test_gap_one_second_over_the_threshold_splits() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE),
        _frame(id_=2, object_name="M42", at=_BASE + SESSION_GAP + timedelta(seconds=1)),
    ]
    assert len(derive_sessions(frames)) == 2


def test_session_reports_the_filters_it_used() -> None:
    frames = [
        _frame(id_=1, object_name="M42", at=_BASE, filter_name="LP"),
        _frame(id_=2, object_name="M42", at=_BASE + timedelta(minutes=1), filter_name="Dark"),
    ]
    (session,) = derive_sessions(frames)
    assert session.filters == {"LP", "Dark"}


def test_insertion_order_does_not_matter() -> None:
    frames = [
        _frame(id_=2, object_name="M42", at=_BASE + timedelta(minutes=5)),
        _frame(id_=1, object_name="M42", at=_BASE),
    ]
    (session,) = derive_sessions(frames)
    assert [frame.id for frame in session.frames] == [1, 2]
