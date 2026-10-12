# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""``WS /ws`` — typed frames from a client-supplied ``seq``.

The frame union is discriminated on ``type`` for the same reason the event union
is: the TypeScript client is generated from it. Frames distinguish state, refusals and liveness:

``snapshot``
    A full state, with the ``reason`` it was sent. A client that asked to resume
    and receives this knows it was resnapshotted and why — it must never render
    a stale snapshot as live, and it must never mistake a resnapshot for a
    continuation.
``event``
    One sequenced event. Apply with :func:`ssc.state.reducer.reduce`.
``error``
    A refusal the client can act on, sent before the socket closes.
``heartbeat``
    Idle liveness with the last delivered sequence; never a state update.

Resume semantics: ``?seq=N`` asks for everything after ``N``. If ``N`` is still
inside the retained ring, the client gets only the events it missed and no
snapshot at all. If it has fallen out of the ring — or is somehow ahead of the
server, which means the server restarted — the client gets a ``snapshot`` frame
with ``reason`` set to ``gap`` or ``server_restarted``.

``seq`` alone cannot tell a restarted process from the one before it: a fresh
process resets its own ``seq`` to 0 and starts counting again, so a client that
resumes with ``?seq=N`` shortly after a restart can find a *plausible* — but
entirely wrong — event at that position, stitched onto a snapshot the new
process never produced. Every frame therefore also carries the store's
:attr:`~ssc.state.store.StateStore.epoch`, a per-process id, and a resuming
client must present the ``epoch`` it last saw alongside ``?seq``. If it is
missing or does not match this process's, the request is treated exactly like
a gap: a ``snapshot`` frame with ``reason="server_restarted"``, never a replay
onto a snapshot this process never sent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from ssc.api.deps import Services, get_services_ws
from ssc.api.security import SESSION_COOKIE
from ssc.state.events import StateEvent
from ssc.state.models import StateSnapshot, refresh_devices_staleness, refresh_mount_staleness
from ssc.state.store import Gap, Replay, StateStore, Subscription

_log = logging.getLogger(__name__)

router = APIRouter()

#: Sent instead of a normal close when the socket is not paired.
WS_UNAUTHORISED = 4401
HEARTBEAT_SECONDS = 10.0

SnapshotReason = Literal["initial", "gap", "overflow", "server_restarted"]


class Frame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    #: This server process's identity (:attr:`ssc.state.store.StateStore.epoch`).
    #: A client resuming with ``?seq=N`` must present the ``epoch`` it last saw
    #: alongside it, so the server can tell a continuation from a stitch onto a
    #: different process's history.
    epoch: str


class SnapshotFrame(Frame):
    type: Literal["snapshot"] = "snapshot"
    reason: SnapshotReason
    seq: int
    oldest_replayable_seq: int
    #: What the client asked to resume from, when it asked. ``None`` on first connect.
    requested_seq: int | None = None
    snapshot: StateSnapshot


class EventFrame(Frame):
    type: Literal["event"] = "event"
    seq: int
    event: StateEvent


class ErrorFrame(Frame):
    type: Literal["error"] = "error"
    code: str
    message: str


class HeartbeatFrame(Frame):
    """Liveness only; seq is the last event delivered on this connection."""

    type: Literal["heartbeat"] = "heartbeat"
    seq: int


ServerFrame = Annotated[
    SnapshotFrame | EventFrame | ErrorFrame | HeartbeatFrame, Field(discriminator="type")
]

SERVER_FRAME_ADAPTER: TypeAdapter[ServerFrame] = TypeAdapter(ServerFrame)


def snapshot_frame(
    store: StateStore, reason: SnapshotReason, *, requested_seq: int | None = None
) -> SnapshotFrame:
    view = store.read()
    # "The truth" a client resyncs to (module docstring) — mount.stale and
    # devices.stale are settled fresh here, mirroring GET /api/state; see
    # ssc.state.models.MountState's docstring for why neither is a computed
    # field that would do this on its own.
    now = datetime.now(tz=UTC)
    snapshot = refresh_devices_staleness(refresh_mount_staleness(view.snapshot, now=now), now=now)
    return SnapshotFrame(
        epoch=store.epoch,
        reason=reason,
        seq=view.seq,
        oldest_replayable_seq=store.oldest_replayable_seq,
        requested_seq=requested_seq,
        snapshot=snapshot,
    )


async def _send(
    websocket: WebSocket, frame: SnapshotFrame | EventFrame | ErrorFrame | HeartbeatFrame
) -> None:
    await websocket.send_text(frame.model_dump_json())


