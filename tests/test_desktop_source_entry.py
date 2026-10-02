"""Source installs and packaged installs expose the same local application."""
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


class DesktopSourceEntryTests(unittest.TestCase):
    def test_source_shortcuts_delegate_to_native_entry(self):
        root = Path(__file__).resolve().parents[1]
        for path in ('app.py', 'scripts/launch_desktop.py'):
            with self.subTest(path=path), patch('desktop.entry.main', return_value=0) as main:
                with self.assertRaises(SystemExit) as stopped:
                    runpy.run_path(str(root / path), run_name='__main__')
                self.assertEqual(stopped.exception.code, 0)
                main.assert_called_once_with()

    def test_server_mode_cannot_bypass_native_access(self):
        from backend import create_app
        with self.assertRaisesRegex(RuntimeError, 'desktop runtime'):
            create_app({'TESTING': False, 'DESKTOP_MODE': False})
