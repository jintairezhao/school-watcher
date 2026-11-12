"""Actual runtime and adapter with local HTTP doubles; no network or paid calls."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import requests

from backend.ai import runtime
from backend.ai.models import AIExecution, AIBudget
from backend.database.db import db
from tests import test_ai_runtime as fixture


class MaterialCaptureTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.AIRuntimeTests()
        self.fixture.setUp()
        self.root = Path(self.fixture.tmp.name) / 'materials'
        self.calls = []

    def tearDown(self):
        self.fixture.tearDown()

    def records(self, session):
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(session.path.glob('event-*.json'))]

    def transport(self, text=None, failure=None, before=None):
        text = json.dumps(fixture.OUTPUT, ensure_ascii=False) if text is None else text
        def post(url, **kwargs):
            self.assertFalse(db.session().in_transaction())
            self.calls.append(kwargs['json'])
            if before:
                before()
            if failure:
                raise failure
            response = MagicMock(status_code=200, headers={'x-request-id': 'fixture-request'})
            response.__enter__.return_value = response
            usage = {'prompt_tokens': 50, 'completion_tokens': 20}
            if kwargs['json']['stream']:
                payload = {'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage}
                raw = ('data: ' + json.dumps(payload) + '\n\ndata: [DONE]\n\n').encode()
            else:
                raw = json.dumps({'choices': [{'message': {'content': text}, 'finish_reason': 'stop'}], 'usage': usage}).encode()
            response.iter_content.side_effect = lambda **_: iter([raw])
            return response
        client = MagicMock()
        client.post.side_effect = post
        return patch('backend.ai.providers.requests.Session', return_value=client)

    def test_disabled_by_default_does_not_create_materials_or_change_billing(self):
        with self.transport():
            result = self.fixture.invoke('default-off')
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(self.root.exists())
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(b.used_tokens == 70 and b.used_requests == 1 for b in AIBudget.query.all()))

    def test_exact_request_is_durable_before_http_and_credentials_are_excluded(self):
        from backend.ai.material_capture import capture_materials
        self.fixture.app.config['DISCOVERY_POLICY'] = {'ai_temperature': 0.37}
        def before():
            record, = [r for r in self.records(session) if r['event'] == 'provider_request']
            self.assertEqual(record['payload']['body'], self.calls[0])
        with capture_materials(self.root, label='offline-request') as session, self.transport(before=before):
            result = self.fixture.invoke('captured')
        records = self.records(session)
        request, = [r for r in records if r['event'] == 'provider_request']
        self.assertEqual(request['payload']['body']['temperature'], 0.37)
        self.assertEqual(request['payload']['body'], self.calls[0])
        self.assertEqual(next(r for r in records if r['event'] == 'provider_response')['payload']['content'], json.dumps(fixture.OUTPUT, ensure_ascii=False))
        self.assertEqual(next(r for r in records if r['event'] == 'execution_result')['payload']['output'], fixture.OUTPUT)
        self.assertTrue(session.complete)
        self.assertEqual(result['status'], 'succeeded')
        saved = '\n'.join(p.read_text(encoding='utf-8') for p in session.path.glob('*.json'))
        self.assertNotIn('never-a-real-key', saved)
        self.assertNotIn('Authorization', saved)
        self.assertEqual(len(self.calls), 1)

    def test_invalid_json_preserves_original_model_text_and_validation_failure(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='bad-output') as session, self.transport(text='{unfinished'):
            result = self.fixture.invoke('invalid')
        self.assertEqual(result['error_code'], 'invalid_json')
        records = self.records(session)
        self.assertEqual(next(r for r in records if r['event'] == 'provider_response')['payload']['content'], '{unfinished')
        self.assertEqual(next(r for r in records if r['event'] == 'execution_result')['payload']['status'], 'failed')
        self.assertTrue(session.complete)
        self.assertEqual(AIExecution.query.one().usage['total_tokens'], 70)

    def test_unknown_result_is_not_zero_and_reuse_does_not_dispatch(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='unknown') as session, self.transport(failure=requests.ReadTimeout('never-a-real-key')):
            first = self.fixture.invoke('unknown')
            second = self.fixture.invoke('unknown')
        self.assertEqual(first['status'], 'uncertain')
        self.assertEqual(second['status'], 'uncertain')
        self.assertFalse(first['usage']['known'])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(sum(r['event'] == 'provider_request' for r in self.records(session)), 1)
        self.assertTrue(session.complete)
        self.assertGreater(AIExecution.query.one().reserved_tokens, 0)
        self.assertTrue(all(b.used_requests == 1 for b in AIBudget.query.all()))
        self.assertNotIn('never-a-real-key', '\n'.join(p.read_text(encoding='utf-8') for p in session.path.glob('*.json')))

    def test_interrupted_dispatch_can_be_recovered_without_another_request(self):
        from backend.ai.material_capture import capture_materials
        class Interrupted(BaseException):
            pass
        with capture_materials(self.root, label='interrupted') as first, self.transport(failure=Interrupted()):
            with self.assertRaises(Interrupted):
                self.fixture.invoke('interrupted')
        self.assertFalse(first.complete)
        row = AIExecution.query.one()
        row.created_at = datetime.utcnow() - timedelta(minutes=10)
        row.heartbeat_at = row.created_at
        db.session.commit()
        self.assertEqual(runtime.recover_uncertain_executions(), 1)
        with capture_materials(self.root, label='recovery') as second, self.transport():
            result = self.fixture.invoke('interrupted')
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(any(r['event'] == 'provider_request' for r in self.records(second)))
        self.assertTrue(second.complete)
        self.assertEqual(AIExecution.query.count(), 1)

    def test_capture_limit_marks_incomplete_but_preserves_runtime_outcome(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='limit', max_record_bytes=512) as session, self.transport():
            result = self.fixture.invoke('limited')
        self.assertFalse(session.complete)
        self.assertIn('record_too_large', session.gaps)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(AIExecution.query.one().usage['total_tokens'], 70)
        self.assertEqual(len(self.calls), 1)

    def test_recording_failure_does_not_change_provider_dispatch_or_settlement(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='disk-failure') as session, self.transport(), \
                patch.object(session, '_write', side_effect=OSError('never-a-real-key')):
            result = self.fixture.invoke('storage-failure')
        self.assertFalse(session.complete)
        self.assertIn('record_write_failed', session.gaps)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(all(b.used_tokens == 70 and b.reserved_tokens == 0 for b in AIBudget.query.all()))

    def test_directory_stream_captures_actual_policy_and_school_limits(self):
        from backend.ai.material_capture import capture_materials
        from backend.ai.runtime import run_skill
        from backend.database.models import AppConfig
        evidence = {'school_id': 1, 'candidates': [{'candidate_id': 'one', 'name': '通知',
            'url': 'https://example.edu.cn/notices/', 'kind': 'notice_source', 'path': '教务部'}],
            'evidence': [{'candidate_id': 'one', 'evidence_id': 'p0', 'text': '本科生申请资助材料'}]}
        output = {'results': [{'candidate_id': 'one', 'role': 'publishing', 'value': 'relevant',
            'historical': 'high', 'reason': '准备材料', 'facts': [{'kind': 'history', 'text': '资助材料',
            'quote': '本科生申请资助材料', 'evidence_id': 'p0'}]}]}
        AppConfig.set('ai_request_limit_school_discovery', '11')
        AppConfig.set('ai_token_limit_school_discovery', '120000')
        self.fixture.app.config['DISCOVERY_POLICY'] = {'ai_temperature': 0.29, 'navigation_batch_size': 6}
        with capture_materials(self.root, label='directory-stream') as session, self.transport(text=json.dumps(output)):
            result = run_skill('student-information', 'assess', evidence, 'directory', 'streamed')
        self.assertEqual(result['status'], 'succeeded')
        request = next(r for r in self.records(session) if r['event'] == 'provider_request')['payload']
        self.assertEqual(request['body'], self.calls[0])
        self.assertTrue(request['body']['stream'])
        self.assertEqual(request['body']['stream_options'], {'include_usage': True})
        self.assertEqual(request['effective_policy']['navigation_batch_size'], 6)
        system = request['body']['messages'][1]['content']
        self.assertIn('"school_requests":11', system)
        self.assertIn('"school_tokens":120000', system)
        self.assertTrue(session.complete)

    def test_resumed_identity_is_recorded_at_request_boundary(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='resolved-identity') as session, self.transport(), \
                patch('backend.ai.connection_recovery.resumed_execution_id', return_value='resolved-execution'):
            result = self.fixture.invoke('requested-execution')
        self.assertEqual(result['provenance']['execution_id'], 'resolved-execution')
        self.assertEqual(next(r for r in self.records(session) if r['event'] == 'provider_request')['execution_id'], 'resolved-execution')
        self.assertTrue(session.complete)

    def test_total_storage_limit_is_enforced_without_affecting_result(self):
        from backend.ai.material_capture import capture_materials
        with capture_materials(self.root, label='total-limit', max_bytes=8192) as session, self.transport():
            result = runtime._execute(execution_id='large-input', purpose='summary', binding=self.fixture.binding,
                skill_id='fixture', skill_version='1', skill_digest='fixture', input_digest='fixture', mode='summary',
                messages=[{'role': 'user', 'content': 'public-fixture-' * 2000}], validator=lambda _: None)
        self.assertEqual(result['status'], 'succeeded')
        self.assertFalse(session.complete)
        self.assertIn('capture_size_limit', session.gaps)
        self.assertLessEqual(sum(p.stat().st_size for p in session.path.glob('*')), 8192)
        self.assertFalse(json.loads((session.path / 'completion.json').read_text(encoding='utf-8'))['payload']['complete'])

    def test_preflight_error_is_retained_without_a_dispatch_or_invoice(self):
        from backend.ai.material_capture import capture_materials
        from backend.ai.configuration import AIConfigError
        with capture_materials(self.root, label='preflight') as session, self.transport(), \
                patch('backend.ai.runtime.credential_for', side_effect=AIConfigError('never-a-real-key', 'not_configured')):
            with self.assertRaises(AIConfigError):
                self.fixture.invoke('not-dispatched')
        self.assertEqual(self.calls, [])
        self.assertEqual(AIExecution.query.count(), 0)
        error, = [r for r in self.records(session) if r['event'] == 'execution_error']
        self.assertEqual(error['payload'], {'code': 'not_configured', 'dispatched': False})
        self.assertTrue(session.complete)


if __name__ == '__main__':
    unittest.main()
