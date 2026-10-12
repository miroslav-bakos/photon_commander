#!/usr/bin/env python3
"""Package an immutable native x86_64 release as a private AppImage."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

try:
    from scripts.build_linux_packages import (
        assert_payload_boundary,
        validate_binary,
        validate_release_identity,
    )
    from scripts.release_manual import write_installation_manual
except ModuleNotFoundError:
    from build_linux_packages import (
        assert_payload_boundary,
        validate_binary,
        validate_release_identity,
    )
    from release_manual import write_installation_manual

ROOT = Path(__file__).resolve().parents[1]
TOOL_SHA256 = '95cbe7cce9717fce90c484e34052ee7c7f1d7635b33c12525b4776826a7d29b6'
RUNTIME_SHA256 = '5b802620a72759cb29522bf70f7dced40f25dd27882d85956a1eb7ab6ca4e289'


def payload_digest(root: Path) -> str:
    """Digest every payload path, mode and byte; timestamps do not alter identity."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        info = path.lstat()
        kind = 'link' if path.is_symlink() else 'file' if path.is_file() else 'dir'
        record = [str(path.relative_to(root)), kind, info.st_mode & 0o7777,
                  info.st_size if kind == 'file' else 0]
        digest.update((json.dumps(record) + '\n').encode())
        if kind == 'link':
            digest.update((os.readlink(path) + '\n').encode())
        elif kind == 'file':
            with path.open('rb') as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


def verified_file(path: Path, expected: str) -> None:
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
        raise SystemExit(f'AppImage build tool checksum mismatch: {path}')


def assemble(appdir: Path, commit: str, epoch: int) -> None:
    appdir.mkdir(parents=True)
    shutil.copytree(ROOT / 'dist/ssc-core', appdir / 'app')
    shutil.copytree(ROOT / 'web/dist', appdir / 'web', ignore=shutil.ignore_patterns('product'))
    for name in ('AppRun', 'photon-commander.desktop'):
        shutil.copy2(ROOT / 'packaging/appimage' / name, appdir / name)
    shutil.copy2(ROOT / 'web/public/icons/icon-192.png', appdir / 'photon-commander.png')
    (appdir / '.DirIcon').symlink_to('photon-commander.png')
    write_installation_manual(appdir, 'appimage', commit, epoch)
    (appdir / 'build-manifest.json').write_text(json.dumps({
        'commit': commit, 'architecture': 'x86_64', 'format': 'AppImage',
        'released_at': epoch,
        'minimum_glibc': '2.35', 'runtime_sha256': RUNTIME_SHA256,
        'appimagetool_sha256': TOOL_SHA256, 'private_release': True,
    }, indent=2) + '\n')
    assert_payload_boundary(appdir)


def check_glibc(appdir: Path) -> None:
    """Reject accidentally compiling against the rolling host's newer libc."""
    import re
    for file in (appdir / 'app').rglob('*'):
        if not file.is_file():
            continue
        with file.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                continue
        symbols = subprocess.check_output(['readelf', '--version-info', str(file)], text=True)
        versions = {tuple(map(int, v.split('.'))) for v in re.findall(r'GLIBC_(\d+\.\d+(?:\.\d+)?)', symbols)}
        if versions and max(versions) > (2, 35):
            raise SystemExit(f'AppImage requires newer glibc than 2.35: {file}')


def build(commit: str, output: Path, tool: Path, runtime: Path) -> Path:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise SystemExit('Build on Linux x86_64 (Ubuntu 22.04 baseline)')
    for command in ('uv', 'npm', 'gcc', 'readelf', 'file', 'patchelf'):
        if shutil.which(command) is None:
            raise SystemExit(f'AppImage build requires {command} before compilation')
    _, _, _, epoch = validate_release_identity(commit)
    verified_file(tool, TOOL_SHA256)
    verified_file(runtime, RUNTIME_SHA256)
    env = {**os.environ, 'SSC_BUILD_COMMIT': commit, 'SSC_BUILD_RELEASED_AT': str(epoch),
           'SOURCE_DATE_EPOCH': str(epoch), 'ARCH': 'x86_64'}
    subprocess.run(['uv', 'sync', '--dev', '--frozen'], cwd=ROOT, env=env, check=True)
    subprocess.run(['uv', 'run', 'python', 'scripts/check-licenses.py', '--commercial'], cwd=ROOT, env=env, check=True)
    subprocess.run(['npm', 'ci'], cwd=ROOT / 'web', env=env, check=True)
    subprocess.run(['npm', 'run', 'build'], cwd=ROOT / 'web', env=env, check=True)
    subprocess.run(['uv', 'run', 'python', 'scripts/build_nuitka.py', '--output', str(ROOT / 'dist/ssc-core')], cwd=ROOT, env=env, check=True)
    validate_binary(ROOT / 'dist/ssc-core/ssc-core', 'Advanced Micro Devices X86-64')
    appdir = ROOT / 'build/Photon.AppDir'
    if appdir.exists():
        shutil.rmtree(appdir)
    assemble(appdir, commit, epoch)
    check_glibc(appdir)
    return package(appdir, commit, epoch, output, tool, runtime, env)


