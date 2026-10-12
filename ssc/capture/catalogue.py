# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Grouping catalogued frames into sessions (DESIGN.md §9.5.4).

A session is a contiguous run of frames on one target, derived purely from
``object``/``date_obs`` — never from insertion order, file path or directory
layout, so a user reorganising the capture folder on disk cannot change
what SSC considers one night's run. :func:`derive_sessions` is a pure
function over :class:`ssc.store.capture.CaptureFrameRecord` rows for exactly
that reason: it is independent of how those rows reached it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from ssc.store.capture import CaptureFrameRecord

#: A gap longer than this between consecutive frames on the same target ends
#: a session and starts the next one. Set well above the measured ~3.5-4 s
#: capture cadence (DESIGN.md §9.5.2) so an ordinary pause mid-run — a
#: meridian flip, a dither settle, a cloud wait, even a short break to swap
#: a filter by hand — never fragments one night's session; and well under a
#: night's own length (dusk to dawn is many hours) so two genuinely separate
#: nights on the same target are never merged into one.
SESSION_GAP: Final = timedelta(minutes=30)


@dataclass(frozen=True, slots=True)
class Session:
    """One contiguous run of frames on a single target."""

    object_name: str | None
    started_at_utc: datetime
    ended_at_utc: datetime
    frames: tuple[CaptureFrameRecord, ...]

    @property
    def filters(self) -> frozenset[str]:
        return frozenset(
            frame.filter_name for frame in self.frames if frame.filter_name is not None
        )

    def frames_for_filter(self, filter_name: str | None) -> tuple[CaptureFrameRecord, ...]:
        return tuple(frame for frame in self.frames if frame.filter_name == filter_name)


def derive_sessions(frames: Iterable[CaptureFrameRecord]) -> list[Session]:
    """Group frames into sessions purely from ``object_name``/``date_obs``.

    Frames are first grouped by target, then split within each target
    wherever consecutive frames (by ``date_obs``) are more than
    :data:`SESSION_GAP` apart. Grouping by target first — rather than by
    time alone across the whole catalogue — is what makes two interleaved
    targets come out as (at least) two sessions rather than one merged run:
    each target's own frames are always contiguous once sorted by object,
    regardless of what was imaged in between.
    """
    ordered = sorted(frames, key=lambda frame: (frame.object_name or "", frame.date_obs))
    sessions: list[Session] = []
    current: list[CaptureFrameRecord] = []
    for frame in ordered:
        if current and (
            frame.object_name != current[-1].object_name
            or frame.date_obs - current[-1].date_obs > SESSION_GAP
        ):
            sessions.append(_session_of(current))
            current = []
        current.append(frame)
    if current:
        sessions.append(_session_of(current))
    return sorted(sessions, key=lambda session: session.started_at_utc)


def _session_of(frames: list[CaptureFrameRecord]) -> Session:
    return Session(
        object_name=frames[0].object_name,
        started_at_utc=frames[0].date_obs,
        ended_at_utc=frames[-1].date_obs,
        frames=tuple(frames),
    )


__all__ = ["SESSION_GAP", "Session", "derive_sessions"]
