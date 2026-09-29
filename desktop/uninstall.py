"""Uninstall only the current installation; personal data belongs to the wizard."""
import os
from pathlib import Path
import subprocess
import sys


def uninstaller_path():
    if sys.platform != 'win32' or not getattr(sys, 'frozen', False):
        return None
    root = Path(sys.executable).resolve().parent
    for candidate in sorted(root.glob('unins[0-9][0-9][0-9].exe')):
        if (candidate.is_file() and candidate.with_suffix('.dat').is_file()
                and candidate.resolve().parent == root):
            return candidate
    return None


def launch_uninstaller(path):
    # Run only after runtime.close() and releasing the profile lock.
    if path != uninstaller_path():
        raise RuntimeError('卸载程序已变化，请重新打开应用后重试。')
    environment = os.environ.copy()
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    import ctypes
    from desktop.runtime import resource_root
    ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        subprocess.Popen([str(path)], cwd=path.parent, env=environment)
    finally:
        ctypes.windll.kernel32.SetDllDirectoryW(str(resource_root()))


def stop_installation(executable, request_exit=None):
    """Match the full executable path, never kill all processes with this name."""
    import psutil
    target = os.path.normcase(os.path.realpath(executable))
    current = os.getpid()
    owned = {}
    profiles = set()
    for process in psutil.process_iter(['pid', 'exe', 'cmdline']):
        info = process.info
        if info['pid'] == current or not info['exe']:
            continue
        if os.path.normcase(os.path.realpath(info['exe'])) != target:
            continue
        owned[process.pid] = process
        arguments = info['cmdline'] or []
        if '--data-dir' in arguments:
            index = arguments.index('--data-dir') + 1
            if index < len(arguments): profiles.add(arguments[index])
        # Only service-owned browsers/drivers, never an installer started by the UI.
        if '--service' in arguments:
            try:
                for child in process.children(recursive=True):
                    if child.pid != current: owned[child.pid] = child
            except psutil.NoSuchProcess:
                pass
    if request_exit and owned:
        for profile in profiles or {None}:
            request_exit(profile)
    _, alive = psutil.wait_procs(list(owned.values()), timeout=5)
    for process in alive:
        try: process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    _, alive = psutil.wait_procs(alive, timeout=5)
    for process in alive:
        try: process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied): pass
    _, alive = psutil.wait_procs(alive, timeout=3)
    if alive:
        raise RuntimeError('后台进程未能退出，卸载已停止。请重启电脑后重试。')
