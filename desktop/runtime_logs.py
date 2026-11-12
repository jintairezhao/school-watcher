"""Dated, size-bounded diagnostics with one writer per process and role.

Children write to an inherited pipe. The owner continuously drains that pipe,
even when its diagnostic disk is unavailable; logging must not stop a service.
Only files created by this module participate in retention. Legacy logs remain
untouched. Segment leases prevent maintenance from deleting a live writer's file.
"""
import codecs
from datetime import date, datetime, timedelta
import io
import logging
import os
from pathlib import Path
import re
import stat
import sys
import threading

from filelock import FileLock, Timeout

MAX_FILE_BYTES = 5 * 1024 * 1024
TOTAL_BYTES = 100 * 1024 * 1024
RETENTION_DAYS = 30
_ROLE = re.compile(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z')
_MONTH = re.compile(r'\d{4}-\d{2}\Z')
_NAME = re.compile(r'(\d{4}-\d{2}-\d{2})-([a-z][a-z0-9-]{0,39})-([1-9]\d*)(?:\.([1-9]\d*))?\.log\Z')


def _today():
    return datetime.now().date()


def _linked(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _directory(path, *, create=False):
    if _linked(path):
        raise OSError('Linked diagnostic directories are not supported')
    if create:
        path.mkdir(exist_ok=True)
    if _linked(path) or not path.is_dir():
        raise OSError('Diagnostic directory is unavailable')
    return path


def _root(data_root, *, create=False):
    return _directory(Path(data_root).absolute() / 'logs', create=create)


def _lock(path, timeout=0):
    if _linked(path):
        raise OSError('Linked diagnostic locks are not supported')
    # A shared writer may acquire a segment in the drain/worker thread and be
    # closed by its owner thread. Lease ownership must follow the object.
    return FileLock(str(path), timeout=timeout, thread_local=False)


def _managed(path):
    match = _NAME.fullmatch(path.name)
    if not match or path.parent.name != match[1][:7] or _linked(path):
        return None
    try:
        day = date.fromisoformat(match[1])
        if not path.is_file():
            return None
    except (ValueError, OSError):
        return None
    return day, int(match[4] or 0)


def _segment_order(path):
    match = _NAME.fullmatch(Path(path).name)
    return (match[1], match[2], int(match[3]), int(match[4] or 0)) if match else ('', '', 0, 0)


def _summary():
    return dict(files=0, bytes=0, remaining_files=0, remaining_bytes=0,
                protected_files=0, protected_bytes=0, reserved_bytes=0,
                over_budget_bytes=0, skipped_files=0)


def _prune_locked(root, days):
    result = _summary()
    records = []
    cutoff = _today() - timedelta(days=days - 1)
    for month in root.iterdir():
        if not _MONTH.fullmatch(month.name):
            continue
        try:
            _directory(month)
            for path in month.iterdir():
                record = _managed(path)
                if record is None:
                    continue
                size = path.stat().st_size
                try:
                    with _lock(Path(str(path) + '.lock')):
                        pass
                    protected = False
                except Timeout:
                    protected = True
                except OSError:
                    result['skipped_files'] += 1
                    protected = True
                records.append((record[0], path, size, protected))
        except OSError:
            result['skipped_files'] += 1
    remaining = sum(item[2] for item in records)
    # Reserve room for all current segments to grow without scanning the whole
    # archive on every log record. New segments trigger another maintenance pass.
    reserve = sum(max(0, MAX_FILE_BYTES - size) for _, _, size, live in records if live)
    count = len(records)
    for day, path, size, protected in sorted(records, key=lambda item: (item[0], _segment_order(item[1]))):
        if protected:
            result['protected_files'] += 1
            result['protected_bytes'] += size
            continue
        if day >= cutoff and remaining + reserve <= TOTAL_BYTES:
            continue
        lease_path = Path(str(path) + '.lock')
        try:
            # All managed writers acquire the directory lock before a lease;
            # reacquiring here also protects against unrelated lock holders.
            with _lock(lease_path):
                _directory(path.parent)
                if _managed(path) is None:
                    result['skipped_files'] += 1
                    continue
                path.unlink()
            lease_path.unlink(missing_ok=True)
        except (OSError, Timeout):
            result['skipped_files'] += 1
            continue
        result['files'] += 1
        result['bytes'] += size
        remaining -= size
        count -= 1
    result['remaining_files'] = count
    result['remaining_bytes'] = remaining
    result['reserved_bytes'] = reserve
    result['over_budget_bytes'] = max(0, remaining - TOTAL_BYTES)
    # Remove only released sidecars of managed files, and empty month folders.
    for month in root.iterdir():
        if not _MONTH.fullmatch(month.name):
            continue
        try:
            _directory(month)
            for sidecar in month.glob('*.log.lock'):
                original = Path(str(sidecar)[:-5])
                match = _NAME.fullmatch(original.name)
                if not match or original.parent.name != match[1][:7] or original.exists() or _linked(sidecar):
                    continue
                try:
                    date.fromisoformat(match[1])
                    with _lock(sidecar):
                        pass
                    sidecar.unlink(missing_ok=True)
                except (OSError, Timeout, ValueError):
                    pass
            month.rmdir()
        except OSError:
            pass
    return result


def prune_runtime_logs(data_root, days=RETENTION_DAYS):
    """Best-effort maintenance of managed logs only, independent of app/DB state.

    Active leased files are always retained. New writers reserve capacity and
    drop excess output instead of exceeding the cap or deleting live content.
    Counts describe actual successful deletes, never estimated savings.
    """
    if not isinstance(days, int) or isinstance(days, bool) or days < 1:
        raise ValueError('Log retention days must be a positive integer')
    result = _summary()
    try:
        root = _root(data_root)
        with _lock(root / '.runtime-logs.lock', timeout=.25):
            _directory(root)
            return _prune_locked(root, days)
    except (OSError, Timeout):
        result['skipped_files'] += 1
        return result


def managed_log_files(data_root):
    """Enumerate only our regular files for an explicitly requested uninstall.

    The caller must first stop all services. This does not traverse linked
    directories or include arbitrary files placed alongside diagnostics.
    """
    paths = []
    try:
        root = _root(data_root)
        global_lock = root / '.runtime-logs.lock'
        if global_lock.is_file() and not _linked(global_lock):
            paths.append(global_lock)
        for month in root.iterdir():
            if not _MONTH.fullmatch(month.name):
                continue
            try:
                _directory(month)
                for path in month.iterdir():
                    original = Path(str(path)[:-5]) if path.name.endswith('.log.lock') else path
                    match = _NAME.fullmatch(original.name)
                    if not match or month.name != match[1][:7] or _linked(path) or not path.is_file():
                        continue
                    try:
                        date.fromisoformat(match[1])
                    except ValueError:
                        continue
                    paths.append(path)
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return paths


def _append(path, payload):
    # No inherited/open log handles: each append closes before another process
    # can prune or rotate. The long-lived lease protects the current segment.
    with path.open('ab') as output:
        output.write(payload)


class ManagedLogWriter(io.TextIOBase):
    encoding = 'utf-8'
    errors = 'replace'

    def __init__(self, data_root, role, pid=None):
        super().__init__()
        self._guard = threading.RLock()
        self._lease = None
        self._path = None
        if not isinstance(role, str) or len(role) > 40 or not _ROLE.fullmatch(role):
            raise ValueError('Invalid diagnostic role')
        self.pid = os.getpid() if pid is None else int(pid)
        if self.pid < 1:
            raise ValueError('Invalid diagnostic process ID')
        self.data_root = Path(data_root)
        self.role = role
        self.dropped_bytes = 0
        self.last_error = None
        self._day = None
        self._next_segment = 0

    def writable(self):
        return not self.closed

    def isatty(self):
        return False

    def flush(self):
        pass

    def _release(self):
        if self._lease:
            self._lease.release()
        self._lease = None
        self._path = None

    def _select(self, root, day):
        self._release()
        if day != self._day:
            self._day = day
            self._next_segment = 0
        month = _directory(root / day.strftime('%Y-%m'), create=True)
        prefix = f'{day.isoformat()}-{self.role}-{self.pid}'
        while True:
            segment = self._next_segment
            self._next_segment += 1
            path = month / (prefix + (f'.{segment}' if segment else '') + '.log')
            if _linked(path):
                raise OSError('Linked diagnostic files are not supported')
            if path.exists() and path.stat().st_size >= MAX_FILE_BYTES:
                continue
            lease = _lock(Path(str(path) + '.lock'))
            try:
                lease.acquire()
            except Timeout:
                continue
            self._lease, self._path = lease, path
            existed = path.exists()
            try:
                _append(path, b'')
                budget = _prune_locked(root, RETENTION_DAYS)
                if budget['remaining_bytes'] + budget['reserved_bytes'] > TOTAL_BYTES:
                    raise OSError('Diagnostic capacity is reserved by active writers')
            except BaseException:
                self._release()
                # Failed allocations never accumulate zero-byte segments or
                # sidecars, even when a service keeps producing output.
                if not existed and path.exists() and not _linked(path) and path.stat().st_size == 0:
                    path.unlink()
                    Path(str(path) + '.lock').unlink(missing_ok=True)
                raise
            return

    def write(self, value):
        if not isinstance(value, str):
            raise TypeError('Diagnostic output must be text')
        if not value:
            return 0
        with self._guard:
            if self.closed:
                return len(value)
            written_bytes = 0
            try:
                root = _root(self.data_root, create=True)
                with _lock(root / '.runtime-logs.lock', timeout=.25):
                    _directory(root)
                    day = _today()
                    if self._path is None or day != self._day:
                        self._select(root, day)
                    for offset in range(0, len(value), 16384):
                        payload = value[offset:offset + 16384].encode('utf-8', errors='replace')
                        while payload:
                            _directory(self._path.parent)
                            if _linked(self._path):
                                raise OSError('Linked diagnostic files are not supported')
                            available = MAX_FILE_BYTES - self._path.stat().st_size
                            end = min(available, len(payload))
                            # Never split a codepoint across files; each segment
                            # must be independently readable as UTF-8.
                            while end > 0 and end < len(payload) and payload[end] & 0xC0 == 0x80:
                                end -= 1
                            if end <= 0:
                                self._select(root, day)
                                continue
                            _append(self._path, payload[:end])
                            written_bytes += end
                            payload = payload[end:]
                self.last_error = None
            except (OSError, Timeout) as exc:
                # The caller/pipe drain continues without an unbounded retry
                # buffer. Keep only counters and a short diagnostic reason.
                self.dropped_bytes += max(0, len(value.encode('utf-8', errors='replace')) - written_bytes)
                self.last_error = str(exc)[:300]
            return len(value)

    def close(self):
        with self._guard:
            if not self.closed:
                self._release()
                super().close()


class RuntimeLogHandler(logging.StreamHandler):
    _school_watcher_managed_logs = True

    def __init__(self, writer):
        super().__init__(writer)

    def close(self):
        try:
            self.stream.close()
        finally:
            super().close()


class _OutputCollector:
    def __init__(self, process, data_root, role):
        self.writer = ManagedLogWriter(data_root, role)
        self._pipe = process.stdout
        self.last_error = None
        if self._pipe is None:
            raise ValueError('Captured processes require stdout=PIPE')
        self._thread = threading.Thread(target=self._drain, name=f'diagnostics-{role}', daemon=True)
        self._thread.start()

    def _drain(self):
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        try:
            while True:
                chunk = getattr(self._pipe, 'read1', self._pipe.read)(65536)
                if not chunk:
                    break
                self._record(decoder.decode(chunk))
            self._record(decoder.decode(b'', final=True))
        except (OSError, ValueError) as exc:
            self.last_error = str(exc)[:300]
        finally:
            self._pipe.close()
            self.writer.close()

    def _record(self, text):
        try:
            self.writer.write(text)
        except Exception as exc:
            # A diagnostic failure must never stop pipe consumption, including
            # an unexpected sink implementation error rather than just ENOSPC.
            self.last_error = str(exc)[:300]
            self.writer.dropped_bytes += len(text.encode('utf-8', errors='replace'))

    def close(self, timeout=1):
        """Join briefly; a live pipe remains open until the child actually exits."""
        self._thread.join(max(0, timeout))
        return not self._thread.is_alive()


def capture_process_output(process, data_root, role):
    return _OutputCollector(process, data_root, role)


def _inherited_pipe(number):
    """Recover windowed-executable streams without taking ownership of std handles."""
    descriptor = None
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            import msvcrt
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetStdHandle.argtypes = [wintypes.DWORD]
            kernel.GetStdHandle.restype = wintypes.HANDLE
            kernel.GetFileType.argtypes = [wintypes.HANDLE]
            kernel.GetFileType.restype = wintypes.DWORD
            kernel.GetCurrentProcess.restype = wintypes.HANDLE
            kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.HANDLE,
                                              ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                                              wintypes.BOOL, wintypes.DWORD]
            kernel.DuplicateHandle.restype = wintypes.BOOL
            handle = kernel.GetStdHandle((-11 if number == 1 else -12) & 0xffffffff)
            if not handle or kernel.GetFileType(handle) != 3:  # FILE_TYPE_PIPE
                return None
            duplicate = wintypes.HANDLE()
            current = kernel.GetCurrentProcess()
            if not kernel.DuplicateHandle(current, handle, current, ctypes.byref(duplicate), 0, False, 2):
                return None
            try:
                descriptor = msvcrt.open_osfhandle(duplicate.value, os.O_WRONLY | os.O_BINARY)
            except OSError:
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                kernel.CloseHandle(duplicate)
                raise
        else:
            if not stat.S_ISFIFO(os.fstat(number).st_mode):
                return None
            descriptor = os.dup(number)
        return io.TextIOWrapper(os.fdopen(descriptor, 'wb', buffering=0), encoding='utf-8',
                                errors='replace', line_buffering=True, write_through=True)
    except (OSError, ValueError):
        if descriptor is not None:
            os.close(descriptor)
        return None


def configure_runtime_logging(data_root, role):
    """Configure the app sink or recover a service's inherited captured pipe."""
    captured = os.environ.get('WATCHER_CAPTURED_LOGS') == '1'
    replace_stdout = sys.stdout is None or isinstance(sys.stdout, ManagedLogWriter)
    replace_stderr = sys.stderr is None or isinstance(sys.stderr, ManagedLogWriter)
    if captured:
        if replace_stdout:
            sys.stdout = _inherited_pipe(1)
        if replace_stderr:
            sys.stderr = _inherited_pipe(2)
        target = sys.stderr or sys.stdout
    else:
        target = None
    if target is None:
        target = ManagedLogWriter(data_root, role)
        handler = RuntimeLogHandler(target)
        if replace_stdout:
            sys.stdout = target
        if replace_stderr:
            sys.stderr = target
    else:
        handler = logging.StreamHandler(target)
        handler._school_watcher_managed_logs = True
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s'))
    root_logger = logging.getLogger()
    for old in tuple(root_logger.handlers):
        if getattr(old, '_school_watcher_managed_logs', False):
            root_logger.removeHandler(old)
            old.close()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)
    prune_runtime_logs(data_root)
    return handler
