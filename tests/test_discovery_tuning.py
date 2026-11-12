"""Local, opt-in feedback trials cannot spend or change policy implicitly."""
from pathlib import Path
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask
from backend.services import discovery_tuning as tuning
from backend.services.discovery_policy import get_policy, progress_state, advance_progress


class DiscoveryTuningTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.local' / 'tuning-test-temp'
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'tuning.json'
        self.app = Flask('tuning-tests')
        self.app.config.update(TESTING=True, SECRET_KEY='test-only', DISCOVERY_TUNING_PATH=self.path,
            SOURCE_CATALOG_PATH=Path(self.tmp.name) / 'catalog.sqlite3')
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.env = patch.dict(os.environ, {'WATCHER_DISCOVERY_POLICY': '{}'})
        self.env.start()
        self.addCleanup(self.env.stop)

    def call(self, action, **payload):
        payload.setdefault('expected_revision', tuning.state()['revision'])
        return tuning.change(action, payload)

    def enabled(self):
        return self.call('enable')

    def feedback(self, kind='too_early'):
        return self.call('feedback', version=tuning.state()['version'], kind=kind, note='实际用户报告漏了学院主页通知', reference='学校A／运行1')

    def test_default_off_does_not_create_storage_or_change_policy(self):
        self.assertFalse(tuning.state()['enabled'])
        self.assertFalse(self.path.exists())
        self.assertNotIn('_tuning', get_policy())
        state = progress_state()
        for i in range(7):
            state = advance_progress(state, str(i))
        self.assertFalse(state['stopped'])
        self.assertTrue(advance_progress(state, '7')['stopped'])

    def test_enabled_parameters_reach_the_actual_progress_clock(self):
        self.enabled()
        values = tuning.state()['effective'] | {'progress_check_decisions': 2, 'progress_no_change_checks': 1}
        self.call('save', parameters=values, reason='先验证暂返是否过早')
        state = advance_progress(progress_state(), 'a')
        self.assertFalse(state['stopped'])
        self.assertTrue(advance_progress(state, 'b')['stopped'])
        self.assertEqual(state['tuning_context']['version'], 2)

    def test_disabling_restores_base_and_keeps_history_feedback(self):
        self.enabled()
        self.feedback()
        values = tuning.state()['effective'] | {'progress_check_decisions': 6}
        self.call('save', parameters=values, reason='根据反馈增加检查间隔')
        result = self.call('disable')
        self.assertEqual(get_policy()['progress_check_decisions'], 4)
        self.assertNotIn('_tuning', get_policy())
        self.assertEqual(len(result['feedback']), 1)
        self.assertEqual(len(result['history']), 2)

    def test_no_role_or_account_required_for_local_page_and_csrf_still_applies(self):
        from backend.routes.discovery_tuning import bp
        from backend.auth import register_hooks
        self.app.register_blueprint(bp)
        register_hooks(self.app)
        client = self.app.test_client()
        self.assertEqual(client.get('/api/discovery-tuning').status_code, 200)
        self.assertEqual(client.post('/api/discovery-tuning/enable', json={'expected_revision':0}).status_code, 403)
        with client.session_transaction() as session:
            session['_csrf_token'] = 'test-token'
        result = client.post('/api/discovery-tuning/enable', json={'expected_revision':0}, headers={'X-CSRF-Token':'test-token'})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json['enabled'])
        self.assertEqual(client.get('/api/discovery-tuning', environ_base={'REMOTE_ADDR':'192.0.2.1'}).status_code, 404)

    def test_stale_change_cannot_overwrite_feedback(self):
        first = self.enabled()
        self.feedback()
        with self.assertRaises(tuning.TuningConflict):
            self.call('disable', expected_revision=first['revision'])
        self.assertTrue(tuning.state()['enabled'])
        self.assertEqual(len(tuning.state()['feedback']), 1)

    def test_invalid_values_do_not_change_the_file(self):
        self.enabled()
        before = self.path.read_bytes()
        for value in (0, True, 1.5, 21):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.call('save', parameters=tuning.state()['effective'] | {'progress_check_decisions':value}, reason='invalid')
        self.assertEqual(self.path.read_bytes(), before)

    def test_rollback_creates_new_version_and_preserves_records(self):
        self.enabled()
        first = tuning.state()['effective']
        self.call('save', parameters=first | {'frontier_unseen_prior':3}, reason='扩大未知分支机会')
        result = self.call('rollback', version=1)
        self.assertEqual(result['version'], 3)
        self.assertEqual(result['parameters'], first)
        self.assertEqual([r['version']for r in result['history']], [1,2,3])

    def test_policy_change_does_not_reset_paid_decision_ids_or_known_material(self):
        self.enabled()
        state = advance_progress(progress_state(['material:1']), 'paid:1', ['material:1'])
        self.call('save', parameters=tuning.state()['effective'] | {'progress_check_decisions':6}, reason='延长试用观察')
        state = advance_progress(state, 'paid:2', ['material:1'])
        self.assertEqual(state['decision_ids'], ['paid:1','paid:2'])
        self.assertEqual(state['seen'], ['material:1'])
        self.assertEqual(advance_progress(state, 'paid:2'), state)

    def test_policy_change_does_not_resurrect_already_stopped_branch(self):
        self.enabled()
        state = progress_state() | {'stopped':True, 'decision_ids':['old'], 'decisions':1}
        self.call('save', parameters=tuning.state()['effective'] | {'progress_check_decisions':8}, reason='后续分支延长观察')
        self.assertEqual(advance_progress(state, 'new'), state)

    def test_access_and_unknown_feedback_are_preserved_without_auto_adjustment(self):
        self.enabled()
        original = tuning.state()['parameters']
        self.feedback('access_problem')
        self.feedback('unknown')
        state = tuning.state()
        self.assertEqual(state['parameters'], original)
        self.assertEqual([f['kind']for f in state['feedback']], ['access_problem','unknown'])

    def ai(self):
        state = tuning.state()
        return dict(status='succeeded', output=dict(parameters=state['effective'] | {'progress_check_decisions':5},
            rationale='用户报告切换过早，先延长一个判断再观察；尚未验证覆盖。', feedback_ids=[state['feedback'][0]['id']]),
            usage={'known':True,'total_tokens':100}, provenance={'execution_id':'test-only'})

    def test_ai_is_explicit_bounded_and_not_applied_automatically(self):
        self.enabled()
        self.feedback()
        state = tuning.state()
        payload = dict(expected_revision=state['revision'], request_id='a'*32, consent=True)
        with patch('backend.ai.configuration.get_model_binding', return_value={'id':1,'version':1,'max_output_tokens':4096}), \
             patch('backend.ai.runtime._execute', return_value=self.ai()) as call:
            result = tuning.suggest(payload)
            self.assertEqual(call.call_args.kwargs['output_tokens'], 1024)
            self.assertEqual(call.call_args.kwargs['purpose'], 'directory')
            self.assertEqual(result['state']['parameters'], state['parameters'])
            self.assertTrue(tuning.suggest(payload)['reused'])
            self.assertEqual(call.call_count, 1)
        result = self.call('apply_suggestion', suggestion_id='a'*32)
        self.assertEqual(result['parameters']['progress_check_decisions'], 5)
        self.assertTrue(result['suggestions'][0]['applied'])
        self.assertEqual(result['suggestions'][0]['applied_version'], result['version'])

    def test_existing_ai_budget_errors_do_not_change_parameters_or_feedback(self):
        from backend.ai.runtime import AIBudgetError
        self.enabled()
        self.feedback()
        before = self.path.read_bytes()
        with patch('backend.ai.configuration.get_model_binding', return_value={'id':1,'version':1,'max_output_tokens':4096}), \
             patch('backend.ai.runtime._execute', side_effect=AIBudgetError('额度已用完', 'budget_exhausted')), \
             self.assertRaises(AIBudgetError):
            tuning.suggest(dict(expected_revision=tuning.state()['revision'], request_id='e'*32, consent=True))
        self.assertEqual(self.path.read_bytes(), before)

    def test_ai_cannot_run_without_feedback_or_explicit_click(self):
        self.enabled()
        with patch('backend.ai.runtime._execute') as call:
            for payload in ({}, dict(expected_revision=tuning.state()['revision'], request_id='b'*32, consent=True)):
                with self.assertRaises(ValueError):
                    tuning.suggest(payload)
            call.assert_not_called()

    def test_only_access_failure_and_unknown_feedback_cannot_trigger_paid_tuning(self):
        self.enabled()
        self.feedback('access_problem')
        self.feedback('unknown')
        with patch('backend.ai.runtime._execute') as call, self.assertRaises(ValueError):
            tuning.suggest(dict(expected_revision=tuning.state()['revision'], request_id='f'*32, consent=True))
        call.assert_not_called()

    def test_stale_ai_suggestion_cannot_replace_new_parameters(self):
        self.enabled()
        self.feedback()
        with patch('backend.ai.configuration.get_model_binding', return_value={'id':1,'version':1,'max_output_tokens':4096}), \
             patch('backend.ai.runtime._execute', return_value=self.ai()):
            tuning.suggest(dict(expected_revision=tuning.state()['revision'], request_id='c'*32, consent=True))
        self.call('save', parameters=tuning.state()['parameters'], reason='另一次明确调整')
        with self.assertRaises(tuning.TuningConflict):
            self.call('apply_suggestion', suggestion_id='c'*32)

    def test_invalid_ai_output_cannot_change_parameters(self):
        self.enabled()
        self.feedback()
        original = tuning.state()['parameters']
        result = self.ai()
        result['output']['parameters']['progress_check_decisions'] = 0
        with patch('backend.ai.configuration.get_model_binding', return_value={'id':1,'version':1,'max_output_tokens':4096}), \
             patch('backend.ai.runtime._execute', return_value=result), self.assertRaises(ValueError):
            tuning.suggest(dict(expected_revision=tuning.state()['revision'], request_id='d'*32, consent=True))
        self.assertEqual(tuning.state()['parameters'], original)


if __name__ == '__main__':
    unittest.main()
