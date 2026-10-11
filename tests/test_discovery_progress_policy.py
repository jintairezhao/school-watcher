"""Progress limits observe acquired evidence, rather than task slices or prose."""
import unittest

from backend.services.discovery_policy import advance_progress, get_policy, progress_state


class DiscoveryProgressPolicyTests(unittest.TestCase):
    def test_two_empty_windows_stop_but_cache_replays_do_not_count_as_decisions(self):
        state = progress_state(['known-page'])
        for decision in range(1, 9):
            state = advance_progress(state, f'execution:{decision}', ['known-page'])
            replay = advance_progress(state, f'execution:{decision}', ['known-page'])
            self.assertEqual(replay, state)
            self.assertEqual(state['stopped'], decision == 8)
        self.assertEqual(state['no_change_checks'], 2)
        self.assertEqual(state['decisions'], 8)

    def test_new_verified_material_resets_stagnation_and_survives_serialization(self):
        import json
        state = progress_state(['known-page'])
        for decision in range(1, 5):
            state = advance_progress(state, str(decision), ['known-page'])
        self.assertEqual(state['no_change_checks'], 1)
        state = json.loads(json.dumps(state))
        for decision in range(5, 9):
            state = advance_progress(state, str(decision), ['known-page', 'validated-source'])
        self.assertFalse(state['stopped'])
        self.assertEqual(state['no_change_checks'], 0)

    def test_defaults_are_shared_and_explicit_policy_overrides_work(self):
        from flask import Flask
        app = Flask(__name__)
        app.config['DISCOVERY_POLICY'] = {'progress_check_decisions': 2, 'progress_no_change_checks': 1,
                                          'discovery_slice_pages': 5, 'unknown_key': 123}
        with app.app_context():
            self.assertEqual(get_policy()['discovery_slice_pages'], 5)
            self.assertNotIn('unknown_key', get_policy())
            state = advance_progress(progress_state(), 'one')
            self.assertFalse(state['stopped'])
            self.assertTrue(advance_progress(state, 'two')['stopped'])


if __name__ == '__main__':
    unittest.main()
