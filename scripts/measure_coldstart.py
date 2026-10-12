"""Measure isolated packaged HTTP readiness with a low-overhead observer."""
from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path


def measure(binary: Path, port: int, *, deadline_s: float = 10.0) -> float:
    # Refuse an occupied port rather than accepting another server as readiness.
    with socket.socket() as probe:
        # Match Uvicorn: ignore previous connections in TIME_WAIT, while an
        # active listening socket still prevents binding to the same address.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with tempfile.TemporaryDirectory(prefix="ssc-coldstart-") as directory:
        environment = {**os.environ, "SSC_DATA_DIR": directory, "SSC_ALPACA_PORT": "0"}
        with tempfile.TemporaryFile() as log:
            start = time.monotonic()
            process = subprocess.Popen(
                [str(binary), "--no-mdns", "--port", str(port)],
                env=environment, stdout=log, stderr=log,
            )
            try:
                while time.monotonic() - start < deadline_s:
                    if process.poll() is not None:
                        log.seek(0)
                        raise RuntimeError("process exited before readiness: " + log.read().decode(errors="replace"))
                    remaining = deadline_s - (time.monotonic() - start)
                    if remaining <= 0:
                        break
                    try:
                        with opener.open(f"http://127.0.0.1:{port}/api/state", timeout=min(0.5, remaining)):
                            pass
                    except urllib.error.HTTPError:
                        # Fresh isolated data requires pairing; 401 still proves
                        # the actual HTTP server is ready, as in the former gate.
                        pass
                    except (OSError, urllib.error.URLError):
                        time.sleep(min(0.05, max(0.0, deadline_s - (time.monotonic() - start))))
                        continue
                    if process.poll() is not None:
                        raise RuntimeError("process exited while answering readiness")
                    elapsed = time.monotonic() - start
                    if elapsed <= deadline_s:
                        return elapsed
                    break
                raise RuntimeError(f"server never answered within {deadline_s:g}s")
            finally:
                if process.poll() is None:
                    process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def main() -> int:
    binary = Path("dist/ssc-core/ssc-core").resolve()
    if not binary.is_file() or not os.access(binary, os.X_OK):
        print("measure-coldstart: packaged binary missing — run scripts/build-package.sh first")
        return 1
    threshold = float(os.environ.get("SSC_COLDSTART_THRESHOLD_S", "3.0"))
    runs = int(os.environ.get("SSC_COLDSTART_RUNS", "3"))
    port = int(os.environ.get("SSC_COLDSTART_PORT", "17778"))
    if not 0 < threshold < float("inf") or runs < 1 or not 0 < port < 65536:
        raise ValueError("invalid cold-start probe settings")
    results = []
    for index in range(runs):
        try:
            elapsed = measure(binary, port)
        except (OSError, RuntimeError) as exc:
            print(f"measure-coldstart: FAIL — {exc}")
            return 1
        results.append(elapsed)
        print(f"measure-coldstart: run {index + 1} => {elapsed:.3f}s", flush=True)
    best = min(results)
    print(f"measure-coldstart: best of {runs} runs = {best:.3f}s (threshold {threshold:g}s)")
    if best > threshold:
        print("measure-coldstart: FAIL — readiness exceeds threshold")
        return 1
    print("measure-coldstart: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