@router.websocket("/ws")
async def state_stream(
    websocket: WebSocket,
    services: Annotated[Services, Depends(get_services_ws)],
    seq: Annotated[int | None, Query(ge=0)] = None,
    epoch: Annotated[str | None, Query()] = None,
) -> None:
    # Mirrors ssc.api.deps.require_session's bypass: with auth_required off
    # there is no cookie to check, and the HTTP side already lets every
    # caller through — the desktop shell and the phone must never disagree
    # about whether a session is needed (module docstring, architecture policy 2026-09-09).
    if services.settings.effective_auth_required:
        session = await services.auth.resolve(websocket.cookies.get(SESSION_COOKIE))
        if session is None:
            await websocket.close(code=WS_UNAUTHORISED, reason="not paired")
            return

    await websocket.accept()
    store = services.state
    subscription = store.subscribe()
    try:
        last_sent = await _prime(websocket, store, subscription, seq, epoch)
        await _pump(websocket, store, subscription, last_sent)
    except WebSocketDisconnect:
        pass
    finally:
        store.unsubscribe(subscription)


async def _prime(
    websocket: WebSocket,
    store: StateStore,
    subscription: Subscription,
    seq: int | None,
    epoch: str | None,
) -> int:
    """Send the resume prefix. Returns the highest ``seq`` already delivered.

    Everything up to the ``await`` is synchronous, so no event can be applied
    between subscribing, reading the buffer and reading the snapshot — the
    prefix and the live feed cannot overlap or leave a hole.

    A ``seq`` without a matching ``epoch`` is never trusted to resume from: a
    ``seq`` that looks resumable can belong to a process that is no longer this
    one, and stitching its events onto *this* process's snapshot is exactly the
    stale-render CLAUDE.md invariant 4 forbids. Missing or mismatched ``epoch``
    is therefore treated exactly like a gap.
    """
    if seq is None:
        frame = snapshot_frame(store, "initial")
        await _send(websocket, frame)
        return frame.seq

    if epoch is None or epoch != store.epoch:
        frame = snapshot_frame(store, "server_restarted", requested_seq=seq)
        await _send(websocket, frame)
        return frame.seq

    outcome = store.since(seq)
    match outcome:
        case Gap():
            reason: SnapshotReason = "server_restarted" if seq > outcome.current_seq else "gap"
            frame = snapshot_frame(store, reason, requested_seq=seq)
            await _send(websocket, frame)
            return frame.seq
        case Replay():
            for item in outcome.events:
                await _send(
                    websocket, EventFrame(epoch=store.epoch, seq=item.seq, event=item.event)
                )
            return outcome.events[-1].seq if outcome.events else seq


async def _pump(
    websocket: WebSocket, store: StateStore, subscription: Subscription, last_sent: int
) -> None:
    """Stream events until the client goes away or the store shuts down."""
    receiver = asyncio.create_task(_drain_client(websocket), name="ws-receive")
    try:
        while True:
            if subscription.overflowed:
                last_sent = await _resnapshot(websocket, store, subscription)
                continue

            getter = asyncio.create_task(subscription.queue.get(), name="ws-queue")
            closed = asyncio.create_task(subscription.closed.wait(), name="ws-closed")
            done, _pending = await asyncio.wait(
                {getter, closed, receiver},
                timeout=HEARTBEAT_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in (getter, closed):
                if task not in done:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            if receiver in done or closed in done:
                return
            if getter not in done:
                await _send(websocket, HeartbeatFrame(epoch=store.epoch, seq=last_sent))
                continue
            item = getter.result()
            if item.seq <= last_sent:
                continue
            await _send(websocket, EventFrame(epoch=store.epoch, seq=item.seq, event=item.event))
            last_sent = item.seq
    finally:
        receiver.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
            await receiver


async def _resnapshot(websocket: WebSocket, store: StateStore, subscription: Subscription) -> int:
    """Recover a subscriber that fell behind, and tell it that is what happened."""
    while not subscription.queue.empty():
        subscription.queue.get_nowait()
    frame = snapshot_frame(store, "overflow")
    subscription.overflowed = False
    await _send(websocket, frame)
    return frame.seq


async def _drain_client(websocket: WebSocket) -> None:
    """Read and discard client frames. Its purpose is noticing the disconnect."""
    while True:
        message = await websocket.receive()
        if message.get("type") == "websocket.disconnect":
            return


def frame_schemas() -> tuple[dict[str, Any], dict[str, Any]]:
    """``(root schema, component definitions)`` for the WS frame union.

    FastAPI does not document WebSockets, so :func:`ssc.main.create_app` merges
    these into ``components.schemas`` by hand. Without them the TypeScript client
    would have to hand-mirror the union, which is exactly the drift the
    discriminator exists to prevent.
    """
    schema = SERVER_FRAME_ADAPTER.json_schema(ref_template="#/components/schemas/{model}")
    definitions: dict[str, Any] = schema.pop("$defs", {})
    return schema, definitions


__all__ = [
    "SERVER_FRAME_ADAPTER",
    "WS_UNAUTHORISED",
    "ErrorFrame",
    "EventFrame",
    "ServerFrame",
    "SnapshotFrame",
    "frame_schemas",
    "router",
    "snapshot_frame",
]
