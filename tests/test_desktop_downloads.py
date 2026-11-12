"""Attachment exports follow personal data, independently of Windows Downloads."""
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from desktop.downloads import choose_destination, install_download_location


class Event:
    def __init__(self, *handlers):
        self.handlers = list(handlers)

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def __isub__(self, handler):
        self.handlers.remove(handler)
        return self


class DesktopDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name) / '个人数据'
        self.original = Mock()
        self.original_new_window = Mock()
        self.core = SimpleNamespace(DownloadStarting=Event(self.original), Profile=SimpleNamespace(),
            NewWindowRequested=Event(self.original_new_window), NavigationStarting=Event(),
            NavigationCompleted=Event(), Navigate=Mock())
        self.view = SimpleNamespace(CoreWebView2=self.core, CoreWebView2InitializationCompleted=Event())
        self.window = SimpleNamespace(native=SimpleNamespace(webview=self.view,
            browser=SimpleNamespace(on_download_starting=self.original, on_new_window_request=self.original_new_window),
            Invoke=lambda action: action()), get_current_url=lambda: 'http://127.0.0.1:9000/announcement/1',
            events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)))
        self.system = patch.dict('sys.modules', {'System': SimpleNamespace(Action=lambda action: action)})
        self.system.start()
        self.addCleanup(self.system.stop)

    def test_real_download_event_uses_personal_data_directory_and_original_filename(self):
        install_download_location(self.window, self.data)
        folder = self.data / 'attachments'
        self.assertTrue(folder.is_dir())
        self.assertEqual(self.core.Profile.DefaultDownloadFolderPath, str(folder))
        self.assertNotIn(self.original, self.core.DownloadStarting.handlers)
        args = SimpleNamespace(ResultFilePath=r'C:\Users\reader\Downloads\125315.xls', Cancel=False)
        with patch('desktop.downloads.choose_destination', return_value=str(folder / '125315.xls')) as choose:
            self.core.DownloadStarting.handlers[0](None, args)
        choose.assert_called_once_with(self.window, folder, '125315.xls')
        self.assertEqual(args.ResultFilePath, str(folder / '125315.xls'))
        self.original.assert_not_called()

    def test_cancel_does_not_fall_back_to_c_drive_or_create_an_export(self):
        install_download_location(self.window, self.data)
        args = SimpleNamespace(ResultFilePath='125315.xls', Cancel=False)
        with patch('desktop.downloads.choose_destination', return_value=None):
            self.core.DownloadStarting.handlers[0](None, args)
        self.assertTrue(args.Cancel)
        self.assertEqual(list((self.data / 'attachments').iterdir()), [])

    def test_save_dialog_starts_in_personal_data_and_disposes_native_resources(self):
        folder = self.data / 'attachments'
        dialog = Mock(FileName=str(folder / '125315.xls'))
        def show(owner):
            self.assertIs(owner, self.window.native)
            self.assertEqual(dialog.InitialDirectory, str(folder))
            self.assertEqual(dialog.FileName, '125315.xls')
            dialog.FileName = str(folder / '125315.xls')
            return 'ok'
        dialog.ShowDialog.side_effect = show
        self.window.localization = {'windows.fileFilter.allFiles': '所有文件'}
        forms = SimpleNamespace(SaveFileDialog=lambda: dialog, DialogResult=SimpleNamespace(OK='ok'))
        with patch.dict('sys.modules', {'System.Windows.Forms': forms}):
            self.assertEqual(choose_destination(self.window, folder, '125315.xls'), str(folder / '125315.xls'))
        dialog.Dispose.assert_called_once()

    def test_late_native_initialization_installs_once(self):
        self.view.CoreWebView2 = None
        install_download_location(self.window, self.data)
        self.assertEqual(self.core.DownloadStarting.handlers, [self.original])
        self.view.CoreWebView2 = self.core
        ready = self.view.CoreWebView2InitializationCompleted.handlers[0]
        ready(None, SimpleNamespace(IsSuccess=False))
        self.assertEqual(self.core.DownloadStarting.handlers, [self.original])
        ready(None, SimpleNamespace(IsSuccess=True))
        ready(None, SimpleNamespace(IsSuccess=True))
        self.assertEqual(len(self.core.DownloadStarting.handlers), 1)

    def test_changing_personal_data_changes_the_default_export_directory(self):
        target = self.data.parent / '迁移后数据'
        install_download_location(self.window, target)
        self.assertEqual(self.core.Profile.DefaultDownloadFolderPath, str(target / 'attachments'))
        self.assertFalse((self.data / 'attachments').exists())

    def test_local_new_tab_links_retain_native_session_and_download_policy(self):
        install_download_location(self.window, self.data)
        url = 'http://127.0.0.1:9000/api/announcements/1/resources/' + 'a' * 64
        event = SimpleNamespace(Uri=url, Handled=False)
        self.core.NewWindowRequested.handlers[0](None, event)
        self.assertTrue(event.Handled)
        self.core.Navigate.assert_called_once_with(url)
        self.original_new_window.assert_not_called()

    def test_official_redirect_opens_browser_without_replacing_local_reader(self):
        install_download_location(self.window, self.data)
        url = 'http://127.0.0.1:9000/api/announcements/1/attachments/' + 'a' * 64
        self.core.NewWindowRequested.handlers[0](None, SimpleNamespace(Uri=url, Handled=False))
        redirect = SimpleNamespace(Uri='https://example.edu.cn/notice.pdf', IsRedirected=True, Cancel=False)
        with patch('desktop.downloads.webbrowser.open') as open_browser:
            self.core.NavigationStarting.handlers[0](None, redirect)
            open_browser.assert_called_once_with(redirect.Uri)
        self.assertTrue(redirect.Cancel)

    def test_other_links_keep_existing_browser_behavior(self):
        install_download_location(self.window, self.data)
        for url in ('https://example.edu.cn/notice.pdf',
                'http://127.0.0.1:9001/api/announcements/1/resources/' + 'a' * 64):
            event = SimpleNamespace(Uri=url, Handled=False)
            self.core.NewWindowRequested.handlers[0](None, event)
            self.assertFalse(event.Handled)
        self.assertEqual(self.original_new_window.call_count, 2)
        self.core.Navigate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
