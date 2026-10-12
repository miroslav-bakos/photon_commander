#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
# Private, unsigned package candidate: no publishing, service or device actions.
set -euo pipefail
cd "$(dirname "$0")/.."

[[ $(uname -m) == aarch64 && $(dpkg --print-architecture) == arm64 ]] || { echo 'Native ARM64 is required.' >&2; exit 1; }
source /etc/os-release
[[ $ID == debian && $VERSION_ID == 12 ]] || { echo 'Build inside Debian 12/bookworm.' >&2; exit 1; }
[[ ${SSC_REPOSITORY_PRIVATE:-} == true ]] || { echo 'This survey-containing candidate requires a private repository.' >&2; exit 1; }
[[ $(getconf GNU_LIBC_VERSION) == 'glibc 2.36' ]] || { echo 'Unexpected Debian 12 glibc baseline.' >&2; exit 1; }
for tool in git uv node npm gcc readelf dpkg-deb; do command -v "$tool" >/dev/null; done
for required in scripts/release_manual.py scripts/build_nuitka.py scripts/build_debian_package.py packaging/linux/environment packaging/linux/photon-commander.service packaging/licenses-allowlist.toml; do [[ -f $required ]] || { echo "Missing packaging prerequisite: $required" >&2; exit 1; }; done
node -e 'if (Number(process.versions.node.split(".")[0]) < 22) process.exit(1)'

release_sha=$(git rev-parse HEAD)
[[ $release_sha =~ ^[a-f0-9]{40}$ && $release_sha == "$(git rev-parse 'refs/tags/v0.0.1^{commit}')" ]] || { echo 'HEAD must be the exact v0.0.1 tag commit.' >&2; exit 1; }
[[ -z $(git status --porcelain=v1 --untracked-files=all) ]] || { echo 'Release checkout must be clean.' >&2; exit 1; }
export SSC_BUILD_COMMIT="$release_sha"
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct "$release_sha")"
export UV_FROZEN=1
export UV_PYTHON=3.12
export UV_PYTHON_PREFERENCE=only-managed
uv python install 3.12
uv sync --dev --frozen
uv run python -c 'import sys, numpy, nuitka, skyfield; assert sys.version_info[:2] == (3, 12)'

# Read pinned public asset metadata without importing the application or loading
# ephemerides. Both downloads are checksum-verified before the build can start.
uv run python - <<'PY'
import ast
import hashlib
import os
from pathlib import Path
import urllib.request


def constants(filename, names):
    tree = ast.parse(Path(filename).read_text())
    return {
        node.target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id in names
    }


def acquire(url, target, expected_sha, expected_size=None):
    assert url.startswith('https://')
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_name(target.name + '.download')
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'Photon Commander private CI asset build'}), timeout=60) as source, pending.open('wb') as output:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if expected_size is not None and size > expected_size:
                    raise RuntimeError('Asset exceeds the pinned size')
                digest.update(chunk)
                output.write(chunk)
        if digest.hexdigest() != expected_sha or (expected_size is not None and size != expected_size):
            raise RuntimeError('Asset does not match the pinned checksum/size')
        os.replace(pending, target)
    finally:
        pending.unlink(missing_ok=True)


kernel = constants('ssc/astro/events.py', {'KERNEL_FILENAME', 'KERNEL_URL', 'KERNEL_SIZE_BYTES', 'KERNEL_SHA256'})
assert kernel['KERNEL_FILENAME'] == 'de440s.bsp'
acquire(kernel['KERNEL_URL'], Path('data/ephemeris') / kernel['KERNEL_FILENAME'], kernel['KERNEL_SHA256'], kernel['KERNEL_SIZE_BYTES'])
wham = constants('scripts/build-wham-hips.py', {'SOURCE_URL', 'SOURCE_SHA256'})
acquire(wham['SOURCE_URL'], Path('data/cache/wham-ss-dr1-161116-integrated.fits'), wham['SOURCE_SHA256'])
PY
uv run python scripts/build-wham-hips.py --source data/cache/wham-ss-dr1-161116-integrated.fits
uv run python scripts/check-licenses.py --commercial
# This existing entry point builds the frontend and Nuitka payload, checks its
# ARM64 ELF identity and packages it, so do not compile the same payload twice.
uv run python scripts/build_debian_package.py "$release_sha" --unsigned --output-dir dist/debian

# Record and enforce the actual bundled ELF glibc requirements. This establishes
# a build baseline, not a claim of Raspberry Pi hardware/operational validation.
uv run python - <<'PY'
import json
import os
from pathlib import Path
import re
import subprocess

requirements = set()
elf_count = 0
for path in Path('dist/ssc-core').rglob('*'):
    if not path.is_file():
        continue
    with path.open('rb') as source:
        if source.read(4) != b'\x7fELF':
            continue
    elf_count += 1
    header = subprocess.check_output(['readelf', '-h', str(path)], text=True)
    if 'AArch64' not in header:
        raise RuntimeError('Bundled ELF is not ARM64')
    symbols = subprocess.check_output(['readelf', '--version-info', str(path)], text=True)
    requirements.update(re.findall(r'\bGLIBC_(\d+(?:\.\d+)+)\b', symbols))
versions = sorted(requirements, key=lambda value: tuple(map(int, value.split('.'))))
if any(tuple(map(int, value.split('.'))) > (2, 36) for value in versions):
    raise RuntimeError('Bundled ELF requires newer glibc than Debian 12')
if not elf_count:
    raise RuntimeError('No compiled ELF payload found')
evidence = {
    'tag': 'v0.0.1', 'commit': os.environ['SSC_BUILD_COMMIT'],
    'distribution': 'Debian 12/bookworm', 'architecture': 'arm64',
    'build_glibc': subprocess.check_output(['getconf', 'GNU_LIBC_VERSION'], text=True).strip(),
    'bundled_elf_count': elf_count, 'required_glibc_versions': versions,
    'package_signed': False, 'hardware_validation_performed': False,
    'publication_performed': False,
}
Path('dist/debian/ci-build-evidence.json').write_text(json.dumps(evidence, indent=2) + '\n')
print('ARM64 ELF glibc requirements verified; package remains an unsigned private candidate.')
PY
