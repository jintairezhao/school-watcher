"""The CSP adapter remains confined to trusted pages and native response delivery."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from desktop.native_bridge import install_csp_bridge


class LoadedEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self


class NativeBridgeTests(unittest.TestCase):
    def test_untrusted_documents_do_not_receive_bridge_and_private_methods_stay_private(self):
        window = SimpleNamespace(events=SimpleNamespace(loaded=LoadedEvent()), run_js=Mock(), evaluate_js=Mock())
        api = SimpleNamespace(update_notification=lambda: None, _private=lambda: None)
        allowed = Mock(return_value=False)
        install_csp_bridge(window, api, allowed)
        window.events.loaded.handlers[0]()
        window.run_js.assert_not_called()
        allowed.return_value = True
        window.events.loaded.handlers[0]()
        script = window.run_js.call_args.args[0]
        self.assertIn('["update_notification"]', script)
        self.assertNotIn('_private', script)

    def test_only_native_replies_use_direct_execution(self):
        original = Mock(return_value='ordinary result')
        window = SimpleNamespace(events=SimpleNamespace(loaded=LoadedEvent()), run_js=Mock(), evaluate_js=original)
        install_csp_bridge(window, SimpleNamespace(), lambda: False)
        reply = 'window.pywebview._returnValuesCallbacks["method"]["request"]({value:"null"})'
        window.evaluate_js(reply)
        window.run_js.assert_called_once_with(reply)
        original.assert_not_called()
        self.assertEqual(window.evaluate_js('document.title'), 'ordinary result')
        original.assert_called_once_with('document.title', None)


if __name__ == '__main__':
    unittest.main()
