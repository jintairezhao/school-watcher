"""Bounded waiting-reader recovery and exact redirect permits; no live calls."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.checks import run_native_branch_pilot as pilot
from scripts.checks.native_sampling_redirects import allowed_observed_redirect
from scripts.checks.native_sampling_lifecycle import park_reader_tasks
from tests import test_native_pilot_expired_resume as recovery_fixture
from tests import test_native_sampling_lifecycle as lifecycle_fixture


class WaitingReaderRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = recovery_fixture.NativePilotExpiredResumeTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.job = self.f.target
        self.job.state = 'waiting'
        self.job.phase = 'sampling_scope'
        self.job.error_code = 'sampling_outside_declared_host'
        self.job.available_at = datetime.utcnow() + timedelta(days=1)
        self.f.db.session.commit()
        self.job_id = self.job.id
        self.scope = dict(self.f.scope, resume_expired_task_ids=[], resume_boundary_task_ids=[self.job_id])
        self.anchor()

    def anchor(self):
        pilot.save(self.f.f.root / 'anchor.json', pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256'] = pilot.sha(self.f.f.root / 'anchor.json')

    def test_expired_scope_wait_becomes_claimable_without_recreating_or_resetting_progress(self):
        from backend.services import tasks
        self.scope['model_free_native_slice'] = True
        before = self.f.rows()
        with patch.object(tasks, 'enqueue', side_effect=AssertionError('No task recreation')):
            pilot.initialize_native_state(self.scope)
        after = self.f.rows()
        for field in ('state', 'available_at', 'deadline_at'):
            before[self.job_id].pop(field)
            after[self.job_id].pop(field)
        self.assertEqual(before, after)
        with pilot.school_queue(self.scope, self.f.school_id):
            handle = tasks.claim(capabilities=['directory'])
        self.assertEqual((handle['id'], handle['generation']), (self.job_id, self.f.target_generation))
        self.assertTrue(tasks.finish(handle, {'synthetic': True}))

    def test_wrong_reason_school_state_and_duplicate_ids_are_not_generic_wake_permissions(self):
        for changes in ({'resume_boundary_task_ids': [self.job.id, self.job.id]},
                        {'resume_boundary_task_ids': [self.f.other_id]},
                        {'resume_boundary_task_ids': [True]}):
            with self.subTest(changes=changes), self.assertRaises(AssertionError):
                pilot.continuation_state(dict(self.scope, **changes))
        for field, value in (('error_code', 'user_paused'), ('state', 'done'), ('worker_id', 'another-worker')):
            old = getattr(self.job, field)
            setattr(self.job, field, value)
            self.f.db.session.commit()
            with self.subTest(field=field), self.assertRaises(AssertionError):
                pilot.continuation_state(self.scope)
            setattr(self.job, field, old)
            self.f.db.session.commit()

    def test_failed_commit_preserves_the_wait_and_every_other_task(self):
        from sqlalchemy.exc import OperationalError
        before = deepcopy(self.f.rows())
        with patch.object(self.f.db.session(), 'commit', side_effect=OperationalError('release', {}, None)):
            with self.assertRaises(OperationalError):
                pilot.initialize_native_state(self.scope)
        self.assertEqual(before, self.f.rows())

    def test_zero_model_mode_accepts_one_waiting_target_and_refuses_another_target(self):
        from tests.test_native_model_free_slice import scope
        value = scope()
        value.update(resume_expired_task_ids=[], resume_boundary_task_ids=[77])
        self.assertTrue(pilot.validate_model_free_scope(value))
        value['resume_expired_task_ids'] = [78]
        with self.assertRaises(ValueError):
            pilot.validate_model_free_scope(value)


class ExactRedirectPermissionTests(unittest.TestCase):
    def setUp(self):
        self.permit = dict(request_url='http://faculty.example.edu/', source_host='faculty.example.edu',
                           status=301, location='https://9.9.9.9/')
        self.scope = dict(website=dict(observed_redirect_permissions=[self.permit]))
        self.ledger = dict(documents=[dict(sequence=1, request=dict(url=self.permit['request_url']))],
            http_attempts=[dict(document=1, host=self.permit['source_host'], status=301,
                                location=self.permit['location'], finished_at='observed')])

    def allowed(self, **changes):
        args = dict(document_id=1, wire_url='https://9.9.9.9/', host='9.9.9.9', method='GET')
        args.update(changes)
        return allowed_observed_redirect(self.scope, self.ledger, **args)

    def test_only_the_declared_fresh_redirect_is_admitted(self):
        self.assertTrue(self.allowed())
        for changes in (dict(wire_url='https://9.9.9.9/other'), dict(document_id=2),
                        dict(wire_url='http://9.9.9.9/'), dict(method='POST'), dict(host='other.example.edu')):
            with self.subTest(changes=changes):
                self.assertFalse(self.allowed(**changes))

    def test_an_old_batch_or_other_document_cannot_supply_redirect_proof(self):
        for field, value in (('document', 2), ('status', 200), ('host', 'another.example.edu'),
                             ('finished_at', None), ('location', 'https://8.8.8.8/')):
            old = self.ledger['http_attempts'][0][field]
            self.ledger['http_attempts'][0][field] = value
            with self.subTest(field=field):
                self.assertFalse(self.allowed())
            self.ledger['http_attempts'][0][field] = old

    def test_private_addresses_are_refused_even_if_declared(self):
        self.permit['location'] = 'https://127.0.0.1/'
        self.ledger['http_attempts'][0]['location'] = self.permit['location']
        self.assertFalse(self.allowed(wire_url=self.permit['location'], host='127.0.0.1'))

    def test_real_bounds_follow_one_declared_hop_but_block_another_ip_path_before_send(self):
        import aiohttp
        from backend.scraper.acquisition import FetchRequest
        from backend.scraper.acquisition import http_transport
        sent = []
        class Response:
            def __init__(self, number):
                self.status = 301 if number == 1 else 302
                self.headers = {'Location': 'https://9.9.9.9/' if number == 1 else '/another-path'}
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        async def request(session, method, url, **kwargs):
            sent.append(str(url))
            return Response(len(sent))
        with tempfile.TemporaryDirectory(prefix='exact-redirect-', dir=pilot.BASE) as directory:
            root = Path(directory)
            scope = dict(self.scope, expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
                         model=dict(maximum_dispatch_attempts=0))
            scope['website'] = dict(scope['website'], allowed_hosts=['faculty.example.edu'],
                maximum_documents=1, maximum_http_attempts=3, maximum_response_bytes=1024)
            bounds = pilot.Bounds(scope, root, root/'observations', time.monotonic()+30)
            with patch.object(aiohttp.ClientSession, '_request', request), \
                    patch('backend.scraper.acquisition.http_transport.public_addresses', return_value=['9.9.9.9']), \
                    patch('backend.scraper.acquisition.http_transport._shared_permit', return_value=None), bounds.install():
                with self.assertRaises(pilot.SamplingBoundary):
                    http_transport.fetch_http(FetchRequest(self.permit['request_url']))
            ledger = pilot.read(bounds.web_file)
            self.assertEqual(len(sent), 2, ledger)
            self.assertEqual([r['status'] for r in ledger['http_attempts']], [301, 302])
            self.assertEqual(ledger['boundary']['reason'], 'outside_declared_host')


class ReaderWindowClosureTests(unittest.TestCase):
    def setUp(self):
        self.f = lifecycle_fixture.FreshSamplingLifecycleTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        with closing(sqlite3.connect(self.f.path)) as c, c:
            c.execute('ALTER TABLE background_tasks ADD COLUMN deadline_at TEXT')
            for identity, school, worker in ((111, 5, 'declared-native-pilot'), (112, 5, None),
                                             (115, 5, None), (116, 4, None), (117, 5, 'another-worker')):
                c.execute('INSERT INTO background_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (identity, json.dumps(dict(school_id=school)), 'running' if worker else 'pending',
                     2, 'token' if worker else None, worker, 'lease' if worker else None,
                     'old', 'keep-checkpoint', 'changed-deadline'))
        self.scope = dict(model_free_native_slice=True, resume_boundary_task_ids=[111],
            existing_max_task_id=114, continuation_school_id=5, original_target_deadline='original-deadline')

    def rows(self):
        with closing(sqlite3.connect(self.f.path)) as c:
            return {r[0]: r for r in c.execute('SELECT * FROM background_tasks')}

    def test_terminal_child_cleanup_parks_only_the_reader_and_new_school_children(self):
        before = self.rows()
        result = park_reader_tasks(self.f.instance, self.scope, child_confirmed_stopped=True)
        after = self.rows()
        self.assertEqual(result['parked_ids'], [111, 115])
        self.assertEqual(result['fenced_ids'], [111])
        self.assertEqual(result['unresolved_owned_ids'], [117])
        for identity in (112, 116, 117):
            self.assertEqual(before[identity], after[identity])
        self.assertEqual(after[111][-1], 'original-deadline')
        self.assertEqual(after[111][3], 2)
        self.assertEqual(after[111][-2], 'keep-checkpoint')

    def test_observation_timeout_does_not_clear_a_running_reader(self):
        before = self.rows()
        result = park_reader_tasks(self.f.instance, self.scope, child_confirmed_stopped=False)
        self.assertEqual(before[111], self.rows()[111])
        self.assertIn(111, result['unresolved_owned_ids'])
