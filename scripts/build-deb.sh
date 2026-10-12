#!/usr/bin/env bash
# Build a deterministic compiled ARM64 Debian package from one exact commit.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
exec uv run python "$ROOT/scripts/build_debian_package.py" "$@"
