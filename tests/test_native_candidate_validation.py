"""Offline harness checks: never launch a product backend or candidate binary."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import json
import shutil
import sqlite3
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('native_validation', ROOT / 'scripts/validate_native_candidate.py')
assert SPEC and SPEC.loader
validation = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(validation)


@pytest.mark.parametrize('system', ['Linux', 'Darwin'])
def test_appimage_extraction_uses_isolation_and_allowlisted_environment(tmp_path, monkeypatch, system):
    """Exercise extraction through its real boundary, mocking only OS execution."""
    monkeypatch.setattr(validation.platform, 'system', lambda: system)
    for key in ['GH_TOKEN', 'ACTIONS_RUNTIME_TOKEN', 'ACTIONS_ID_TOKEN_REQUEST_TOKEN',
                'ACTIONS_ID_TOKEN_REQUEST_URL', 'GITHUB_TOKEN', 'SSC_SECRET', 'HTTP_PROXY']:
        monkeypatch.setenv(key, 'must-not-reach-candidate')
    monkeypatch.setenv('HOME', '/home/synthetic-runner')
    calls = []

    class Process:
        pid = 123456

        def __init__(self):
            self.stdout = io.BytesIO()

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(validation.subprocess, 'Popen', lambda cmd, **kwargs: calls.append((cmd, kwargs)) or Process())
    monkeypatch.setattr(validation.os, 'killpg', lambda *_: None)
    package = tmp_path / 'candidate.AppImage'
    package.touch()
    validation.extract_package(package, tmp_path, 'linux-x86_64', tmp_path)
    command, options = calls[0]
    assert command[-2:] == [str(package), '--appimage-extract']
    assert '--net' in command if system == 'Linux' else command[0] == '/usr/bin/sandbox-exec'
    assert 'must-not-reach-candidate' not in options['env'].values()
    assert set(options['env']) <= {'PATH', 'HOME', 'USER', 'LOGNAME', 'TMPDIR', 'LANG', 'LC_ALL',
                                  'GITHUB_ACTIONS', 'RUNNER_ENVIRONMENT'}
    assert options['start_new_session'] is True
    if system == 'Darwin':
        profile = (tmp_path / 'network.sb').read_text()
        assert '(deny network*)' in profile
        assert '127.0.0.1:*' not in profile
        assert profile.count('localhost:*') == 3
    else:
        # sudo may reset HOME; setpriv does not restore it. Reapply the entire
        # minimal environment after dropping privileges, not before sudo.
        assert command.index('/usr/bin/env') > command.index('--init-groups')
        assert command[command.index('/usr/bin/env') + 1] == '-i'
        assert 'HOME=/home/synthetic-runner' in command
        assert 'must-not-reach-candidate' not in ' '.join(command)


def test_isolation_failure_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(validation.platform, 'system', lambda: 'Linux')
    calls = []

    class Process:
        pid = 123456

        def __init__(self):
            self.stdout = io.BytesIO()

        def wait(self, timeout):
            return 1

    monkeypatch.setattr(validation.subprocess, 'Popen', lambda cmd, **kwargs: calls.append(cmd) or Process())
    monkeypatch.setattr(validation.os, 'killpg', lambda *_: None)
    with pytest.raises(RuntimeError, match='candidate process failed'):
        validation.run_isolated(['candidate'], tmp_path)
    assert len(calls) == 1


def test_seed_is_accepted_by_actual_migrator(tmp_path):
    from ssc.settings.models import DeviceSettings, StreamingSettings
    from ssc.store import migrate, open_database

    app = tmp_path / 'app'
    app.mkdir()
    data = tmp_path / 'data'
    data.mkdir()
    migrations = ROOT / 'ssc/store/migrations'
    shutil.copy2(migrations / '0001_initial.sql', app / '0001_initial.sql')
    validation.seed(app / 'ssc-core', data)

    async def apply():
        database = await open_database(data / 'ssc.sqlite3')
        try:
            await migrate(database, directory=migrations)
        finally:
            await database.aclose()

    asyncio.run(apply())
    with sqlite3.connect(data / 'ssc.sqlite3') as db:
        sections = dict(db.execute('SELECT key,value FROM config'))
        assert db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0] == len(list(migrations.glob('*.sql')))
    device = DeviceSettings.model_validate_json(sections['device'])
    assert not device.auto_connect
    assert device.host is device.ethernet_host is device.last_healthy_host is None
    assert not StreamingSettings.model_validate_json(sections['streaming']).broadcast_when_unparked


@pytest.mark.parametrize('embedded_commit_matches', [True, False])
def test_candidate_identity_independent_of_workflow_revision(tmp_path, monkeypatch, embedded_commit_matches):
    commit = 'a' * 40
    workflow_sha = 'b' * 40
    embedded = commit if embedded_commit_matches else 'c' * 40
    package_name = 'Photon-Commander-macos-arm64.tar.gz'
    package = io.BytesIO()
    with tarfile.open(fileobj=package, mode='w:gz') as archive:
        files = {'app/ssc-core': 'not executed', 'app/COMMIT': embedded,
                 'app/VERSION': '0.0.0+g' + commit[:12],
                 'build-manifest.json': json.dumps({'commit': commit, 'version': '0.0.0+g' + commit[:12]})}
        for name, value in files.items():
            content = value.encode()
            member = tarfile.TarInfo('payload/' + name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    bundle = tmp_path / 'input.zip'
    with zipfile.ZipFile(bundle, 'w') as archive:
        archive.writestr(package_name, package.getvalue())
        archive.writestr(package_name + '.sha256', hashlib.sha256(package.getvalue()).hexdigest())
    monkeypatch.setenv('GITHUB_REPOSITORY', 'owner/repo')
    monkeypatch.setenv('GH_TOKEN', 'synthetic-token')
    artifact = {'name': f'photon-macos-arm64-{commit}-unsigned', 'expired': False,
                'id': 10, 'digest': 'sha256:' + validation.digest(bundle)}
    monkeypatch.setattr(validation, 'api', lambda url: {'artifacts': [artifact]} if '/artifacts?' in url else
                        {'conclusion': 'success', 'head_sha': workflow_sha, 'path': '.github/workflows/build-desktop.yml'})
    monkeypatch.setattr(validation.subprocess, 'run', lambda cmd, **kwargs: shutil.copyfile(bundle, cmd[-1]))
    work = tmp_path / 'work'
    work.mkdir()
    args = SimpleNamespace(commit=commit, run='123', target='macos-arm64')
    if embedded_commit_matches:
        _, evidence = validation.prepare(args, work)
        assert evidence['source_sha'] == commit
        assert evidence['workflow_sha'] == workflow_sha
    else:
        with pytest.raises(RuntimeError, match='embedded COMMIT mismatch'):
            validation.prepare(args, work)


@pytest.mark.parametrize(('message', 'category'), [
    (b'host must be * or localhost', 'sandbox_profile_invalid'),
    (b'PermissionError: secret-path', 'permission_denied'),
    (b'error while loading shared libraries: secret-path', 'dynamic_library_missing'),
    (b'unknown error: private-token-value', 'unknown'),
])
def test_private_output_is_bounded_and_only_classifies(message, category):
    output = validation.PrivateOutput(io.BytesIO(message + b'private-token-value' * 10000))
    result = output.diagnostics('backend_startup', 1)
    assert len(output.data) <= output.LIMIT
    assert result == {'stage': 'backend_startup', 'returncode': 1, 'classifier': category}
    assert 'private-token' not in json.dumps(result)
    assert 'secret-path' not in json.dumps(result)


def test_process_failure_preserves_partial_checks():
    failure = validation.ProcessFailureError({'stage': 'backend_startup', 'returncode': -6, 'classifier': 'unknown'},
                                        {'cycle_1': {'health': True}})
    assert failure.checks == {'cycle_1': {'health': True}}
    assert failure.diagnostics['returncode'] == -6


@pytest.mark.parametrize(('failure', 'expected_step', 'http_status'), [
    ('startup', 'startup', 503),
    ('pairing', 'pairing', 403),
    ('location', 'location', 400),
    ('save', 'save', 500),
    ('reload', 'reload', 500),
    ('compile', 'compile', 500),
    ('schedule', 'compile', None),
    ('websocket', 'websocket', None),
    ('final_health', 'final_health', 503),
])
def test_smoke_failures_reach_sanitized_evidence(tmp_path, monkeypatch, failure, expected_step, http_status):
    """Exercise smoke and main's real failure serialization, with no subprocess/network."""
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setenv('RUNNER_ENVIRONMENT', 'github-hosted')
    monkeypatch.setattr(validation.platform, 'system', lambda: 'Linux')
    monkeypatch.setattr(validation.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(validation.platform, 'platform', lambda: 'synthetic-test-platform')
    monkeypatch.setattr(validation, 'seed', lambda *_: None)
    monkeypatch.setattr(validation.time, 'sleep', lambda *_: None)
    ticks = iter(range(0, 10000, 100))
    monkeypatch.setattr(validation.time, 'monotonic', lambda: next(ticks))

    class FakeSocket:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def bind(self, *_):
            pass

        def getsockname(self):
            return ('127.0.0.1', 12345)

    class FakeProcess:
        def __init__(self, *_, **__):
            self.stdout = io.BytesIO(b'private-token-do-not-publish')
            self.code = None

        def poll(self):
            return self.code

        def terminate(self):
            self.code = 0

        def wait(self, timeout):
            return self.code

    clients = []

    class FakeClient:
        def __init__(self, _):
            self.health_calls = 0
            self.cycle = len(clients)
            clients.append(self)

        def request(self, path, body=None, method=None):
            if path == '/api/health':
                self.health_calls += 1
                step = 'startup' if self.health_calls == 1 else 'final_health'
                result = {'status': 'ok'}
            elif path == '/api/auth/pairing':
                step, result = 'pairing', {'token': 'private-token-do-not-publish'}
            elif path == '/api/auth/pair':
                step, result = 'pairing', {'scope': 'control'}
            elif path == '/api/settings/location/fix':
                step, result = 'location', {}
            elif path == '/api/plans':
                step, result = 'save', {}
            elif path.endswith('/compile'):
                step = 'compile'
                result = {'schedule': {'items': [{'start_utc': '2026-10-12T20:00:00Z'}] * 3}}
                if failure == 'schedule':
                    result['schedule']['items'] = []
            else:
                step, result = 'reload', {'document': {'id': 'native-smoke'}}
            # Reload fails after a completed first cycle, exercising retained checks.
            if step == failure and (failure != 'reload' or self.cycle == 1):
                raise validation.urllib.error.HTTPError('https://private-token-do-not-publish', http_status,
                                                        'secret body', {}, None)
            return result

        def websocket(self):
            if failure == 'websocket':
                raise RuntimeError('private-token-do-not-publish')

    monkeypatch.setattr(validation.socket, 'socket', FakeSocket)
    monkeypatch.setattr(validation.subprocess, 'Popen', FakeProcess)
    monkeypatch.setattr(validation, 'Client', FakeClient)
    evidence = tmp_path / 'result.json'
    monkeypatch.setattr(validation.sys, 'argv', ['validate', '--target', 'linux-x86_64', '--commit', 'a' * 40,
                        '--run', '1', '--evidence', str(evidence), '--isolated-binary', str(tmp_path / 'ssc-core'),
                        '--work', str(tmp_path)])
    with pytest.raises(SystemExit) as stopped:
        validation.main()
    assert stopped.value.code == 1
    serialized = evidence.read_text()
    result = json.loads(serialized)
    assert result['process_failure']['stage'] == expected_step
    assert result['process_failure'].get('http_status') == http_status
    assert 'private-token' not in serialized
    assert 'secret body' not in serialized
    if failure == 'startup':
        assert result['process_failure']['classifier'] == 'timeout'
    if failure == 'reload':
        assert result['completed_checks']['cycle_1']['compile_count'] == 3
