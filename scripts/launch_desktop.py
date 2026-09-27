"""Open the local site, starting its server once when necessary."""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
import secrets

ROOT = Path(__file__).resolve().parents[1]


def main(open_browser=True):
    from dotenv import load_dotenv
    from filelock import FileLock, Timeout

    load_dotenv(ROOT / '.env')
    port = int(os.environ.get('WATCHER_PORT', '5000'))
    address = f'http://127.0.0.1:{port}'
    log_dir = Path(os.environ.get('WATCHER_DATA_DIR', str(ROOT / 'data')))
    log_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('WATCHER_BROWSER', '1')
    os.environ.setdefault('WATCHER_BROWSER_URL', 'http://127.0.0.1:8765')
    os.environ.setdefault('WATCHER_ORIGIN_BROKER_URL', address + '/internal/browser-origin')
    os.environ.setdefault('WATCHER_PUBLIC_ORIGIN', address)
    # Local traffic must not be sent through a configured system proxy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def running():
        try:
            with opener.open(address + '/static/js/directory.js', timeout=1) as response:
                if b'function directoryRequest(' in response.read(65536):
                    return True
                raise RuntimeError(f'Port {port} is in use by another application.')
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Port {port} is in use, but School Watcher did not respond.') from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return False

    with FileLock(str(log_dir / 'desktop-launch.lock'), timeout=60):
        if os.environ['WATCHER_BROWSER'] == '1' and not os.environ.get('WATCHER_BROWSER_TOKEN'):
            token_path = log_dir / 'browser-service.token'
            if not token_path.exists():
                with token_path.open('x', encoding='ascii') as token_file:
                    token_file.write(secrets.token_hex(32))
                token_path.chmod(0o600)
            token = token_path.read_text(encoding='ascii').strip()
            if len(token) < 32:
                raise RuntimeError('The saved browser service token is invalid.')
            os.environ['WATCHER_BROWSER_TOKEN'] = token
        if not running():
            python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            log_path = log_dir / 'desktop-launch.log'
            with log_path.open('ab') as log:
                log.write(b'\n--- Desktop launch ---\n')
                log.flush()
                result = subprocess.run([python, str(ROOT / 'scripts' / 'maintenance' / 'migrate_safely.py')],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=flags, timeout=90)
                if result.returncode:
                    raise RuntimeError(f'Database upgrade failed. See {log_path}')
                process = subprocess.Popen([python, str(ROOT / 'app.py')], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    creationflags=flags, start_new_session=os.name != 'nt')
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise RuntimeError(f'Server could not start. See {log_path}')
                    if running():
                        break
                    time.sleep(0.4)
                else:
                    raise RuntimeError(f'Server did not become ready within 30 seconds. See {log_path}')
        # The website never starts collection. Keep a separate, single worker locally.
        if os.environ['WATCHER_BROWSER'] == '1':
            browser_lock = FileLock(str(log_dir / 'browser-service.lock'), timeout=0)
            try:
                browser_lock.acquire()
            except Timeout:
                pass
            else:
                browser_lock.release()
                with (log_dir / 'browser-launch.log').open('ab') as browser_log:
                    subprocess.Popen([sys.executable, str(ROOT / 'scripts' / 'run_browser.py'), '--supervise'], cwd=ROOT,
                        stdin=subprocess.DEVNULL, stdout=browser_log, stderr=subprocess.STDOUT,
                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                        start_new_session=os.name != 'nt')
        worker_lock = FileLock(str(log_dir / 'worker.lock'), timeout=0)
        try:
            worker_lock.acquire()
        except Timeout:
            pass
        else:
            worker_lock.release()
            python = str(Path(sys.executable).with_name('python.exe')) if os.name == 'nt' else sys.executable
            flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            with (log_dir / 'worker-launch.log').open('ab') as worker_log:
                subprocess.Popen([python, str(ROOT / 'scripts' / 'run_worker.py')], cwd=ROOT,
                    stdin=subprocess.DEVNULL, stdout=worker_log, stderr=subprocess.STDOUT,
                    creationflags=flags, start_new_session=os.name != 'nt')
        if open_browser:
            webbrowser.open(address + '/')
    return address


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--no-browser', action='store_true', help='Check/start without opening a browser')
    args = parser.parse_args()
    try:
        address = main(open_browser=not args.no_browser)
        if sys.stdout:
            print(address)
    except Exception as exc:
        message = f'School Watcher could not open.\n\n{exc}'
        if os.name == 'nt' and not args.no_browser:
            ctypes.windll.user32.MessageBoxW(None, message, 'School Watcher', 0x10)
        elif sys.stderr:
            print(message, file=sys.stderr)
        sys.exit(1)
