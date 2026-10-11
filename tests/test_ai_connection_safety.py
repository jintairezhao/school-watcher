"""Transport evidence and durable connection pauses, with no live provider calls."""
import json
from datetime import datetime
from pathlib import Path
import socket
import subprocess
import sys
import unittest
from unittest.mock import patch

import requests
from urllib3.exceptions import MaxRetryError, NameResolutionError, NewConnectionError, ReadTimeoutError

from backend.ai.configuration import AIConfigError, get_model_binding, save_profile, save_limits
from backend.ai.models import AIExecution, AIBudget
from backend.ai.providers import ProviderError, complete
from backend.ai.runtime import test_connection
from backend.database.db import db
import test_ai_runtime
from test_directory_streaming import StreamResponse, event


class ProviderConnectionSafetyTests(unittest.TestCase):
    profile = {'provider': 'deepseek', 'region': 'default', 'model': 'fixture'}

    def failure(self, error, *, frames=None, streaming=True):
        with patch('backend.ai.providers.requests.Session') as factory:
            if frames is None:
                factory.return_value.post.side_effect = error
            else:
                factory.return_value.post.return_value = StreamResponse(frames, error)
            with self.assertRaises(ProviderError) as caught:
                complete(self.profile, 'private-fixture-key', [], streaming=streaming)
        self.assertNotIn('private', json.dumps(caught.exception.diagnostics))
        return caught.exception

    def test_proven_dns_and_new_tcp_failures_are_known_zero_with_safe_diagnostics(self):
        for reason, code in (
                (NameResolutionError('private-host', None, socket.gaierror(-2, 'private detail')), 'dns_resolution_failed'),
                (NewConnectionError(None, 'private detail'), 'connect_failed')):
            with self.subTest(code=code):
                error = self.failure(requests.ConnectionError(MaxRetryError(None, '/private', reason)))
                self.assertEqual(error.code, code)
                self.assertFalse(error.uncertain)
                self.assertTrue(error.retryable)
                self.assertEqual(error.usage, {'known': True, 'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0})
                self.assertEqual(error.diagnostics['request_phase'], 'before_http_request')
                self.assertEqual(error.diagnostics['exception_type'], type(reason).__name__)

    def test_connect_timeout_is_known_zero_only_before_response(self):
        before = self.failure(requests.ConnectTimeout('private detail'))
        self.assertFalse(before.uncertain)
        self.assertTrue(before.usage['known'])
        self.assertEqual(before.diagnostics['request_phase'], 'before_http_request')
        after = self.failure(requests.ConnectTimeout('private detail'), frames=[])
        self.assertTrue(after.uncertain)
        self.assertFalse(after.usage['known'])
        self.assertEqual(after.diagnostics['request_phase'], 'response_body')

    def test_zero_bytes_and_fast_unknown_connection_error_never_imply_zero_cost(self):
        with patch('backend.ai.providers.time.monotonic', return_value=1):
            error = self.failure(requests.ConnectionError('private detail'))
        self.assertTrue(error.uncertain)
        self.assertFalse(error.retryable)
        self.assertFalse(error.usage['known'])
        self.assertEqual(error.diagnostics['received_bytes'], 0)
        self.assertEqual(error.diagnostics['request_phase'], 'dispatch_started')
        self.assertEqual(error.diagnostics['exception_type'], 'ConnectionError')

    def test_wrapped_read_failure_preserves_partial_content_and_usage_without_message_matching(self):
        error = self.failure(requests.ConnectionError(ReadTimeoutError(None, '/private', 'private detail')),
            frames=[event('{"results":[{"candidate_id":"a"},'),
                    event(usage={'prompt_tokens': 11, 'completion_tokens': 7})])
        self.assertEqual(error.code, 'read_timeout')
        self.assertTrue(error.uncertain)
        self.assertIn('candidate_id', error.content)
        self.assertEqual(error.usage['total_tokens'], 18)
        self.assertEqual(error.diagnostics['request_phase'], 'response_body')
        self.assertEqual(error.diagnostics['exception_type'], 'ReadTimeoutError')


class PersistentConnectionPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ai_runtime.AIRuntimeTests()
        self.fixture.setUp()
        self.binding = self.fixture.binding

    def tearDown(self):
        self.fixture.tearDown()

    def fail_twice(self):
        with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True)) as provider:
            first = self.fixture.invoke('connection-one')
            second = self.fixture.invoke('connection-two')
        self.assertEqual((first['status'], second['status'], provider.call_count), ('uncertain', 'uncertain', 2))
        return first

    def test_two_failures_pause_new_work_across_sessions_but_cached_result_and_reservations_survive(self):
        cached = self.fail_twice()
        balances = [(r.key, r.used_tokens, r.reserved_tokens, r.used_requests) for r in AIBudget.query.all()]
        db.session.remove()
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            self.assertEqual(self.fixture.invoke('connection-one'), cached)
            with self.assertRaises(AIConfigError) as caught:
                self.fixture.invoke('new-task-new-school')
            self.assertEqual(caught.exception.code, 'provider_connection_paused')
            provider.assert_not_called()
        self.assertEqual(AIExecution.query.count(), 2)
        self.assertEqual(balances, [(r.key, r.used_tokens, r.reserved_tokens, r.used_requests) for r in AIBudget.query.all()])

    def test_pause_survives_fresh_python_process(self):
        self.fail_twice()
        database = self.fixture.app.config['SQLALCHEMY_DATABASE_URI']
        code = '''import json
from flask import Flask
from unittest.mock import patch
from backend.database.db import db
from backend.ai.configuration import AIConfigError
from backend.ai.runtime import run_skill
app = Flask(__name__)
app.config.update(SQLALCHEMY_DATABASE_URI=__FIXTURE_DATABASE__, SQLALCHEMY_TRACK_MODIFICATIONS=False)
db.init_app(app)
with app.app_context(), patch('backend.ai.providers.complete', side_effect=AssertionError('unexpected provider call')):
    try:
        run_skill('summarize-university-notice', 'summary', {'title':'fixture', 'paragraphs':[{'id':'p1','text':'fixture'}], 'attachments_unread':True}, 'summary', 'fresh-process')
    except AIConfigError as exc:
        assert exc.code == 'provider_connection_paused', exc.code
        print(exc.code)
    else:
        raise AssertionError('new process bypassed persistent pause')
'''.replace('__FIXTURE_DATABASE__', repr(database))
        result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(result.stdout.strip(), 'provider_connection_paused')

    def test_successful_connection_test_is_billed_and_clears_pause_without_reconciling_unknown_usage(self):
        self.fail_twice()
        unknown = sum(r.reserved_tokens for r in AIBudget.query.all())
        with patch('backend.ai.providers.complete', return_value=self.fixture.response({'ok': True})) as provider:
            result = test_connection(self.binding['id'])
        self.assertTrue(result['tested'])
        provider.assert_called_once()
        self.assertEqual(sum(r.reserved_tokens for r in AIBudget.query.all()), unknown)
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            self.assertEqual(self.fixture.invoke('after-test')['status'], 'succeeded')
        provider.assert_called_once()

    def test_enable_toggle_and_late_success_do_not_clear_existing_pause(self):
        self.fail_twice()
        # A request already in flight may finish after the second failure.
        db.session.add(AIExecution(execution_id='inflight-late-success', purpose='summary',
            profile_id=self.binding['id'], config_version=str(self.binding['version']),
            provider=self.binding['provider'], model=self.binding['model'], skill_id='fixture',
            skill_version='1', skill_digest='fixture', input_digest='fixture', mode='summary',
            status='succeeded', request_sent=True, finished_at=datetime.utcnow(), usage={'known': True, 'total_tokens': 7}))
        db.session.commit()
        save_profile({'enabled': False}, self.binding['id'])
        save_profile({'enabled': True}, self.binding['id'])
        self.fixture.binding = get_model_binding('summary')
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            with self.assertRaises(AIConfigError) as caught:
                self.fixture.invoke('after-toggle')
        self.assertEqual(caught.exception.code, 'provider_connection_paused')
        provider.assert_not_called()

    def test_one_failure_then_success_then_failure_does_not_pause(self):
        for name, failure in [('first', True), ('success', False), ('last', True)]:
            with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True) if failure else None,
                       return_value=self.fixture.response()):
                self.fixture.invoke(name)
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            self.assertEqual(self.fixture.invoke('still-allowed')['status'], 'succeeded')
        provider.assert_called_once()

    def test_school_and_purpose_changes_cannot_bypass_pause(self):
        from backend.ai.runtime import _execute
        def invoke(school):
            return _execute(execution_id=f'school-{school}', purpose='directory', binding=self.binding,
                skill_id='fixture', skill_version='1', skill_digest='fixture', input_digest=str(school),
                mode='fixture', messages=[], validator=lambda value: None, school_id=school)
        with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True)) as provider:
            invoke(11)
            invoke(22)
            with self.assertRaises(AIConfigError) as caught:
                invoke(33)
            with self.assertRaises(AIConfigError):
                self.fixture.invoke('different-purpose')
        self.assertEqual(caught.exception.code, 'provider_connection_paused')
        self.assertEqual(provider.call_count, 2)
        self.assertIsNone(db.session.get(AIExecution, 'school-33'))

    def test_connection_test_cannot_bypass_instance_budget(self):
        self.fail_twice()
        save_limits({'total': 1})
        with patch('backend.ai.providers.complete') as provider:
            with self.assertRaises(AIConfigError) as caught:
                test_connection(self.binding['id'])
        self.assertEqual(caught.exception.code, 'budget_exhausted')
        provider.assert_not_called()
        self.assertEqual(AIExecution.query.count(), 2)

    def test_failure_test_and_new_untested_configuration_cannot_clear_pause(self):
        self.fail_twice()
        with patch('backend.ai.providers.complete', side_effect=ProviderError('authentication_failed')):
            self.assertFalse(test_connection(self.binding['id'])['tested'])
        save_profile({'api_key': 'new-isolated-key'}, self.binding['id'])
        from backend.ai.configuration import _metadata
        from backend.ai.models import AIProfile
        self.fixture.binding = _metadata(db.session.get(AIProfile, self.binding['id']))
        with patch('backend.ai.providers.complete') as provider:
            with self.assertRaises(AIConfigError) as caught:
                self.fixture.invoke('untested-config')
        self.assertEqual(caught.exception.code, 'provider_connection_paused')
        provider.assert_not_called()
        with patch('backend.ai.providers.complete', return_value=self.fixture.response({'ok': True})):
            self.assertTrue(test_connection(self.binding['id'])['tested'])
        self.fixture.binding = get_model_binding('summary')
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()):
            self.assertEqual(self.fixture.invoke('verified-config')['status'], 'succeeded')

    def test_stale_connection_test_cannot_clear_pause_after_config_version_changed(self):
        self.fail_twice()
        def late_test(*args, **kwargs):
            save_profile({'enabled': False}, self.binding['id'])
            save_profile({'enabled': True}, self.binding['id'])
            return self.fixture.response({'ok': True})
        with patch('backend.ai.providers.complete', side_effect=late_test):
            test_connection(self.binding['id'])
        self.fixture.binding = get_model_binding('summary')
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            with self.assertRaises(AIConfigError) as caught:
                self.fixture.invoke('after-stale-test')
        self.assertEqual(caught.exception.code, 'provider_connection_paused')
        provider.assert_not_called()

    def test_dispatch_rechecks_failure_history_and_refunds_an_unsent_reservation(self):
        from backend.ai.runtime import credential_for
        def credential(*args, **kwargs):
            key = credential_for(*args, **kwargs)
            if db.session.get(AIExecution, 'reserved-before-pause'):
                for index in range(2):
                    db.session.add(AIExecution(execution_id=f'other-worker-{index}', purpose='directory',
                        profile_id=self.binding['id'], config_version=str(self.binding['version']),
                        provider=self.binding['provider'], model=self.binding['model'], skill_id='fixture',
                        skill_version='1', skill_digest='fixture', input_digest=str(index), mode='fixture',
                        status='uncertain', error_code='connection_lost', request_sent=True,
                        finished_at=datetime.utcnow(), usage={'known': False}))
                db.session.commit()
            return key
        with patch('backend.ai.runtime.credential_for', side_effect=credential), \
             patch('backend.ai.providers.complete') as provider:
            result = self.fixture.invoke('reserved-before-pause')
        self.assertEqual(result['error_code'], 'provider_connection_paused')
        self.assertTrue(result['usage']['known'])
        self.assertFalse(db.session.get(AIExecution, 'reserved-before-pause').request_sent)
        self.assertTrue(all((r.used_tokens, r.reserved_tokens, r.used_requests, r.reserved_requests, r.active_count) == (0, 0, 0, 0, 0)
                            for r in AIBudget.query.all()))
        provider.assert_not_called()

    def test_partial_output_and_known_usage_survive_connection_pause(self):
        from backend.ai.runtime import _execute
        def invoke(ident):
            return _execute(execution_id=ident, purpose='directory', binding=self.binding,
                skill_id='fixture', skill_version='1', skill_digest='fixture', input_digest=ident,
                mode='fixture', messages=[], validator=lambda value: None,
                partial_validator=lambda content: json.loads(content))
        usage = {'known': True, 'input_tokens': 11, 'output_tokens': 7, 'total_tokens': 18}
        with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True,
                content='{"kept":"validated fixture"}', usage=usage)) as provider:
            for ident in ('partial-one', 'partial-two'):
                result = invoke(ident)
                self.assertEqual(result['status'], 'partial')
                self.assertEqual(result['output'], {'kept': 'validated fixture'})
                self.assertEqual(result['usage'], usage)
            with self.assertRaises(AIConfigError):
                invoke('partial-three')
        self.assertEqual(provider.call_count, 2)
        self.assertTrue(all(r.used_tokens == 36 and r.reserved_tokens == 0 for r in AIBudget.query.all()))

    def test_a_separately_verified_profile_does_not_inherit_or_clear_another_pause(self):
        from backend.ai.configuration import _metadata
        from backend.ai.models import AIProfile
        self.fail_twice()
        other = save_profile({'name': 'separate fixture', 'provider': 'deepseek', 'model': 'another-model',
                              'api_key': 'isolated-other-key'})
        with patch('backend.ai.providers.complete', return_value=self.fixture.response({'ok': True})):
            self.assertTrue(test_connection(other['id'])['tested'])
        self.fixture.binding = _metadata(db.session.get(AIProfile, other['id']))
        with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            self.assertEqual(self.fixture.invoke('other-profile')['status'], 'succeeded')
            self.fixture.binding = self.binding
            with self.assertRaises(AIConfigError):
                self.fixture.invoke('original-still-paused')
        provider.assert_called_once()


if __name__ == '__main__':
    unittest.main()
