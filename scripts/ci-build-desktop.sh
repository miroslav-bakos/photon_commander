#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Miroslav Bakoš
# Private unsigned candidates, never publication or device commands.
set -euo pipefail
cd "$(dirname "$0")/.."
[[ ${SSC_REPOSITORY_PRIVATE:-} == true ]] || { echo 'Candidate builds currently require a private repository.' >&2; exit 1; }
[[ ${SSC_BUILD_COMMIT:-} =~ ^[a-f0-9]{40}$ && $SSC_BUILD_COMMIT == "$(git rev-parse HEAD)" ]] || { echo 'Exact immutable checkout required.' >&2; exit 1; }
[[ -z $(git status --porcelain=v1 --untracked-files=all) ]] || { echo 'Clean source checkout required.' >&2; exit 1; }
case ${SSC_DESKTOP_TARGET:-}:$(uname -s):$(uname -m) in
 linux-x86_64:Linux:x86_64) source /etc/os-release; [[ $ID == ubuntu && $VERSION_ID == 22.04 ]] ;;
 macos-x86_64:Darwin:x86_64|macos-arm64:Darwin:arm64) command -v clang >/dev/null ;;
 *) echo 'Runner does not match the requested native target.' >&2; exit 1 ;;
esac
export SOURCE_DATE_EPOCH="$(git show -s --format=%ct "$SSC_BUILD_COMMIT")"
export UV_FROZEN=1 UV_PYTHON=3.12 UV_PYTHON_PREFERENCE=only-managed
uv sync --dev --frozen
# Managed Python on macOS may not discover the runner's system CA bundle.
# Use the already pinned certifi roots while retaining certificate validation.
SSL_CERT_FILE="$(uv run python -c 'import certifi; print(certifi.where())')"
export SSL_CERT_FILE
uv run python - <<'PY'
import ast, hashlib, os, urllib.request
from pathlib import Path

def constants(path, names):
 return {n.target.id:ast.literal_eval(n.value) for n in ast.parse(Path(path).read_text()).body if isinstance(n,ast.AnnAssign) and isinstance(n.target,ast.Name) and n.target.id in names}
def acquire(url, target, digest, size=None):
 assert url.startswith('https://')
 target.parent.mkdir(parents=True,exist_ok=True)
 pending=target.with_name(target.name+'.download'); h=hashlib.sha256(); total=0
 try:
  with urllib.request.urlopen(url,timeout=60) as response,pending.open('wb') as output:
   while chunk:=response.read(1024*1024):
    total+=len(chunk)
    if size is not None and total>size:raise RuntimeError('Asset exceeds pinned size')
    h.update(chunk);output.write(chunk)
  if h.hexdigest()!=digest or (size is not None and total!=size):raise RuntimeError('Asset checksum/size mismatch')
  os.replace(pending,target)
 finally:pending.unlink(missing_ok=True)
k=constants('ssc/astro/events.py',{'KERNEL_FILENAME','KERNEL_URL','KERNEL_SHA256','KERNEL_SIZE_BYTES'})
assert k['KERNEL_FILENAME']=='de440s.bsp'
acquire(k['KERNEL_URL'],Path('data/ephemeris/de440s.bsp'),k['KERNEL_SHA256'],k['KERNEL_SIZE_BYTES'])
PY
uv run python -c 'from pathlib import Path; assert not any(p.is_file() for p in Path("data/hips").rglob("*")), "Survey pixels must not be present"'
uv run python scripts/check-licenses.py --commercial
mkdir -p dist/desktop
# Exact corresponding application source travels with private candidates.
# An explicit tracked-path list excludes Git metadata and README-only imagery.
uv run python - <<'PY'
import hashlib,os,subprocess
from pathlib import Path
commit=os.environ['SSC_BUILD_COMMIT']
source=Path('dist/desktop')/f'Photon-Commander-source-{commit}.tar.gz'
paths=['ssc','web','scripts','packaging','data','docs','pyproject.toml','uv.lock','LICENSE','THIRD_PARTY_NOTICES.md']
subprocess.run(['git','archive','--format=tar.gz','-o',str(source),commit,*paths],check=True)
source.with_name(source.name+'.sha256').write_text(hashlib.sha256(source.read_bytes()).hexdigest()+'  '+source.name+'\n')
PY
if [[ $SSC_DESKTOP_TARGET == linux-x86_64 ]]; then
 uv run python - <<'PY'
