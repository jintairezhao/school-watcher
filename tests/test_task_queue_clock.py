"""Queue waits/offline periods are not website failures or execution time."""
from datetime import datetime,timedelta
import unittest
from unittest.mock import patch
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.services import tasks


class TaskQueueClockTests(unittest.TestCase):
    def setUp(self):
        from tests.test_durable_runtime import RuntimeTests
        self.fixture=RuntimeTests();self.fixture.setUp()
    def tearDown(self):self.fixture.tearDown()

    def test_a_pending_collection_survives_queue_wait_and_computer_shutdown(self):
        row=tasks.enqueue('collect',self.fixture.dept_id,{'department_id':self.fixture.dept_id})
        then=datetime.utcnow()-timedelta(days=2)
        row.queued_at=row.updated_at=then;row.available_at=then
        row.deadline_at=then+timedelta(hours=2);row.phase='pagination';row.checkpoint={'next_page':7}
        db.session.commit()
        handle=tasks.claim()
        self.assertIsNotNone(handle)
        self.assertEqual(handle['checkpoint'],{'next_page':7})
        self.assertGreater(handle['deadline_at'],datetime.utcnow()+timedelta(minutes=119))
        self.assertEqual(row.attempts,0)

    def test_actual_processing_still_consumes_a_finite_budget(self):
        row=tasks.enqueue('collect',self.fixture.dept_id,{'department_id':self.fixture.dept_id})
        handle=tasks.claim()
        now=datetime.utcnow()
        row.updated_at=now-timedelta(hours=3);row.deadline_at=now-timedelta(hours=1)
        db.session.commit()
        tasks.handoff(handle,tasks.TaskDeferred(phase='pagination',checkpoint={'next_page':8}))
        self.assertIsNone(tasks.claim())
        db.session.refresh(row)
        self.assertEqual(row.state,'failed')
        self.assertEqual(row.checkpoint,{'next_page':8})

    def test_legacy_queue_expiry_keeps_the_resume_point_and_failure_counter(self):
        row=tasks.enqueue('collect',self.fixture.dept_id,{'department_id':self.fixture.dept_id})
        row.state='failed';row.error_code='deadline_exceeded';row.checkpoint={'next_page':9};row.attempts=2
        db.session.commit()
        tasks.enqueue('collect',self.fixture.dept_id,{'department_id':self.fixture.dept_id})
        self.assertEqual(row.checkpoint,{'next_page':9})
        self.assertEqual(row.attempts,2)

    def test_visible_body_does_not_queue_behind_every_old_batch_update(self):
        for i in range(20):
            row=tasks.enqueue('collect',i+100,{'department_id':i+100})
            row.queued_at=datetime.utcnow()-timedelta(minutes=10)
        body=tasks.enqueue('content',7,{'announcement_id':7})
        db.session.commit()
        self.assertEqual(tasks.claim()['id'],body.id)

    def test_two_slots_keep_one_background_turn_during_body_reads(self):
        from backend.worker import run
        from threading import Barrier
        for i in range(3):tasks.enqueue('content',i+100,{'announcement_id':i+100})
        tasks.enqueue('collect',self.fixture.dept_id,{'school_id':self.fixture.school_id,'department_id':self.fixture.dept_id})
        barrier=Barrier(2);seen=[]
        def execute(app,handle,pause):
            seen.append(handle['kind']);barrier.wait(timeout=5)
            with app.app_context():tasks.finish(handle,{'done':True})
        with patch('backend.worker.execute',side_effect=execute):
            run(self.fixture.app,once=True,roles=('http',),concurrency=2)
        self.assertCountEqual(seen,['content','collect'])
