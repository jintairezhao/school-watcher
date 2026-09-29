"""An update keeps installation paths and starts only after controlled shutdown."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from desktop.updater import Update, UpdateError
from desktop.window import UpdateAPI


class UpdateInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.package = self.root / '更新包.exe'
        self.package.write_bytes(b'installer fixture')
        self.update = Update('9.0.0', self.package.name, 'https://example.invalid',
                             self.package.stat().st_size, hashlib.sha256(self.package.read_bytes()).hexdigest(), '')

    def test_installed_update_preserves_program_path_and_skips_wizard(self):
        from desktop.install_update import prepare_install, install_command
        program = self.root / '自选程序目录'
        with patch('desktop.install_update.sys.platform', 'win32'), \
                patch('desktop.install_update.uninstaller_path', return_value=program / 'unins000.exe'):
            request = prepare_install(self.package, self.update)
            command = install_command(request)
        self.assertIn('/UPDATE', command)
        self.assertIn('/SILENT', command)
        self.assertIn('/DIR=' + str(program), command)
        self.assertTrue(any(part.startswith('/WATCHERPID=') for part in command))
        self.assertFalse(any('data-dir' in part or 'cache-dir' in part for part in command))

    def test_explicit_relocation_keeps_interactive_directory_selection(self):
        from desktop.install_update import prepare_install, install_command
        with patch('desktop.install_update.sys.platform', 'win32'), \
                patch('desktop.install_update.uninstaller_path', return_value=self.root / 'unins000.exe'):
            command = install_command(prepare_install(self.package, self.update, change_locations=True))
        self.assertIn('/CHANGELOCATIONS', command)
        self.assertNotIn('/SILENT', command)
        self.assertNotIn('/UPDATE', command)

    def test_portable_app_does_not_silently_install_over_another_copy(self):
        from desktop.install_update import prepare_install, install_command
        with patch('desktop.install_update.sys.platform', 'win32'), \
                patch('desktop.install_update.uninstaller_path', return_value=None):
            command = install_command(prepare_install(self.package, self.update))
        self.assertIn('/CHANGELOCATIONS', command)
        self.assertNotIn('/SILENT', command)
        self.assertFalse(any(part.startswith('/DIR=') for part in command))

    def test_update_is_queued_instead_of_launched_while_services_are_running(self):
        queued, quit_app = Mock(return_value=True), Mock(return_value=True)
        api = UpdateAPI(self.root, quit_app, install_handoff=queued)
        api._path, api._update = self.package, self.update
        api._window = Mock()
        api._window.create_confirmation_dialog.return_value = True
        with patch('desktop.install_update.uninstaller_path', return_value=self.root / 'unins000.exe'), \
                patch('desktop.install_update.sys.platform', 'win32'), \
                patch('subprocess.Popen') as spawn:
            self.assertEqual(api.install()['phase'], 'installing')
        queued.assert_called_once()
        spawn.assert_not_called()

    def test_cancel_does_not_queue_an_install(self):
        queued = Mock()
        api = UpdateAPI(self.root, Mock(), install_handoff=queued)
        api._path, api._update = self.package, self.update
        api._window = Mock()
        api._window.create_confirmation_dialog.return_value = False
        api.install()
        queued.assert_not_called()

    def test_installer_is_reverified_after_shutdown(self):
        from desktop.install_update import prepare_install, launch_install
        request = prepare_install(self.package, self.update)
        self.package.write_bytes(b'replaced installer')
        with patch('subprocess.Popen') as spawn, self.assertRaises(UpdateError):
            launch_install(request)
        spawn.assert_not_called()

    def test_entry_releases_services_and_profile_lock_before_launching_update(self):
        from desktop.entry import main
        events = []
        runtime = SimpleNamespace(requested_update='queued-install', requested_uninstaller=None,
                                  restart_requested=False, close=Mock(side_effect=lambda **kw: events.append(('close', kw))))
        lock = Mock()
        lock.release.side_effect = lambda: events.append(('unlock', None))
        with patch('desktop.entry.configure', return_value=self.root), \
                patch.dict(os.environ), patch('desktop.entry.logging.FileHandler'), patch('desktop.entry.logging.basicConfig'), \
                patch('desktop.entry.DesktopRuntime', return_value=runtime), \
                patch('desktop.window.run_window'), patch('filelock.FileLock', return_value=lock), \
                patch('desktop.install_update.launch_install', side_effect=lambda request: events.append(('install', request))):
            main(['--data-dir', str(self.root)])
        self.assertEqual(events, [('close', {'strict': True}), ('unlock', None), ('install', 'queued-install')])

    def test_service_shutdown_failure_prevents_installation(self):
        from desktop.entry import main
        runtime = SimpleNamespace(requested_update='queued-install', close=Mock(side_effect=RuntimeError('still running')))
        with patch('desktop.entry.configure', return_value=self.root), \
                patch.dict(os.environ), patch('desktop.entry.logging.FileHandler'), patch('desktop.entry.logging.basicConfig'), \
                patch('desktop.entry.DesktopRuntime', return_value=runtime), \
                patch('desktop.window.run_window'), patch('filelock.FileLock'), \
                patch('desktop.install_update.launch_install') as launch, self.assertRaises(RuntimeError):
            main(['--data-dir', str(self.root)])
        launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
