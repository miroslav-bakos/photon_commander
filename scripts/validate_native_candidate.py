#!/usr/bin/env python3
"""Validate downloaded native candidates on isolated GitHub runners; never rebuild.

Only sanitized booleans and artifact identities are written as evidence. Backend
logs, cookies, pairing tokens and the temporary database are deliberately private.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
import platform
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from contextlib import suppress
from pathlib import Path

TARGETS = {
    'linux-x86_64': ('Linux', 'x86_64'),
    'macos-x86_64': ('Darwin', 'x86_64'),
    'macos-arm64': ('Darwin', 'arm64'),
    'rpi-arm64': ('Linux', 'aarch64'),
}


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def api(path):
    request = urllib.request.Request('https://api.github.com/' + path, headers={
        'Authorization': 'Bearer ' + os.environ['GH_TOKEN'],
        'Accept': 'application/vnd.github+json',
    })
    with urllib.request.urlopen(request, timeout=90) as response:
        return json.load(response)


def candidate_environment():
    """Allow only runtime essentials; runner/service credentials never propagate."""
    allowed = {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL',
               'GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT'}
    return {key: value for key, value in os.environ.items() if key in allowed}


def run_isolated(command, work, *, cwd=None, timeout=600):
    """The single fail-closed execution boundary for every candidate process."""
    if platform.system() == 'Linux':
        command = ['sudo', '--preserve-env=GITHUB_ACTIONS,RUNNER_ENVIRONMENT', 'unshare', '--net',
                   '--', 'sh', '-c', 'ip link set lo up && exec "$@"', 'sh',
                   'setpriv', '--reuid', str(os.getuid()), '--regid', str(os.getgid()),
                   '--init-groups', *command]
    else:
        require(platform.system() == 'Darwin', 'unsupported isolation platform')
        profile = work / 'network.sb'
        profile.write_text('(version 1) (allow default) (deny network*)\n'
                           '(allow network-bind (local ip "127.0.0.1:*"))\n'
                           '(allow network-inbound (local ip "127.0.0.1:*"))\n'
                           '(allow network-outbound (remote ip "127.0.0.1:*"))\n')
        command = ['/usr/bin/sandbox-exec', '-f', str(profile), *command]
    isolated = subprocess.Popen(command, env=candidate_environment(), cwd=cwd,
                                start_new_session=True, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    try:
        require(isolated.wait(timeout=timeout) == 0, 'isolated candidate failed')
    finally:
        # Candidate descendants share a dedicated process group, including on
        # timeout or failure before the harness can perform normal cleanup.
        with suppress(ProcessLookupError):
            os.killpg(isolated.pid, signal.SIGKILL)
        isolated.wait(timeout=10)


def extract_package(package, payload, target, work):
    if target == 'rpi-arm64':
        subprocess.run(['dpkg-deb', '-x', str(package), str(payload)], check=True,
                       env=candidate_environment(), stdout=subprocess.DEVNULL)
    elif target.startswith('linux'):
        package.chmod(0o700)
        run_isolated([str(package), '--appimage-extract'], work, cwd=payload, timeout=120)
    else:
        require(hasattr(tarfile, 'data_filter'), 'safe tar extraction unavailable')
        with tarfile.open(package) as bundle:
            bundle.extractall(payload, filter='data')


def prepare(args, work):
    require(re.fullmatch(r'[a-f0-9]{40}', args.commit), 'invalid source SHA')
    require(re.fullmatch(r'[0-9]+', args.run), 'invalid run ID')
    repo = os.environ['GITHUB_REPOSITORY']
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo), 'invalid repository')
    run = api(f'repos/{repo}/actions/runs/{args.run}')
    require(run['conclusion'] == 'success', 'origin run status')
    require(re.fullmatch(r'[a-f0-9]{40}', run['head_sha']), 'origin workflow SHA')
    expected_workflow = 'build-rpi5.yml' if args.target == 'rpi-arm64' else 'build-desktop.yml'
    require(run['path'] == '.github/workflows/' + expected_workflow, 'origin workflow')
    name = (f'photon-commander-{args.commit}-bookworm-arm64-unsigned' if args.target == 'rpi-arm64'
            else f'photon-{args.target}-{args.commit}-unsigned')
    artifacts = api(f'repos/{repo}/actions/runs/{args.run}/artifacts?per_page=100')['artifacts']
    matches = [a for a in artifacts if a['name'] == name and not a['expired']]
    require(len(matches) == 1, 'unique artifact missing')
    artifact = matches[0]
    # curl drops credentials on cross-host redirects; never use --location-trusted.
    archive = work / 'artifact.zip'
    subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                    '--max-time', '300', '-H', 'Authorization: Bearer ' + os.environ['GH_TOKEN'],
                    f'https://api.github.com/repos/{repo}/actions/artifacts/{artifact["id"]}/zip',
                    '-o', str(archive)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    artifact_hash = artifact.get('digest')
    require(artifact_hash and artifact_hash == 'sha256:' + digest(archive), 'artifact digest mismatch')
    downloads = work / 'downloads'
    with zipfile.ZipFile(archive) as bundle:
        for member in bundle.infolist():
            require(not Path(member.filename).is_absolute() and '..' not in Path(member.filename).parts,
                    'unsafe archive member')
        bundle.extractall(downloads)
    manifests = list(downloads.rglob('build-manifest.json'))
    for manifest in manifests:
        require(json.loads(manifest.read_text())['commit'] == args.commit, 'external manifest source mismatch')
    pattern = '*.deb' if args.target == 'rpi-arm64' else ('*.AppImage' if args.target.startswith('linux') else '*macos-*.tar.gz')
    packages = list(downloads.rglob(pattern))
    require(len(packages) == 1, 'unique native package missing')
    package = packages[0]
    expected = package.with_name(package.name + '.sha256').read_text().split()[0]
    require(re.fullmatch(r'[a-f0-9]{64}', expected) and digest(package) == expected, 'package hash mismatch')
    payload = work / 'payload'
    payload.mkdir()
    extract_package(package, payload, args.target, work)
    binaries = list(payload.rglob('ssc-core'))
    require(len(binaries) == 1 and binaries[0].is_file(), 'unique compiled backend missing')
    binary = binaries[0]
    embedded = list(payload.rglob('build-manifest.json'))
    require(embedded, 'embedded manifest missing')
    for manifest in embedded:
        require(json.loads(manifest.read_text())['commit'] == args.commit, 'embedded manifest source mismatch')
    require((binary.parent / 'COMMIT').read_text().strip() == args.commit, 'embedded COMMIT mismatch')
    version = (binary.parent / 'VERSION').read_text().strip()
    # This workflow validates untagged exact-SHA candidates, not tagged releases.
    require(version == f'0.0.0+g{args.commit[:12]}', 'embedded VERSION mismatch')
    if args.target.startswith('macos'):
        require(all(json.loads(p.read_text()).get('version') == version for p in embedded),
                'embedded manifest version mismatch')
    return binary, {'backend_version': version, 'source_sha': args.commit, 'workflow_sha': run['head_sha'], 'run_id': args.run, 'target': args.target,
                        'artifact_id': artifact['id'], 'artifact_sha256': artifact_hash,
                        'package_sha256': expected, 'backend_sha256': digest(binaries[0])}


def seed(binary, data):
    """Apply the exact packaged first migration; startup applies the remainder."""
    candidates = list(binary.parent.rglob('0001_initial.sql'))
    require(len(candidates) == 1, 'packaged initial migration missing')
    sql = candidates[0].read_text(encoding='utf-8')
    with sqlite3.connect(data / 'ssc.sqlite3') as db:
        db.executescript(sql)
        db.execute('CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, applied_at TEXT NOT NULL)')
        stamp = '2026-08-01T12:00:00Z'
        db.execute('INSERT INTO schema_migrations VALUES (?,?,?,?)',
                   (1, 'initial', hashlib.sha256(sql.encode()).hexdigest(), stamp))
        for key, value in [('device', {'auto_connect': False, 'host': None, 'ethernet_host': None,
                                      'last_healthy_host': None}),
                           ('streaming', {'broadcast_when_unparked': False})]:
            db.execute('INSERT INTO config VALUES (?,?,?)', (key, json.dumps(value), stamp))


class Client:
    def __init__(self, port):
        self.port = port
        self.base = f'http://127.0.0.1:{port}'
        self.cookies = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                               urllib.request.HTTPCookieProcessor(self.cookies))

    def request(self, path, body=None, method=None):
        req = urllib.request.Request(self.base + path,
            data=None if body is None else json.dumps(body).encode(), method=method,
            headers={'Content-Type': 'application/json', 'Origin': self.base})
        with self.http.open(req, timeout=120) as response:
            return json.load(response)

    def websocket(self):
        key = base64.b64encode(os.urandom(16)).decode()
        cookie = '; '.join(f'{c.name}={c.value}' for c in self.cookies)
        with socket.create_connection(('127.0.0.1', self.port), timeout=15) as sock:
            sock.sendall((f'GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n'
                          f'Origin: {self.base}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                          f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nCookie: {cookie}\r\n\r\n').encode())
            stream = sock.makefile('rb')
            require(b' 101 ' in stream.readline(), 'websocket upgrade')
            headers = {}
            while True:
                line = stream.readline()
                if line == b'\r\n':
                    break
                require(line and len(line) < 8192, 'websocket response header')
                name, value = line.decode().split(':', 1)
                headers[name.lower()] = value.strip()
            accept = base64.b64encode(hashlib.sha1((key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest()).decode()
            require(headers.get('sec-websocket-accept') == accept, 'websocket accept')
            first = stream.read(2)
            require(len(first) == 2 and first[0] == 0x81 and not first[1] & 128, 'websocket text frame')
            size = first[1] & 127
            if size == 126:
                size = struct.unpack('!H', stream.read(2))[0]
            elif size == 127:
                size = struct.unpack('!Q', stream.read(8))[0]
            require(size <= 16 * 1024 * 1024, 'websocket frame limit')
            frame = json.loads(stream.read(size))
            require(frame['type'] == 'snapshot' and isinstance(frame['seq'], int), 'websocket snapshot')
            # Masked normal-close frame; no cookie or frame content is logged.
            mask = os.urandom(4)
            close = struct.pack('!H', 1000)
            sock.sendall(b'\x88\x82' + mask + bytes(v ^ mask[i % 4] for i, v in enumerate(close)))


def smoke(binary, work):
    data = work / 'state'
    data.mkdir()
    seed(binary, data)
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = candidate_environment()
    env.update(SSC_HOST='127.0.0.1', SSC_PORT=str(port), SSC_DATA_DIR=str(data),
               SSC_MDNS_ENABLED='false', SSC_AUTH_REQUIRED='true', SSC_ALPACA_PORT='0')
    document = {'schema_version': 1, 'id': 'native-smoke', 'name': 'Public synthetic smoke',
                'created_at': '2026-08-01T12:00:00Z', 'updated_at': '2026-08-01T12:00:00Z',
                'items': [
                    {'kind': 'startup', 'id': 'startup', 'start': {'kind': 'event', 'event': 'astronomical_dusk', 'offset_min': 0}, 'duration_min': 5},
                    {'kind': 'goto_and_stack', 'id': 'target', 'start': {'kind': 'relative', 'offset_min': 0}, 'duration_min': 30,
                     'target_name': 'Synthetic target', 'ra_j2000_h': 0.71, 'dec_j2000_deg': 41.27,
                     'framing': {'mode': 'altaz', 'pa_offset_deg': 0, 'lens': 'tele'}},
                    {'kind': 'shutdown', 'id': 'shutdown', 'start': {'kind': 'relative', 'offset_min': 5}}]}
    checks = {}
    saved = None
    for cycle in range(2):
        process = subprocess.Popen([str(binary), '--no-mdns'], cwd=binary.parent, env=env,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            client = Client(port)
            deadline = time.monotonic() + 90
            while True:
                require(process.poll() is None, 'backend exited before health')
                try:
                    require(client.request('/api/health')['status'] == 'ok', 'startup health')
                    break
                except (OSError, ValueError):
                    require(time.monotonic() < deadline, 'startup timeout')
                    time.sleep(1)
            token = client.request('/api/auth/pairing')['token']
            require(client.request('/api/auth/pair', {'token': token})['scope'] == 'control', 'loopback pairing')
            if cycle == 0:
                client.request('/api/settings/location/fix', {'source': 'manual', 'latitude_deg': 45.0,
                               'longitude_deg': 10.0, 'timezone': 'Europe/Rome'}, 'PUT')
                client.request('/api/plans', {'document': document})
                saved = client.request('/api/plans/native-smoke')
            else:
                require(client.request('/api/plans/native-smoke') == saved, 'restart persistence')
            for _ in range(3):
                result = client.request('/api/plans/native-smoke/compile', {'night_date': '2026-10-12'})
                items = result['schedule']['items']
                require(len(items) == len(document['items']) and all(i['start_utc'] for i in items), 'compiled schedule')
            client.websocket()
            require(process.poll() is None and client.request('/api/health')['status'] == 'ok', 'post-compile health')
            checks[f'cycle_{cycle + 1}'] = {'health': True, 'pairing': True, 'compile_count': 3,
                                          'websocket_snapshot': True, 'persistence': True}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            require(process.poll() is not None, 'backend cleanup')
    return checks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', choices=TARGETS, required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--run', required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    parser.add_argument('--isolated-binary', type=Path)
    parser.add_argument('--work', type=Path)
    args = parser.parse_args()
    evidence = {'status': 'failed', 'target': args.target,
                'runner_os': platform.platform(), 'runner_arch': platform.machine(),
                'validation_scope': 'compiled-payload-only',
                'installer_gui_field_validation': 'not-performed',
                'bookworm_userspace_validation': 'not-performed' if args.target == 'rpi-arm64' else 'not-applicable'}
    try:
        require(os.environ.get('GITHUB_ACTIONS') == 'true' and os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted', 'GitHub hosted runner required')
        require((platform.system(), platform.machine()) == TARGETS[args.target], 'native runner mismatch')
        if args.isolated_binary:
            checks = smoke(args.isolated_binary, args.work)
            args.evidence.write_text(json.dumps(checks))
            return
        with tempfile.TemporaryDirectory(prefix='photon-native-') as directory:
            work = Path(directory)
            binary, identity = prepare(args, work)
            evidence.update(identity)
            child_result = work / 'checks.json'
            command = [sys.executable, str(Path(__file__).resolve()), '--target', args.target,
                       '--commit', args.commit, '--run', args.run, '--evidence', str(child_result),
                       '--isolated-binary', str(binary), '--work', str(work)]
            try:
                run_isolated(command, work)
            finally:
                if child_result.exists():
                    evidence['checks'] = json.loads(child_result.read_text())
            evidence['checks'] = json.loads(child_result.read_text())
            evidence['status'] = 'passed'
    except Exception as error:
        # Do not emit exception text: urllib errors may contain sensitive URLs.
        evidence['failure_type'] = type(error).__name__
        if type(error) is RuntimeError:
            evidence['failure_stage'] = str(error)
        if args.isolated_binary:
            args.evidence.write_text(json.dumps(evidence))
        raise SystemExit(1) from None
    finally:
        if not args.isolated_binary:
            args.evidence.parent.mkdir(parents=True, exist_ok=True)
            args.evidence.write_text(json.dumps(evidence, indent=2) + '\n')


if __name__ == '__main__':
    main()
