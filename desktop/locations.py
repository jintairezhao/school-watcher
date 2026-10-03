"""User-selected locations and verified data copies; never erase the old profile."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

REGISTRY_FIELDS = {'data': 'DataDirectory', 'downloads': 'DownloadDirectory',
                   'cache': 'CacheDirectory', 'backups': 'BackupDirectory'}


class LocationError(ValueError):
    pass


def default_data_dir():
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local')) / 'SchoolWatcher'
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'School Watcher'
    return Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share')) / 'school-watcher'


def _settings_file():
    override = os.environ.get('WATCHER_LOCATION_SETTINGS')
    return Path(override) if override else default_data_dir() / 'desktop-locations.json'


def load_locations():
    if sys.platform == 'win32' and not os.environ.get('WATCHER_LOCATION_SETTINGS'):
        import winreg
        values = {}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\SchoolWatcher') as key:
                for field, name in REGISTRY_FIELDS.items():
                    try:
                        value, kind = winreg.QueryValueEx(key, name)
                        if kind == winreg.REG_SZ: values[field] = value
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            pass
    else:
        try:
            values = json.loads(_settings_file().read_text(encoding='utf-8'))
        except FileNotFoundError:
            values = {}
        except (ValueError, UnicodeError) as exc:
            raise LocationError('目录设置无法读取，请恢复目录设置文件后重试。') from exc
    if not isinstance(values, dict):
        raise LocationError('目录设置格式不正确。')
    for value in values.values():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise LocationError('目录设置必须使用完整路径。')
    return values


def save_locations(*, data_dir=None, download_dir=None, cache_dir=None, backup_dir=None):
    values = load_locations()
    for field, value in (('data', data_dir), ('downloads', download_dir), ('cache', cache_dir), ('backups', backup_dir)):
        if value is not None:
            path = Path(value).expanduser()
            if not path.is_absolute(): raise LocationError('请选择完整的文件夹路径。')
            values[field] = str(path.resolve())
    if sys.platform == 'win32' and not os.environ.get('WATCHER_LOCATION_SETTINGS'):
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\SchoolWatcher') as key:
            before = {}
            for name in REGISTRY_FIELDS.values():
                try:
                    before[name] = winreg.QueryValueEx(key, name)
                except FileNotFoundError:
                    before[name] = None
            changed = []
            try:
                for field, name in REGISTRY_FIELDS.items():
                    if field in values:
                        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, values[field])
                        changed.append(name)
            except OSError:
                for name in reversed(changed):
                    original = before[name]
                    if original is None:
                        winreg.DeleteValue(key, name)
                    else:
                        winreg.SetValueEx(key, name, 0, original[1], original[0])
                raise
    else:
        path = _settings_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
        try:
            with os.fdopen(handle, 'w', encoding='utf-8') as output:
                json.dump(values, output, ensure_ascii=False)
                output.flush(); os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def configured_data_dir():
    return Path(load_locations().get('data', default_data_dir()))


def downloads_dir(data_dir):
    return Path(load_locations().get('downloads', Path(data_dir) / 'updates'))


def effective_locations(data_dir=None):
    data = Path(data_dir or configured_data_dir()).resolve()
    saved = load_locations()
    if data_dir is not None and data != configured_data_dir().resolve():
        saved = {}  # Explicit smoke-test / portable profiles stay isolated.
    return {'data': data, 'cache': Path(saved.get('cache', data)),
            'backups': Path(saved.get('backups', data / 'backups')),
            'downloads': Path(saved.get('downloads', data / 'updates'))}


def location_state(data_dir=None):
    return {**{key: str(value) for key, value in effective_locations(data_dir).items()},
            'program': str(program_dir()), 'platform': sys.platform}


def program_dir():
    executable = Path(sys.executable).resolve()
    if sys.platform == 'darwin' and getattr(sys, 'frozen', False):
        return executable.parents[2]
    return executable.parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parents[1]


def validate_data_target(source, target, program_dir=None):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target == source: return target
    if target == Path(target.anchor) or source.is_relative_to(target) or target.is_relative_to(source):
        raise LocationError('请选择与原数据目录分开的文件夹，不要选择磁盘根目录。')
    if program_dir:
        program = Path(program_dir).resolve()
        if target == program or target.is_relative_to(program) or program.is_relative_to(target):
            raise LocationError('数据目录需要与程序安装目录分开。')
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise LocationError('目标文件夹已有文件，请选择一个空文件夹。')
    return target


def _hash(path):
    with path.open('rb') as file:
        return hashlib.file_digest(file, 'sha256').digest()


def _copy_file(source, destination):
    with source.open('rb') as file:
        sqlite = file.read(16) == b'SQLite format 3\x00'
    if sqlite:
        # backup() includes committed WAL pages, unlike copying the .db file alone.
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(destination)) as copied:
                original.backup(copied)
                if copied.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
                    raise LocationError('数据库副本校验失败，原目录保持不变。')
    else:
        shutil.copy2(source, destination)
        if _hash(source) != _hash(destination):
            raise LocationError('文件副本校验失败，原目录保持不变。')


def migrate_data(source, target, *, program_dir=None, exclude=(), progress=None):
    source = Path(source).resolve()
    target = validate_data_target(source, target, program_dir)
    if target == source: return target
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix='.school-watcher-move-', dir=target.parent)).resolve()
    try:
        if source.exists():
            for directory, dirs, files in os.walk(source, followlinks=False):
                relative = Path(directory).relative_to(source)
                destination = stage / relative
                destination.mkdir(parents=True, exist_ok=True)
                dirs[:] = [d for d in dirs if relative / d not in exclude]
                files = [f for f in files if relative / f not in exclude]
                if not relative.parts:
                    dirs[:] = [d for d in dirs if d != 'webview']
                for name in [*dirs, *files]:
                    old = Path(directory) / name
                    if old.is_junction():
                        raise LocationError('数据目录含有目录联接，请移除联接后重试。')
                    if old.is_symlink():
                        resolved = old.resolve()
                        if not resolved.is_relative_to(source):
                            raise LocationError('数据目录含有指向外部的链接，请移除链接后重试。')
                        new = destination / name
                        new.symlink_to(os.path.relpath(stage / resolved.relative_to(source), new.parent),
                                       target_is_directory=old.is_dir())
                        if name in dirs: dirs.remove(name)
                for name in files:
                    old = Path(directory) / name
                    if old.is_symlink() or name.endswith(('.lock', '-wal', '-shm', '.log')) or name in (
                            'desktop-instance.json', 'desktop-maintenance.json', 'desktop-worker-state.json'):
                        continue
                    if not old.is_file(): raise LocationError('数据目录包含不支持的文件类型。')
                    if progress: progress('copying', name)
                    _copy_file(old, destination / name)
                    if progress: progress('copied', name)
        # The selected target was checked above; recheck before replacing an empty directory.
        validate_data_target(source, target, program_dir)
        if target.exists(): target.rmdir()
        os.replace(stage, target)
        return target
    finally:
        if stage.exists() and stage.parent == target.parent and stage.name.startswith('.school-watcher-move-'):
            shutil.rmtree(stage)


def validate_locations(values, source=None):
    if not isinstance(values, dict) or set(values) != set(REGISTRY_FIELDS):
        raise LocationError('请完整填写文件位置。')
    old = effective_locations(source)
    paths = {}
    for key, value in values.items():
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise LocationError('请选择完整的文件夹路径。')
        paths[key] = Path(value).resolve()
    validate_data_target(old['data'], paths['data'], program_dir())
    for key in ('cache', 'backups', 'downloads'):
        target = paths[key]
        if target == old[key]:
            continue
        # Defaults follow a relocated data directory and are copied with it.
        follows_data = (old[key].is_relative_to(old['data']) and
                        target == paths['data'] / old[key].relative_to(old['data']))
        if not follows_data:
            validate_data_target(old[key], target, program_dir())
        for other, directory in paths.items():
            if other == key:
                continue
            if (key == 'cache' and target == paths['data']) or (other == 'cache' and directory == paths['data']):
                continue
            if directory == target or directory.is_relative_to(target) or (other != 'data' and target.is_relative_to(directory)):
                if key == 'cache' and other == 'data' and target == directory:
                    continue  # The original default keeps caches beside the database.
                raise LocationError('请为缓存、备份和下载选择独立的文件夹。')
    return paths


def apply_changes(values, *, source=None, progress=None):
    """Called only after all app services stopped, while holding the profile lock."""
    old = effective_locations(source)
    paths = validate_locations(values, source)
    # Probe write access before any copy; never point preferences at a partial copy.
    for target in paths.values():
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=target.parent):
            pass
    exclude = set()
    for key in ('backups', 'downloads'):
        origin = old[key]
        if origin.is_relative_to(old['data']) and paths[key] != paths['data'] / origin.relative_to(old['data']):
            exclude.add(origin.relative_to(old['data']))
    if old['cache'] == old['data'] and paths['cache'] != paths['data']:
        exclude.update((Path('discovery_cache.sqlite3'), Path('discovery_cache.sqlite3.snapshots.sqlite3'), Path('fetch-evidence')))
    new_data = migrate_data(old['data'], paths['data'], program_dir=program_dir(), exclude=exclude, progress=progress)
    for key in ('cache', 'backups', 'downloads'):
        origin, target = old[key], paths[key]
        if target == origin:
            continue
        if origin.is_relative_to(old['data']) and target == new_data / origin.relative_to(old['data']):
            continue
        if key == 'cache':
            # The legacy cache directory also contains personal data: only copy cache files.
            target.mkdir(parents=True, exist_ok=True)
            for name in ('discovery_cache.sqlite3', 'discovery_cache.sqlite3.snapshots.sqlite3', 'fetch-evidence'):
                item = origin / name
                if item.is_file():
                    if progress: progress('copying', name)
                    _copy_file(item, target / name)
                    if progress: progress('copied', name)
                elif item.is_dir():
                    migrate_data(item, target / name, progress=progress)
        else:
            migrate_data(origin, target, progress=progress)
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    save_locations(data_dir=new_data, cache_dir=paths['cache'], backup_dir=paths['backups'], download_dir=paths['downloads'])
    return new_data


def remove_personal_data():
    """Explicit uninstaller opt-in. Remove only known app files, never a chosen root."""
    import re
    from filelock import FileLock, Timeout
    paths = effective_locations()
    data = paths['data']
    data.mkdir(parents=True, exist_ok=True)
    def erase(path, root):
        # Refuse links/junctions anywhere on the traversal, including the root itself.
        if path.is_symlink() or path.is_junction():
            raise LocationError('目录中含有链接，未删除该目录。请手动检查。')
        if not path.resolve().is_relative_to(root.resolve()):
            raise LocationError('文件超出应用目录，已停止删除。')
        if path.is_dir():
            for child in path.iterdir(): erase(child, root)
            path.rmdir()
        else:
            path.unlink(missing_ok=True)
    try:
        with FileLock(str(data / 'desktop-app.lock'), timeout=0):
            for root in set(paths.values()):
                if root == Path(root.anchor) or root.is_symlink() or root.is_junction():
                    raise LocationError('数据目录无效，未删除个人数据。')
            for name in ('school_watcher.db', 'school_watcher.db-wal', 'school_watcher.db-shm',
                         'source_catalog.sqlite3', 'source_catalog.sqlite3-wal', 'source_catalog.sqlite3-shm',
                         '.env', '.field-key', '.secret_key', 'desktop-settings.json', 'desktop-instance.json',
                         'browser-status.json', 'browser-install.json', 'desktop-browser.json', 'desktop-locations.json',
                         'desktop-maintenance.json', 'desktop-worker-state.json'):
                erase(data / name, data)
            for name in ('desktop.log', 'desktop-app.log', 'desktop-browser-setup.log', 'desktop-browser-download.log',
                         'desktop-browser.log', 'desktop-migrate.log', 'desktop-web.log', 'desktop-worker.log',
                         'desktop-gui-trace.log', 'worker.lock', 'source_catalog.sqlite3.publish.lock'):
                erase(data / name, data)
            for file in data.glob('school_watcher.db.backup-*'):
                if re.fullmatch(r'school_watcher\.db\.backup-\d{8}-\d{6}-\d+', file.name):
                    erase(file, data)
            for name in ('webview', 'browsers', 'catalog-generations', 'source-governance-evidence', 'rollback'):
                erase(data / name, data)
            cache = paths['cache']
            for name in ('discovery_cache.sqlite3', 'discovery_cache.sqlite3-wal', 'discovery_cache.sqlite3-shm',
                         'discovery_cache.sqlite3.worker.lock', 'discovery_cache.sqlite3.snapshots.sqlite3',
                         'discovery_cache.sqlite3.snapshots.sqlite3-journal', 'fetch-evidence', 'fetch-evidence.lock'):
                erase(cache / name, cache)
            backup = paths['backups']
            for file in backup.glob('watcher-*.zip'):
                if re.fullmatch(r'watcher-\d{8}T\d{6,12}\.zip', file.name): erase(file, backup)
            downloads = paths['downloads']
            for folder in downloads.iterdir() if downloads.exists() else []:
                if re.fullmatch(r'\d+\.\d+\.\d+', folder.name) and folder.is_dir():
                    for file in folder.iterdir():
                        if re.fullmatch(r'School-Watcher-[\w.\-]+\.(exe|dmg|zip)(\.part)?', file.name):
                            erase(file, downloads)
                    if not any(folder.iterdir()): folder.rmdir()
        (data / 'desktop-app.lock').unlink(missing_ok=True)
        if sys.platform == 'win32' and not os.environ.get('WATCHER_LOCATION_SETTINGS'):
            import winreg
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r'Software\SchoolWatcher') as key:
                for name in REGISTRY_FIELDS.values():
                    try: winreg.DeleteValue(key, name)
                    except FileNotFoundError: pass
        else:
            _settings_file().unlink(missing_ok=True)
        for root in sorted(set(paths.values()), key=lambda p: len(p.parts), reverse=True):
            if root.is_dir() and not any(root.iterdir()): root.rmdir()
    except Timeout as exc:
        raise LocationError('学校通知仍在运行，请退出后重试。') from exc


def apply_locations(data_dir, download_dir=None, cache_dir=None, backup_dir=None):
    from filelock import FileLock, Timeout
    source = configured_data_dir().resolve()
    source.mkdir(parents=True, exist_ok=True)
    try:
        with FileLock(str(source / 'desktop-app.lock'), timeout=0):
            old = effective_locations(source)
            target = Path(data_dir).resolve()
            values = {key: str(target / path.relative_to(source) if path.is_relative_to(source) else path)
                      for key, path in old.items()}
            for key, value in (('downloads', download_dir), ('cache', cache_dir), ('backups', backup_dir)):
                if value is not None: values[key] = str(value)
            return apply_changes(values, source=source)
    except Timeout as exc:
        raise LocationError('请先退出学校通知，再更改数据目录。') from exc
