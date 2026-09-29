"""The CSP adapter remains confined to its native window and public API methods."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from desktop.native_bridge import install_csp_bridge


class NativeBridgeTests(unittest.TestCase):
    def window(self):
        return SimpleNamespace(events=SimpleNamespace(closed=SimpleNamespace(is_set=lambda: False)),
                               run_js=Mock(), evaluate_js=Mock())

    def test_adapter_only_changes_its_window_and_excludes_private_methods(self):
        window = self.window()
        api = SimpleNamespace(update_notification=lambda: None, _private=lambda: None)
        loader = Mock(return_value=('original', 'finish'))
        util = SimpleNamespace(load_js_files=loader)
        with patch.dict('sys.modules', {'webview': SimpleNamespace(util=util)}):
            restore = install_csp_bridge(window, api)
            script, finish = util.load_js_files(window, 'cocoa')
            self.assertIn('["update_notification"]', script)
            self.assertNotIn('_private', script)
            self.assertEqual(finish, 'finish')
            self.assertEqual(util.load_js_files(object(), 'cocoa'), ('original', 'finish'))
            restore()
            self.assertIs(util.load_js_files, loader)

    def test_only_native_replies_use_direct_execution(self):
        original = Mock(return_value='ordinary result')
        window = self.window()
        window.evaluate_js = original
        util = SimpleNamespace(load_js_files=Mock())
        with patch.dict('sys.modules', {'webview': SimpleNamespace(util=util)}):
            restore = install_csp_bridge(window, SimpleNamespace())
        reply = 'window.pywebview._returnValuesCallbacks["method"]["request"]({value:"null"})'
        window.evaluate_js(reply)
        window.run_js.assert_called_once_with(reply)
        original.assert_not_called()
        self.assertEqual(window.evaluate_js('document.title'), 'ordinary result')
        original.assert_called_once_with('document.title', None)
        window.events.closed.is_set = lambda: True
        window.evaluate_js(reply)
        window.run_js.assert_called_once_with(reply)
        restore()


if __name__ == '__main__':
    unittest.main()
