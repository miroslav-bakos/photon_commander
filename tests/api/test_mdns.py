from __future__ import annotations

from typing import Any

import zeroconf.asyncio

from ssc.main import MdnsAdvertiser
from ssc.settings.config import AppConfig


async def test_mdns_registration_allows_duplicate_instance_names(monkeypatch) -> None:
    registered: list[tuple[object, bool]] = []

    class FakeAsyncZeroconf:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def async_register_service(
            self, info: object, *, allow_name_change: bool = False
        ) -> None:
            registered.append((info, allow_name_change))

        async def async_unregister_service(self, _info: object) -> None:
            pass

        async def async_close(self) -> None:
            pass

    monkeypatch.setattr("ssc.main.local_ipv4_addresses", lambda: ["192.168.1.12"])
    monkeypatch.setattr(zeroconf.asyncio, "AsyncZeroconf", FakeAsyncZeroconf)

    advertiser = MdnsAdvertiser(AppConfig())
    await advertiser.start()
    await advertiser.stop()

    assert len(registered) == 1
    assert registered[0][1] is True
