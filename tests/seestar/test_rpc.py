"""Request/response correlation, the real error envelope, timeouts, backpressure."""

from __future__ import annotations

import asyncio
import json

import pytest

from ssc.seestar.rpc import (
    CODE_METHOD_NOT_FOUND,
    CODE_NO_SOLVE_DATA,
    JsonRpcClient,
    RpcConnectionLostError,
    RpcRemoteError,
    RpcResponse,
    RpcTimeoutError,
    parse_device_uptime,
)
from ssc.seestar.transport import ConnectionLostError
from tests.seestar.conftest import ScriptedTransport


async def _client(transport: ScriptedTransport, **kwargs: object) -> JsonRpcClient:
    await transport.connect()
    client = JsonRpcClient(transport, **kwargs)  # type: ignore[arg-type]
    await client.start()
    return client


# --------------------------------------------------------------------------- #
# Timestamp is a device uptime, not a clock
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("969.942410482", 969.942410482), ("1723.51", 1723.51), (12.0, 12.0)],
)
def test_timestamp_parses_as_seconds_of_uptime(raw: object, expected: float) -> None:
    assert parse_device_uptime(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["2026-08-28T21:00:00Z", "", None, {}, True])
def test_unparseable_timestamp_is_unknown_not_zero(raw: object) -> None:
    assert parse_device_uptime(raw) is None


# --------------------------------------------------------------------------- #
# Correlation
# --------------------------------------------------------------------------- #


async def test_responses_are_matched_by_id_even_out_of_order() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        first = asyncio.create_task(client.call("get_wheel_state"))
        second = asyncio.create_task(client.call("get_focuser_position"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        transport.feed_response(2, "get_focuser_position", result=1209)
        transport.feed_response(1, "get_wheel_state", result={"id": 0})

        assert (await first).result == {"id": 0}
        assert (await second).result == 1209
    finally:
        await client.aclose()


async def test_the_request_is_the_shape_observed_to_work() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        task = asyncio.create_task(client.call("get_device_state", {"keys": ["device"]}))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(1, "get_device_state", result={})
        await task
    finally:
        await client.aclose()

    sent = json.loads(transport.sent[0])
    assert sent == {"id": 1, "method": "get_device_state", "params": {"keys": ["device"]}}
    assert "jsonrpc" not in sent


# --------------------------------------------------------------------------- #
# The error model: 103 is ordinary, and it must not look like success
# --------------------------------------------------------------------------- #


async def test_a_non_zero_code_raises_and_leaves_the_connection_healthy() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        task = asyncio.create_task(client.call("scope_get_equ_coord"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(
            1, "scope_get_equ_coord", code=CODE_METHOD_NOT_FOUND, error="method not found"
        )

        with pytest.raises(RpcRemoteError) as caught:
            await task
        assert caught.value.code == CODE_METHOD_NOT_FOUND
        assert caught.value.error.label == "method not found"
        assert client.is_running is True

        follow_up = asyncio.create_task(client.call("test_connection"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(2, "test_connection", result="server connected!")
        assert (await follow_up).result == "server connected!"
    finally:
        await client.aclose()


async def test_a_non_zero_code_can_be_a_legitimate_answer() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        task = asyncio.create_task(client.call("get_last_solve_result", raise_on_error=False))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(
            1, "get_last_solve_result", code=CODE_NO_SOLVE_DATA, error="no solve data"
        )

        response = await task
        assert response.ok is False
        assert response.device_error is not None
        assert response.device_error.label == "no solve data"
    finally:
        await client.aclose()


def test_an_error_nested_inside_result_is_not_read_as_success() -> None:
    response = RpcResponse(
        request_id=1,
        method="get_solve_result",
        code=0,
        result={"code": CODE_NO_SOLVE_DATA, "error": "no solve data"},
    )

    assert response.ok is True  # the outer envelope really did succeed
    assert response.nested_error is not None
    assert response.any_error is not None
    assert response.any_error.code == CODE_NO_SOLVE_DATA


def test_an_empty_result_is_a_real_answer_not_an_error() -> None:
    # get_view_state when idle, and an invalid get_device_state key, both do this.
    response = RpcResponse(request_id=1, method="get_view_state", code=0, result={})
    assert response.ok is True
    assert response.any_error is None


# --------------------------------------------------------------------------- #
# Timeout, loss, cancellation, backpressure
# --------------------------------------------------------------------------- #


async def test_a_timeout_raises_rather_than_returning_an_empty_result() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        with pytest.raises(RpcTimeoutError) as caught:
            await client.call("test_connection", timeout=0.02)
        assert caught.value.method == "test_connection"
    finally:
        await client.aclose()


async def test_an_in_flight_call_fails_distinctly_when_the_server_resets() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        task = asyncio.create_task(client.call("get_setting", timeout=2.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.fail(ConnectionLostError("connection reset by peer", was_reset=True))

        with pytest.raises(RpcConnectionLostError):
            await task

        failure = await client.wait_closed()
        assert isinstance(failure, ConnectionLostError)
        assert failure.was_reset is True
    finally:
        await client.aclose()


async def test_calling_a_dead_client_fails_immediately() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        transport.fail(ConnectionLostError("gone", was_reset=True))
        await client.wait_closed()

        with pytest.raises(RpcConnectionLostError):
            await client.call("test_connection")
    finally:
        await client.aclose()


async def test_cancellation_drops_the_correlation_entry() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        task = asyncio.create_task(client.call("test_connection", timeout=5.0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # A late reply for the abandoned id must not resolve anything, and the
        # client must still be usable.
        transport.feed_response(1, "test_connection", result="server connected!")
        follow_up = asyncio.create_task(client.call("test_connection"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(2, "test_connection", result="server connected!")
        assert (await follow_up).ok is True
    finally:
        await client.aclose()


async def test_backpressure_bounds_the_requests_on_the_wire() -> None:
    transport = ScriptedTransport()
    client = await _client(transport, max_in_flight=1)
    try:
        first = asyncio.create_task(client.call("test_connection", timeout=2.0))
        second = asyncio.create_task(client.call("get_wheel_state", timeout=2.0))
        for _ in range(6):
            await asyncio.sleep(0)

        assert len(transport.sent) == 1, "the second request must wait for a free slot"

        transport.feed_response(1, "test_connection", result="server connected!")
        await first
        for _ in range(6):
            await asyncio.sleep(0)
        assert len(transport.sent) == 2

        transport.feed_response(2, "get_wheel_state", result={"id": 0})
        await second
    finally:
        await client.aclose()


async def test_unsolicited_frames_reach_the_handshake_seam() -> None:
    seen: list[dict[str, object]] = []
    transport = ScriptedTransport()
    client = await _client(transport, on_unsolicited=lambda raw: seen.append(dict(raw)))
    try:
        transport.feed(json.dumps({"Event": "Greeting", "Timestamp": "1.0"}))
        for _ in range(6):
            await asyncio.sleep(0)
        assert seen == [{"Event": "Greeting", "Timestamp": "1.0"}]
    finally:
        await client.aclose()


async def test_a_garbage_line_does_not_kill_the_reader() -> None:
    transport = ScriptedTransport()
    client = await _client(transport)
    try:
        transport.feed("not json at all")
        task = asyncio.create_task(client.call("test_connection"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        transport.feed_response(1, "test_connection", result="server connected!")
        assert (await task).ok is True
    finally:
        await client.aclose()
