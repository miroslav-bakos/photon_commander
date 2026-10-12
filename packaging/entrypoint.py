"""Compiled backend entry point, with an optional desktop launch."""
from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
import uuid
import webbrowser
from pathlib import Path
from typing import IO

from ssc.main import main


def desktop_configuration() -> str:
    """Use the per-user install's frontend and data; never write in Program Files."""
    root = Path(sys.executable).resolve().parent.parent
    os.environ.setdefault('SSC_STATIC_DIR', str(root / 'web'))
    if os.name == 'nt':
        data = Path(os.environ['LOCALAPPDATA']) / 'Photon Commander'
    else:
        data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share'))) / 'photon-commander'
    os.environ.setdefault('SSC_DATA_DIR', str(data))
    os.environ.setdefault('SSC_HOST', '127.0.0.1')
    os.environ.setdefault('SSC_PORT', '7777')
    return f"http://127.0.0.1:{os.environ['SSC_PORT']}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def backend_matches(url: str, instance: str) -> bool:
    """Neither a generic HTTP 200 nor another installation is readiness."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(url + '/api/health', timeout=1) as response:
            body = response.read(32769)
            if response.status != 200 or len(body) > 32768:
                return False
        health = json.loads(body)
        return (isinstance(health, dict) and health.get('application') == 'photon-commander'
                and health.get('status') == 'ok' and health.get('desktop_instance') == instance)
    except (OSError, urllib.error.URLError, ValueError):
        return False


def startup_failure(message: str) -> None:
    """Keep an actionable failure visible when launched from a Windows shortcut."""
    sys.stderr.write('Photon Commander: ' + message + '\n')
    try:
        folder = Path(os.environ['SSC_DATA_DIR'])
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / 'startup-error.txt'
        path.write_text(message + '\n', encoding='utf-8')
        path.chmod(0o600)
    except (OSError, KeyError):
        pass
    if os.name == 'nt':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'Photon Commander — startup failed', 0x10)


def open_when_ready(url: str, instance: str, stopped: threading.Event | None = None) -> bool:
    stopped = stopped or threading.Event()
    for _ in range(120):
        if stopped.is_set():
            return False
        if backend_matches(url, instance):
            webbrowser.open(url)
            return True
        stopped.wait(.5)
    if not stopped.is_set():
        startup_failure('The backend did not become ready. Check whether the configured port is occupied, '
                        'the data folder is writable and the OS keyring is available. See the console output; '
                        'close Photon Commander before retrying.')
    return False


def acquire_desktop_lock(folder: Path) -> IO[bytes] | None:
    """Hold an OS lock for the entire backend lifetime, before database/device I/O."""
    folder.mkdir(parents=True, exist_ok=True)
    lock = (folder / 'desktop.lock').open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            if not lock.read(1):
                lock.write(b'0')
                lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        return None
    return lock


def run_desktop() -> None:
    url = desktop_configuration()
    folder = Path(os.environ['SSC_DATA_DIR'])
    try:
        lock = acquire_desktop_lock(folder)
    except OSError:
        startup_failure('Cannot create the desktop lock. Restore write access to the Photon data folder.')
        return
    identity_path = folder / 'desktop-instance.json'
    if lock is None:
        try:
            identity = json.loads(identity_path.read_text(encoding='utf-8'))
            if identity.get('url') == url and isinstance(identity.get('instance'), str):
                # A second click during startup also waits for the same instance.
                open_when_ready(url, identity['instance'])
                return
        except (OSError, ValueError, AttributeError):
            pass
        startup_failure('Photon Commander is already starting or running. Close its existing process before retrying.')
        return
    stopped = threading.Event()
    try:
        try:
            instance = uuid.uuid4().hex
            os.environ['SSC_DESKTOP_INSTANCE'] = instance
            tmp = identity_path.with_suffix('.tmp')
            tmp.write_text(json.dumps({'url': url, 'instance': instance}), encoding='utf-8')
            tmp.chmod(0o600)
            tmp.replace(identity_path)
            ready_thread = threading.Thread(target=open_when_ready, args=(url, instance, stopped), daemon=True)
            ready_thread.start()
        except (OSError, RuntimeError):
            stopped.set()
            startup_failure('Cannot save desktop startup information or start the readiness check. '
                            'Restore write access to the Photon data folder and retry.')
            raise
        try:
            main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                stopped.set()
                startup_failure(f'The backend exited with code {exc.code}. Check the console output, '
                                'port availability and write access to the data folder.')
                raise
        except Exception as exc:
            stopped.set()
            startup_failure(f'The backend could not start ({type(exc).__name__}). Check the console output, '
                            'the data folder and the OS keyring; saved data has not been deleted.')
            raise
        finally:
            stopped.set()
            ready_thread.join(timeout=2)
    finally:
        lock.close()


if __name__ == '__main__':
    if '--desktop' in sys.argv:
        sys.argv.remove('--desktop')
        run_desktop()
    else:
        main()
