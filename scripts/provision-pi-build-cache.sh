#!/usr/bin/env bash
# Install build-only tools from pinned, authenticated Debian package inputs.
set -euo pipefail
root=${SSC_APPLIANCE_ROOT:-/mnt/photon/Photon}
[[ $(uname -m) == aarch64 ]] || { echo 'Native ARM64 host required.' >&2; exit 1; }
[[ -d "$root/tools" && -d "$root/shared/cache" ]] || { echo 'Appliance tools/cache directories missing.' >&2; exit 1; }
[[ $(findmnt -n -o FSTYPE -T "$root") == ext4 ]] || { echo 'Appliance SSD must be mounted as ext4.' >&2; exit 1; }
prefix="$root/tools/build-cache-4.11.2"
if [[ ! -d $prefix ]]; then
  stage=$(mktemp -d "$root/tools/.build-cache.XXXXXX")
  trap 'rm -rf -- "$stage"' EXIT
  mkdir -p "$stage/packages" "$stage/extracted" "$stage/bin" "$stage/libexec" "$stage/lib"
  (
    cd "$stage/packages"
    apt-get download ccache=4.11.2-2 libhiredis1.1.0=1.2.0-6+b3
    printf '%s\n' \
      '668adcf17218750ebebe6e6ce071a660628f5a6a60fcf86900bec9424f17a8a8  ccache_4.11.2-2_arm64.deb' \
      'e9ca6df21fb5e4234c8bbf994ce72ad41cc25d265b2e09dbf478dc5f0d20ce71  libhiredis1.1.0_1.2.0-6+b3_arm64.deb' > SHA256SUMS
    sha256sum -c SHA256SUMS
  )
  for package in "$stage"/packages/*.deb; do dpkg-deb -x "$package" "$stage/extracted"; done
  cp -f "$stage/extracted/usr/bin/ccache" "$stage/libexec/ccache"
  cp -a "$stage"/extracted/usr/lib/aarch64-linux-gnu/libhiredis.so.* "$stage/lib/"
  cat > "$stage/bin/ccache" <<'WRAPPER'
#!/bin/sh
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export LD_LIBRARY_PATH="$here/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
exec "$here/libexec/ccache" "$@"
WRAPPER
  chmod 755 "$stage/bin/ccache"
  (cd "$stage"; sha256sum bin/ccache libexec/ccache lib/libhiredis.so.1.1.0 > TOOL-SHA256SUMS)
  "$stage/bin/ccache" --version
  mv -- "$stage" "$prefix"
  trap - EXIT
fi
(cd "$prefix"; sha256sum -c TOOL-SHA256SUMS)
"$prefix/bin/ccache" --version
link="$root/tools/.build-cache-link.$$"
ln -s "$prefix" "$link"
mv -Tf -- "$link" "$root/tools/build-cache"
mkdir -p "$root/shared/cache/nuitka/ccache"
echo "Build cache ready: $root/shared/cache/nuitka (no service or device changes)"
