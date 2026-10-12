#!/usr/bin/env bash
# The M1 acceptance gate: ConformU against ssc/alpaca, backed by the Seestar
# emulator. No live hardware, no network access beyond loopback.
#
# What "pass" means here is deliberately narrower than "zero ConformU output":
# firmware 8.46 has no RA/Dec getter and no Park/Unpark verb (see
# ssc/alpaca/devices/telescope.py's module docstring), so a fully ASCOM-clean
# report is not achievable and pretending otherwise would be the silent
# degradation architecture policy invariant 7 forbids. Instead:
#
#   * ConformU's own ErrorCount (protocol/behaviour violations) is the hard
#     gate. Any device with ErrorCount > 0 fails the build — that is a genuine
#     driver defect, alpaca-driver's to fix.
#   * ConformU's IssueCount (softer conformance deviations — "mandatory member
#     not implemented", timing notes, etc.) is reported in full every run but
#     does not fail the build by itself. docs/CONFORMANCE.md is the checked-in
#     record of which issues are expected and why; this script does not
#     silently swallow anything — every issue is printed, every run.
#
# Usage: scripts/run-conformu.sh [output-dir]
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="${1:-$REPO_ROOT/reports/conformu}"
HOST="${SSC_CONFORMU_HOST:-127.0.0.1}"
PORT="${SSC_CONFORMU_PORT:-5555}"
CONFORMU_BIN="${CONFORMU_BIN:-conformu}"
DEVICES=(telescope camera focuser filterwheel)
READY_TIMEOUT_S=15

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

if ! command -v "$CONFORMU_BIN" >/dev/null 2>&1; then
    echo "run-conformu: '$CONFORMU_BIN' not found on PATH." >&2
    echo "  Install it: see scripts/install-conformu.sh, or set CONFORMU_BIN." >&2
    exit 2
fi

echo "run-conformu: ConformU version: $("$CONFORMU_BIN" --version)"

SERVER_LOG="$OUT_DIR/alpaca-server.log"
uv run python "$REPO_ROOT/scripts/conformu_server.py" --host "$HOST" --port "$PORT" \
    >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

cleanup() {
    if kill -0 "$SERVER_PID" 2>/dev/null; then
        kill -TERM "$SERVER_PID" 2>/dev/null || true
        wait "$SERVER_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT

# Probe, not sleep: poll the management endpoint until it answers or we give up.
ready=0
for _ in $(seq 1 $((READY_TIMEOUT_S * 10))); do
    if curl -sf "http://${HOST}:${PORT}/management/apiversions" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ! kill -0 "$SERVER_PID" 2>/dev/null; then
        echo "run-conformu: the Alpaca server exited before it became ready. Log:" >&2
        cat "$SERVER_LOG" >&2
        exit 2
    fi
    sleep 0.1
done
if [[ "$ready" -ne 1 ]]; then
    echo "run-conformu: server did not answer /management/apiversions within ${READY_TIMEOUT_S}s. Log:" >&2
    cat "$SERVER_LOG" >&2
    exit 2
fi
echo "run-conformu: Alpaca server ready on ${HOST}:${PORT}"

fail=0
for device in "${DEVICES[@]}"; do
    echo "run-conformu: checking ${device}/0 ..."
    results_file="$OUT_DIR/${device}-results.json"
    log_file="$OUT_DIR/${device}.log"
    set +e
    "$CONFORMU_BIN" conformance "http://${HOST}:${PORT}/api/v1/${device}/0" \
        -r "$results_file" -n "$log_file" -p "$OUT_DIR" \
        >"$OUT_DIR/${device}.stdout" 2>&1
    set -e
    if [[ ! -f "$results_file" ]]; then
        echo "run-conformu: ${device}: ConformU produced no results file — treating as a hard failure." >&2
        tail -n 60 "$OUT_DIR/${device}.stdout" >&2 || true
        fail=1
        continue
    fi
    if ! device_fail=$(uv run python "$REPO_ROOT/scripts/summarize_conformu.py" "$device" "$results_file"); then
        fail=1
    fi
    echo "$device_fail"
done

echo
if [[ "$fail" -ne 0 ]]; then
    echo "run-conformu: FAIL — at least one device reported a ConformU ErrorCount > 0. See $OUT_DIR" >&2
    exit 1
fi
echo "run-conformu: PASS — zero ConformU errors across ${DEVICES[*]}. Issues (if any) are printed above; see docs/CONFORMANCE.md for which are expected. Reports in $OUT_DIR"
