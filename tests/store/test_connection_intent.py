import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ssc.api.services import DeviceService
from ssc.api.tasks import TaskRegistry
from ssc.state.models import LinkState, LinkStatus, StateSnapshot
from ssc.state.store import StateStore


@pytest.mark.parametrize('succeed', [False, True])
async def test_disconnect_invalidates_inflight_preferred_attempt(settings, succeed):
    await settings.set_ethernet_address('10.0.0.1', 4701)
    await settings.set_wifi_address('10.0.0.2', 4701)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gateway:
        default_control_port = 4701
        def __init__(self):
            self.calls = []
        def attach(self, sink):
            self.sink = sink
            return lambda: None
        async def connect(self, host, *, control_port):
            self.calls.append(host)
            entered.set()
            await release.wait()
            if not succeed:
                raise RuntimeError('timeout')
            self.sink.on_link(LinkState(status=LinkStatus.READY, changed_at=datetime.now(UTC)))
        async def disconnect(self):
            pass

    gateway = Gateway()
    state = StateStore(StateSnapshot(server_started_at=datetime.now(UTC)))
    service = DeviceService(gateway, state, settings, TaskRegistry(), SimpleNamespace())
    task = asyncio.create_task(service.connect_preferred())
    await entered.wait()
    await service.disconnect()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert gateway.calls == ['10.0.0.1']
    assert service._suspended
    assert state.snapshot.link.status == LinkStatus.DISCONNECTED
    assert settings.device.last_healthy_host is None
    service.close()


async def test_disconnect_invalidates_inflight_rediscovery(settings):
    await settings.set_wifi_address('10.0.0.1', 4701)
    entered, release = asyncio.Event(), asyncio.Event()

    class Gateway:
        default_control_port = 4701

        def __init__(self):
            self.calls = []

        def attach(self, sink):
            return lambda: None

        async def connect(self, host, *, control_port):
            self.calls.append(host)
            raise RuntimeError('offline')

        async def discover(self, *, timeout):  # noqa: ASYNC109 - gateway API signature
            entered.set()
            await release.wait()
            return [SimpleNamespace(host='10.0.0.2', control_port=4701,
                                    reachable=True, serial=None)]

        async def disconnect(self):
            pass

    gateway = Gateway()
    state = StateStore(StateSnapshot(server_started_at=datetime.now(UTC)))
    service = DeviceService(gateway, state, settings, TaskRegistry(), SimpleNamespace())
    task = asyncio.create_task(service.connect_preferred())
    await entered.wait()
    await service.disconnect()
    release.set()
    await asyncio.gather(task, return_exceptions=True)
    assert gateway.calls == ['10.0.0.1']
    assert service._suspended
    assert state.snapshot.link.status == LinkStatus.DISCONNECTED
    assert settings.device.host == '10.0.0.1'
    service.close()