import hashlib,json,os,tarfile,urllib.request
from pathlib import Path
from scripts.build_appimage import TOOL_SHA256
archive=Path('packaging/third-party-sources/appimage-runtime/source-built-runtime-materials.tar.gz')
manifest=json.loads(archive.with_name('SOURCE-MANIFEST.json').read_text())[0]
assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['sha256']
root=Path('build/appimage-runtime-input');root.mkdir(parents=True,exist_ok=True)
with tarfile.open(archive) as source:source.extractall(root,filter='data')
for line in (root/'compiler-apk-checksums.sha256').read_text().splitlines():
 expected,name=line.split(maxsplit=1);target=root/name;target.parent.mkdir(parents=True,exist_ok=True)
 for repository in ('main','community'):
  try:
   url=f'https://dl-cdn.alpinelinux.org/alpine/v3.21/{repository}/x86_64/{Path(name).name}'
   with urllib.request.urlopen(url,timeout=60) as response:data=response.read()
   if hashlib.sha256(data).hexdigest()!=expected:raise RuntimeError('Compiler APK checksum mismatch')
   target.write_bytes(data);break
  except urllib.error.HTTPError:continue
 else:raise RuntimeError('Pinned official compiler APK is unavailable: '+Path(name).name)
url=os.environ.get('SSC_APPIMAGETOOL_URL','')
if not url.startswith('https://github.com/AppImage/appimagetool/releases/download/'):raise RuntimeError('Official AppImage tool URL required')
with urllib.request.urlopen(url,timeout=60) as response:data=response.read()
if hashlib.sha256(data).hexdigest()!=TOOL_SHA256:raise RuntimeError('Tool differs from existing pin; select its matching official release URL')
tool=Path('build/appimagetool-x86_64.AppImage');tool.write_bytes(data);tool.chmod(0o755)
PY
 bash build/appimage-runtime-input/setup-offline.sh > build/appimage-runtime-input/ci-runtime-build.log 2>&1
 uv run python scripts/build_appimage.py "$SSC_BUILD_COMMIT" --unsigned --output-dir dist/desktop --appimagetool build/appimagetool-x86_64.AppImage --runtime build/appimage-runtime-input/output/runtime-x86_64
else
 npm --prefix web ci
 npm --prefix web run build
 uv run python scripts/build_nuitka.py --output dist/ssc-core
 uv run python - <<'PY'
import hashlib,json,os,shutil,subprocess,tarfile
from pathlib import Path
from markdown_it import MarkdownIt
from scripts.build_nuitka import assert_compiled_boundary
from scripts.build_release_identity import release_version
root=Path.cwd();target=os.environ['SSC_DESKTOP_TARGET'];commit=os.environ['SSC_BUILD_COMMIT'];arch=target.removeprefix('macos-')
stage=root/'build'/f'Photon-Commander-{target}'
if stage.exists():shutil.rmtree(stage)
stage.mkdir(parents=True)
shutil.copytree(root/'dist/ssc-core',stage/'app')
shutil.copytree(root/'web/dist',stage/'web',ignore=shutil.ignore_patterns('product'))
assert_compiled_boundary(stage/'app')
# README-only DSS illustration, caches and owner configuration are never copied.
shutil.copytree(root/'docs',stage/'docs')
(stage/'application-source').mkdir()
shutil.copy2(root/'dist/desktop'/f'Photon-Commander-source-{commit}.tar.gz',stage/'application-source')
renderer=MarkdownIt('commonmark',{'html':False})
nav='<nav>'+' '.join(f'<a href="getting-started.{lang}.html">{lang}</a>' for lang in ('en','sk','cs'))+'</nav>'
for lang in ('en','sk','cs'):
 manual=(root/'docs/installation'/f'macos.{lang}.txt').read_text()
 (stage/f'INSTALLATION.{lang}.txt').write_text(f'Exact build commit: {commit}\nPackage: {target}\nUnsigned; notarization and hardware validation pending.\n\n'+manual)
 md=stage/'docs'/f'getting-started.{lang}.md'
 md.with_suffix('.html').write_text(f'<!doctype html><html lang="{lang}"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Photon Commander</title><style>body{{font:17px/1.6 system-ui;max-width:1000px;margin:auto;padding:24px}}img{{max-width:100%}}code{{overflow-wrap:anywhere}}</style>{nav}{renderer.render(md.read_text())}</html>')
