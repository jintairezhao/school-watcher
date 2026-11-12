"""Explicit extra sampling time preserves native progress and closed ledgers."""
from copy import deepcopy
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch
from sqlalchemy.exc import OperationalError
from scripts.checks import run_native_branch_pilot as pilot
from tests import test_native_pilot_continuation as continuation


class NativePilotExpiredResumeTests(unittest.TestCase):
    def setUp(self):
        self.f=continuation.NativePilotContinuationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.db=self.f.db
        from backend.database.models import BackgroundTask,School
        from backend.services import tasks
        self.target=BackgroundTask.query.one()
        self.school_id=self.target.payload['school_id']
        self.target.deadline_at=datetime.utcnow()-timedelta(days=1)
        self.target.attempts=1;self.target.claim_count=3
        other=School(name='其他开发学校',url='https://other.example.edu.cn/')
        self.db.session.add(other);self.db.session.flush()
        self.other=tasks.enqueue('discover',other.id,{'school_id':other.id},commit=False)
        self.other.deadline_at=datetime.utcnow()-timedelta(days=2)
        self.db.session.commit()
        self.target_id=self.target.id;self.other_id=self.other.id
        self.target_generation=self.target.generation
        self.scope=dict(self.f.scope,continuation_school_id=self.school_id,
            resume_expired_task_ids=[self.target_id],maximum_wall_seconds=20)
        self.anchor()

    def anchor(self):
        self.original=pilot.continuation_state(self.scope)
        pilot.save(self.f.root/'anchor.json',self.original)
        self.scope['continuation_anchor_sha256']=pilot.sha(self.f.root/'anchor.json')

    def rows(self):
        from backend.database.models import BackgroundTask
        return {j.id:{c.name:deepcopy(getattr(j,c.name)) for c in j.__table__.columns}
            for j in BackgroundTask.query.populate_existing()}

    def test_explicit_resume_changes_only_target_deadline_and_declared_ceiling(self):
        before=self.rows();start=datetime.utcnow()
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('No task recreation')):
            self.assertEqual(pilot.initialize_native_state(self.scope),self.school_id)
        after=self.rows();deadline=after[self.target_id].pop('deadline_at')
        old_deadline=before[self.target_id].pop('deadline_at')
        self.assertLess(old_deadline,start)
        self.assertTrue(start+timedelta(seconds=19)<=deadline<=datetime.utcnow()+timedelta(seconds=20))
        self.assertEqual(after,before)
        state=pilot.continuation_state(self.scope_without_expired())['state']
        for field in ('executions','budgets','columns_sha256','model_counter','cumulative_web_ledger_sha256'):
            self.assertEqual(state[field],self.original['state'][field])
        self.assertEqual(state['native_school_request_limit'],'3')

    def scope_without_expired(self):
        scope=dict(self.scope);scope.pop('resume_expired_task_ids');return scope

    def test_explicit_resume_releases_deferred_target_before_native_claim(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        target=self.db.session.get(BackgroundTask,self.target_id)
        target.available_at=datetime.utcnow()+timedelta(days=1)
        self.db.session.commit();self.anchor()
        before=self.rows()
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('No task recreation')):
            pilot.initialize_native_state(self.scope)
        after=self.rows()
        available=after[self.target_id].pop('available_at')
        after[self.target_id].pop('deadline_at')
        before[self.target_id].pop('available_at')
        before[self.target_id].pop('deadline_at')
        self.assertLessEqual(available,datetime.utcnow())
        self.assertEqual(after,before)
        with pilot.school_queue(self.scope,self.school_id):
            handle=tasks.claim(capabilities=['directory'])
            self.assertIsNotNone(handle)
            self.assertEqual((handle['id'],handle['generation']),(self.target_id,self.target_generation))
            self.assertTrue(tasks.finish(handle,{'synthetic':True}))

    def test_default_admission_still_refuses_expiry_and_invalid_recovery_lists(self):
        for changes in (dict(resume_expired_task_ids=[]),dict(resume_expired_task_ids=[self.other_id]),
                dict(resume_expired_task_ids=[self.target_id,self.target_id]),dict(maximum_wall_seconds=901),
                dict(resume_expired_task_ids=[True])):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):
                pilot.continuation_state(dict(self.scope,**changes))

    def test_live_or_finished_target_cannot_be_released(self):
        from backend.database.models import BackgroundTask
        job=self.db.session.get(BackgroundTask,self.target_id)
        job.state='running';job.token='owned';self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'live ownership'):pilot.continuation_state(self.scope)
        job.state='done';job.token=None;self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'finished or unrelated'):pilot.continuation_state(self.scope)

    def test_final_commit_failure_rolls_back_deadline_and_budget_limit(self):
        from backend.database.models import AppConfig
        before=self.rows()
        with patch.object(self.db.session(),'commit',side_effect=OperationalError('injected recovery commit',{},None)):
            with self.assertRaises(OperationalError):pilot.initialize_native_state(self.scope)
        self.assertEqual(self.rows(),before)
        self.assertEqual(AppConfig.get('ai_request_limit_school_discovery'),'2')

    def test_worker_excludes_other_schools_and_unrelated_assessment_tasks(self):
        from backend.services import tasks
        from backend.database.models import BackgroundTask
        assessment=tasks.enqueue('student_assessment','unrelated',{'school_id':self.school_id},commit=False)
        assessment.capability='http';assessment.deadline_at=datetime.utcnow()-timedelta(days=1)
        self.db.session.commit();assessment_id=assessment.id
        self.anchor();pilot.initialize_native_state(self.scope)
        with pilot.school_queue(self.scope,self.school_id):
            handle=tasks.claim(capabilities=['http','directory'])
            self.assertEqual(handle['id'],self.target_id)
            self.assertEqual(handle['generation'],self.target_generation)
            self.assertTrue(tasks.finish(handle,{'synthetic':True}))
            self.assertIsNone(tasks.claim(capabilities=['http','directory']))
        self.assertEqual(self.db.session.get(BackgroundTask,self.other_id).state,'pending')
        self.assertEqual(self.db.session.get(BackgroundTask,assessment_id).state,'pending')


if __name__=='__main__':unittest.main()
