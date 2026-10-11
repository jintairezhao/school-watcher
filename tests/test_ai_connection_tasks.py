"""Connection pauses preserve durable work and resume only unsent requests."""
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import patch

from backend.ai.configuration import AIConfigError
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.database.source_governance_models import DiscoveryWorkItem
from backend.services import tasks
import test_ai_runtime
import test_automatic_student_value
import test_directory_work
import test_shared_summaries
import test_source_exploration

PAUSED = 'provider_connection_paused'
BINDING = {'id': 1, 'version': 1, 'provider': 'deepseek', 'model': 'fixture'}


def waiting(test, job, callback):
    handle = tasks.claim(capabilities=['http', 'directory'])
    test.assertEqual(handle['id'], job.id)
    checkpoint = dict(handle.get('checkpoint') or {}, preserved_cursor=7)
    with tasks.execution_scope(handle):
        tasks.checkpoint(checkpoint)
        with test.assertRaises(tasks.TaskDeferred) as caught:
            callback()
    test.assertEqual((caught.exception.phase, caught.exception.state, caught.exception.error_code),
                     ('ai_connection', 'waiting', PAUSED))
    test.assertTrue(tasks.handoff(handle, caught.exception))
    db.session.refresh(job)
    test.assertEqual((job.state, job.phase, job.error_code), ('waiting', 'ai_connection', PAUSED))
    test.assertEqual(job.checkpoint['preserved_cursor'], 7)
    test.assertIn('ai_connection_binding', job.checkpoint)
    return job


class DirectoryPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_directory_work.DirectoryWorkTests(); self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()

    def test_directory_gate_pauses_material_without_spending_attempt_or_repeating_call(self):
        with patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('saved', PAUSED)) as model:
            result = self.fixture.call()
            self.fixture.call()
        self.assertEqual(result['error_code'], PAUSED)
        self.assertTrue(all(row.state == 'paused' and row.attempts == 0 for row in DiscoveryWorkItem.query.all()))
        model.assert_called_once()

    def test_verified_resume_continues_same_material_without_losing_candidates(self):
        with patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('saved', PAUSED)):
            self.fixture.call()
        job = tasks.enqueue('navigation_review', 'resume-material', {'school_id': self.fixture.school})
        handle = tasks.claim(capabilities=['http', 'directory'])
        output = {'status': 'succeeded', 'output': {'results': [
            {'candidate_id': str(index), 'kind': 'unit'} for index in range(3)]}}
        with tasks.execution_scope(handle):
            tasks.checkpoint({'ai_connection_resume': 1, 'ai_connection_binding':
                {'id': None, 'version': 1, 'provider': None, 'model': None}})
            with patch('backend.ai.runtime.run_skill', return_value=output) as model:
                self.assertEqual(self.fixture.call()['status'], 'succeeded')
        self.assertTrue(all(row.state == 'succeeded' and row.attempts == 1 for row in DiscoveryWorkItem.query.all()))
        model.assert_called_once()


class StudentPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_automatic_student_value.AutomaticStudentValueTests(); self.fixture.setUp()
        self.fixture.binding.update(BINDING)
    def tearDown(self):
        self.fixture.tearDown()

    def check(self, listing, returned):
        from backend.services import student_information as value
        job = (value.queue_listing_assessment if listing else value.queue_source_assessment)(self.fixture.source.id)
        response = {'status': 'failed', 'error_code': PAUSED, 'usage': {'known': True, 'total_tokens': 0}}
        with patch('backend.ai.runtime.run_skill', return_value=response,
                   side_effect=None if returned else AIConfigError('saved', PAUSED)) as model:
            waiting(self, job, lambda: value.process(job.payload))
        model.assert_called_once()

    def test_source_exception_saves_waiting_task(self): self.check(False, False)
    def test_source_result_saves_waiting_task(self): self.check(False, True)
    def test_listing_exception_saves_waiting_task(self): self.check(True, False)
    def test_listing_result_saves_waiting_task(self): self.check(True, True)


class SummaryPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_shared_summaries.SharedSummaryTests(); self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()

    def check(self, returned):
        from backend.services import summaries
        row = self.fixture.request()
        row.progress = {'chunks': {'saved': {'facts': []}}}; db.session.commit()
        job = db.session.get(BackgroundTask, row.task_id)
        with patch('backend.ai.runtime.run_skill', return_value={'status': 'failed', 'error_code': PAUSED},
                   side_effect=None if returned else AIConfigError('saved', PAUSED)) as model:
            waiting(self, job, lambda: summaries.generate_summary(row.id))
        db.session.refresh(row)
        self.assertEqual(row.state, 'pending')
        self.assertEqual(row.error_code, PAUSED)
        self.assertIn('saved', row.progress['chunks'])
        self.assertIsNone(row.completed_at)
        model.assert_called_once()

    def test_exception_does_not_finalize_summary(self): self.check(False)
    def test_returned_pause_does_not_finalize_summary(self): self.check(True)


class ExplorationPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_source_exploration.ExplorationTests(); self.fixture.setUp()
    def tearDown(self):
        self.fixture.tearDown()

    def check(self, returned):
        from backend.services.source_exploration import run, STEP_ACTION, events
        proposal = self.fixture.p
        job = tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id, 'school_id': proposal.school_id})
        with patch('backend.ai.configuration.get_model_binding', return_value=BINDING), \
             patch('backend.services.directory_work.run_material', return_value={'status': 'needs_recovery', 'error_code': PAUSED},
                   side_effect=None if returned else AIConfigError('saved', PAUSED)) as model:
            waiting(self, job, lambda: run(proposal.id))
        journal = events(proposal.id, STEP_ACTION)
        self.assertEqual(len(journal), 1)
        detail = json.loads(journal[0].detail_json)
        self.assertFalse(detail['applied'])
        self.assertTrue(detail['manifest_started'])
        self.assertTrue(detail['input'])
        self.assertEqual(detail['result']['error_code'], PAUSED)
        self.assertEqual(json.loads(proposal.validation_json)['workflow']['ai_status'], PAUSED)
        model.assert_called_once()

    def test_exception_keeps_saved_material_waiting(self): self.check(False)
    def test_returned_pause_keeps_saved_material_waiting(self): self.check(True)


class ConnectionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_ai_runtime.AIRuntimeTests(); self.fixture.setUp()
        self.binding = self.fixture.binding
    def tearDown(self):
        self.fixture.tearDown()

    def failures(self):
        from backend.ai.providers import ProviderError
        with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True)):
            self.fixture.invoke('unknown-one'); self.fixture.invoke('unknown-two')

    def job(self, ident, *, binding=None, phase='ai_connection', paused=False):
        from backend.ai.connection_recovery import binding_ref
        row = tasks.enqueue('student_assessment', ident, {'school_id': 1, 'binding': {'id': self.binding['id'], 'version': self.binding['version']},
            **({'discovery_pause_requested': True} if paused else {})})
        row.state, row.phase, row.error_code = 'waiting', phase, PAUSED
        row.checkpoint = {'value_cursor': 3, 'value_rows': [{'saved': True}], 'ai_connection_binding': binding_ref(binding or self.binding)}
        row.deadline_at = datetime.utcnow() - timedelta(days=1)
        db.session.commit()
        return row

    def success(self):
        from backend.ai.runtime import test_connection
        with patch('backend.ai.providers.complete', return_value=self.fixture.response({'ok': True})) as provider:
            self.assertTrue(test_connection(self.binding['id'])['tested'])
        provider.assert_called_once()

    def test_successful_test_resumes_only_matching_connection_waits_without_clearing_unknown_spend(self):
        from backend.ai.models import AIBudget, AIExecution
        self.failures()
        ready = self.job('ready')
        manual = self.job('manual', paused=True)
        other = self.job('other', binding=dict(self.binding, model='another-model'))
        unknown = self.job('unknown', phase='ai_result')
        balances = [(row.key, row.reserved_tokens) for row in AIBudget.query.all()]
        self.success()
        for row in (ready, manual, other, unknown): db.session.refresh(row)
        self.assertEqual((ready.state, ready.phase, ready.checkpoint['value_cursor']), ('pending', 'ai_resume', 3))
        self.assertEqual(ready.checkpoint['value_rows'], [{'saved': True}])
        self.assertEqual(ready.checkpoint['ai_connection_resume'], 1)
        self.assertGreater(ready.deadline_at, datetime.utcnow())
        self.assertTrue(all(row.state == 'waiting' for row in (manual, other, unknown)))
        self.assertEqual(balances, [(row.key, row.reserved_tokens) for row in AIBudget.query.all()])
        self.assertEqual(db.session.get(AIExecution, 'unknown-one').status, 'uncertain')
        self.assertEqual(db.session.get(AIExecution, 'unknown-two').status, 'uncertain')

    def test_test_finishing_before_waiting_handoff_does_not_strand_task(self):
        from backend.ai.connection_recovery import defer_connection
        self.failures()
        job = tasks.enqueue('student_assessment', 'race', {'school_id': 1})
        handle = tasks.claim(capabilities=['directory', 'http'])
        with tasks.execution_scope(handle):
            with self.assertRaises(tasks.TaskDeferred) as caught:
                defer_connection(self.binding, checkpoint={'value_cursor': 9})
        self.success()
        self.assertTrue(tasks.handoff(handle, caught.exception))
        db.session.refresh(job)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.checkpoint['value_cursor'], 9)
        self.assertEqual(job.checkpoint['ai_connection_resume'], 1)

    def test_restart_or_failed_test_cannot_resume_waiting_job(self):
        from backend.ai.connection_recovery import resume_connection_tasks
        from backend.ai.runtime import test_connection
        from backend.ai.providers import ProviderError
        self.failures()
        job = self.job('stays-waiting')
        self.assertEqual(resume_connection_tasks(self.binding), 0)
        with patch('backend.ai.providers.complete', side_effect=ProviderError('connection_lost', uncertain=True)):
            self.assertFalse(test_connection(self.binding['id'])['tested'])
        db.session.refresh(job)
        self.assertEqual(job.state, 'waiting')
        self.assertNotIn('ai_connection_resume', job.checkpoint)

    def test_verified_config_update_refreshes_compatible_task_binding(self):
        from backend.ai.configuration import save_profile
        self.failures()
        job = self.job('new-credentials')
        changed = save_profile({'api_key': 'new-isolated-credential'}, self.binding['id'])
        self.success()
        db.session.refresh(job)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(job.payload['binding']['version'], changed['version'])
        self.assertEqual(job.checkpoint['ai_connection_binding']['version'], changed['version'])

    def test_successful_test_respects_manual_pause_and_explicit_continue_then_resumes(self):
        from backend.services.discovery_control import control
        self.failures()
        tasks.enqueue('discover', 1, {'school_id': 1})
        job = self.job('manual-then-continue')
        job.payload = dict(job.payload, automatic_source=True); db.session.commit()
        control(1, 'pause')
        self.success()
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase), ('waiting', 'user_paused'))
        control(1, 'resume')
        db.session.refresh(job)
        self.assertEqual((job.state, job.phase), ('pending', 'ai_resume'))
        self.assertEqual(job.checkpoint['value_cursor'], 3)

    def seed(self, ident, **changes):
        from backend.ai.models import AIExecution
        fields = dict(execution_id=ident, purpose='summary', profile_id=self.binding['id'],
            config_version=str(self.binding['version']), provider=self.binding['provider'], model=self.binding['model'],
            skill_id='fixture', skill_version='1', skill_digest='fixture', input_digest='fixture', mode='fixture',
            status='failed', error_code=PAUSED, request_sent=False, usage={'known': True, 'total_tokens': 0})
        fields.update(changes)
        row = AIExecution(**fields); db.session.add(row); db.session.commit()
        return row

    def test_safe_unsent_pause_gets_one_new_durable_execution_and_then_reuses_it(self):
        from backend.ai.runtime import _execute
        from backend.ai.models import AIExecution
        self.seed('unsent')
        job = tasks.enqueue('summary', 'safe-resume', {})
        handle = tasks.claim(capabilities=['http', 'directory'])
        with tasks.execution_scope(handle):
            tasks.checkpoint({'ai_connection_resume': 1})
            with patch('backend.ai.providers.complete', return_value=self.fixture.response()) as model:
                def invoke():
                    return _execute(execution_id='unsent', purpose='summary', binding=self.binding, skill_id='fixture',
                        skill_version='1', skill_digest='fixture', input_digest='fixture', mode='fixture',
                        messages=[], validator=lambda value: None)
                first, second = invoke(), invoke()
        self.assertEqual(first, second)
        self.assertEqual(first['status'], 'succeeded')
        self.assertNotEqual(first['provenance']['execution_id'], 'unsent')
        self.assertEqual(db.session.get(AIExecution, 'unsent').status, 'failed')
        model.assert_called_once()

    def test_unknown_partial_positive_usage_and_dispatched_rows_cannot_get_new_ids(self):
        from backend.ai.connection_recovery import resumed_execution_id
        checkpoint = {'ai_connection_resume': 2}
        for ident, changes in (
                ('unknown', {'status': 'uncertain', 'usage': {'known': False}, 'request_sent': True}),
                ('partial', {'status': 'partial', 'output': {'saved': True}}),
                ('positive', {'usage': {'known': True, 'total_tokens': 1}}),
                ('positive-input', {'usage': {'known': True, 'total_tokens': 0, 'input_tokens': 1}}),
                ('invalid-zero', {'usage': {'known': True, 'total_tokens': False}}),
                ('unsettled', {'reserved_tokens': 12}),
                ('sent-zero', {'request_sent': True})):
            self.seed(ident, **changes)
            self.assertEqual(resumed_execution_id(ident, checkpoint), ident)
        self.seed('base')
        actual = resumed_execution_id('base', {'ai_connection_resume': 1})
        self.seed(actual, status='uncertain', usage={'known': False}, request_sent=True)
        self.assertEqual(resumed_execution_id('base', checkpoint), actual)


if __name__ == '__main__':
    unittest.main()
