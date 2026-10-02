"""Startup checks stay in the background and never download without a user action."""
from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from desktop.updater import Update, UpdateError
from desktop.window import UpdateAPI


class StartupUpdateTests(unittest.TestCase):
    def setUp(self):
        self.api = UpdateAPI(Path('unused-update-test-profile'), Mock())
        self.update = Update('9.0.0', 'fixture.exe', 'https://example.invalid', 100, 'a' * 64, '更新说明')

    def test_startup_is_nonblocking_once_and_does_not_download(self):
        entered, release = threading.Event(), threading.Event()
        def slow_check():
            entered.set()
            release.wait(3)
            return self.update
        with patch('desktop.window.check_update', side_effect=slow_check) as check, \
                patch('desktop.window.download_update') as download:
            try:
                thread = self.api.startup_check()
                self.assertTrue(entered.wait(2))
                self.assertTrue(thread.is_alive())
                self.assertEqual(self.api.state()['phase'], 'checking')
                self.assertIsNone(self.api.startup_check())
            finally:
                release.set()
                thread.join(3)
            check.assert_called_once()
            download.assert_not_called()
        self.assertTrue(self.api.notification()['available'])
        self.assertEqual(self.api.notification()['version'], '9.0.0')

    def test_latest_and_offline_do_not_notify(self):
        for result in (None, UpdateError('网络不可用')):
            api = UpdateAPI(Path('unused'), Mock())
            with patch('desktop.window.check_update', side_effect=[result]):
                api.startup_check().join(3)
            self.assertFalse(api.notification()['available'])

    def test_manual_check_reports_network_failure_after_silent_startup_failure(self):
        with patch('desktop.window.check_update',side_effect=UpdateError('暂时无法连接 GitHub，请检查网络后重试。')) as check:
            self.api.startup_check().join(3)
            self.assertFalse(self.api.notification()['available'])
            manual=self.api.check()
        self.assertEqual(check.call_count,2)
        self.assertEqual(manual['phase'],'error')
        self.assertIn('请检查网络后重试',manual['message'])

    def test_dismissal_survives_navigation_and_recheck_but_not_next_launch(self):
        with patch('desktop.window.check_update', return_value=self.update):
            self.api.startup_check().join(3)
            self.api.dismiss_notification()
            self.assertFalse(self.api.notification()['available'])
            self.api.check()
            self.assertFalse(self.api.notification()['available'])
            second_launch = UpdateAPI(Path('unused'), Mock())
            second_launch.startup_check().join(3)
            self.assertTrue(second_launch.notification()['available'])

    def test_startup_preserves_an_existing_manual_check_or_download(self):
        with patch('desktop.window.check_update', return_value=self.update) as check:
            self.api.check()
            self.api._state['phase'] = 'downloading'
            self.api.startup_check().join(3)
            check.assert_called_once()
            self.assertEqual(self.api.state()['phase'], 'downloading')

    def test_failed_recheck_clears_stale_release_details(self):
        with patch('desktop.window.check_update', side_effect=[self.update, UpdateError('offline')]):
            self.api.check()
            self.api.check()
        self.assertFalse(self.api.notification()['available'])
        self.assertNotIn('notes', self.api.state())
        self.assertIsNone(self.api._update)


if __name__ == '__main__':
    unittest.main()
