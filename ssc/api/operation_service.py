# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Backend-owned command lifecycles shared by every reconnecting client."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from ssc.api.tasks import TaskRegistry
from ssc.state.events import OperationsChanged
from ssc.state.models import OperationState, OperationStatus
from ssc.state.store import StateStore

OperationWork = Callable[["OperationContext"], Awaitable[dict[str, Any] | None]]


class OperationBusyError(Exception):
    """An exclusive operation of this kind is already running."""


class OperationNotFoundError(Exception):
    """No retained operation has the requested id."""


class OperationNotCancellableError(Exception):
    """Cancellation would leave an unsafe or ambiguous partial mutation."""


class OperationContext:
    def __init__(self, service: OperationService, operation_id: str) -> None:
        self._service = service
        self.operation_id = operation_id

    def progress(
        self,
        *,
        message: str,
        current: float | None = None,
        total: float | None = None,
    ) -> None:
        self._service.update(
            self.operation_id,
            message=message,
            progress_current=current,
            progress_total=total,
        )


class OperationService:
    """Starts work once, retains its outcome, and never depends on a request task."""

    def __init__(self, state: StateStore, tasks: TaskRegistry, *, retained: int = 50) -> None:
        self._state = state
        self._tasks = tasks
        self._retained = retained
        self._running: dict[str, asyncio.Task[Any]] = {}
        self._idempotency: dict[tuple[str, str], str] = {}

    @property
    def items(self) -> list[OperationState]:
        return self._state.snapshot.operations

    def get(self, operation_id: str) -> OperationState:
        for item in self.items:
            if item.id == operation_id:
                return item
        raise OperationNotFoundError(f"operation {operation_id!r} does not exist")

    def active(self, kind: str) -> OperationState | None:
        return next(
            (
                item
                for item in self.items
                if item.kind == kind and item.status in {OperationStatus.QUEUED, OperationStatus.RUNNING}
            ),
            None,
        )

    def start(
        self,
        *,
        kind: str,
        work: OperationWork,
        message: str,
        idempotency_key: str | None = None,
        exclusive: bool = True,
        cancellable: bool = False,
    ) -> OperationState:
        if idempotency_key is not None:
            existing_id = self._idempotency.get((kind, idempotency_key))
            if existing_id is not None:
                try:
                    return self.get(existing_id)
                except OperationNotFoundError:
                    # The retained operation ring evicted this old result; its
                    # key must not turn a legitimate later retry into a 500.
                    self._idempotency.pop((kind, idempotency_key), None)
        if exclusive and (active := self.active(kind)) is not None:
            raise OperationBusyError(f"{kind} operation {active.id} is already running")
        now = datetime.now(tz=UTC)
        item = OperationState(
            id=uuid.uuid4().hex,
            kind=kind,
            status=OperationStatus.QUEUED,
            message=message,
            created_at=now,
            updated_at=now,
            idempotency_key=idempotency_key,
            cancellable=cancellable,
        )
        if idempotency_key is not None:
            self._idempotency[(kind, idempotency_key)] = item.id
        self._replace(item)
        task = self._tasks.spawn(self._run(item.id, work), name=f"operation-{kind}-{item.id[:8]}")
        self._running[item.id] = task
        task.add_done_callback(lambda _task: self._running.pop(item.id, None))
        return item

    async def _run(self, operation_id: str, work: OperationWork) -> None:
        self.update(operation_id, status=OperationStatus.RUNNING)
        try:
            result = await work(OperationContext(self, operation_id))
        except asyncio.CancelledError:
            self.update(
                operation_id,
                status=OperationStatus.CANCELLED,
                message="Cancelled.",
                finished_at=datetime.now(tz=UTC),
            )
            raise
        except Exception as exc:
            self.update(
                operation_id,
                status=OperationStatus.FAILED,
                message="Failed.",
                error=f"{type(exc).__name__}: {exc}",
                finished_at=datetime.now(tz=UTC),
            )
            return
        self.update(
            operation_id,
            status=OperationStatus.SUCCEEDED,
            message="Completed.",
            result=result,
            finished_at=datetime.now(tz=UTC),
        )

    async def cancel(self, operation_id: str) -> OperationState:
        item = self.get(operation_id)
        if item.status not in {OperationStatus.QUEUED, OperationStatus.RUNNING}:
            return item
        if not item.cancellable:
            raise OperationNotCancellableError(
                f"{item.kind} operation {item.id} cannot be cancelled safely"
            )
        task = self._running.get(operation_id)
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self.get(item.id)

    def update(self, operation_id: str, **updates: object) -> OperationState:
        item = self.get(operation_id)
        updates.setdefault("updated_at", datetime.now(tz=UTC))
        replacement = item.model_copy(update=updates)
        self._replace(replacement)
        return replacement

    def _replace(self, replacement: OperationState) -> None:
        items = [item for item in self.items if item.id != replacement.id]
        items.append(replacement)
        retained = items[-self._retained :]
        retained_ids = {item.id for item in retained}
        self._idempotency = {
            key: operation_id
            for key, operation_id in self._idempotency.items()
            if operation_id in retained_ids
        }
        self._state.apply(OperationsChanged(operations=retained))


__all__ = [
    "OperationBusyError",
    "OperationContext",
    "OperationNotCancellableError",
    "OperationNotFoundError",
    "OperationService",
]
