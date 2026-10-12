# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``(snapshot, event) -> snapshot``. Pure, total, and shared with the client.

This is the single definition of what an event *means*. The server applies it to
produce the authoritative snapshot; a reconnecting client applies the same
function to the replayed events. If the two implementations ever disagree, the
reconnect test in ``tests/api/test_ws_replay.py`` fails, which is the entire
reason the function exists instead of the mutation being inlined into the store.

No clock, no I/O, no ``datetime.now()``: every timestamp a snapshot carries comes
out of the event, so replay reconstructs it exactly rather than approximately.
"""

from __future__ import annotations

from ssc.state.events import (
    AutoConnectChanged,
    AutofocusChanged,
    CameraFovChanged,
    CaptureChanged,
    DeviceChanged,
    DeviceEventReceived,
    DevicesChanged,
    DiscoveryChanged,
    LinkChanged,
    LocationChanged,
    MountChanged,
    NoticeCleared,
    NoticeRaised,
    OperationsChanged,
    PlansChanged,
    PolarAlignmentChanged,
    RunChanged,
    SettingsChanged,
    SkymapChanged,
    StateEvent,
)
from ssc.state.models import MAX_NOTICES, StateSnapshot


def reduce(snapshot: StateSnapshot, event: StateEvent) -> StateSnapshot:
    """Apply one event. Never mutates ``snapshot``; returns the successor."""
    match event:
        case LinkChanged():
            return snapshot.model_copy(update={"link": event.link})
        case DeviceChanged():
            return snapshot.model_copy(update={"device": event.device})
        case MountChanged():
            return snapshot.model_copy(update={"mount": event.mount})
        case DevicesChanged():
            return snapshot.model_copy(update={"devices": event.devices})
        case CameraFovChanged():
            return snapshot.model_copy(update={"camera_fov": event.camera_fov})
        case AutoConnectChanged():
            return snapshot.model_copy(update={"auto_connect": event.auto_connect})
        case DiscoveryChanged():
            return snapshot.model_copy(update={"discovery": event.discovery})
        case LocationChanged():
            return snapshot.model_copy(update={"location": event.location})
        case SettingsChanged():
            return snapshot.model_copy(update={"settings": event.settings})
        case NoticeRaised():
            kept = [notice for notice in snapshot.notices if notice.id != event.notice.id]
            kept.append(event.notice)
            return snapshot.model_copy(update={"notices": kept[-MAX_NOTICES:]})
        case NoticeCleared():
            kept = [notice for notice in snapshot.notices if notice.id != event.notice_id]
            return snapshot.model_copy(update={"notices": kept})
        case DeviceEventReceived():
            return snapshot.model_copy(
                update={
                    "last_device_event": event.event,
                    "device_events_seen": snapshot.device_events_seen + 1,
                }
            )
        case PlansChanged():
            return snapshot.model_copy(update={"plans": event.plans})
        case SkymapChanged():
            return snapshot.model_copy(update={"skymap": event.skymap})
        case RunChanged():
            return snapshot.model_copy(update={"run": event.run})
        case CaptureChanged():
            return snapshot.model_copy(update={"capture": event.capture})
        case PolarAlignmentChanged():
            return snapshot.model_copy(update={"polar_alignment": event.polar_alignment})
        case AutofocusChanged():
            return snapshot.model_copy(update={"autofocus": event.autofocus})
        case OperationsChanged():
            return snapshot.model_copy(update={"operations": event.operations})


def replay(snapshot: StateSnapshot, events: list[StateEvent]) -> StateSnapshot:
    """Fold :func:`reduce` over an ordered run of events."""
    for event in events:
        snapshot = reduce(snapshot, event)
    return snapshot


__all__ = ["reduce", "replay"]
