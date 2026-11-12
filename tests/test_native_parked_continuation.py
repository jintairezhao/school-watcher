"""Only a declared continuation can release parked work; no website/model I/O."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from scripts.checks import run_native_branch_pilot as pilot
from scripts.checks.native_sampling_lifecycle import park_continuation_tasks
from tests import test_native_pilot_expired_resume as recovery_fixture
from tests import test_native_sampling_lifecycle as lifecycle_fixture


class ParkedRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery_fixture.NativePilotExpiredResumeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.job = self.f.target
        self.job_id = self.job.id
        self.job.phase = 'directory_slice'
        self.job.error_code = ''
        self.job.deadline_at = datetime.utcnow() + timedelta(minutes=10)
        self.job.available_at = datetime.utcnow() + timedelta(days=1)
        self.f.db.session.commit()
        self.scope = dict(self.f.scope, resume_expired_task_ids=[],
            resume_parked_task_ids=[self.job_id], bounded_native_continuation=True,
            existing_max_task_id=self.f.other_id, preserved_old_task_ids=[self.f.other_id])
        self.anchor()

    def anchor(self):
        with closing(sqlite3.connect(self.f.f.root/'school_watcher.db')) as c:
            c.row_factory = sqlite3.Row
            rows = [dict(r) for r in c.execute('SELECT * FROM background_tasks')]
        self.scope['original_target_deadlines'] = {
            str(r['id']):r['deadline_at'] for r in rows if r['id'] == self.job_id}
        path = self.f.f.root/'task-certificate.json'
        digest = lambda r: hashlib.sha256(json.dumps(r,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        pilot.save(path, dict(admitted_task_ids=[self.job_id], protected_task_ids=[self.f.other_id],
            all_existing_task_rows={str(r['id']):digest(r) for r in rows}))
        self.scope.update(full_task_row_fingerprints=path.relative_to(pilot.ROOT).as_posix(),
            full_task_row_fingerprints_sha256=pilot.sha(path))
        pilot.save(self.f.f.root/'anchor.json', pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256'] = pilot.sha(self.f.f.root/'anchor.json')

    def test_unexpired_parked_task_is_claimable_without_resetting_identity_or_progress(self):
        from backend.services import tasks
        before = self.f.rows()
        with patch.object(tasks,'enqueue',side_effect=AssertionError('No recreation')):
            pilot.initialize_native_state(self.scope)
        after = self.f.rows()
        self.assertLessEqual(after[self.job_id].pop('available_at'),datetime.utcnow())
        before[self.job_id].pop('available_at')
        self.assertEqual(after,before)
        with pilot.school_queue(self.scope,self.f.school_id):
            handle = tasks.claim(capabilities=['directory'])
            self.assertEqual((handle['id'],handle['generation']),(self.job_id,self.f.target_generation))
            self.assertTrue(tasks.finish(handle,{'fixture':True}))

    def test_expired_parked_task_gets_only_the_declared_extra_window(self):
        self.job.deadline_at = datetime.utcnow()-timedelta(days=1)
        self.f.db.session.commit()
        self.anchor()
        before = self.f.rows()
        pilot.initialize_native_state(self.scope)
        after = self.f.rows()
        stamp = after[self.job_id].pop('deadline_at')
        self.assertTrue(datetime.utcnow()+timedelta(seconds=18) < stamp < datetime.utcnow()+timedelta(seconds=21))
        after[self.job_id].pop('available_at')
        before[self.job_id].pop('deadline_at')
        before[self.job_id].pop('available_at')
        self.assertEqual(after,before)

    def test_changed_rows_and_invalid_or_overlapping_permissions_are_rejected(self):
        for changes in (dict(resume_parked_task_ids=[True]),dict(resume_parked_task_ids=[self.job.id]*2),
                        dict(bounded_native_continuation=False),dict(maximum_wall_seconds=901),
                        dict(resume_expired_task_ids=[self.job.id])):
            with self.subTest(changes=changes),self.assertRaises(AssertionError):
                pilot.continuation_state(dict(self.scope,**changes))
        self.job.checkpoint = {'changed':True}
        self.f.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'row changed'):
            pilot.initialize_native_state(self.scope)

    def test_commit_failure_rolls_back_release_without_touching_other_jobs(self):
        from sqlalchemy.exc import OperationalError
        before = deepcopy(self.f.rows())
        with patch.object(self.f.db.session(),'commit',side_effect=OperationalError('fixture',{},None)):
            with self.assertRaises(OperationalError):
                pilot.initialize_native_state(self.scope)
        self.assertEqual(self.f.rows(),before)


class ContinuationClosureTests(unittest.TestCase):
    def setUp(self):
        self.f = lifecycle_fixture.FreshSamplingLifecycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        with closing(sqlite3.connect(self.f.path)) as c,c:
            c.executescript('ALTER TABLE background_tasks ADD COLUMN deadline_at TEXT;'
                            'CREATE TABLE departments(id INTEGER,school_id INTEGER);'
                            'INSERT INTO departments VALUES(700,5);')
            for identity,school,state,worker in [(110,5,'pending',None),(111,5,'pending',None),
                    (112,5,'running','declared-native-pilot'),(113,5,'pending',None),
                    (114,4,'pending',None),(115,5,'running','other-worker')]:
                payload = dict(department_id=700) if identity==113 else dict(school_id=school)
                c.execute('INSERT INTO background_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (identity,json.dumps(payload),state,3,'token' if worker else None,worker,
                     'future' if worker else None,'original','saved-progress','extended'))
        self.scope = dict(self.f.scope, continuation_school_id=5, existing_max_task_id=112,
            resume_parked_task_ids=[111,112], bounded_native_continuation=True, maximum_wall_seconds=20,
            preserved_old_task_ids=[110], original_target_deadlines={'111':'before111','112':'before112'})

    def rows(self):
        with closing(sqlite3.connect(self.f.path)) as c:
            c.row_factory = sqlite3.Row
            return {r['id']:dict(r) for r in c.execute('SELECT * FROM background_tasks')}

    def test_parent_closes_only_declared_and_new_school_tasks_after_child_exit(self):
        before = self.rows()
        result = park_continuation_tasks(self.f.instance,self.scope,child_confirmed_stopped=True)
        self.assertEqual(result['parked_ids'],[111,112,113])
        self.assertEqual(result['fenced_ids'],[112])
        self.assertEqual(result['unresolved_owned_ids'],[115])
        after = self.rows()
        for identity in (110,114,115):
            self.assertEqual(after[identity],before[identity])
        for identity in (111,112,113):
            self.assertEqual(after[identity]['generation'],3)
            self.assertEqual(after[identity]['checkpoint'],'saved-progress')
        self.assertEqual(after[111]['deadline_at'],'before111')
        self.assertEqual(after[112]['deadline_at'],'before112')

    def test_observation_timeout_cannot_fence_a_child_whose_exit_is_unconfirmed(self):
        before = self.rows()
        result = park_continuation_tasks(self.f.instance,self.scope,child_confirmed_stopped=False)
        self.assertEqual(result['unresolved_owned_ids'],[112,115])
        after = self.rows()
        self.assertEqual(after[112],before[112])
        self.assertEqual(after[115],before[115])

    def test_limits_restore_even_when_the_child_raises(self):
        from scripts.checks import native_sampling_lifecycle as wrapper
        before = self.f.values()
        with self.assertRaisesRegex(RuntimeError,'fixture child'):
            with wrapper.scope_lifecycle(self.f.instance,self.scope,self.f.output):
                raise RuntimeError('fixture child')
        self.assertEqual(self.f.values(),before)
        self.assertTrue(json.loads((self.f.output/'temporary-limits-restored.json').read_text())['restored'])
        self.assertFalse(json.loads((self.f.output/'admitted-window-closed.json').read_text())['passed'])
