"""Size review is advisory and applies only to the Windows setup artifact."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from desktop.build import WINDOWS_SETUP_SIZE_TARGET, artifact_size_report, write_size_report


class PackageSizeTests(unittest.TestCase):
    def test_large_setup_warns_without_blocking_or_limiting_other_packages(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for suffix in ('windows-x64-setup.exe', 'windows-x64-portable.zip', 'macos-arm64.dmg', 'macos-x64.dmg'):
                with (root / f'School-Watcher-2.5-{suffix}').open('wb') as artifact:
                    artifact.truncate(WINDOWS_SETUP_SIZE_TARGET + 1)
            summary = root / 'summary.md'
            with patch.dict(os.environ, {'GITHUB_ACTIONS': 'true', 'GITHUB_STEP_SUMMARY': str(summary)}), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                report = write_size_report(root, '2.5')
            self.assertEqual(len(report['artifacts']), 4)
            self.assertEqual(output.getvalue().count('::warning::'), 1)
            for artifact in report['artifacts']:
                if artifact['name'].endswith('-setup.exe'):
                    self.assertTrue(artifact['above_soft_target'])
                else:
                    self.assertNotIn('soft_target_bytes', artifact)
            self.assertEqual(json.loads((root / 'ASSET_SIZES.json').read_text()), report)
            self.assertIn('100 MB soft target', summary.read_text())

    def test_target_boundary_and_only_current_release_files_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with (root / 'School-Watcher-2.5-windows-x64-setup.exe').open('wb') as artifact:
                artifact.truncate(WINDOWS_SETUP_SIZE_TARGET)
            (root / 'School-Watcher-2.4-windows-x64-setup.exe').write_bytes(b'old')
            (root / 'SHA256SUMS.txt').write_text('checksums')
            (root / 'School-Watcher-2.5-other.zip').mkdir()
            records = artifact_size_report(root, '2.5')['artifacts']
            self.assertEqual(len(records), 1)
            self.assertFalse(records[0]['above_soft_target'])
            self.assertEqual(records[0]['mb'], 100)
            self.assertEqual(records[0]['bytes'], WINDOWS_SETUP_SIZE_TARGET)


if __name__ == '__main__':
    unittest.main()
