#!/usr/bin/env bash
# Install ConformU (ASCOM Initiative's cross-platform conformance checker) as
# a pinned, self-contained Linux binary under ~/.local/lib, symlinked from
# ~/.local/bin so `conformu` resolves for scripts/run-conformu.sh and for CI.
#
# ConformU ships no package-manager distribution; this is the vendor's own
# release artifact, pinned by exact version+URL rather than "latest" so a
# CI run next month tests against the same checker as today.
set -euo pipefail

CONFORMU_VERSION="4.5.0"
CONFORMU_URL="https://github.com/ASCOMInitiative/ConformU/releases/download/v${CONFORMU_VERSION}/conformu.linux-x64.tar.xz"
INSTALL_DIR="${CONFORMU_INSTALL_DIR:-$HOME/.local/lib/conformu-${CONFORMU_VERSION}}"
BIN_DIR="${CONFORMU_BIN_DIR:-$HOME/.local/bin}"

if command -v conformu >/dev/null 2>&1 && conformu --version 2>&1 | grep -q "$CONFORMU_VERSION"; then
    echo "install-conformu: $(conformu --version) already installed, nothing to do"
    exit 0
fi

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "install-conformu: downloading ConformU ${CONFORMU_VERSION} ..."
curl -fL -o "$tmp/conformu.tar.xz" "$CONFORMU_URL"

rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
tar xf "$tmp/conformu.tar.xz" -C "$INSTALL_DIR"

mkdir -p "$BIN_DIR"
ln -sf "$INSTALL_DIR/conformu" "$BIN_DIR/conformu"

echo "install-conformu: installed to $INSTALL_DIR, linked at $BIN_DIR/conformu"
"$BIN_DIR/conformu" --version
