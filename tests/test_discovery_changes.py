"""Changes are probed once; ordinary reads and polling never schedule paid AI."""
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, School, Subscription
from backend.services import tasks
from backend.services.discovery_changes import baseline, seed_check, remember_navigation
from backend.services.source_inventory import Inventory
from backend.services.runtime_catalog import RuntimeCatalog
from backend.scraper.discovery.inventory_crawler import crawl_site

ROOT = 'https://example.edu.cn/'


class ChangeFingerprintTests(unittest.TestCase):
    def test_new_notices_do_not_invalidate_column_but_new_template_does(self):
        from tests.test_direct_onboarding import HTML, URL
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'scratch.db')
            key = store.ensure_site('测试大学', ROOT)
            store.finish(key, ROOT, state='reference_only')
            store.enqueue(key, URL, '通知公告', 'channel', 1, [], 'school_domain')
            def run(html):
                with store.connect() as c:
                    c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (key, URL))
                with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as ai:
                    crawl_site(store, key, max_pages=1, workers=1, focus='layered',
                        fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
                    ai.assert_not_called()
                return baseline(store.get_page(key, URL))
            first = run(HTML)
            updated = HTML.replace('2026-10-01', '2026-10-02').replace('/info/0.htm', '/info/999.htm').replace('课程选课', '学籍核对')
            self.assertEqual(run(updated)['hash'], first['hash'])
            self.assertEqual(run(updated)['change'], 'unchanged')
            self.assertEqual(run(updated.replace('id="notices"', 'id="new-list"'))['change'], 'changed')

    def test_catalogue_restores_decisions_after_scratch_eviction_without_ai(self):
        from backend.scraper.discovery.structure import extract_structure
        from backend.scraper.discovery.layered import route_structure
        html = '<nav><a href="/grow/">成长空间</a></nav>'
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'old.db')
            key = store.ensure_site('测试大学', ROOT)
            fetcher = lambda url: {'url': url, 'html': html, 'status': 200}
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as ai:
                crawl_site(store, key, max_pages=1, workers=1, focus='layered', fetcher=fetcher)
                self.assertEqual(ai.call_count, 1)
            page = store.get_page(key, ROOT)
            parsed = route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT)
            for link in parsed['links']:
                if link['url'] == ROOT + 'grow/':
                    link.update(kind='channel', decision='follow')
            remember_navigation(store, key, ROOT, parsed, True)
            catalog = RuntimeCatalog(Path(folder) / 'catalog.db', versioned=False)
            catalog.publish(store, key)
            fresh = Inventory(Path(folder) / 'new.db')
            fresh.ensure_site('测试大学', ROOT)
            seed_check(fresh, key, catalog, 'manual:1')
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as ai:
                crawl_site(fresh, key, max_pages=1, workers=1, focus='layered', fetcher=fetcher)
                ai.assert_not_called()
            self.assertEqual(baseline(fresh.get_page(key, ROOT))['change'], 'unchanged')
            self.assertIsNotNone(fresh.get_page(key, ROOT + 'grow/'))
            seed_check(fresh, key, catalog, 'manual:1')
            self.assertEqual(fresh.get_page(key, ROOT)['state'], 'fetched')

    def test_independent_department_and_channel_are_probed_even_if_root_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'scratch.db')
            key = store.ensure_site('测试大学', ROOT)
            for kind in ('unit', 'channel'):
                store.enqueue(key, ROOT + kind + '/', kind, kind, 1, [], 'school_domain')
                store.finish(key, ROOT + kind + '/', state='fetched', html='<main>旧结构</main>')
            store.finish(key, ROOT, state='fetched', html='<main>学校主页</main>')
            catalog = RuntimeCatalog(Path(folder) / 'catalog.db', versioned=False)
            catalog.publish(store, key)
            seed_check(store, key, catalog, 'manual:1')
            self.assertEqual({p['state'] for p in store.report(key)['pages']}, {'pending'})


class DeltaPipelineTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_first_subscription_is_idempotent_but_empty_results_retry_automatically(self):
        from backend.routes.subscriptions import subscribe_school
        from backend.worker import _schedule_due
        school = db.session.get(School, self.fixture.school_id)
        user_id = Subscription.query.one().user_id
        Subscription.query.delete(); school.subscriber_count = 0; db.session.commit()
        subscribe_school(school, user_id)
        task = BackgroundTask.query.filter_by(kind='discover').one()
        self.assertEqual(task.payload['trigger'], 'first_subscription')
        task.state = 'done'; task.finished_at = datetime.utcnow() - timedelta(days=90); db.session.commit()
        task_id = task.id
        _schedule_due()
        self.assertEqual(db.session.get(BackgroundTask, task_id).generation, 2)
        self.assertEqual(db.session.get(BackgroundTask, task_id).payload['trigger'], 'background_discovery')
        school = db.session.get(School, self.fixture.school_id)
        Subscription.query.delete(); school.subscriber_count = 0; db.session.commit()
        subscribe_school(school, user_id)
        self.assertEqual(db.session.get(BackgroundTask, task_id).generation, 2)

    def test_normal_read_has_no_ai_and_legacy_automatic_job_is_skipped(self):
        from backend.services.source_onboarding import onboard_page
        from backend.services.content_cache import request_content
        from backend.services.student_information import process, queue_assessment
        from tests.test_direct_onboarding import URL
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No paid AI')):
            onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
            ann = Announcement.query.first()
            ann.content_text = '普通通知正文'; ann.content_html = '<p>普通通知正文</p>'
            ann.content_cached_at = datetime.utcnow(); db.session.commit()
            request_content(ann)
            self.assertIsNone(queue_assessment('article', ann.id))
            self.assertEqual(process({'subject_kind': 'article', 'subject_id': ann.id})['state'], 'skipped')
        self.assertEqual(BackgroundTask.query.filter_by(kind='student_assessment').count(), 0)

    def test_verified_template_repair_preserves_source_subscription_and_notice_ids(self):
        from backend.services.source_onboarding import onboard_page
        from backend.database.source_governance_models import SourceConfigVersion
        from tests.test_direct_onboarding import URL, HTML
        first = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        ids = [a.id for a in Announcement.query.order_by(Announcement.id)]
        subscription = Subscription.query.one(); subscription.department_ids = first['department_ids']; db.session.commit()
        changed = HTML.replace('id="notices"', 'id="new-notices"')
        def fetch(url, purpose):
            return changed if url == URL else self.fixture.fetch(url, purpose)
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Working rules need no AI')):
            second = onboard_page({'school_id': self.fixture.school_id, 'url': URL, 'revalidate': True}, fetcher=fetch)
        self.assertEqual(second['department_ids'], first['department_ids'])
        self.assertEqual([a.id for a in Announcement.query.order_by(Announcement.id)], ids)
        self.assertEqual(subscription.department_ids, first['department_ids'])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(SourceConfigVersion.query.count(), 2)

    def test_analysis_get_is_read_only_post_shared_and_login_required(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL
        onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        ann = Announcement.query.first()
        ann.content_text = '申请材料和历史参考'; ann.content_html = '<p>申请材料和历史参考</p>'; db.session.commit()
        client = self.fixture.app.test_client()
        endpoint = f'/api/announcements/{ann.id}/student-information'
        self.assertEqual(client.get(endpoint).status_code, 401)
        with client.session_transaction() as session:
            session['user_id'] = Subscription.query.one().user_id
            session['_csrf_token'] = 'token'
        self.assertEqual(client.get(endpoint).status_code, 200)
        self.assertEqual(BackgroundTask.query.filter_by(kind='student_assessment').count(), 0)
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            for _ in range(2):
                self.assertEqual(client.post(endpoint, headers={'X-CSRF-Token': 'token'}).status_code, 202)
        job = BackgroundTask.query.filter_by(kind='student_assessment').one()
        self.assertEqual(job.payload['requested_by'], Subscription.query.one().user_id)
        self.assertEqual(job.generation, 1)

    def test_observed_redirect_can_repair_url_without_replacing_subscription(self):
        from backend.services.source_onboarding import onboard_page
        from tests.test_direct_onboarding import URL, HTML
        first = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        moved = ROOT + 'moved-notices/'
        second = onboard_page({'school_id': self.fixture.school_id, 'url': moved,
            'revalidate': True, 'previous_url': URL},
            fetcher=lambda url, purpose: HTML if url == moved else self.fixture.fetch(url, purpose))
        self.assertEqual(second['department_ids'], first['department_ids'])
        self.assertEqual(Department.query.one().list_url, moved)

    def test_legacy_periodic_scan_cannot_trigger_exploration(self):
        from backend.worker import dispatch
        with patch('backend.services.discovery_cache.adapt_site', side_effect=AssertionError('No periodic scan')):
            result = dispatch('discover', {'school_id': self.fixture.school_id, 'refresh': True})
        self.assertTrue(result['automatic_discovery_disabled'])

    def test_unchanged_connected_column_not_requeued_changed_column_once(self):
        from tests.test_direct_onboarding import HTML, URL
        from backend.services.source_onboarding import onboard_page, queue_columns
        store = Inventory(Path(self.fixture.temp.name) / 'columns.db')
        key = store.ensure_site('测试大学', ROOT)
        store.finish(key, ROOT, state='reference_only')
        store.enqueue(key, URL, '通知公告', 'channel', 1, [], 'school_domain')
        def read(html):
            with store.connect() as c:
                c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (key, URL))
            crawl_site(store, key, max_pages=1, workers=1, focus='layered',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        read(HTML)
        initial = queue_columns(self.fixture.school_id, store, key)
        job = db.session.get(BackgroundTask, initial['onboarding_ids'][0])
        job.result = onboard_page(job.payload, fetcher=self.fixture.fetch)
        job.state = 'done'; db.session.commit()
        read(HTML.replace('2026-10-01', '2026-10-02'))
        self.assertEqual(queue_columns(self.fixture.school_id, store, key)['onboarding_ids'], [])
        read(HTML.replace('id="notices"', 'id="redesigned"'))
        self.assertEqual(queue_columns(self.fixture.school_id, store, key)['onboarding_ids'], [job.id])
        self.assertEqual(queue_columns(self.fixture.school_id, store, key)['onboarding_ids'], [])
        db.session.refresh(job)
        self.assertTrue(job.payload['revalidate'])
        self.assertEqual(job.generation, 2)


if __name__ == '__main__':
    unittest.main()
