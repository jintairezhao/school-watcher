"""Discovery pause survives queueing, handoffs and restart without replaying paid work."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
import unittest
from unittest.mock import patch

import test_shared_summaries as fixture_module
from backend.database.db import db
from backend.database.models import BackgroundTask, Subscription, RuntimeLease, VerificationSession
from backend.services import tasks
from backend.services.discovery_control import control, pause_if_requested, PAUSE_KEY
from backend.services.onboarding_progress import record_progress


class DiscoveryPauseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.SharedSummaryTests(); self.fixture.setUp()
        self.school_id = self.fixture.ann.school_id
        db.session.add_all([Subscription(user_id=self.fixture.admin.id, school_id=self.school_id),
                            Subscription(user_id=self.fixture.reader.id, school_id=self.school_id)])
        db.session.commit()
        self.row = tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'ai_assist': True})
        self.row.checkpoint = {'directory_refresh_started': True, 'ai_navigation': {'root': 'succeeded'},
                               'discovery_progress': {'checked_pages': 608, 'pending_pages': 392, 'phase': 'crawl'}}
        db.session.commit()
        self.client = self.fixture.client(self.fixture.admin)
        self.endpoint = f'/api/subscriptions/{self.school_id}/discovery'
        self.headers = {'X-CSRF-Token': 'token'}

    def tearDown(self):
        self.fixture.tearDown()

    def command(self, action):
        return self.client.post(self.endpoint, json={'action': action}, headers=self.headers)

    def test_pending_pause_and_resume_preserve_checkpoint_and_generation(self):
        checkpoint, generation, identity = self.row.checkpoint, self.row.generation, self.row.id
        with patch('backend.services.onboarding_progress.ai_available', return_value=False):
            result = self.command('pause')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json['state'], 'paused')
            self.assertTrue(result.json['can_resume'])
            self.assertFalse(result.json['active'])
            self.assertEqual(result.json['checked_pages'], 608)
            self.assertIsNone(tasks.claim(capabilities=['directory']))
            tasks.enqueue('discover', self.school_id, {'refresh': True}, expedite=True)
            self.assertEqual(self.command('pause').json['state'], 'paused')
            self.assertEqual(self.client.get(self.endpoint).json['state'], 'paused')
            self.assertIn('>继续</button>', self.client.get(f'/subscriptions/{self.school_id}').text)
            self.assertEqual(self.command('resume').json['state'], 'pending')
        db.session.refresh(self.row)
        self.assertEqual(self.row.checkpoint, checkpoint)
        self.assertEqual((self.row.id, self.row.generation), (identity, generation))
        handle = tasks.claim(capabilities=['directory'])
        self.assertEqual(handle['checkpoint'], checkpoint)
        self.assertEqual(handle['generation'], generation)

    def test_running_pause_waits_for_safe_boundary_and_releases_lane(self):
        handle = tasks.claim(capabilities=['directory'])
        self.assertEqual(self.command('pause').json['state'], 'pausing')
        self.assertEqual(self.command('resume').status_code, 409)
        self.assertTrue(tasks.heartbeat(handle))
        # In-flight page / AI results can still commit before the next boundary.
        with tasks.execution_scope(handle):
            checkpoint = dict(handle['checkpoint'], ai_navigation={'root': 'succeeded', 'next': 'succeeded'})
            tasks.checkpoint(checkpoint)
            record_progress(checked_pages=609, pending_pages=391)
            with self.assertRaises(tasks.TaskDeferred) as deferred:
                pause_if_requested()
        db.session.rollback()
        self.assertTrue(tasks.handoff(handle, deferred.exception))
        self.assertEqual(self.client.get(self.endpoint).json['state'], 'paused')
        self.assertEqual(RuntimeLease.query.filter_by(key='directory:writer').count(), 0)
        self.assertFalse(tasks.finish(handle, {'late': True}))
        self.command('resume')
        resumed = tasks.claim(capabilities=['directory'])
        self.assertEqual(resumed['checkpoint']['discovery_progress']['checked_pages'], 609)
        self.assertEqual(resumed['checkpoint']['ai_navigation']['next'], 'succeeded')
        self.assertEqual(resumed['generation'], handle['generation'])

    def test_competing_browser_handoff_keeps_pause_and_resume_capability(self):
        handle = tasks.claim(capabilities=['directory'])
        self.command('pause')
        checkpoint = dict(handle['checkpoint'], browser={'submitted': True, 'request_id': 'same-request'})
        tasks.handoff(handle, tasks.TaskDeferred(capability='browser', phase='render', checkpoint=checkpoint, delay=45))
        db.session.refresh(self.row)
        wait_until = self.row.available_at
        self.assertEqual(self.row.phase, 'user_paused')
        self.command('resume')
        db.session.refresh(self.row)
        self.assertEqual((self.row.phase, self.row.capability), ('render', 'browser'))
        self.assertEqual(self.row.available_at, wait_until)
        self.assertEqual(self.row.checkpoint['browser']['request_id'], 'same-request')
        self.assertIsNone(tasks.claim())

    def test_expired_worker_can_be_paused_without_losing_deadline_on_resume(self):
        handle = tasks.claim(capabilities=['directory'])
        self.row.lease_until = datetime.utcnow() - timedelta(minutes=1)
        self.row.deadline_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        self.assertEqual(self.command('pause').json['state'], 'paused')
        self.assertFalse(tasks.finish(handle, {'stale': True}))
        self.command('resume')
        db.session.refresh(self.row)
        self.assertGreater(self.row.deadline_at, datetime.utcnow())
        self.assertIsNotNone(tasks.claim(capabilities=['directory']))

    def test_crash_after_pause_request_stops_before_next_request(self):
        handle = tasks.claim(capabilities=['directory'])
        self.command('pause')
        self.row.lease_until = datetime.utcnow() - timedelta(seconds=1)
        self.row.deadline_at = datetime.utcnow() - timedelta(hours=1)
        db.session.commit()
        db.session.query(RuntimeLease).update({'expires_at': datetime.utcnow() - timedelta(seconds=1)})
        db.session.commit()
        self.assertIsNone(tasks.claim(capabilities=['directory']))
        self.assertEqual(self.client.get(self.endpoint).json['state'], 'paused')
        db.session.refresh(self.row)
        self.assertEqual(self.row.generation, handle['generation'])
        self.assertEqual(RuntimeLease.query.filter_by(key='directory:writer').count(), 0)
        self.command('resume')
        self.assertIsNotNone(tasks.claim(capabilities=['directory']))

    def test_retry_honors_pause_but_completed_work_remains_done(self):
        handle = tasks.claim(capabilities=['directory']); self.command('pause')
        self.assertTrue(tasks.finish(handle, error='temporary website issue'))
        self.assertEqual(self.client.get(self.endpoint).json['state'], 'paused')
        self.command('resume')
        self.row.available_at = datetime.utcnow(); db.session.commit()
        handle = tasks.claim(capabilities=['directory']); self.command('pause')
        self.assertTrue(tasks.finish(handle, result={'complete': True}))
        db.session.refresh(self.row)
        self.assertEqual(self.row.state, 'done')
        self.assertFalse(self.row.payload.get(PAUSE_KEY))
        self.assertEqual(self.command('pause').status_code, 409)

    def test_verified_while_paused_only_continues_after_manual_resume(self):
        self.row.state, self.row.phase = 'waiting', 'verification'
        self.row.checkpoint = dict(self.row.checkpoint, verification_id='fixture-session')
        db.session.add(VerificationSession(id='fixture-session', source_id='fixture', origin='https://example.edu.cn',
            url='https://example.edu.cn/', task_id=self.row.id, status='required'))
        db.session.commit()
        self.command('pause')
        session = db.session.get(VerificationSession, 'fixture-session')
        session.status = 'verified'; db.session.commit()
        tasks.resume_verification('fixture')
        self.assertEqual(self.client.get(self.endpoint).json['state'], 'paused')
        self.command('resume'); db.session.refresh(self.row)
        self.assertEqual((self.row.state, self.row.phase), ('pending', 'render'))

    def test_commands_require_subscription_admin_and_csrf(self):
        reader = self.fixture.client(self.fixture.reader)
        self.assertEqual(reader.post(self.endpoint, json={'action':'pause'}, headers=self.headers).status_code, 403)
        self.assertEqual(self.client.post(self.endpoint, json={'action':'pause'}).status_code, 403)
        self.assertEqual(self.command('invalid').status_code, 400)
        for value in ([], 'pause', {'action':None}):
            self.assertEqual(self.client.post(self.endpoint, json=value, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post('/api/subscriptions/999999/discovery', json={'action':'pause'},
                                         headers=self.headers).status_code, 404)

    def test_actual_crawler_finishes_current_page_then_resumes_remaining_frontier(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        inventory = Inventory(Path(self.fixture.temp.name) / 'pause-inventory.db')
        root_url = 'https://example.edu.cn/'
        key = inventory.ensure_site('测试学校', root_url)
        handle = tasks.claim(capabilities=['directory'])
        visited = []
        def fetcher(url):
            visited.append(url)
            if len(visited) == 1:
                # A separate DB connection emulates the UI while a page is in flight.
                def pause_request():
                    with self.fixture.app.app_context():
                        control(self.school_id, 'pause')
                        db.session.remove()
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(pause_request).result(timeout=10)
                return {'html':'<html><title>测试学校</title><a href="/college/">测试学院</a></html>', 'status':200, 'url':url}
            return {'html':'<html><title>测试学院</title></html>', 'status':200, 'url':url}
        def progress(processed, snapshot):
            record_progress(checked_pages=processed, pending_pages=snapshot['states'].get('pending', 0))
        with patch('backend.scraper.discovery.ai_navigation.assist_navigation'), tasks.execution_scope(handle):
            with self.assertRaises(tasks.TaskDeferred) as deferred:
                crawl_site(inventory, key, fetcher=fetcher, progress=progress)
        db.session.rollback(); tasks.handoff(handle, deferred.exception)
        self.assertEqual(visited, [root_url])
        self.assertEqual(inventory.progress_snapshot(key)['states'].get('fetched'), 1)
        self.command('resume')
        resumed = tasks.claim(capabilities=['directory'])
        with patch('backend.scraper.discovery.ai_navigation.assist_navigation'), tasks.execution_scope(resumed):
            crawl_site(inventory, key, fetcher=fetcher)
        self.assertEqual(visited, [root_url, root_url + 'college/'])
        self.assertEqual(inventory.progress_snapshot(key)['states'].get('fetched'), 2)

    def test_pause_during_ai_keeps_its_discovered_links_without_a_second_call(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        inventory = Inventory(Path(self.fixture.temp.name) / 'pause-ai.db')
        root_url = 'https://example.edu.cn/'
        key = inventory.ensure_site('测试学校', root_url)
        # Run the real assist_navigation, including its mid-page ledger checkpoint.
        self.row.checkpoint = {}; db.session.commit()
        handle = tasks.claim(capabilities=['directory'])
        def ai_result(*args, **kwargs):
            def pause_request():
                with self.fixture.app.app_context():
                    control(self.school_id, 'pause')
                    db.session.remove()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(pause_request).result(timeout=10)
            return {'status':'succeeded', 'output':{'results':[
                {'candidate_id':'link-0', 'decision':'propose', 'kind':'unit'}]}}
        def fetcher(url):
            return {'html':'<html><title>测试学校</title><a href="/ai-unit/">了解更多</a></html>'
                    if url == root_url else '<html><title>下属单位</title></html>', 'status':200, 'url':url}
        with patch('backend.ai.runtime.run_skill', side_effect=ai_result) as ai:
            with tasks.execution_scope(handle), self.assertRaises(tasks.TaskDeferred) as deferred:
                crawl_site(inventory, key, fetcher=fetcher)
            db.session.rollback(); tasks.handoff(handle, deferred.exception)
            self.assertEqual(ai.call_count, 1)
            self.command('resume')
            resumed = tasks.claim(capabilities=['directory'])
            with tasks.execution_scope(resumed):
                crawl_site(inventory, key, fetcher=fetcher)
            self.assertEqual(ai.call_count, 1)
        self.assertTrue(any(p['url'] == root_url + 'ai-unit/' and p['state'] == 'fetched'
                            for p in inventory.report(key)['pages']))


if __name__ == '__main__':
    unittest.main()
