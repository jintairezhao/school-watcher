"""Source and PyInstaller entry point. GUI dependencies are loaded only by the UI."""
import argparse
import json
import logging
import os
from pathlib import Path
import sys

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from desktop import VERSION
from desktop.runtime import DesktopRuntime, configure, resource_root, run_service, user_data_dir


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--version', action='version', version=VERSION)
    parser.add_argument('--data-dir', type=Path, default=None)
    parser.add_argument('--service', choices=['migrate', 'web', 'worker', 'browser', 'probe', 'browser-setup', 'browser-download'])
    parser.add_argument('--smoke-test', action='store_true')
    parser.add_argument('--gui-smoke-test', action='store_true')
    parser.add_argument('--report', type=Path)
    parser.add_argument('--configure-locations', action='store_true')
    parser.add_argument('--download-dir', type=Path)
    parser.add_argument('--cache-dir', type=Path)
    parser.add_argument('--backup-dir', type=Path)
    parser.add_argument('--remove-personal-data', action='store_true')
    parser.add_argument('--stop-for-uninstall', action='store_true')
    args = parser.parse_args(argv)
    if args.stop_for_uninstall:
        if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
            raise RuntimeError('卸载清理仅适用于 Windows 安装版。')
        from desktop.uninstall import stop_installation
        from desktop.window import request_exit
        stop_installation(sys.executable, request_exit)
        return 0
    if args.service:
        from desktop.processes import guard_service
        guard_service()
    if args.remove_personal_data:
        from desktop.locations import remove_personal_data
        remove_personal_data()
        return 0
    if args.configure_locations:
        from desktop.locations import apply_locations
        apply_locations(args.data_dir or user_data_dir(), args.download_dir, args.cache_dir, args.backup_dir)
        return 0
    if not args.service:
        import secrets
        os.environ['WATCHER_DESKTOP_TOKEN'] = secrets.token_hex(32)
        os.environ['WATCHER_BROWSER_TOKEN'] = secrets.token_hex(32)
    data = configure(args.data_dir or user_data_dir())
    if args.gui_smoke_test:
        import faulthandler
        trace = (data / 'desktop-gui-trace.log').open('w')
        faulthandler.enable(file=trace)
        faulthandler.dump_traceback_later(60, file=trace)
    # Windowed executables have no stdout/stderr. Services still need real streams.
    if sys.stdout is None:
        sys.stdout = (data / f'desktop-{args.service or "app"}.log').open('a', encoding='utf-8')
    if sys.stderr is None:
        sys.stderr = sys.stdout
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s',
                        handlers=[logging.FileHandler(data / 'desktop.log', encoding='utf-8')])
    if args.service:
        run_service(args.service)
        return 0
    from filelock import FileLock, Timeout
    lock = FileLock(str(data / 'desktop-app.lock'), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        from desktop.window import activate_existing
        if not activate_existing(data):
            raise RuntimeError('School Watcher 正在启动，请稍候。')
        return 0
    runtime = DesktopRuntime(data)
    try:
        if args.smoke_test:
            import urllib.request
            import http.cookiejar
            address = runtime.start()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            with opener.open(runtime.open_url(), timeout=15) as response:
                assert response.status == 200 and b'/admin' in response.read()
            for path in ('/admin', '/api/admin/stats', '/static/js/directory.js', '/static/css/fonts.css'):
                with opener.open(address + path, timeout=10) as response:
                    assert response.status == 200 and response.read(100)
            assert runtime.processes['browser-setup'].wait(timeout=900) == 0, 'Browser preparation failed'
            probe = runtime.spawn('probe')
            assert probe.wait(timeout=90) == 0, 'Headless or headed browser smoke check failed'
            info = {'version': VERSION, 'status': 'passed', 'web': True, 'chromium': True,
                    'data_dir': str(data), 'resource_root': str(resource_root())}
            if args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps(info, indent=2), encoding='utf-8')
            print(json.dumps(info), flush=True)
        else:
            from desktop.window import run_window
            run_window(runtime, smoke_test=args.gui_smoke_test)
            if args.gui_smoke_test and args.report:
                args.report.parent.mkdir(parents=True, exist_ok=True)
                args.report.write_text(json.dumps({'status': 'passed', 'native_window': True,
                    'version': VERSION}), encoding='utf-8')
        return 0
    finally:
        runtime.close()
        lock.release()
        if args.gui_smoke_test:
            faulthandler.cancel_dump_traceback_later()
        uninstaller = getattr(runtime, 'requested_uninstaller', None)
        if uninstaller:
            from desktop.uninstall import launch_uninstaller
            launch_uninstaller(uninstaller)
        if getattr(runtime, 'restart_requested', False):
            import subprocess
            environment = os.environ.copy()
            environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
            command = [sys.executable] if getattr(sys, 'frozen', False) else [sys.executable, str(Path(__file__).resolve())]
            if os.name == 'nt' and getattr(sys, 'frozen', False):
                import ctypes
                ctypes.windll.kernel32.SetDllDirectoryW(None)
            subprocess.Popen(command, env=environment, cwd=str(user_data_dir()),
                             creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


if __name__ == '__main__':
    try:
        from multiprocessing import freeze_support
        freeze_support()
        raise SystemExit(main())
    except Exception as exc:
        logging.exception('Desktop application failed')
        if os.name == 'nt' and not any(flag in sys.argv for flag in ('--service', '--smoke-test', '--gui-smoke-test', '--stop-for-uninstall')):
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, f'学校通知未能启动。\n\n{exc}', 'School Watcher', 0x10)
        elif sys.stderr:
            print(str(exc), file=sys.stderr)
        raise SystemExit(1)
