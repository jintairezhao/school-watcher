"""Drain owned services before verified copying; retain the old profile on failure."""
import logging
import threading
import time

from filelock import FileLock, Timeout

from desktop.locations import apply_changes, effective_locations, validate_locations
from desktop.maintenance import Maintenance
from desktop.runtime import local_json


class MigrationCancelled(Exception):
    pass


class Migration:
    def __init__(self, runtime, values, *, timeout=60):
        self.runtime, self.values, self.timeout = runtime, dict(values), timeout
        self.control = Maintenance(runtime.data_dir)
        self.cancelled = threading.Event()
        self.done = threading.Event()
        self.restart = False
        self.lock = threading.Lock()
        self.copied = 0
        self._state = dict(phase='preparing', step=0, message='正在准备迁移…', detail='', can_cancel=True)

    def state(self):
        with self.lock:
            return {**self._state, 'done': self.done.is_set(), 'restart': self.restart}

    def update(self, **values):
        with self.lock:
            self._state.update(values)

    def cancel(self):
        with self.lock:
            if self._state['can_cancel']:
                self.cancelled.set()
                self._state.update(message='正在取消…', can_cancel=False)
        return self.state()

    def _wait(self, predicate, *, cancellable=True):
        deadline = time.monotonic() + self.timeout
        while True:
            if cancellable and self.cancelled.is_set():
                raise MigrationCancelled()
            if predicate():
                return
            if time.monotonic() >= deadline:
                raise RuntimeError('等待保存超时，本次未迁移。请稍后重试。')
            time.sleep(.2)

    def _paused(self):
        self.control.send('pause')
        worker = self.runtime.processes.get('worker')
        if worker is None or worker.poll() is not None:
            raise RuntimeError('抓取服务已停止，请重新打开应用后重试。')
        state = self.control.status()
        owned = bool(state and state.get('pid') == worker.pid)
        if state and not owned:
            # Windows virtualenv launchers own a separate interpreter process.
            import psutil
            try:
                owned = state.get('pid') in {child.pid for child in psutil.Process(worker.pid).children(recursive=True)}
            except psutil.NoSuchProcess:
                pass
        if state and owned:
            count = state.get('active', 0)
            self.update(detail=f'正在保存 {count} 项进行中的任务' if count else '抓取进度已保存')
            return state.get('phase') == 'paused' and count == 0
        return False

    def _web_idle(self):
        self.control.send('pause')
        info = local_json(self.runtime.address + '/_desktop/health', self.control.token, timeout=2)
        return info.get('maintenance') is True and info.get('active_requests') == 0

    def _resume(self):
        self.control.send('resume')
        worker = self.runtime.processes.get('worker')
        if worker and worker.poll() is None:
            # If pause was never seen, there is no acknowledgement to wait for.
            state = self.control.status()
            if state and state.get('phase') in ('paused', 'pausing'):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    state = self.control.status()
                    if state and state.get('phase') == 'running':
                        break
                    time.sleep(.1)
                else:
                    raise RuntimeError('服务恢复超时，请重新打开应用。')
        self.runtime.resume_worker()

    def progress(self, phase, name=''):
        if phase == 'copied':
            self.copied += 1
        self.update(message='正在迁移并校验数据…',
                    detail=f'已校验 {self.copied} 个文件' + (f' · {name}' if name else ''))

    def run(self):
        entered = False
        transfer = None
        try:
            validate_locations(self.values, self.runtime.data_dir)
            folder = effective_locations(self.runtime.data_dir)['backups'] / '.transfers'
            folder.mkdir(parents=True, exist_ok=True)
            transfer = FileLock(str(folder / 'operation.lock'), timeout=0)
            transfer.acquire()
            if self.cancelled.is_set():
                raise MigrationCancelled()
            self.runtime.begin_maintenance()
            entered = True
            self.update(phase='pausing', message='正在暂停抓取并保存进度…', step=0)
            self._wait(self._paused)
            self.update(detail='正在等待其他操作完成')
            self._wait(self._web_idle)
            with self.lock:
                if self.cancelled.is_set():
                    raise MigrationCancelled()
                self._state.update(phase='stopping', step=1, message='正在关闭后台服务…',
                                   detail='', can_cancel=False)
            # From this point an error restarts the old profile, never a half-copy.
            self.restart = True
            self.control.send('stop')
            worker = self.runtime.processes['worker']
            worker.wait(timeout=10)
            if worker.returncode != 0:
                raise RuntimeError('抓取服务未正常退出，本次未迁移。')
            self.runtime.close(strict=True)
            self.update(phase='copying', step=2, message='正在迁移并校验数据…')
            apply_changes(self.values, source=self.runtime.data_dir, progress=self.progress)
            self.update(phase='complete', step=3, message='迁移完成', detail='重新打开后继续未完成的抓取')
        except MigrationCancelled:
            self.update(phase='cancelled', message='已取消迁移', detail='继续使用原来的文件位置', can_cancel=False)
        except Timeout:
            self.update(phase='error', message='暂时无法迁移', detail='导入或导出正在进行，请完成后重试。', can_cancel=False)
        except Exception as exc:
            logging.exception('Location migration stopped; original data retained')
            self.update(phase='error', message='迁移未完成', detail=str(exc), can_cancel=False)
        finally:
            if entered and not self.restart:
                try:
                    self._resume()
                except Exception:
                    logging.exception('Could not resume worker; restart original profile')
                    self.restart = True
                    self.update(detail='原数据仍保留，重新打开后继续。')
            if transfer and transfer.is_locked:
                transfer.release()
            self.control.clear()
            self.done.set()
