# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``GET /api/state`` — the full snapshot and the ``seq`` it is current as of."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

from ssc.api.deps import ServicesDep, SessionDep
from ssc.api.schemas import StateResponse
from ssc.state.models import refresh_devices_staleness, refresh_mount_staleness

router = APIRouter(prefix="/api", tags=["state"])


@router.get("/state", response_model=StateResponse)
async def read_state(services: ServicesDep, _session: SessionDep) -> StateResponse:
    view = services.state.read()
    #: A full snapshot is "the truth" a client resyncs to (architecture policy invariant
    #: 4), so ``mount.stale`` is settled fresh here rather than trusting
    #: whatever it read as at the last poll tick — see
    #: ``ssc.state.models.MountState``'s docstring for why it is not a
    #: computed field instead. ``devices.stale`` gets the same treatment,
    #: same reason (``AlpacaDevicesState``'s docstring).
    now = datetime.now(tz=UTC)
    snapshot = refresh_devices_staleness(refresh_mount_staleness(view.snapshot, now=now), now=now)
    return StateResponse(
        epoch=services.state.epoch,
        seq=view.seq,
        oldest_replayable_seq=services.state.oldest_replayable_seq,
        snapshot=snapshot,
    )
