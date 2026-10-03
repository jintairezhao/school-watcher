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

    def test_ai_follows_unusual_student_route_and_keeps_low_value_as_evidence(self):
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
        self.assertEqual(routes['职工专栏']['decision'], 'route_reference')
        self.assertEqual(result['status'], 'processed')

    def test_navigation_has_a_small_per_page_budget_and_preserves_other_links(self):
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
            self.assertEqual(assist({'root_url': ROOT}, page, html, parse())['status'], 'pending')
            parsed = parse()
            result = assist({'root_url': ROOT}, page, html, parsed)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(result['deferred_routes'], 19)
        self.assertEqual(len({row['url'] for row in parsed['links']}), 35)


class DiscoveryBudgetTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_slices_share_budget_and_refresh_continues_unread_pages(self):
        from backend.services.discovery_cache import DiscoveryCache, adapt_site
        from backend.services import tasks
        from backend.scraper.discovery.inventory_crawler import crawl_site
        self.fixture.app.config['DISCOVERY_PAGE_BUDGET'] = 4
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
            tasks.finish(handle, second)
            self.assertEqual(len(read), 4)
            self.assertFalse(second['continuation_required'])
            self.assertTrue(second['exploration_limited'])
            self.assertEqual(second['deferred_pages'], 3)
            saved = list(read)
            tasks.enqueue('discover', self.fixture.school_id, payload, replace_finished=True)
            handle = tasks.claim(capabilities=['directory'])
            with tasks.execution_scope(handle):
                result = adapt_site('测试大学', ROOT, monthly=True)
            self.assertEqual(len(read), 7)
            self.assertEqual(read[:4], saved)
            self.assertEqual(len(set(read)), 7)
            self.assertFalse(result['continuation_required'])

    def test_navigation_children_resume_at_seventeenth_link_without_reopening_spent_scan(self):
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
        parent.state = 'done'; parent.checkpoint = {'student_scan_pages': 40}; db.session.commit()
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
            self.assertEqual(model.call_count, 2)
            self.assertEqual(first['deferred_routes'], 19)
            self.assertEqual(len(analyzed), 16)
            self.assertEqual(db.session.get(BackgroundTask, parent.id).state, 'done')
            self.assertEqual(parent.checkpoint['student_scan_pages'], 40)
            second = round(2)
            self.assertEqual(model.call_count, 4)
            self.assertEqual(second['deferred_routes'], 3)
            # All routes can be decided while their target pages still await
            # the next bounded crawl. Do not lose those late-arriving leads.
            third = round(3)
            self.assertEqual(third['deferred_routes'], 0)
            self.assertEqual(third['deferred_pages'], 35)
            db.session.refresh(parent)
            self.assertEqual(parent.state, 'done')
            self.assertEqual(parent.result['deferred_pages'], 35)
        self.assertEqual(len(analyzed), 35)
        self.assertEqual(len(set(analyzed)), 35)
        self.assertIn(ROOT + 'space/16', analyzed)

    def test_navigation_page_budget_survives_multiple_crawl_slices(self):
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
        self.assertEqual(BackgroundTask.query.filter_by(kind='navigation_review').count(), 2)
        self.assertTrue(handle['checkpoint']['navigation_budget_limited'])


if __name__ == '__main__':
    unittest.main()
