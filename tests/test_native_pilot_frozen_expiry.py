"""A signed full-row freeze excludes old jobs without recovering them."""
from copy import deepcopy
from datetime import datetime,timedelta
from contextlib import closing
import hashlib
import json
import sqlite3
import unittest

from scripts.checks import run_native_branch_pilot as pilot
from tests import test_native_pilot_continuation as continuation


class NativePilotFrozenExpiryTests(unittest.TestCase):
    def setUp(self):
        self.f=continuation.NativePilotContinuationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        self.db=self.f.db;self.target=BackgroundTask.query.one();self.school_id=self.target.payload['school_id']
        self.target.deadline_at=datetime.utcnow()-timedelta(days=1)
        self.frozen=tasks.enqueue('onboard','frozen-old',dict(school_id=self.school_id),commit=False)
        self.frozen.state='waiting';self.frozen.phase='ai_budget';self.frozen.error_code='budget_exhausted'
        self.frozen.deadline_at=datetime.utcnow()-timedelta(days=2);self.frozen.checkpoint={'retained_paid_cursor':7}
        self.db.session.commit();self.frozen_id=self.frozen.id
        self.scope=dict(self.f.scope,continuation_school_id=self.school_id,resume_expired_task_ids=[self.target.id],maximum_wall_seconds=20)

    def freeze(self):
        with closing(sqlite3.connect((self.f.root/'school_watcher.db').as_uri()+'?mode=ro',uri=True)) as c:
            c.row_factory=sqlite3.Row
            rows={str(r['id']):hashlib.sha256(json.dumps(dict(r),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest() for r in c.execute('SELECT * FROM background_tasks')}
        path=self.f.root/'frozen-rows.json'
        pilot.save(path,dict(all_existing_task_rows=rows,admitted_task_ids=[self.target.id],protected_task_ids=[self.frozen.id],task_contents_copied=False))
        self.scope.update(full_task_row_fingerprints=path.relative_to(pilot.ROOT).as_posix(),full_task_row_fingerprints_sha256=pilot.sha(path))
        return path

    def test_frozen_expired_waiting_job_does_not_block_selected_pending_job(self):
        self.freeze();before=deepcopy(self.frozen.checkpoint)
        result=pilot.continuation_state(self.scope)
        self.assertEqual(len(result['state']['jobs']),2)
        self.assertEqual(self.frozen.checkpoint,before)
        self.assertEqual(self.frozen.state,'waiting')

    def test_without_full_row_freeze_expired_waiting_job_still_refuses(self):
        with self.assertRaisesRegex(AssertionError,'explicit native recovery'):pilot.continuation_state(self.scope)

    def test_changed_frozen_row_or_certificate_is_rejected(self):
        path=self.freeze();self.frozen.checkpoint={'retained_paid_cursor':8};self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'task row'):pilot.continuation_state(self.scope)
        self.frozen.checkpoint={'retained_paid_cursor':7};self.db.session.commit()
        certificate=pilot.read(path);certificate['protected_task_ids']=[];pilot.save(path,certificate)
        with self.assertRaisesRegex(AssertionError,'freeze certificate'):pilot.continuation_state(self.scope)

    def test_freeze_partition_cannot_hide_an_admitted_task(self):
        path=self.freeze();certificate=pilot.read(path);certificate['protected_task_ids'].append(self.target.id)
        pilot.save(path,certificate);self.scope['full_task_row_fingerprints_sha256']=pilot.sha(path)
        with self.assertRaisesRegex(AssertionError,'freeze partition'):pilot.continuation_state(self.scope)

    def test_explicit_resume_changes_only_target_and_retains_frozen_wait(self):
        from backend.database.models import BackgroundTask
        self.freeze();before={c.name:deepcopy(getattr(self.frozen,c.name)) for c in self.frozen.__table__.columns}
        target_id=self.target.id
        anchor=pilot.continuation_state(self.scope);pilot.save(self.f.root/'anchor.json',anchor)
        self.scope['continuation_anchor_sha256']=pilot.sha(self.f.root/'anchor.json')
        pilot.initialize_native_state(self.scope)
        frozen=self.db.session.get(BackgroundTask,self.frozen_id)
        self.assertEqual({c.name:getattr(frozen,c.name) for c in frozen.__table__.columns},before)
        self.assertGreater(self.db.session.get(BackgroundTask,target_id).deadline_at,datetime.utcnow())


if __name__=='__main__':unittest.main()
