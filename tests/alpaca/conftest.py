"""Fixtures for the Alpaca driver tests.

Everything runs against the in-process Seestar emulator and an in-process ASGI
transport. No test here opens a socket — ``pytest-socket`` is on, and the offline
path is the point.

The emulator models the recorded firmware 8.46 surface, including the verified
view/goto path and its ``code`` 210 precondition. Where a test needs a device
state the emulator cannot reach on its own — an alt-az mount, a wheel mid-move, a
missing field — it patches one method through the emulator's own ``add_method``
extension point rather than reaching into its internals.
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Callable, Iterator, Mapping

import httpx
import pytest

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.server import ServerConfig, create_app
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator, _Reply
from ssc.seestar.session import BackoffPolicy, JsonRpcSeestarSession, SeestarSession

#: The observed lifecycle with the silences collapsed, and a goto that stays in
#: progress until a test ends it — so "Slewing is true right after the async call
#: returns" is asserted deterministically rather than raced against a timer.
QUIET = EmulatorConfig(
    event_stream_silence_s=0.01,
    close_event_stream_after_push=False,
    event_idle_timeout_s=None,
    reset_refusal_s=0.05,
    goto_settle_s=60.0,
)


@pytest.fixture
async def emulator() -> AsyncIterator[SeestarEmulator]:
    device = SeestarEmulator(QUIET)
    try:
        yield device
    finally:
        await device.aclose()


def make_backend(
    emulator: SeestarEmulator, *, cache_ttl_s: float = 0.0, ready_budget_s: float = 5.0
) -> SeestarBackend:
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(1),
        call_timeout=2.0,
        event_reattach_delay_s=0.01,
    )
    return SeestarBackend(session, cache_ttl_s=cache_ttl_s, ready_budget_s=ready_budget_s)


def make_targetable_backend(
    emulator: SeestarEmulator, *, cache_ttl_s: float = 0.0, ready_budget_s: float = 5.0
) -> tuple[SeestarBackend, list[tuple[str, int]]]:
    """A backend with no session until one is addressed — ``ssc.set_device_address``'s
    other half.

    Returns the backend alongside the list its session factory appends
    ``(host, port)`` to, every time it is asked to build one, so a test can
    assert what a ``set_target_address`` call actually caused on the next
    connect rather than only that the action answered something plausible.
    """
    calls: list[tuple[str, int]] = []

    def factory(host: str, port: int) -> SeestarSession:
        calls.append((host, port))
        return JsonRpcSeestarSession(
            emulator.transport_factory(),
            backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
            rng=random.Random(1),
            call_timeout=2.0,
            event_reattach_delay_s=0.01,
        )

    backend = SeestarBackend(
        session_factory=factory, cache_ttl_s=cache_ttl_s, ready_budget_s=ready_budget_s
    )
    return backend, calls


@pytest.fixture
async def backend(emulator: SeestarEmulator) -> AsyncIterator[SeestarBackend]:
    # cache_ttl_s = 0 so every assertion sees the device's current answer rather
    # than one cached a moment earlier. Caching gets its own test.
    instance = make_backend(emulator, cache_ttl_s=0.0)
    try:
        yield instance
    finally:
        await instance.aclose()


@pytest.fixture
def app(backend: SeestarBackend) -> Iterator[object]:
    yield create_app(backend, config=ServerConfig(server_name="SSC Alpaca test"))


@pytest.fixture
async def http(app: object) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=transport, base_url="http://alpaca") as client:
        yield client


@pytest.fixture
async def linked(http: httpx.AsyncClient) -> httpx.AsyncClient:
    """An HTTP client with all four devices connected."""
    await connect(http)
    return http


# --------------------------------------------------------------------------- #
# Emulator scripting — the emulator's own extension point, never its internals
# --------------------------------------------------------------------------- #


def override_method(emulator: SeestarEmulator, method: str, result: object) -> None:
    """Pin one method's result, for a device state the emulator cannot reach."""
    emulator.add_method(method, lambda _params: _Reply(result=result))


def override_mount(emulator: SeestarEmulator, **fields: object) -> None:
    """Patch the ``mount`` section of ``get_device_state``, leaving the rest alone."""
    delegate = emulator._methods["get_device_state"]

    def handler(params: Mapping[str, object]) -> _Reply:
        reply = delegate(params)
        result = reply.result
        if isinstance(result, dict) and isinstance(result.get("mount"), dict):
            mount = {**result["mount"], **fields}
            return _Reply(result={**result, "mount": mount})
        return reply

    emulator.add_method("get_device_state", handler)


def record_calls(emulator: SeestarEmulator, method: str) -> list[Mapping[str, object]]:
    """Capture the parameters a verb is sent, without changing what it does."""
    delegate = emulator._methods[method]
    calls: list[Mapping[str, object]] = []

    def handler(params: Mapping[str, object]) -> _Reply:
        calls.append(dict(params))
        return delegate(params)

    emulator.add_method(method, handler)
    return calls


# --------------------------------------------------------------------------- #
# Request helpers
# --------------------------------------------------------------------------- #


async def api_get(
    http: httpx.AsyncClient,
    device: str,
    member: str,
    /,
    *,
    device_number: int = 0,
    **params: object,
) -> httpx.Response:
    return await http.get(
        f"/api/v1/{device}/{device_number}/{member}",
        params={k: str(v) for k, v in params.items()},
    )


async def api_put(
    http: httpx.AsyncClient,
    device: str,
    member: str,
    /,
    *,
    device_number: int = 0,
    **params: object,
) -> httpx.Response:
    return await http.put(
        f"/api/v1/{device}/{device_number}/{member}",
        data={k: str(v) for k, v in params.items()},
    )


async def value(http: httpx.AsyncClient, device: str, member: str, /, **params: object) -> object:
    response = await api_get(http, device, member, **params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ErrorNumber"] == 0, body
    return body["Value"]


def _error_number(response: httpx.Response) -> int:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ErrorNumber"] != 0, body
    assert body["ErrorMessage"], "a non-zero ErrorNumber must carry a message"
    number = body["ErrorNumber"]
    assert isinstance(number, int)
    return number


async def error_number(
    http: httpx.AsyncClient, device: str, member: str, /, **params: object
) -> int:
    return _error_number(await api_get(http, device, member, **params))


async def put_error_number(
    http: httpx.AsyncClient, device: str, member: str, /, **params: object
) -> int:
    return _error_number(await api_put(http, device, member, **params))


async def action(
    http: httpx.AsyncClient,
    name: str,
    parameters: str | None = None,
    *,
    device: str = "telescope",
) -> httpx.Response:
    payload: dict[str, object] = {"Action": name}
    if parameters is not None:
        payload["Parameters"] = parameters
    return await api_put(http, device, "action", **payload)


async def connect(http: httpx.AsyncClient, *devices: str) -> None:
    for device in devices or ("telescope", "camera", "focuser", "filterwheel"):
        response = await api_put(http, device, "connected", Connected=True)
        assert response.status_code == 200, response.text
        assert response.json()["ErrorNumber"] == 0, response.text


Override = Callable[..., None]
