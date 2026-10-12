# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Backend command lifecycle guarantees used by reconnecting clients."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from ssc.api.operation_service import (
    OperationContext,
    OperationNotCancellableError,
    OperationService,
)
from ssc.api.tasks import TaskRegistry
from ssc.state.models import OperationStatus, StateSnapshot
from ssc.state.store import StateStore


@pytest.mark.anyio
async def test_idempotency_key_starts_work_only_once_and_retains_result() -> None:
    tasks = TaskRegistry()
    service = OperationService(
        StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC))),
        tasks,
    )
    release = asyncio.Event()
    calls = 0

    async def work(context: OperationContext) -> dict[str, object]:
        nonlocal calls
        calls += 1
        context.progress(message="Halfway", current=1, total=2)
        await release.wait()
        return {"answer": 42}

    try:
        first = service.start(
            kind="test",
            work=work,
            message="Queued",
            idempotency_key="same-request",
        )
        duplicate = service.start(
            kind="test",
            work=work,
            message="Queued again",
            idempotency_key="same-request",
        )
        assert duplicate.id == first.id
        await asyncio.sleep(0)
        assert calls == 1
        assert service.get(first.id).message == "Halfway"

        release.set()
        running_task = service._running[first.id]
        await running_task

        retained = service.get(first.id)
        assert retained.status is OperationStatus.SUCCEEDED
        assert retained.result == {"answer": 42}
        assert retained.finished_at is not None
    finally:
        await tasks.cancel_all()


@pytest.mark.anyio
async def test_cancel_is_authoritative_and_retained() -> None:
    tasks = TaskRegistry()
    service = OperationService(
        StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC))),
        tasks,
    )

    async def work(_context: OperationContext) -> None:
        await asyncio.Event().wait()

    operation = service.start(
        kind="cancel-test", work=work, message="Queued", cancellable=True
    )
    await asyncio.sleep(0)
    cancelled = await service.cancel(operation.id)

    assert cancelled.status is OperationStatus.CANCELLED
    assert cancelled.finished_at is not None
    assert service.get(operation.id).status is OperationStatus.CANCELLED
    await tasks.cancel_all()


@pytest.mark.anyio
async def test_unsafe_partial_mutation_refuses_cancellation() -> None:
    tasks = TaskRegistry()
    service = OperationService(
        StateStore(StateSnapshot(server_started_at=datetime.now(tz=UTC))),
        tasks,
    )

    async def work(_context: OperationContext) -> None:
        await asyncio.Event().wait()

    operation = service.start(kind="unsafe-write", work=work, message="Queued")
    await asyncio.sleep(0)
    with pytest.raises(OperationNotCancellableError):
        await service.cancel(operation.id)
    assert service.get(operation.id).status is OperationStatus.RUNNING
    await tasks.cancel_all()
