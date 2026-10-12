# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""The WS event vocabulary: a discriminated union, on purpose.

The ``type`` literal is the discriminator, so ``StateEvent`` round-trips through
``TypeAdapter`` and the TypeScript client is *generated* from the OpenAPI schema
rather than hand-mirrored. A hand-mirrored union is a client that silently
diverges the first time an event gains a field.

Every event is a whole-section replacement rather than a partial patch. That is
slightly more bytes and dramatically less ambiguity: a replaying client cannot
end up with a half-applied section, and :func:`ssc.state.reducer.reduce` stays
provably total.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from ssc.state.models import (
    AlpacaDevicesState,
    AutoConnectState,
    AutofocusState,
    CameraFieldOfView,
    CaptureState,
    DeviceDescriptor,
    DeviceEventRecord,
    DiscoveryState,
    LinkState,
    LocationState,
    MountState,
    Notice,
    OperationState,
    PlanSummary,
    PolarAlignmentState,
    RunState,
    SettingsSummary,
    SkymapState,
    StateModel,
)


class LinkChanged(StateModel):
    type: Literal["link.changed"] = "link.changed"
    link: LinkState


class DeviceChanged(StateModel):
    type: Literal["device.changed"] = "device.changed"
    device: DeviceDescriptor | None = None


class MountChanged(StateModel):
    """The whole ``mount`` section, replaced — a fresh poll tick, or a
    just-issued control's own read-back (``PUT /api/mount/tracking``)."""

    type: Literal["mount.changed"] = "mount.changed"
    mount: MountState | None = None


class DevicesChanged(StateModel):
    """The whole ``devices`` section, replaced — a fresh poll tick, mirroring
    ``MountChanged``: every device's own freshness rides on ``polled_at``
    advancing on every successful tick, not only when something changed."""

    type: Literal["devices.changed"] = "devices.changed"
    devices: AlpacaDevicesState | None = None


class CameraFovChanged(StateModel):
    """The whole ``camera_fov`` section, replaced — published once a camera
    is found and its field of view is fully known (see
    :class:`~ssc.state.models.CameraFieldOfView`). Unlike ``MountChanged``,
    this is not re-sent on every tick: the geometry it carries is read once
    per connection and does not change while connected."""

    type: Literal["camera_fov.changed"] = "camera_fov.changed"
    camera_fov: CameraFieldOfView | None = None


class AutoConnectChanged(StateModel):
    type: Literal["auto_connect.changed"] = "auto_connect.changed"
    auto_connect: AutoConnectState


class DiscoveryChanged(StateModel):
    type: Literal["discovery.changed"] = "discovery.changed"
    discovery: DiscoveryState


class LocationChanged(StateModel):
    type: Literal["location.changed"] = "location.changed"
    location: LocationState | None = None


class SettingsChanged(StateModel):
    type: Literal["settings.changed"] = "settings.changed"
    settings: SettingsSummary


class NoticeRaised(StateModel):
    type: Literal["notice.raised"] = "notice.raised"
    notice: Notice


class NoticeCleared(StateModel):
    type: Literal["notice.cleared"] = "notice.cleared"
    notice_id: str


class DeviceEventReceived(StateModel):
    type: Literal["device.event"] = "device.event"
    event: DeviceEventRecord


class PlansChanged(StateModel):
    """The whole plan list, replaced — mirrors ``SettingsChanged``: simpler
    for a client to apply correctly than a per-plan patch, at the cost of a
    few more bytes on a list that is expected to stay small."""

    type: Literal["plans.changed"] = "plans.changed"
    plans: list[PlanSummary]


class SkymapChanged(StateModel):
    """The whole tile-cache section, replaced: survey cache stats, pack
    coverage and every prefetch job's progress travel together so a client
    never shows a job at 100% beside a survey whose coverage says otherwise."""

    type: Literal["skymap.changed"] = "skymap.changed"
    skymap: SkymapState


class RunChanged(StateModel):
    """The whole ``run`` section, replaced, after every journal append —
    so a desktop and a phone never disagree about which item is running or
    whether a gate has parked the scope."""

    type: Literal["run.changed"] = "run.changed"
    run: RunState | None = None


class CaptureChanged(StateModel):
    """The whole ``capture`` section, replaced — so a desktop and a phone
    never disagree about whether SSC is mid-exposure right now (module
    docstring's own requirement, restated for a second live-mutating
    surface)."""

    type: Literal["capture.changed"] = "capture.changed"
    capture: CaptureState


class PolarAlignmentChanged(StateModel):
    """The whole ``polar_alignment`` section, replaced — after every point's
    slew/settle/capture/solve step and at the run's end, so a phone at the
    tripod and a desktop watching never disagree about which point is in
    progress (module docstring's own requirement, applied to a third
    live-mutating surface after ``run``/``capture``)."""

    type: Literal["polar_alignment.changed"] = "polar_alignment.changed"
    polar_alignment: PolarAlignmentState


class AutofocusChanged(StateModel):
    type: Literal["autofocus.changed"] = "autofocus.changed"
    autofocus: AutofocusState


class OperationsChanged(StateModel):
    type: Literal["operations.changed"] = "operations.changed"
    operations: list[OperationState]


#: The whole vocabulary, discriminated on ``type``.
StateEvent = Annotated[
    LinkChanged
    | DeviceChanged
    | MountChanged
    | DevicesChanged
    | CameraFovChanged
    | AutoConnectChanged
    | DiscoveryChanged
    | LocationChanged
    | SettingsChanged
    | NoticeRaised
    | NoticeCleared
    | DeviceEventReceived
    | PlansChanged
    | SkymapChanged
    | RunChanged
    | CaptureChanged
    | PolarAlignmentChanged
    | AutofocusChanged
    | OperationsChanged,
    Field(discriminator="type"),
]

STATE_EVENT_ADAPTER: TypeAdapter[StateEvent] = TypeAdapter(StateEvent)


__all__ = [
    "STATE_EVENT_ADAPTER",
    "AutoConnectChanged",
    "AutofocusChanged",
    "CameraFovChanged",
    "CaptureChanged",
    "DeviceChanged",
    "DeviceEventReceived",
    "DevicesChanged",
    "DiscoveryChanged",
    "LinkChanged",
    "LocationChanged",
    "MountChanged",
    "NoticeCleared",
    "NoticeRaised",
    "OperationsChanged",
    "PlansChanged",
    "PolarAlignmentChanged",
    "RunChanged",
    "SettingsChanged",
    "SkymapChanged",
    "StateEvent",
]
