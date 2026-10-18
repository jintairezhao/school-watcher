"""Native dialogs share the web preference and catalog without opening a database."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from desktop.updater import Update, UpdateError
from desktop.window import UpdateAPI


class DesktopLocalizationTests(unittest.TestCase):
    def test_catalog_audit_includes_independent_native_pages(self):
        from scripts.checks.check_localization_catalog import audit
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'frontend/static/i18n').mkdir(parents=True)
            (root/'frontend/static/i18n/en.json').write_text('{}', encoding='utf8')
            (root/'desktop/ui').mkdir(parents=True)
            (root/'desktop/ui/updates.html').write_text('<h1>未登记的原生窗口文字</h1>', encoding='utf8')
            self.assertTrue(any('updates.html' in item and 'Unregistered UI text' in item for item in audit(root)['problems']))

    def test_install_confirmation_uses_current_language_without_starting_an_installer(self):
        from desktop.localization import DesktopLanguage
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            language = DesktopLanguage(Path(folder)); language.set_locale('en')
            api = UpdateAPI(Path(folder), Mock(), language=language)
            api._path, api._update, api._window = Path('fixture.exe'), Mock(), Mock()
            api._window.create_confirmation_dialog.return_value = False
            with patch('desktop.install_update.prepare_install', return_value=SimpleNamespace(in_place=True)), \
                    patch('desktop.window.sys.platform', 'win32'):
                api.install()
            title, message = api._window.create_confirmation_dialog.call_args.args
            self.assertEqual(title, 'Update School Watcher')
            self.assertIn('personal data will be preserved', message)
            self.assertNotRegex(title+message, r'[\u4e00-\u9fff]')
            self.assertEqual(api._state['phase'], 'idle')

    def test_update_states_follow_language_without_repeating_requests(self):
        from desktop.localization import DesktopLanguage
        with tempfile.TemporaryDirectory() as folder:
            language = DesktopLanguage(Path(folder))
            api = UpdateAPI(Path(folder), Mock(), language=language)
            update = Update('9.0.0', 'fixture.exe', 'https://example.invalid', 100, 'a'*64, '原始发布说明')
            with patch('desktop.window.check_update', return_value=update) as check:
                api.check()
                language.set_locale('en')
                self.assertEqual(api.state()['message'], 'New version available: 9.0.0')
                self.assertEqual(api.state()['notes'], '原始发布说明')
                language.set_locale('zh-CN')
                self.assertEqual(api.state()['message'], '发现新版本 9.0.0')
                check.assert_called_once()
            language.set_locale('en')
            with patch('desktop.window.check_update', side_effect=UpdateError('暂时无法连接 GitHub，请检查网络后重试。')):
                self.assertNotRegex(api.check()['message'], r'[\u4e00-\u9fff]')
            with patch('desktop.window.check_update', return_value=None):
                self.assertEqual(api.check()['message'], 'You are up to date')

    def test_native_language_is_validated_and_cached_separately_from_runtime_settings(self):
        from desktop.localization import DesktopLanguage
        with tempfile.TemporaryDirectory() as folder:
            data = Path(folder)
            (data/'desktop-settings.json').write_text('{"port":5678}', encoding='utf8')
            language = DesktopLanguage(data)
            self.assertFalse(language.set_locale('../../en'))
            self.assertEqual(list(data.iterdir()), [data/'desktop-settings.json'])
            self.assertTrue(language.set_locale('en'))
            self.assertEqual(DesktopLanguage(data).locale, 'en')
            self.assertEqual(json.loads((data/'desktop-settings.json').read_text()), {'port':5678})
            (data/'desktop-language.json').write_text('broken', encoding='utf8')
            self.assertEqual(DesktopLanguage(data).locale, 'zh-CN')

    def test_native_pages_boot_the_shared_catalog_and_preserve_original_values(self):
        from bs4 import BeautifulSoup
        from desktop.localization import DesktopLanguage, native_page
        with tempfile.TemporaryDirectory() as folder:
            language = DesktopLanguage(Path(folder)); language.set_locale('en')
            for name in ('updates', 'components', 'migration', 'loading'):
                soup = BeautifulSoup(native_page(name, language), 'html.parser')
                boot = json.loads(soup.select_one('#uiLocaleData').get_text())
                self.assertEqual(soup.html['lang'], 'en')
                self.assertEqual(boot['preferences']['locale'], 'en')
                self.assertEqual(boot['english']['软件更新'], 'Software update')
            state = language.state({'message':'正在保存 2 项进行中的任务',
                                    'detail':'已校验 12 个文件 · source_catalog.sqlite3',
                                    'notes':'原始发布说明', 'path':'D:/中文目录', 'phase':'copying'})
            self.assertNotRegex(state['message']+state['detail'], r'[\u4e00-\u9fff]')
            self.assertIn('source_catalog.sqlite3', state['detail'])
            self.assertEqual(state['notes'], '原始发布说明')
            self.assertEqual(state['path'], 'D:/中文目录')


if __name__ == '__main__':
    unittest.main()
