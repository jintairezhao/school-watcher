"""The retained real unknown response permits one observed read, not ownership."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.ai.skill_loader import load_skill, validate_output
from backend.scraper.discovery.layered import assist, navigation_candidates, route_structure
from backend.scraper.discovery.navigation_roles import supported
from backend.scraper.discovery.structure import extract_structure

ROOT = 'https://www.cupk.edu.cn/'
TARGET = ROOT + 'library/xxsyjy/'
ACTUAL = json.loads((Path(__file__).parent / 'fixtures/navigation_unknown_actual.json').read_text(encoding='utf-8'))
MENU = ('<nav id="library-menu"><li><span>教学支持</span><ul><li>'
        '<a href="/library/xxsyjy/">信息素养教育</a></li></ul></li></nav>')


class UnknownNavigationReuseTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def page(self, name, policy='valuable'):
        return {'url': ROOT + f'library/{name}/', 'label': name, 'kind': 'channel', 'depth': 2,
                'path_json': json.dumps(['教学支持', '图书馆'] + (['图书借阅'] if name == 'jyzj' else [])),
                'discovery_policy': policy}

    def parse(self, page, html):
        return route_structure(extract_structure(html, page['url'], ROOT, page['kind'], page['label'],
            json.loads(page['path_json'])), page, html, ROOT, policy=page['discovery_policy'])

    def run_pages(self, policy):
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        parsed_pages = []

        def actual_response(*args, **kwargs):
            validate_output(load_skill('student-information', 'navigation'), ACTUAL['output'], args[2])
            return {'status': 'succeeded', 'output': copy.deepcopy(ACTUAL['output']), 'usage': ACTUAL['usage']}

        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 2, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=actual_response) as model:
            results = []
            for name in ('jygz', 'jyzj'):
                handle['checkpoint'] = {}
                page, html = self.page(name, policy), '<title>图书馆</title>' + MENU
                parsed = self.parse(page, html)
                before_nodes = copy.deepcopy(parsed['nodes'])
                result = assist({'root_url': ROOT}, page, html, parsed)
                results.append(result)
                parsed_pages.append(parsed)
                self.assertEqual(result['status'], 'processed')
                self.assertEqual(parsed['nodes'], before_nodes, 'Unknown must not create or upgrade owner relationships')
            self.assertEqual(model.call_count, 1)
        self.assertEqual(results[1]['cached_routes'], 1)
        for parsed in parsed_pages:
            link = next(row for row in parsed['links'] if row['url'] == TARGET)
            self.assertEqual(link['ai_role'], 'unknown')
            self.assertEqual(link['ai_value'], 'unknown')
            self.assertTrue(link['probe_only'])
            self.assertEqual(link['kind'], 'gateway')
            self.assertEqual(link['decision'], 'follow')
        return parsed_pages

    def test_actual_unknown_quote_can_support_an_explicit_observed_read_action(self):
        page = self.page('jygz')
        link, _ = navigation_candidates(MENU, self.parse(page, MENU), page)[0]
        action = dict(ACTUAL['output']['results'][0], action='read_observed')
        self.assertTrue(supported(action, link['_navigation_material']))
        action['facts'] = [{'quote': '图书借阅 / 图书馆 / 教学支持'}]
        self.assertFalse(supported(action, link['_navigation_material']))
        action['facts'] = []
        self.assertFalse(supported(action, link['_navigation_material']))

    def test_actual_unknown_is_reused_for_reading_in_valuable_policy(self):
        self.run_pages('valuable')

    def test_actual_unknown_is_reused_for_reading_in_ordinary_layered_policy(self):
        self.run_pages('layered')

    def test_saved_ordinary_unknown_checkpoint_advances_to_an_observed_read_without_rebilling(self):
        from backend.ai.skill_loader import digest
        page = self.page('jygz', 'layered')
        parsed = self.parse(page, MENU)
        candidates = navigation_candidates(MENU, parsed, page)
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {
            'route_material_hash': digest([(link['url'], link['label'], context) for link, context in candidates]),
            'route_cursor': 1, 'admitted_routes': [],
            'unresolved_routes': [{'name': '信息素养教育', 'url': TARGET}]}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Saved uncertainty must not be repurchased')) as model:
            result = assist({'root_url': ROOT}, page, MENU, parsed)
        model.assert_not_called()
        self.assertEqual(result['status'], 'processed')
        self.assertEqual(handle['checkpoint']['unresolved_routes'], [])
        self.assertEqual(parsed['links'][0]['ai_role'], 'unknown')
        self.assertTrue(parsed['links'][0]['probe_only'])
        self.assertEqual(parsed['links'][0]['decision'], 'follow')

    def test_unknown_action_reuses_only_the_same_complete_observed_menu(self):
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        first = self.page('jygz')
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda data: handle.update(checkpoint=data)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 2, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'succeeded', 'output': ACTUAL['output']}) as model:
            assist({'root_url': ROOT}, first, MENU, self.parse(first, MENU))
            handle['checkpoint'] = {}
            changed = MENU.replace('教学支持', '科研支持')
            second = self.page('jyzj')
            result = assist({'root_url': ROOT}, second, changed, self.parse(second, changed),
                            decision_allowed=lambda execution_id: False)
            self.assertEqual(result['status'], 'branch_stopped')
            self.assertEqual(result['cached_routes'], 0)
            self.assertEqual(model.call_count, 1)

    def test_unknown_read_is_deduplicated_and_target_material_remains_refreshable(self):
        from backend.database.models import Department
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.services.discovery_changes import seed_check
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.source_inventory import Inventory
        parsed_pages = self.run_pages('valuable')
        folder = Path(self.fixture.temp.name)
        store = Inventory(folder / 'unknown-frontier.db')
        key = store.ensure_site('中国石油大学克拉玛依校区', ROOT)
        store.finish(key, ROOT, state='fetched', html='<main>校区首页</main>')
        for parsed in parsed_pages:
            for link in parsed['links']:
                if link['decision'] == 'follow':
                    store.enqueue(key, link['url'], link['label'], link['kind'], 1, link['path'], 'school_domain')
        reads = []
        target_html = '<main>原来只有办事说明</main>'

        def fetch(url):
            reads.append(url)
            return {'url': url, 'status': 200, 'html': target_html if url == TARGET else '<main>校区首页</main>'}

        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Public reads do not buy navigation decisions')):
            crawl_site(store, key, max_pages=3, workers=1, focus='valuable', fetcher=fetch)
            first_hash = store.get_page(key, TARGET)['content_hash']
            self.assertEqual(reads, [TARGET])
            # Re-observing the cached action cannot requeue the fetched target.
            link = parsed_pages[1]['links'][0]
            store.enqueue(key, TARGET, link['label'], link['kind'], 1, link['path'], 'school_domain')
            crawl_site(store, key, max_pages=3, workers=1, focus='valuable', fetcher=fetch)
            self.assertEqual(reads, [TARGET])
            catalog = RuntimeCatalog(folder / 'unknown-catalog.db')
            catalog.publish(store, key, merge=True)
            seed_check(store, key, catalog, 'real-material-refresh')
            target_html = '<section><h2>通知公告</h2><ul>' + ''.join(
                f'<li><a href="/info/1001/{12340 + index}.htm">关于开展学生学习活动{index}的通知</a>'
                '<span>2026-10-15</span></li>' for index in range(3)) + '</ul></section>'
            crawl_site(store, key, max_pages=3, workers=1, focus='valuable', fetcher=fetch)
        current = store.get_page(key, TARGET)
        self.assertNotEqual(current['content_hash'], first_hash)
        self.assertTrue(current['feed_json'], 'The new target body is independently observed as a list')
        self.assertEqual(reads.count(TARGET), 2)
        self.assertEqual(Department.query.count(), 0, 'An unknown menu action cannot activate a source')

    def test_unknown_action_cannot_reclassify_and_requeue_an_already_fetched_target(self):
        from backend.database.db import db
        from backend.scraper.discovery.ai_navigation import process_navigation
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('中国石油大学克拉玛依校区', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': MENU, 'status': 200})
        store.enqueue(key, TARGET, '信息素养教育', 'channel', 1, [], 'school_domain')
        store.finish(key, TARGET, state='fetched', html='<main>目标的既有实读材料</main>')
        before = store.get_page(key, TARGET)
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state, parent.capability = 'done', 'http'
        db.session.commit()
        tasks.enqueue('navigation_review', 'unknown-read-dedup', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 2, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'succeeded', 'output': ACTUAL['output']}):
            result = process_navigation(handle['payload'])
        tasks.finish(handle, result)
        after = store.get_page(key, TARGET)
        self.assertEqual(result['role_corrections'], 0)
        self.assertEqual(after['state'], 'fetched')
        self.assertEqual(after['kind'], before['kind'])
        self.assertEqual(after['priority'], before['priority'])
        self.assertEqual(after['content_hash'], before['content_hash'])


if __name__ == '__main__':
    unittest.main()
