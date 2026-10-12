#!/usr/bin/env bash
# Poll in one process: repeated curl/sleep forks materially perturb Pi startup.
# Thresholds remain unchanged; measure spawn to an actual HTTP response.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
exec python3 scripts/measure_coldstart.py
