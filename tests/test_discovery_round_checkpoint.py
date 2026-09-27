"""A restarted round resumes remaining school slices without claiming coverage."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.sources.discover_sources import load_round, save_round


class DiscoveryRoundCheckpointTests(unittest.TestCase):
    def test_restart_retains_finished_slice_and_does_not_mark_unfinished_school(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'round.json'
            state = load_round(path, ['a', 'b'], 'student', 20)
            state['completed_slices']['a'] = {'processed_this_run': 20}
            save_round(path, state)
            resumed = load_round(path, ['b', 'a'], 'student', 20)
            self.assertEqual(set(resumed['completed_slices']), {'a'})
            self.assertFalse(resumed['coverage_accepted'])
            self.assertFalse(path.with_suffix('.json.tmp').exists())
            with self.assertRaises(ValueError):
                load_round(path, ['a', 'b'], 'all', 20)
            with self.assertRaises(ValueError):
                load_round(path, ['a', 'b', 'c'], 'student', 20)


if __name__ == '__main__':
    unittest.main()
