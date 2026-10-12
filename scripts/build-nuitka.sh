#!/usr/bin/env bash
# Build the native standalone Photon Commander backend with Nuitka.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

exec uv run python scripts/build_nuitka.py "$@"