def package(appdir: Path, commit: str, epoch: int, output: Path, tool: Path, runtime: Path, env: dict[str, str]) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    artifact = output / f'Photon-Commander-0.0.0+g{commit[:12]}-x86_64.AppImage'
    subprocess.run([str(tool), '--appimage-extract-and-run', '--no-appstream', '--runtime-file', str(runtime), str(appdir), str(artifact)], env=env, check=True)
    artifact.chmod(0o755)
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix('.AppImage.sha256').write_text(f'{digest}  {artifact.name}\n')
    for name in ('build-manifest.json', *(p.name for p in appdir.glob('INSTALLATION*.txt'))):
        shutil.copy2(appdir / name, output / name)
    (output / 'payload.sha256').write_text(payload_digest(appdir) + '\n')
    return artifact


def repackage(packaging_commit: str, source: Path, output: Path, tool: Path, runtime: Path, trusted_payload_sha256: str) -> Path:
    """Reuse a validated native payload only for packaging/legal-only changes."""
    validate_release_identity(packaging_commit)
    verified_file(tool, TOOL_SHA256)
    verified_file(runtime, RUNTIME_SHA256)
    assert_payload_boundary(source)
    if payload_digest(source) != trusted_payload_sha256:
        raise SystemExit('Compiled payload digest does not match validated build evidence')
    manifest = json.loads((source / 'build-manifest.json').read_text())
    commit = manifest['commit']
    import re
    if re.fullmatch(r'[0-9a-f]{40}', commit) is None:
        raise SystemExit('Invalid compiled payload commit')
    changes = subprocess.check_output(['git', 'diff', '--name-only', commit, packaging_commit], cwd=ROOT, text=True).splitlines()
    allowed = ('packaging/third-party-licenses/', 'packaging/third-party-sources/', 'docs/')
    allowed_files = {'THIRD_PARTY_NOTICES.md', 'scripts/build_appimage.py', 'tests/release/test_appimage.py'}
    if any(name not in allowed_files and not name.startswith(allowed) for name in changes):
        raise SystemExit('Native payload reuse refused: application or launcher changed')
    epoch = int(subprocess.check_output(['git', 'show', '-s', '--format=%ct', commit], cwd=ROOT, text=True))
    if manifest['released_at'] != epoch or 'expires_at' in manifest:
        raise SystemExit('Compiled payload release identity mismatch')
    if (source / 'app/VERSION').read_text().strip() != f'0.0.0+g{commit[:12]}':
        raise SystemExit('Compiled payload version mismatch')
    target = ROOT / 'build/Photon-repacked.AppDir'
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, symlinks=True)
    shutil.copy2(ROOT / 'THIRD_PARTY_NOTICES.md', target / 'app/THIRD_PARTY_NOTICES.md')
    for directory in ('third-party-licenses', 'third-party-sources'):
        shutil.copytree(ROOT / 'packaging' / directory, target / 'app' / directory, dirs_exist_ok=True)
    manifest.update(packaging_commit=packaging_commit, runtime_sha256=RUNTIME_SHA256,
                    appimagetool_sha256=TOOL_SHA256,
                    backend_sha256=hashlib.sha256((target / 'app/ssc-core').read_bytes()).hexdigest())
    (target / 'build-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    assert_payload_boundary(target)
    validate_binary(target / 'app/ssc-core', 'Advanced Micro Devices X86-64')
    check_glibc(target)
    env = {**os.environ, 'SOURCE_DATE_EPOCH': str(epoch), 'ARCH': 'x86_64'}
    return package(target, commit, epoch, output, tool, runtime, env)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--appimagetool', type=Path, required=True)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--payload-sha256', help='Trusted digest recorded for the previously validated AppDir')
    parser.add_argument('--repackage-appdir', type=Path, help='Reuse existing AppDir only when Git proves packaging/legal-only changes')
    parser.add_argument('--unsigned', action='store_true')
    args = parser.parse_args()
    if not args.unsigned:
        parser.error('Explicit --unsigned is required for private unsigned artifacts')
    if args.repackage_appdir is not None:
        if args.payload_sha256 is None:
            parser.error('--repackage-appdir requires --payload-sha256 from validated build evidence')
        print(repackage(args.commit, args.repackage_appdir.resolve(), args.output_dir.resolve(), args.appimagetool.resolve(), args.runtime.resolve(), args.payload_sha256))
    else:
        print(build(args.commit, args.output_dir.resolve(), args.appimagetool.resolve(), args.runtime.resolve()))
