"""Physical runtime logs are bounded, dated and independent of application data."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from filelock import FileLock

from desktop import runtime_logs


class RuntimeLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.day = patch.object(runtime_logs, '_today', return_value=date(2026, 10, 31)); self.day_mock = self.day.start()
        self.addCleanup(self.day.stop)

    def files(self):
        return sorted((self.root / 'logs').glob('*/*.log'))

    def test_rolls_by_local_day_and_month_without_touching_legacy_files(self):
        legacy = self.root / 'desktop.log'; legacy.write_bytes(b'keep legacy intact')
        with runtime_logs.ManagedLogWriter(self.root, 'app') as writer:
            writer.write('October\n')
            self.day_mock.return_value = date(2026, 11, 1); writer.write('November\n')
        self.assertEqual([p.parent.name for p in self.files()], ['2026-10','2026-11'])
        self.assertTrue(self.files()[0].name.startswith('2026-10-31-app-'))
        self.assertEqual(legacy.read_bytes(), b'keep legacy intact')

    def test_large_multibyte_records_split_into_valid_utf8_bounded_segments(self):
        value = '通知读写测试🙂' * 2000
        with patch.object(runtime_logs, 'MAX_FILE_BYTES', 127), patch.object(runtime_logs, 'TOTAL_BYTES', 1024 * 1024):
            with runtime_logs.ManagedLogWriter(self.root, 'worker') as writer:
                self.assertEqual(writer.write(value), len(value))
            files = sorted(self.files(), key=runtime_logs._segment_order)
            self.assertGreater(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 127 for p in files))
            self.assertEqual(''.join(p.read_text(encoding='utf8') for p in files), value)

    def test_threads_share_writer_without_truncation_or_interleaved_records(self):
        with runtime_logs.ManagedLogWriter(self.root, 'app') as writer:
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda i:writer.write(f'record-{i}\n'), range(200)))
        lines = '\n'.join(p.read_text(encoding='utf8') for p in self.files()).splitlines()
        self.assertEqual(set(lines), {f'record-{i}' for i in range(200)})

    def test_separate_processes_capture_all_records_without_shared_handle_rotation(self):
        code = "from desktop.runtime_logs import ManagedLogWriter; import sys; w=ManagedLogWriter(sys.argv[1],'worker'); [w.write(str(i)+'\\n') for i in range(100)]; w.close()"
        children = [subprocess.Popen([sys.executable,'-c',code,str(self.root)], cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) for _ in range(3)]
        for child in children:
            _, error = child.communicate(timeout=15); self.assertEqual(child.returncode, 0, error)
        real_files = sorted((self.root / 'logs').glob('*/*.log'))
        self.assertEqual(len(real_files), 3)
        for path in real_files:self.assertEqual(len(path.read_text().splitlines()), 100)

    def test_prune_uses_calendar_retention_cap_and_live_file_locks(self):
        with patch.object(runtime_logs, 'MAX_FILE_BYTES', 64), patch.object(runtime_logs, 'TOTAL_BYTES', 256):
            active = runtime_logs.ManagedLogWriter(self.root, 'app'); active.write('active')
            active_path = self.files()[0]
            month = self.root / 'logs' / '2026-09'; month.mkdir()
            expired = month / '2026-09-01-worker-12345.log'; expired.write_bytes(b'old')
            legacy = month / 'personal-note.log'; legacy.write_bytes(b'private')
            current = self.root / 'logs' / '2026-10'
            for i in range(6):(current / f'2026-10-20-worker-{20000+i}.log').write_bytes(b'x' * 64)
            result = runtime_logs.prune_runtime_logs(self.root)
            self.assertFalse(expired.exists()); self.assertTrue(active_path.exists()); self.assertTrue(legacy.exists())
            self.assertGreater(result['files'], 0)
            self.assertEqual(result['protected_files'], 1)
            self.assertLessEqual(result['remaining_bytes'], 256)
            active.close()

    def test_live_writer_in_other_process_is_not_pruned(self):
        month = self.root / 'logs' / '2026-10'; month.mkdir(parents=True)
        path = month / '2026-10-01-worker-99999.log'; path.write_bytes(b'live')
        with FileLock(str(path) + '.lock'):
            with patch.object(runtime_logs, 'TOTAL_BYTES', 1):
                result = runtime_logs.prune_runtime_logs(self.root)
        self.assertTrue(path.exists()); self.assertEqual(result['protected_files'], 1)

    def test_all_active_writers_cannot_exceed_cap_or_accumulate_empty_segments(self):
        with patch.object(runtime_logs, 'MAX_FILE_BYTES', 64), patch.object(runtime_logs, 'TOTAL_BYTES', 128):
            writers = [runtime_logs.ManagedLogWriter(self.root, role) for role in ('app', 'web', 'worker')]
            try:
                writers[0].write('first'); writers[1].write('second')
                for _ in range(30):writers[2].write('excess output')
                self.assertEqual(len(self.files()), 2)
                self.assertEqual(len(list((self.root / 'logs').glob('*/*.log.lock'))), 2)
                self.assertGreater(writers[2].dropped_bytes, 0)
                writers[0].write('x' * 59);writers[1].write('y' * 58)
                self.assertEqual(sum(path.stat().st_size for path in self.files()), 128)
                self.assertTrue(any('first' in path.read_text() for path in self.files()))
                self.assertTrue(any('second' in path.read_text() for path in self.files()))
            finally:
                for writer in writers:writer.close()

    def test_invalid_roles_and_linked_directories_are_refused(self):
        for role in ('../outside', 'web/child', '', 'role with spaces'):
            with self.assertRaises(ValueError):runtime_logs.ManagedLogWriter(self.root, role)
        outside = self.root / 'outside'; outside.mkdir(); logs = self.root / 'logs'
        try: logs.symlink_to(outside, target_is_directory=True)
        except OSError:self.skipTest('Link creation unavailable')
        writer = runtime_logs.ManagedLogWriter(self.root, 'app'); writer.write('must not escape'); writer.close()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(runtime_logs.prune_runtime_logs(self.root)['files'], 0)

    @unittest.skipUnless(os.name == 'nt', 'Windows junction regression')
    def test_junction_month_cannot_escape_on_write_or_prune(self):
        outside = self.root / 'outside'; outside.mkdir()
        original = outside / '2026-10-31-app-99999.log'; original.write_text('private')
        logs = self.root / 'logs'; logs.mkdir()
        month = logs / '2026-10'
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(month), str(outside)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        with runtime_logs.ManagedLogWriter(self.root, 'app') as writer:
            writer.write('cannot escape')
            self.assertGreater(writer.dropped_bytes, 0)
        with patch.object(runtime_logs, 'TOTAL_BYTES', 1):
            self.assertEqual(runtime_logs.prune_runtime_logs(self.root)['files'], 0)
        self.assertEqual(list(outside.iterdir()), [original])
        self.assertNotIn(original, runtime_logs.managed_log_files(self.root))
        linked_data = self.root / 'linked-data'; linked_data.mkdir()
        result = subprocess.run(['cmd', '/c', 'mklink', '/J', str(linked_data / 'logs'), str(outside)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, result.stderr)
        with runtime_logs.ManagedLogWriter(linked_data, 'app') as writer:
            writer.write('cannot escape the root either')
            self.assertGreater(writer.dropped_bytes, 0)
        self.assertEqual(runtime_logs.prune_runtime_logs(linked_data)['files'], 0)
        self.assertEqual(runtime_logs.managed_log_files(linked_data), [])
        self.assertEqual(list(outside.iterdir()), [original])

    def test_uninstall_enumeration_excludes_unknown_and_invalid_dates(self):
        with runtime_logs.ManagedLogWriter(self.root, 'app') as writer:writer.write('managed')
        month = self.root / 'logs' / '2026-10'
        (month / 'notes.log').write_text('private')
        (month / '2026-10-32-app-100.log').write_text('invalid day')
        (month / '2026-09-01-app-100.log').write_text('wrong month')
        result = runtime_logs.managed_log_files(self.root)
        self.assertEqual(sum(path.suffix == '.log' for path in result), 1)
        self.assertTrue(all(path.name not in ('notes.log', '2026-10-32-app-100.log',
            '2026-09-01-app-100.log') for path in result))

    def test_partial_disk_failure_does_not_escape_to_callers(self):
        with runtime_logs.ManagedLogWriter(self.root, 'worker') as writer:
            with patch.object(runtime_logs, '_append', side_effect=OSError('disk full')):
                self.assertEqual(writer.write('runtime must continue'), 21)
            self.assertGreater(writer.dropped_bytes, 0)
            writer.write('disk recovered')
        self.assertIn('disk recovered', ''.join(p.read_text() for p in self.files()))

    def test_capture_drains_large_no_newline_output_even_when_sink_fails(self):
        code = "import os; [os.write(1,b'x'*65536) for _ in range(64)]"
        process = subprocess.Popen([sys.executable,'-c',code], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        with patch.object(runtime_logs, '_append', side_effect=OSError('disk full')):
            collector = runtime_logs.capture_process_output(process, self.root, 'probe')
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(collector.close(timeout=3))
        self.assertGreater(collector.writer.dropped_bytes, 0)

    def test_unexpected_sink_failure_still_drains_child_pipe(self):
        process = subprocess.Popen([sys.executable,'-c',"import os; [os.write(1,b'x'*65536) for _ in range(32)]"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        with patch.object(runtime_logs.ManagedLogWriter, 'write', side_effect=RuntimeError('sink failed')):
            collector = runtime_logs.capture_process_output(process, self.root, 'probe')
            self.assertEqual(process.wait(timeout=10), 0)
            self.assertTrue(collector.close(timeout=3))
        self.assertEqual(collector.last_error, 'sink failed')
        self.assertGreater(collector.writer.dropped_bytes, 0)

    def test_capture_close_is_bounded_without_closing_a_live_childs_pipe(self):
        process = subprocess.Popen([sys.executable,'-c','import time;time.sleep(2)'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        collector = runtime_logs.capture_process_output(process, self.root, 'probe')
        try:
            start=time.monotonic(); self.assertFalse(collector.close(timeout=.01))
            self.assertLess(time.monotonic()-start, .5)
            self.assertFalse(process.stdout.closed)
        finally:
            process.kill();process.wait(timeout=3);self.assertTrue(collector.close(timeout=3))

    def test_windowed_child_restores_inherited_pipe_instead_of_opening_flat_logs(self):
        code = "import sys,logging;from desktop.runtime_logs import configure_runtime_logging;sys.stdout=None;sys.stderr=None;configure_runtime_logging(sys.argv[1],'web');logging.warning('BOOT_CAPTURE_中文');print('PRINT_CAPTURE',flush=True)"
        environment = os.environ.copy();environment['WATCHER_CAPTURED_LOGS']='1'
        process = subprocess.Popen([sys.executable,'-c',code,str(self.root)], cwd=Path(__file__).resolve().parents[1],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        collector = runtime_logs.capture_process_output(process, self.root, 'web')
        self.assertEqual(process.wait(timeout=10), 0);self.assertTrue(collector.close(timeout=3))
        text=''.join(p.read_text(encoding='utf8') for p in self.files())
        self.assertIn('BOOT_CAPTURE_中文',text);self.assertIn('PRINT_CAPTURE',text)
        self.assertEqual(list(self.root.glob('*.log')), [])

    def test_boot_failure_traceback_reaches_managed_capture(self):
        process = subprocess.Popen([sys.executable,'-c',"raise RuntimeError('CHILD_BOOT_FAILURE')"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
        collector=runtime_logs.capture_process_output(process,self.root,'migrate')
        self.assertNotEqual(process.wait(timeout=5),0);self.assertTrue(collector.close(timeout=3))
        self.assertIn('CHILD_BOOT_FAILURE',''.join(p.read_text() for p in self.files()))

    def test_app_streams_stay_usable_when_logging_is_configured_twice(self):
        previous = logging.getLogger().handlers[:]
        try:
            with patch.object(sys, 'stdout', None), patch.object(sys, 'stderr', None), \
                    patch.dict(os.environ, {'WATCHER_CAPTURED_LOGS': '0'}):
                first = runtime_logs.configure_runtime_logging(self.root, 'app')
                second = runtime_logs.configure_runtime_logging(self.root, 'app')
                self.assertIsNot(first.stream, second.stream)
                self.assertTrue(first.stream.closed)
                print('app stdout stays usable', flush=True)
                logging.warning('one handler only')
                second.close()
        finally:
            logging.getLogger().handlers[:] = previous
        content = ''.join(p.read_text() for p in self.files())
        self.assertIn('app stdout stays usable', content)
        self.assertEqual(content.count('one handler only'), 1)

    def test_browser_driver_output_is_captured_without_flat_file_or_network(self):
        from desktop.browser import install_chromium
        script = self.root / 'fake-driver.py'
        script.write_text("import sys; print('DRIVER_DOWNLOAD_OUTPUT',flush=True);sys.exit(7)")
        with patch('playwright._impl._driver.compute_driver_executable', return_value=(sys.executable, str(script))):
            with self.assertRaises(subprocess.CalledProcessError) as error:
                install_chromium(self.root)
        self.assertEqual(error.exception.returncode, 7)
        self.assertIn('DRIVER_DOWNLOAD_OUTPUT', ''.join(p.read_text() for p in self.files()))
        self.assertFalse((self.root / 'desktop-browser-download.log').exists())


if __name__ == '__main__':unittest.main()
