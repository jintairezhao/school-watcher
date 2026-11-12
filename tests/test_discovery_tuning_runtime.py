"""Exercise tuning's HTTP/runtime/budget chain; replace only provider I/O."""
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from backend.database.db import db
from backend.database import models  # Register the runtime's task/config tables.
from backend.ai.configuration import bind_profile, save_limits, save_profile
from backend.ai.models import AIBudget, AIExecution, AIProfile
from backend.ai.providers import ProviderError, ProviderResult
from backend.routes.discovery_tuning import bp
from backend.services import discovery_tuning as tuning


class DiscoveryTuningRuntimeTests(unittest.TestCase):
    def setUp(self):
        parent = Path(__file__).resolve().parents[1] / '.local' / 'tuning-test-temp'
        parent.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            'FIELD_ENC_KEY': 'isolated-tuning-runtime-encryption-key',
            'WATCHER_DISCOVERY_POLICY': '{}',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        root = Path(self.tmp.name)
        self.path = root / 'discovery-tuning.json'
        self.app = Flask('tuning-runtime-tests')
        self.app.config.update(TESTING=True, SECRET_KEY='isolated-test',
            SQLALCHEMY_DATABASE_URI='sqlite:///' + str(root / 'test.sqlite3'),
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            SOURCE_CATALOG_PATH=root / 'catalog.sqlite3', DISCOVERY_TUNING_PATH=self.path)
        db.init_app(self.app)
        self.app.register_blueprint(bp)
        self.context = self.app.app_context()
        self.context.push()
        self.addCleanup(self.context.pop)
        self.addCleanup(self.close_database)
        db.create_all()
        profile = save_profile(dict(name='isolated-provider', provider='deepseek',
            model='deepseek-chat', api_key='never-a-real-key'))
        row = db.session.get(AIProfile, profile['id'])
        row.enabled, row.tested_version = True, row.version
        db.session.commit()
        bind_profile('directory', row.id)
        self.client = self.app.test_client()
        self.network = patch.object(socket.socket, 'connect',
            side_effect=AssertionError('This test must not access any network'))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.change('enable')
        self.change('feedback', version=tuning.state()['version'], kind='too_early',
            note='用户报告学院主页通知漏查，先扩大观察机会', reference='隔离反馈样本')

    def close_database(self):
        db.session.remove()
        db.engine.dispose()

    def change(self, action, **payload):
        return tuning.change(action, dict(expected_revision=tuning.state()['revision'], **payload))

    def payload(self, letter='a'):
        return dict(expected_revision=tuning.state()['revision'], request_id=letter * 32, consent=True)

    def suggest(self, payload):
        return self.client.post('/api/discovery-tuning/suggest', json=payload)

    def response(self, output=None):
        state = tuning.state()
        if output is None:
            output = dict(parameters=state['effective'] | {'progress_check_decisions': 5},
                rationale='反馈提示暂返过早，建议先延长观察，效果尚未知。',
                feedback_ids=[state['feedback'][0]['id']])
        content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        return ProviderResult(content, 'stop',
            dict(known=True, input_tokens=50, output_tokens=20, total_tokens=70), 'isolated-response')

    def assert_settled_usage(self, tokens):
        budgets = AIBudget.query.all()
        self.assertEqual({row.key.split(':')[0] for row in budgets}, {'total', 'directory'})
        for row in budgets:
            self.assertEqual((row.used_tokens, row.used_requests, row.reserved_tokens,
                row.reserved_requests, row.active_count), (tokens, 1, 0, 0, 0))

    def test_real_runtime_records_usage_reuses_request_and_requires_explicit_apply(self):
        before = tuning.state()
        payload = self.payload()

        def respond(binding, key, messages, **kwargs):
            self.assertEqual(key, 'never-a-real-key')
            self.assertEqual(binding['provider'], 'deepseek')
            self.assertEqual(kwargs['max_tokens'], 1024)
            self.assertTrue(kwargs['streaming'])
            self.assertFalse(db.session().in_transaction())
            evidence = json.loads(messages[1]['content'])
            self.assertEqual(evidence['base_version'], before['version'])
            return self.response()

        with patch('backend.ai.providers.complete', side_effect=respond) as provider:
            first = self.suggest(payload)
            self.assertEqual(first.status_code, 200, first.json)
            retry = self.suggest(payload)  # Original revision is now stale.
            self.assertEqual(retry.status_code, 200, retry.json)
            self.assertTrue(retry.json['reused'])
            self.assertEqual(provider.call_count, 1)
        row = AIExecution.query.one()
        self.assertEqual((row.execution_id, row.purpose, row.skill_id, row.mode, row.status),
            ('discovery-tuning:' + payload['request_id'], 'directory', 'discovery-tuning', 'advice', 'succeeded'))
        self.assertTrue(row.request_sent)
        self.assertEqual(first.json['suggestion']['provenance']['execution_id'], row.execution_id)
        self.assertEqual(first.json['suggestion']['usage']['total_tokens'], 70)
        self.assert_settled_usage(70)
        self.assertEqual(tuning.state()['parameters'], before['parameters'])
        applied = self.client.post('/api/discovery-tuning/apply_suggestion', json=dict(
            expected_revision=tuning.state()['revision'], suggestion_id=payload['request_id']))
        self.assertEqual(applied.status_code, 200, applied.json)
        self.assertEqual(applied.json['parameters']['progress_check_decisions'], 5)
        self.assertEqual(applied.json['version'], before['version'] + 1)
        self.assertEqual(AIExecution.query.count(), 1)
        self.assert_settled_usage(70)

    def test_existing_monthly_limits_block_before_dispatch_without_losing_feedback(self):
        before = self.path.read_bytes()
        for purpose in ('total', 'directory'):
            with self.subTest(purpose=purpose):
                save_limits({'total': 0, 'directory': 0} | {purpose: 1})
                with patch('backend.ai.providers.complete') as provider:
                    result = self.suggest(self.payload())
                    self.assertEqual(result.status_code, 400, result.json)
                    provider.assert_not_called()
                self.assertEqual(AIExecution.query.count(), 0)
                self.assertEqual(self.path.read_bytes(), before)
                for row in AIBudget.query.all():
                    self.assertEqual((row.used_tokens, row.reserved_tokens, row.active_count,
                        row.used_requests, row.reserved_requests), (0, 0, 0, 0, 0))

    def test_invalid_model_output_is_recorded_once_without_applying_or_retrying(self):
        before = self.path.read_bytes()
        output = json.loads(self.response().content)
        output['parameters']['progress_check_decisions'] = 0
        payload = self.payload()
        with patch('backend.ai.providers.complete', return_value=self.response(output)) as provider:
            self.assertEqual(self.suggest(payload).status_code, 400)
            self.assertEqual(self.suggest(payload).status_code, 400)
            self.assertEqual(provider.call_count, 1)
        row = AIExecution.query.one()
        self.assertEqual((row.status, row.error_code, row.output), ('failed', 'output_validation_failed', None))
        self.assert_settled_usage(70)
        self.assertEqual(self.path.read_bytes(), before)

    def test_uncertain_provider_result_preserves_reservation_and_never_redispatches(self):
        before = self.path.read_bytes()
        payload = self.payload()
        with patch('backend.ai.providers.complete', side_effect=ProviderError('timeout', uncertain=True)) as provider:
            self.assertEqual(self.suggest(payload).status_code, 400)
            self.assertEqual(self.suggest(payload).status_code, 400)
            self.assertEqual(provider.call_count, 1)
        row = AIExecution.query.one()
        self.assertEqual(row.status, 'uncertain')
        self.assertFalse(row.usage['known'])
        self.assertGreater(row.reserved_tokens, 0)
        for budget in AIBudget.query.all():
            self.assertEqual((budget.used_tokens, budget.used_requests, budget.active_count,
                budget.reserved_requests), (0, 1, 0, 0))
            self.assertEqual(budget.reserved_tokens, row.reserved_tokens)
        self.assertEqual(self.path.read_bytes(), before)

    def test_parameters_changed_during_call_reject_stale_advice_but_keep_execution(self):
        payload = self.payload()
        output = self.response()

        def respond(*args, **kwargs):
            self.change('save', parameters=tuning.state()['parameters'] | {'frontier_unseen_prior': 3},
                reason='调用期间明确调整，旧建议不能覆盖')
            return output

        with patch('backend.ai.providers.complete', side_effect=respond) as provider:
            result = self.suggest(payload)
            self.assertEqual(result.status_code, 409, result.json)
            retry = self.suggest(self.payload())
            self.assertEqual(retry.status_code, 400, retry.json)
            self.assertEqual(provider.call_count, 1)
        state = tuning.state()
        self.assertEqual(state['parameters']['frontier_unseen_prior'], 3)
        self.assertEqual(state['suggestions'], [])
        self.assertEqual(AIExecution.query.one().status, 'succeeded')
        self.assert_settled_usage(70)


if __name__ == '__main__':
    unittest.main()