(stage/'docs/index.html').write_text('<!doctype html><meta charset="utf-8"><title>Photon Commander guides</title>'+nav)
shutil.copy2(stage/'INSTALLATION.en.txt',stage/'INSTALLATION.txt')
launcher=stage/'Photon Commander.command'
launcher.write_text('''#!/bin/bash
set -euo pipefail
APPDIR=$(cd -- "$(dirname -- "$0")" && pwd)
export SSC_STATIC_DIR="$APPDIR/web"
export SSC_DATA_DIR="${SSC_DATA_DIR:-$HOME/Library/Application Support/Photon Commander}"
export SSC_HOST=127.0.0.1
export SSC_PORT="${SSC_PORT:-7777}"
mkdir -p "$SSC_DATA_DIR"
exec "$APPDIR/app/ssc-core" --desktop "$@"
''');launcher.chmod(0o755)
actual=subprocess.check_output(['lipo','-archs',str(stage/'app/ssc-core')],text=True).split()
if actual!=[arch]:raise RuntimeError('Backend Mach-O architecture mismatch')
for hips in stage.rglob('hips'):
 if hips.is_dir() and any(p.is_file() for p in hips.rglob('*')):raise RuntimeError('Bundled survey pixels found')
version=release_version(root,commit)
manifest={'commit':commit,'version':version,'target':target,'build_os':subprocess.check_output(['sw_vers','-productVersion'],text=True).strip(),'bundled_survey_pixels':False,'signed':False,'notarized':False,'hardware_validation_performed':False,'publication_performed':False}
(stage/'build-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
out=root/'dist/desktop'/f'Photon-Commander-{version}-{target}.tar.gz'
with tarfile.open(out,'w:gz') as archive:archive.add(stage,arcname=stage.name)
out.with_name(out.name+'.sha256').write_text(hashlib.sha256(out.read_bytes()).hexdigest()+'  '+out.name+'\n')
shutil.copy2(stage/'build-manifest.json',root/'dist/desktop/build-manifest.json')
for manual in stage.glob('INSTALLATION*.txt'):shutil.copy2(manual,root/'dist/desktop'/manual.name)
PY
fi
uv run python - <<'PY'
import json,os,platform
from pathlib import Path
from scripts.build_release_identity import release_version
for directory in Path('dist/ssc-core').rglob('hips'):
 if any(p.is_file() for p in directory.rglob('*')):raise RuntimeError('Bundled survey pixels found')
commit=os.environ['SSC_BUILD_COMMIT']
evidence={'commit':commit,'version':release_version(Path.cwd(),commit),'target':os.environ['SSC_DESKTOP_TARGET'],'build_os':platform.platform(),'bundled_survey_pixels':False,'signed':False,'notarized':False,'hardware_validation_performed':False,'publication_performed':False}
Path('dist/desktop/ci-build-evidence.json').write_text(json.dumps(evidence,indent=2)+'\n')
PY
