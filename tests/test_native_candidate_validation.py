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
    calls = []

    class Process:
        pid = 123456

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
        assert '(deny network*)' in (tmp_path / 'network.sb').read_text()


def test_isolation_failure_does_not_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(validation.platform, 'system', lambda: 'Linux')
    calls = []

    class Process:
        pid = 123456

        def wait(self, timeout):
            return 1

    monkeypatch.setattr(validation.subprocess, 'Popen', lambda cmd, **kwargs: calls.append(cmd) or Process())
    monkeypatch.setattr(validation.os, 'killpg', lambda *_: None)
    with pytest.raises(RuntimeError, match='isolated candidate failed'):
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
