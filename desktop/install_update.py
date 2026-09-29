"""Verified installation handoff, executed only after the app services have stopped."""
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

from desktop.uninstall import uninstaller_path
from desktop.updater import UpdateError, file_hash


@dataclass(frozen=True)
class InstallRequest:
    package: Path
    sha256: str
    platform: str
    program: Path | None
    change_locations: bool = False

    @property
    def in_place(self):
        return self.platform == 'win32' and self.program is not None and not self.change_locations


def prepare_install(package, update, *, change_locations=False):
    package = Path(package).resolve()
    if not package.is_file() or file_hash(package) != update.sha256:
        raise UpdateError('安装包已发生变化，请重新下载。')
    uninstaller = uninstaller_path() if sys.platform == 'win32' else None
    return InstallRequest(package, update.sha256, sys.platform,
                          uninstaller.parent if uninstaller else None, change_locations)


def install_command(request):
    if request.platform == 'darwin':
        return ['open', str(request.package)]
    if request.platform != 'win32':
        raise UpdateError('当前系统不支持桌面安装更新。')
    command = [str(request.package), '/SP-', '/NORESTART', '/NOFORCECLOSEAPPLICATIONS',
               '/WATCHERPID=' + str(os.getpid()), '/LOG']
    if request.program:
        command.append('/DIR=' + str(request.program))
    if request.in_place:
        command.extend(['/UPDATE', '/SILENT'])
    else:
        # Portable/source copies must not silently update a different installed copy.
        command.append('/CHANGELOCATIONS')
    return command


def launch_install(request):
    # Recheck after shutdown, too: a stale or replaced download must not execute.
    if not request.package.is_file() or file_hash(request.package) != request.sha256:
        raise UpdateError('安装包已发生变化，请重新打开应用并下载更新。')
    environment = os.environ.copy()
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    frozen_windows = os.name == 'nt' and getattr(sys, 'frozen', False)
    if frozen_windows:
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    try:
        return subprocess.Popen(install_command(request), cwd=request.package.parent, env=environment)
    finally:
        if frozen_windows:
            from desktop.runtime import resource_root
            ctypes.windll.kernel32.SetDllDirectoryW(str(resource_root()))
