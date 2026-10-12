"""Fresh and upgraded installations require phone pairing without losing local recovery."""
import shutil

from ssc.main import create_app
from ssc.settings.models import SecuritySettings
from ssc.settings.store import SettingsStore
from ssc.store.database import open_database
from ssc.store.migrations import MIGRATIONS_DIR, migrate
from tests.api.conftest import TestClient, pairing_token


def test_fresh_install_requires_pairing_and_local_pc_can_bootstrap(config, gateway):
    assert config.auth_required is None
    assert SecuritySettings().auth_required is True
    app = create_app(config, gateway=gateway)
    with TestClient(app, client=("127.0.0.1", 45000)) as desktop:
        assert desktop.get('/api/state').status_code == 401
        token = pairing_token(desktop)
        phone = TestClient(app, client=("192.168.1.77", 45001))
        assert phone.get('/api/auth/pairing').status_code == 401
        assert phone.post('/api/device/disconnect').status_code == 401
        assert phone.post('/api/auth/pair', json={'token': token}).status_code == 200
        assert phone.get('/api/state').status_code == 200
        assert phone.post('/api/device/disconnect').status_code == 204


async def test_upgrade_restores_pairing_once_then_preserves_explicit_setting(tmp_path):
    before = tmp_path/'before'
    before.mkdir()
    for source in MIGRATIONS_DIR.glob('*.sql'):
        if int(source.name[:4]) < 19:
            shutil.copy2(source, before/source.name)
    path = tmp_path/'settings.sqlite'
    db = await open_database(path)
    try:
        await migrate(db, directory=before)
        await db.execute("INSERT INTO config VALUES ('security', '{\"auth_required\":false,\"control_pin_set\":true}', '2026-01-01T00:00:00Z')")
        await migrate(db)
        settings = SettingsStore(db)
        await settings.load()
        assert settings.effective_auth_required is True
        assert settings.security.control_pin_set is True
        await settings.set_auth_required(False)
    finally:
        await db.aclose()
    db = await open_database(path)
    try:
        await migrate(db)
        settings = SettingsStore(db)
        await settings.load()
        assert settings.effective_auth_required is False
    finally:
        await db.aclose()
