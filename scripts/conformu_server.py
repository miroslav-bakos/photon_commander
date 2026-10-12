"""Stand up the SSC Alpaca driver on loopback, backed by the Seestar emulator.

Used by ``scripts/run-conformu.sh`` to give ConformU something real to test
against without live hardware. Not part of ``ssc`` — this is test/CI
infrastructure owned by ``devops-packaging``, built entirely from the public
``ssc.alpaca`` and ``ssc.seestar`` surface (no privileged access to internals).

Prints ``SSC-ALPACA-READY`` to stdout once uvicorn has bound the socket, so the
driving shell script can probe deterministically rather than sleep. Exits
cleanly on SIGTERM/SIGINT (systemd-style shutdown), matching the durability
expectations the rest of the project holds itself to.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import signal
from collections.abc import Sequence

import uvicorn

from ssc.alpaca.backend import SeestarBackend
from ssc.alpaca.server import ServerConfig, create_app
from ssc.seestar.emulator import EmulatorConfig, SeestarEmulator
from ssc.seestar.session import BackoffPolicy, JsonRpcSeestarSession

#: Fast timing so a full ConformU pass over four devices finishes in seconds,
#: not minutes. Mirrors tests/alpaca/conftest.py's QUIET fixture in spirit —
#: this script does not import tests/ (devops-packaging does not own it) — but
#: is not identical: goto_settle_s is a couple of seconds rather than 60, since
#: nothing here holds a "goto in progress" open across a whole ConformU run.
_EMULATOR_CONFIG = EmulatorConfig(
    event_stream_silence_s=0.01,
    close_event_stream_after_push=False,
    event_idle_timeout_s=None,
    reset_refusal_s=0.05,
    goto_settle_s=2.0,
)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5555)
    return parser.parse_args(argv)


async def _serve(host: str, port: int) -> None:
    emulator = SeestarEmulator(_EMULATOR_CONFIG)
    session = JsonRpcSeestarSession(
        emulator.transport_factory(),
        backoff=BackoffPolicy(initial_s=0.02, factor=2.0, cap_s=0.1),
        rng=random.Random(1),
        call_timeout=5.0,
        event_reattach_delay_s=0.01,
    )
    backend = SeestarBackend(session, cache_ttl_s=0.0, ready_budget_s=5.0)
    app = create_app(
        backend,
        config=ServerConfig(host=host, port=port, server_name="SSC Alpaca (ConformU emulator)"),
    )

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    async def _serve_task() -> None:
        await server.serve()

    serve_task = asyncio.ensure_future(_serve_task())

    # uvicorn.Server flips `.started` once the socket is bound and listening;
    # there is no event to await instead, only a plain attribute on an object
    # this script does not own, so this really is a poll rather than a wait.
    while not server.started and not serve_task.done():  # noqa: ASYNC110
        await asyncio.sleep(0.01)
    print("SSC-ALPACA-READY", flush=True)

    await stop.wait()
    server.should_exit = True
    await serve_task
    await backend.aclose()
    await emulator.aclose()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    asyncio.run(_serve(args.host, args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
