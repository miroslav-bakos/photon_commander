"""Manual mount dispatch shares paired acquisition admission, except emergency stop."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from ssc.main import AlpacaDeviceGateway


async def test_manual_mount_dispatch_holds_motion_guard_and_stop_remains_available():
    lock = asyncio.Lock()
    entered = []

    @asynccontextmanager
    async def guard():
        async with lock:
            entered.append(True)
            yield

    async def put(member, **params):
        if member == "park":
            assert lock.locked()
        else:
            assert not lock.locked()

    gateway = object.__new__(AlpacaDeviceGateway)
    gateway._before_motion = guard
    gateway._telescope = SimpleNamespace(put=AsyncMock(side_effect=put))
    await gateway._mount_put("park")
    await gateway._mount_put("abortslew")
    await gateway._mount_put("moveaxis", Axis=0, Rate=0.0)
    assert entered == [True]
