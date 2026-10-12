#!/usr/bin/env bash
# Build the ssc-core one-dir PyInstaller artifact.
#
# What this wrapper does that a bare ``pyinstaller packaging/ssc-core.spec``
# would not:
#   1. Fails loudly, before spending build time, if the ephemeris kernel or
#      the migrations directory are missing — a build that silently omits
#      either is a build that fails on a user's machine, not in CI.
#   2. Stamps the version from the git tag (architecture policy "Version honesty") into
#      ``dist/ssc-core/VERSION``, read by ``scripts/measure-coldstart.sh`` and
#      intended for the app itself to report once wired up (OPEN item below).
#   3. Never touches the network. Everything it needs must already be on disk.
set -euo pipefail

if [[ $# -gt 1 || ( $# -eq 1 && $1 != "--private-appliance" ) ]]; then
    echo "usage: scripts/build-package.sh [--private-appliance]" >&2
    exit 1
fi

PRIVATE_APPLIANCE=0
if [[ ${1:-} == "--private-appliance" ]]; then
    PRIVATE_APPLIANCE=1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

KERNEL="data/ephemeris/de440s.bsp"
MIGRATIONS="ssc/store/migrations"
WHAM_PROPERTIES="data/hips/wham/properties"
WHAM_TILE="data/hips/wham/Norder0/Dir0/Npix0.png"

if [[ ! -f "$KERNEL" ]]; then
    echo "build-package: missing $KERNEL — fetch it first (see ssc/astro/events.py KERNEL_URL)." >&2
    echo "build-package: the kernel is vendored/cached, never fetched at build or run time." >&2
    exit 1
fi

if [[ -z "$(find "$MIGRATIONS" -maxdepth 1 -name '*.sql' -print -quit 2>/dev/null)" ]]; then
    echo "build-package: no SQL files found under $MIGRATIONS — refusing to build." >&2
    exit 1
fi

if [[ ! -f "$WHAM_PROPERTIES" || ! -f "$WHAM_TILE" ]]; then
    echo "build-package: incomplete bundled WHAM H-alpha pack — refusing to build." >&2
    exit 1
fi

if [[ $PRIVATE_APPLIANCE -eq 1 ]]; then
    # The owner's private Pi may retain explicitly labelled POC survey sources.
    # This still checks every dependency licence, but is not redistributable.
    uv run python scripts/check-licenses.py
    echo "build-package: PRIVATE APPLIANCE artifact — not for redistribution"
else
    uv run python scripts/check-licenses.py --commercial
fi

if [[ -n ${SSC_BUILD_COMMIT:-} ]]; then
    [[ $SSC_BUILD_COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "build-package: invalid immutable commit identity" >&2; exit 1; }
    VERSION="0.0.0+g${SSC_BUILD_COMMIT:0:12}"
else
    VERSION="$(git describe --tags --always --dirty 2>/dev/null || echo 0.0.0+unknown)"
fi
echo "build-package: version $VERSION"

rm -rf build dist
uv run pyinstaller --noconfirm --clean packaging/ssc-core.spec

if [[ ! -f dist/ssc-core/_internal/data/hips/wham/properties || \
      ! -f dist/ssc-core/_internal/data/hips/wham/Norder0/Dir0/Npix0.png ]]; then
    echo "build-package: PyInstaller artifact omitted the bundled WHAM H-alpha pack." >&2
    exit 1
fi

echo "$VERSION" > dist/ssc-core/VERSION
COMMIT="${SSC_BUILD_COMMIT:-$(git rev-parse HEAD)}"
[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] || { echo "build-package: invalid full commit" >&2; exit 1; }
echo "$COMMIT" > dist/ssc-core/COMMIT
# Keep the legal material visible beside the executable instead of burying it
# in PyInstaller's private ``_internal`` directory.
cp -f LICENSE THIRD_PARTY_NOTICES.md dist/ssc-core/
cp -rf packaging/third-party-licenses dist/ssc-core/
cp -rf packaging/third-party-sources dist/ssc-core/
echo "build-package: artifact at dist/ssc-core/ssc-core (version $VERSION)"
