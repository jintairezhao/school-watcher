"""Useful publishing routes precede exhaustive organization mapping."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.layered import route_structure, assist
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://example.edu.cn/'


class ValueRouteTests(unittest.TestCase):
    def test_observed_notice_is_read_before_a_large_department_roster(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Inventory(Path(folder) / 'sources.db')
            key = store.ensure_site('测试大学', ROOT)
            store.finish(key, ROOT, state='fetched')
            store.enqueue(key, ROOT + 'colleges/', '院系设置', 'directory', 1, [], 'school_domain')
            store.enqueue(key, ROOT + 'notices/', '选课通知', 'channel', 1, [], 'school_domain')
            first = store.claim(key, focus='valuable')
            self.assertEqual(first['url'], ROOT + 'notices/')
            store.finish(key, first['url'], state='fetched')
            self.assertEqual(store.claim(key, focus='valuable')['url'], ROOT + 'colleges/')

    def test_ai_follows_unusual_student_route_and_keeps_low_value_readable(self):
        html = '<div class="main-menu"><a href="/grow/">成长空间</a><a href="/staff/">职工专栏</a><a href="/other/">探索入口</a></div>'
        page = {'url': ROOT, 'label': '测试大学', 'kind': 'root', 'path_json': '[]',
                'depth': 0, 'discovery_policy': 'valuable'}
        parsed = route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT,
                                 policy='valuable')
        handle = {'payload': {'school_id': 1}, 'checkpoint': {}}
        def reply(*args, **kwargs):
            rows = []
            for candidate in args[2]['candidates']:
                value, role = {'成长空间': ('relevant', 'gateway'), '职工专栏': ('low', 'gateway'),
                               '探索入口': ('unknown', 'unknown')}[candidate['name']]
                rows.append({'candidate_id': candidate['candidate_id'], 'value': value, 'role': role})
            return {'status': 'succeeded', 'output': {'results': rows}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=reply):
            result = assist({'root_url': ROOT}, page, html, parsed)
        routes = {row['label']: row for row in parsed['links']}
        self.assertEqual(routes['成长空间']['decision'], 'follow')
        self.assertEqual(routes['探索入口']['decision'], 'follow')
        self.assertEqual(routes['职工专栏']['decision'], 'follow')
        self.assertEqual(routes['职工专栏']['ai_value'], 'low')
        self.assertEqual(result['status'], 'processed')

    def test_navigation_batches_all_candidates_without_a_per_page_cutoff(self):
        html = '<nav>' + ''.join(f'<a href="/space/{i}">成长空间{i}</a>' for i in range(35)) + '</nav>'
        page = {'url': ROOT, 'label': '测试大学', 'kind': 'root', 'path_json': '[]',
                'depth': 0, 'discovery_policy': 'valuable'}
        handle = {'payload': {'school_id': 1}, 'checkpoint': {}}
        def parse():
            return route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT, policy='valuable')
        def reply(*args, **kwargs):
            return {'status': 'succeeded', 'output': {'results': [
                {'candidate_id': c['candidate_id'], 'role': 'gateway', 'value': 'relevant'}
                for c in args[2]['candidates']]}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=reply) as model:
            for index in range(5):
                parsed = parse()
                result = assist({'root_url': ROOT}, page, html, parsed)
                self.assertEqual(result['status'], 'pending' if index < 4 else 'processed')
        self.assertEqual(model.call_count, 5)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['deferred_routes'], 0)
        self.assertEqual(result['reviewed_routes'], 35)
        self.assertEqual(len({row['url'] for row in parsed['links']}), 35)


class DiscoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_slices_continue_immediately_without_a_school_page_cutoff(self):
        from backend.services.discovery_cache import DiscoveryCache, adapt_site
        from backend.services import tasks
        from backend.scraper.discovery.inventory_crawler import crawl_site
        self.fixture.app.config['DISCOVERY_PAGE_BUDGET'] = 4
        self.fixture.app.config['DISCOVERY_POLICY'] = {'discovery_slice_pages': 3}
        pages = {ROOT: '<nav>' + ''.join(f'<a href="/notices/{i}">通知公告{i}</a>' for i in range(6)) + '</nav>'}
        pages.update({ROOT + f'notices/{i}': '<main>尚未发布通知</main>' for i in range(6)})
        read = []
        def fetch(url):
            read.append(url)
            return {'url': url, 'html': pages[url], 'status': 200}
        def crawl(*args, **kwargs):
            return crawl_site(*args, **kwargs, fetcher=fetch)
        payload = {'school_id': self.fixture.school_id, 'ai_assist': False}
        tasks.enqueue('discover', self.fixture.school_id, payload)
        handle = tasks.claim(capabilities=['directory'])
        with patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=crawl):
            with tasks.execution_scope(handle):
                first = adapt_site('测试大学', ROOT)
                self.assertTrue(first['continuation_required'])
                second = adapt_site('测试大学', ROOT)
                self.assertEqual(len(read), 6)
                self.assertTrue(second['continuation_required'])
                self.assertFalse(second['exploration_limited'])
                self.assertEqual(second['deferred_pages'], 1)
                result = adapt_site('测试大学', ROOT)
            tasks.finish(handle, result)
            self.assertEqual(len(read), 7)
            self.assertEqual(len(set(read)), 7)
            self.assertFalse(result['continuation_required'])

    def test_navigation_child_resumes_all_batches_and_reopens_remaining_crawl(self):
        from datetime import datetime
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.services.discovery_changes import baseline
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav>' + ''.join(f'<a href="/space/{i}">成长空间{i}</a>' for i in range(35)) + '</nav>'
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; parent.capability = 'http'
        parent.checkpoint = {'student_scan_pages': 40}; db.session.commit()
        analyzed = []
        def reply(*args, **kwargs):
            analyzed.extend(c['url'] for c in args[2]['candidates'])
            return {'status': 'succeeded', 'output': {'results': [
                {'candidate_id': c['candidate_id'], 'role': 'gateway', 'value': 'relevant'}
                for c in args[2]['candidates']]}}
        def round(number):
            page = dict(store.get_page(key, ROOT), discovery_policy='valuable',
                navigation_routes=baseline(store.get_page(key, ROOT)).get('routes', []))
            job = tasks.enqueue('navigation_review', f'round-{number}', {
                'parent_task_id': parent.id, 'parent_generation': parent.generation,
                'school_id': self.fixture.school_id, 'school_generation': parent.generation,
                'site': store.report(key)['site'], 'page': page})
            while True:
                handle = tasks.claim(capabilities=['directory'])
                self.assertEqual(handle['id'], job.id)
                try:
                    with tasks.execution_scope(handle):
                        result = process_navigation(handle['payload'])
                except tasks.TaskDeferred as deferred:
                    tasks.handoff(handle, deferred)
                    db.session.get(BackgroundTask, job.id).available_at = datetime.utcnow(); db.session.commit()
                else:
                    tasks.finish(handle, result)
                    return result
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=reply) as model:
            first = round(1)
            self.assertEqual(model.call_count, 5)
            self.assertEqual(first['deferred_routes'], 0)
            self.assertEqual(first['reviewed_routes'], 35)
            self.assertEqual(len(analyzed), 35)
            self.assertEqual(db.session.get(BackgroundTask, parent.id).state, 'pending')
            self.assertEqual(parent.checkpoint['student_scan_pages'], 40)
            self.assertEqual(first['deferred_pages'], 35)
        self.assertEqual(len(analyzed), 35)
        self.assertEqual(len(set(analyzed)), 35)
        self.assertIn(ROOT + 'space/16', analyzed)

    def test_navigation_queue_deduplicates_without_a_fixed_page_limit(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import queue_navigation
        self.fixture.app.config['DISCOVERY_AI_PAGE_BUDGET'] = 2
        tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            for index in range(3):
                queue_navigation({'root_url': ROOT}, {'url': ROOT + str(index), 'kind': 'gateway',
                    'discovery_policy': 'valuable'})
            queue_navigation({'root_url': ROOT}, {'url': ROOT + '0', 'kind': 'gateway',
                'discovery_policy': 'valuable'})
        self.assertEqual(BackgroundTask.query.filter_by(kind='navigation_review').count(), 3)
        self.assertNotIn('navigation_budget_limited', handle['checkpoint'])


if __name__ == '__main__':
    unittest.main()
