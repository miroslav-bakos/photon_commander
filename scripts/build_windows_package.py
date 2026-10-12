"""Build an immutable Windows x64 installer with a native backend."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
from pathlib import Path

from build_nuitka import assert_compiled_boundary
from release_manual import write_installation_manual

ROOT = Path(__file__).resolve().parents[1]


def validate_windows_binary(binary: Path) -> None:
    with binary.open('rb') as stream:
        header = stream.read(64)
        if len(header) != 64 or header[:2] != b'MZ':
            raise SystemExit('Backend is not a Windows executable')
        stream.seek(struct.unpack_from('<I', header, 60)[0])
        pe = stream.read(6)
        if len(pe) != 6 or pe[:4] != b'PE\0\0' or struct.unpack_from('<H', pe, 4)[0] != 0x8664:
            raise SystemExit('Backend is not a Windows x64 executable')


def run(*command: str, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(command, cwd=ROOT, env=env, text=True).strip()


def release_identity(commit: str) -> int:
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise SystemExit('Full immutable commit required')
    if run('git', 'rev-parse', 'HEAD') != commit:
        raise SystemExit('Checkout does not match requested commit')
    if run('git', 'status', '--porcelain', '--untracked-files=all'):
        raise SystemExit('Build requires a clean checkout')
    return int(run('git', 'show', '-s', '--format=%ct', commit))


def assemble(output: Path, commit: str, epoch: int) -> None:
    shutil.copytree(ROOT / 'dist/ssc-core', output / 'app')
    shutil.copytree(ROOT / 'web/dist', output / 'web', ignore=shutil.ignore_patterns('product'))
    write_installation_manual(output, 'windows', commit, epoch)
    (output / 'build-manifest.json').write_text(json.dumps({'commit': commit, 'architecture': 'win64', 'released_at': epoch}, indent=2) + '\n')
    assert_compiled_boundary(output)


def build(commit: str, iscc: str, output: Path) -> Path:
    if os.name != 'nt' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise SystemExit('Build on Windows x64; cross-compilation is unsupported')
    epoch = release_identity(commit)
    env = {**os.environ, 'SSC_BUILD_COMMIT': commit, 'SSC_BUILD_RELEASED_AT': str(epoch), 'SOURCE_DATE_EPOCH': str(epoch)}
    run('uv', 'sync', '--dev', '--frozen', env=env)
    run('uv', 'run', 'python', 'scripts/check-licenses.py', '--commercial', env=env)
    npm = shutil.which('npm')
    if npm is None:
        raise SystemExit('Node.js/npm unavailable')
    subprocess.run([npm, 'ci'], cwd=ROOT / 'web', env=env, check=True)
    subprocess.run([npm, 'run', 'build'], cwd=ROOT / 'web', env=env, check=True)
    run('uv', 'run', 'python', 'scripts/build_nuitka.py', '--output', str(ROOT / 'dist/ssc-core'), env=env)
    validate_windows_binary(ROOT / 'dist/ssc-core/ssc-core.exe')
    stage = ROOT / 'build/windows-payload'
    if stage.exists():
        shutil.rmtree(stage)
    assemble(stage, commit, epoch)
    output.mkdir(parents=True, exist_ok=True)
    version = f'0.0.0+g{commit[:12]}'
    run(iscc, f'/DReleaseVersion={version}', f'/DPayloadDir={stage}', f'/DOutputDir={output}', str(ROOT / 'packaging/windows/photon-commander.iss'))
    installer = output / f'Photon-Commander-{version}-win64.exe'
    if not installer.is_file():
        raise SystemExit('Installer was not produced')
    digest = hashlib.sha256(installer.read_bytes()).hexdigest()
    installer.with_suffix('.exe.sha256').write_text(f'{digest}  {installer.name}\n')
    for manual in stage.glob('INSTALLATION*.txt'):
        shutil.copy2(manual, output / manual.name)
    shutil.copy2(stage / 'build-manifest.json', output / 'build-manifest.json')
    return installer


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('--iscc', default=r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'dist/windows')
    args = parser.parse_args()
    print(build(args.commit, args.iscc, args.output_dir.resolve()))
