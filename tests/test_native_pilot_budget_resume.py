"""A new approved budget releases only the declared paused native task."""
from copy import deepcopy
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from scripts.checks import run_native_branch_pilot as pilot
from tests import test_native_pilot_expired_resume as fixture_module


class NativePilotBudgetResumeTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture_module.NativePilotExpiredResumeTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.db=self.f.db
        self.job=self.f.target
        self.job.phase='sampling_boundary';self.job.error_code='sampling_document_budget'
        self.job.available_at=datetime.utcnow()+timedelta(days=1)
        self.db.session.commit()
        self.job_id=self.job.id
        self.scope=dict(self.f.scope,resume_expired_task_ids=[],resume_budget_task_ids=[self.job_id])
        self.anchor()

    def anchor(self):
        pilot.save(self.f.f.root/'anchor.json',pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256']=pilot.sha(self.f.f.root/'anchor.json')

    def test_expired_budget_pause_preserves_every_field_except_availability_and_deadline(self):
        before=self.f.rows();start=datetime.utcnow()
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('No task recreation')):
            pilot.initialize_native_state(self.scope)
        after=self.f.rows()
        for field in ('available_at','deadline_at'):
            self.assertGreaterEqual(after[self.job_id][field],start)
            before[self.job_id].pop(field);after[self.job_id].pop(field)
        self.assertEqual(after,before)
        self.assertEqual(pilot.preserved_native_task_ids(self.scope),frozenset([self.f.other_id]))

    def test_unexpired_budget_pause_keeps_original_deadline(self):
        self.job.deadline_at=datetime.utcnow()+timedelta(hours=1);self.db.session.commit();self.anchor()
        original=self.f.rows()[self.job_id]['deadline_at']
        pilot.initialize_native_state(self.scope)
        self.assertEqual(self.f.rows()[self.job_id]['deadline_at'],original)

    def test_wrong_school_non_budget_pause_and_duplicate_ids_are_refused(self):
        for values in ({'resume_budget_task_ids':[self.f.other_id]},
                       {'resume_budget_task_ids':[self.job.id,self.job.id]},
                       {'resume_budget_task_ids':[True]}):
            with self.subTest(values=values),self.assertRaises(AssertionError):
                pilot.continuation_state(dict(self.scope,**values))
        for code in ('sampling_outside_declared_host','user_paused',''):
            self.job.error_code=code;self.db.session.commit()
            with self.subTest(code=code),self.assertRaises(AssertionError):pilot.continuation_state(self.scope)

    def test_failed_commit_rolls_back_release_without_touching_other_tasks(self):
        from sqlalchemy.exc import OperationalError
        before=deepcopy(self.f.rows())
        with patch.object(self.db.session(),'commit',side_effect=OperationalError('budget release',{},None)):
            with self.assertRaises(OperationalError):pilot.initialize_native_state(self.scope)
        self.assertEqual(self.f.rows(),before)


if __name__=='__main__':unittest.main()
