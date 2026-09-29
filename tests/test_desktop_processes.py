"""Real process lifetime regression tests; all children use isolated profiles."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import psutil

ROOT = Path(__file__).resolve().parents[1]
CHILD = '''
import json, os, subprocess, sys, time
from pathlib import Path
from desktop.processes import guard_service
guard_service()
child = subprocess.Popen([sys._base_executable, '-c', 'import time; time.sleep(120)'])
Path(sys.argv[1] + '.tmp').write_text(json.dumps([os.getpid(), child.pid]))
Path(sys.argv[1] + '.tmp').replace(sys.argv[1])
time.sleep(120)
'''
PARENT = '''
import sys, time
from pathlib import Path
from unittest.mock import patch
from desktop.runtime import DesktopRuntime
runtime = DesktopRuntime(Path(sys.argv[1]) / 'data')
if sys.argv[5] == 'pipe': runtime._job.close()  # Exercise the macOS/Unix fallback on every host.
with patch('desktop.runtime.service_command', return_value=[sys.executable, sys.argv[2], sys.argv[3]]):
    service = runtime.spawn('web')
Path(sys.argv[4]).write_text(str(service.pid))
if sys.argv[5] == 'normal':
    while not Path(sys.argv[3]).exists(): time.sleep(.05)
    runtime.close()
else:
    time.sleep(120)
'''


class ServiceLifetimeTests(unittest.TestCase):
    def test_exit_race_does_not_abort_cleanup_of_other_children(self):
        from desktop.runtime import stop_process
        root, child = Mock(pid=1001), Mock(pid=1002)
        root.children.return_value = [child]
        child.terminate.side_effect = psutil.AccessDenied(child.pid)
        process = Mock(pid=root.pid)
        with patch('psutil.Process', return_value=root), \
                patch('psutil.wait_procs', side_effect=[([root, child], []), ([], [])]):
            stop_process(process)
        root.terminate.assert_called_once()
        child.terminate.assert_called_once()
        process.wait.assert_called_once()

    def exercise(self, mode):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            child_file, parent_file = folder / 'child.py', folder / 'parent.py'
            child_file.write_text(CHILD)
            parent_file.write_text(PARENT)
            ready, started = folder / 'ready.json', folder / 'started'
            env = os.environ.copy()
            env.update(PYTHONPATH=os.pathsep.join((str(ROOT), env.get('PYTHONPATH', ''))),
                       WATCHER_LOCATION_SETTINGS=str(folder / 'locations.json'))
            processes = []
            with (folder / 'parent.log').open('w') as log:
                parent = subprocess.Popen([sys.executable, str(parent_file), str(folder),
                    str(child_file), str(ready), str(started), mode], env=env, stdout=log, stderr=log,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            try:
                until = time.monotonic() + 15
                while not ready.exists() and parent.poll() is None and time.monotonic() < until:
                    time.sleep(.05)
                self.assertTrue(ready.exists(), (folder / 'parent.log').read_text())
                pids = set(json.loads(ready.read_text())) | {int(started.read_text())}
                for pid in pids:
                    try: processes.append(psutil.Process(pid))
                    except psutil.NoSuchProcess: pass
                if mode in ('crash', 'pipe'):
                    parent.kill()  # No Python finally / atexit handlers get to run.
                parent.wait(timeout=15)
                _, alive = psutil.wait_procs(processes, timeout=10)
                self.assertFalse(alive, f'Background processes survived {mode}: {[p.pid for p in alive]}')
            finally:
                if parent.poll() is None:
                    processes.extend(psutil.Process(parent.pid).children(recursive=True))
                    parent.kill(); parent.wait(timeout=5)
                for process in processes:
                    try: process.kill()
                    except psutil.NoSuchProcess: pass
                psutil.wait_procs(processes, timeout=5)

    def test_force_killing_app_removes_service_and_grandchild(self):
        self.exercise('crash')

    def test_normal_close_removes_service_and_grandchild(self):
        self.exercise('normal')

    def test_parent_pipe_also_cleans_up_without_windows_job_support(self):
        self.exercise('pipe')

    def test_closed_startup_pipe_never_runs_service(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'started'
            env = os.environ.copy()
            env['WATCHER_SERVICE_GUARD'] = '1'
            result = subprocess.run([sys.executable, '-c',
                'from desktop.processes import guard_service; guard_service(); '
                'from pathlib import Path; import sys; Path(sys.argv[1]).touch()', str(marker)],
                input=b'', env=env, capture_output=True, timeout=10, cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())

    def test_short_lived_service_can_finish_while_parent_pipe_is_open(self):
        env = os.environ.copy()
        env['WATCHER_SERVICE_GUARD'] = '1'
        process = subprocess.Popen([sys.executable, '-c',
            'from desktop.processes import guard_service; guard_service()'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=ROOT)
        try:
            process.stdin.write(b'start\n'); process.stdin.flush()
            code = process.wait(timeout=10)
            self.assertEqual(code, 0, process.stderr.read().decode(errors='replace'))
        finally:
            if process.poll() is None: process.kill(); process.wait(timeout=5)
            process.stdin.close(); process.stdout.close(); process.stderr.close()


class UninstallerTests(unittest.TestCase):
    def test_uninstall_stops_only_this_installation_and_service_children(self):
        from desktop.uninstall import stop_installation
        target = str(ROOT / 'fixture-installation' / 'SchoolWatcher.exe')
        def process(pid, path, args=()):
            item = Mock(pid=pid, info={'pid': pid, 'exe': path, 'cmdline': list(args)})
            item.children.return_value = []
            return item
        app = process(1001, target)
        service = process(1002, target, ['--service', 'web', '--data-dir', str(ROOT / 'fixture-data')])
        browser = process(1003, 'owned-browser.exe')
        installer = process(1004, 'unins000.exe')
        unrelated = process(1005, str(ROOT / 'another-installation' / 'SchoolWatcher.exe'))
        helper = process(os.getpid(), target, ['--stop-for-uninstall'])
        app.children.return_value = [installer, helper]
        service.children.return_value = [browser]
        owned = [app, service, browser]
        request = Mock()
        with patch('psutil.process_iter', return_value=[app, service, unrelated, helper]), \
                patch('psutil.wait_procs', side_effect=[([], owned), (owned, []), ([], [])]):
            stop_installation(target, request)
        request.assert_called_once_with(str(ROOT / 'fixture-data'))
        for item in owned: item.terminate.assert_called_once()
        for item in (installer, unrelated, helper): item.terminate.assert_not_called()
        app.children.assert_not_called()

    def test_uninstall_refuses_to_continue_if_a_process_survives(self):
        from desktop.uninstall import stop_installation
        target = str(ROOT / 'fixture-installation' / 'SchoolWatcher.exe')
        process = Mock(pid=1001, info={'pid': 1001, 'exe': target, 'cmdline': []})
        process.terminate.side_effect = psutil.AccessDenied(process.pid)
        process.kill.side_effect = psutil.AccessDenied(process.pid)
        with patch('psutil.process_iter', return_value=[process]), \
                patch('psutil.wait_procs', return_value=([], [process])):
            with self.assertRaisesRegex(RuntimeError, '卸载已停止'):
                stop_installation(target)

    def test_requires_uninstaller_and_its_matching_data_beside_running_app(self):
        from desktop.uninstall import uninstaller_path
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app = root / 'SchoolWatcher.exe'; app.touch()
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(app)), patch('sys.platform', 'win32'):
                self.assertIsNone(uninstaller_path())
                (root / 'unins000.exe').touch()
                self.assertIsNone(uninstaller_path())
                (root / 'unins000.dat').touch()
                self.assertEqual(uninstaller_path(), root / 'unins000.exe')

    def test_source_mode_does_not_offer_to_uninstall_python(self):
        from desktop.uninstall import uninstaller_path
        with patch('sys.frozen', False, create=True):
            self.assertIsNone(uninstaller_path())

    def test_source_mode_refuses_uninstall_process_cleanup(self):
        from desktop.entry import main
        with patch('sys.frozen', False, create=True), patch('desktop.uninstall.stop_installation') as stop:
            with self.assertRaisesRegex(RuntimeError, 'Windows 安装版'):
                main(['--stop-for-uninstall'])
            stop.assert_not_called()


if __name__ == '__main__':
    unittest.main()
