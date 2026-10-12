# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš

"""Owned background tasks. Nothing is created and forgotten.

An unawaited task whose exception is never retrieved is a night that fails
silently. Every long-lived coroutine goes in here: the registry holds a strong
reference, logs the exception if one escapes, and cancels the lot on lifespan
shutdown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

_log = logging.getLogger(__name__)


class TaskRegistry:
    """A strong-referenced set of background tasks with loud failure."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._finished)
        return task

    def _finished(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        exception = task.exception()
        if exception is not None:
            _log.error("background task %s failed", task.get_name(), exc_info=exception)

    @property
    def active(self) -> int:
        return len(self._tasks)

    async def cancel_all(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task


__all__ = ["TaskRegistry"]
