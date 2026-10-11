"""AI identifies observed entrances; executable website evidence stays authoritative."""
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from backend.ai.skill_loader import load_skill, validate_output, SkillValidationError
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.scraper.discovery.layered import assist, navigation_candidates, route_structure
from backend.scraper.discovery.structure import extract_structure
from backend.services.source_inventory import Inventory

ROOT = 'https://example.edu.cn/'


def reply(evidence, role='publishing', value='potential'):
    return {'status': 'succeeded', 'output': {'results': [
        {'candidate_id': candidate['candidate_id'], 'role': role, 'value': value,
         'historical': 'unknown', 'reason': '官网入口与附近文字',
         'facts': [{'kind': 'relevance', 'text': candidate['name'],
                    'evidence_id': candidate['candidate_id'], 'quote': candidate['name']}]}
        for candidate in evidence['candidates']]}}


class PrimaryNavigationTests(unittest.TestCase):
    def page(self):
        return {'url': ROOT, 'label': '测试大学', 'kind': 'root', 'path_json': '[]',
                'depth': 0, 'discovery_policy': 'valuable'}

    def parse(self, html):
        return route_structure(extract_structure(html, ROOT, ROOT), self.page(), html, ROOT,
                               policy='valuable')

    def test_recognizable_units_and_columns_are_sent_to_ai_too(self):
        html = '<title>测试大学官网</title><nav><a href="/org/">组织机构</a>' \
               '<a href="/teaching/">教务处</a><a href="/notices/">通知公告</a></nav>'
        self.assertEqual({link['label'] for link, _ in navigation_candidates(html, self.parse(html), self.page())},
                         {'组织机构', '教务处', '通知公告'})
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'sources.db')
            key = inventory.ensure_site('测试大学', ROOT)
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as queue:
                crawl_site(inventory, key, max_pages=1, workers=1, focus='valuable',
                           fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
            self.assertEqual(queue.call_count, 1)
            # Model latency does not prevent probing an actual published URL.
            self.assertIsNotNone(inventory.get_page(key, ROOT + 'notices/'))

    def test_low_value_does_not_suppress_a_real_column_and_context_reaches_ai(self):
        html = '<title>测试大学官网</title><main><h1>办事与服务</h1><section>' \
               '<p>这里持续发布学生和教职工办事材料。</p><a href="/work/">通知公告</a></section></main>'
        parsed, handle = self.parse(html), {'payload': {'school_id': 1}, 'checkpoint': {}}
        def answer(*args, **kwargs):
            evidence = args[2]
            self.assertIn('测试大学官网', evidence['evidence'][0]['text'])
            self.assertIn('办事与服务', evidence['evidence'][0]['text'])
            self.assertIn('学生和教职工办事材料', evidence['evidence'][0]['text'])
            self.assertEqual(evidence['candidates'][0]['kind'], 'channel')
            return reply(evidence, value='low')
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=answer) as model:
            result = assist({'root_url': ROOT}, self.page(), html, parsed)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['reviewed_routes'], 1)
        self.assertEqual(parsed['links'][0]['decision'], 'follow')
        self.assertEqual(parsed['links'][0]['ai_value'], 'low')

    def test_explicit_ai_opt_out_is_checked_before_model_configuration(self):
        html = '<nav><a href="/grow/">成长空间</a></nav>'
        with patch('backend.services.tasks.current_execution', return_value={'payload': {'ai_assist': False}}), \
             patch('backend.ai.configuration.get_model_binding') as binding, \
             patch('backend.ai.runtime.run_skill') as model:
            result = assist({'root_url': ROOT}, self.page(), html, self.parse(html))
        binding.assert_not_called()
        model.assert_not_called()
        self.assertEqual(result['error_code'], 'ai_disabled')

    def test_model_cannot_change_names_urls_ownership_or_cite_another_candidate(self):
        evidence = {'school_id': 1, 'candidates': [
            {'candidate_id': 'one', 'name': '教务处', 'url': ROOT + 'office/', 'kind': 'unit', 'path': ''}],
            'evidence': [{'candidate_id': 'one', 'evidence_id': 'one', 'text': '教务处发布办事通知'}]}
        skill = load_skill('student-information', 'navigation')
        for field, value in [('name', '虚构单位'), ('url', ROOT + 'invented/'),
                             ('parent_entity_id', 'made-up'), ('candidate_id', 'unknown')]:
            output = reply(evidence)['output']
            output['results'][0][field] = value
            with self.subTest(field=field), self.assertRaises(SkillValidationError):
                validate_output(skill, output, evidence)
        output = reply(evidence)['output']
        output['results'][0]['facts'][0]['quote'] = '虚构的奖学金保证'
        with self.assertRaises(SkillValidationError):
            validate_output(skill, output, evidence)
        evidence['evidence'].append({'candidate_id': 'different', 'evidence_id': 'different', 'text': '教务处'})
        output = reply(evidence)['output']
        output['results'][0]['facts'][0]['evidence_id'] = 'different'
        with self.assertRaises(SkillValidationError):
            validate_output(skill, output, evidence)


class NavigationPersistenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_identical_observed_navigation_reuses_roles_across_pages_without_copying_owner(self):
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        site = {'root_url': ROOT}
        first = dict(PrimaryNavigationTests().page(), path_json='["学校"]')
        second = dict(first, url=ROOT + 'index/teaching.htm', label='本科生教育',
                      path_json='["招生与培养"]')
        first_html = '<title>测试大学</title><nav id="school-menu"><ul><li>' \
            '<a href="office/">学生事务中心</a></li></ul></nav><h1>首页新闻</h1>'
        second_html = '<title>本科生教育-测试大学</title><nav id="school-menu"><ul><li>' \
            '<a href="../office/">学生事务中心</a></li></ul></nav><h1>本科生教育</h1>'

        def run(page, html):
            parsed = route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
                page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')
            handle['checkpoint'] = {}
            result = assist(site, page, html, parsed)
            return result, parsed

        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *a, **kw: reply(a[2], role='unit')) as model:
            run(first, first_html)
            result, parsed = run(second, second_html)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['cached_routes'], 1)
        self.assertEqual(parsed['links'][0]['path'], ['招生与培养'])
        self.assertEqual(parsed['links'][0]['label'], '学生事务中心')

    def test_navigation_change_and_another_dom_region_do_not_reuse_roles(self):
        from backend.scraper.discovery.navigation_roles import region_material
        from bs4 import BeautifulSoup
        def material(html):
            soup = BeautifulSoup(html, 'lxml')
            return region_material(soup.select('a')[-1], ROOT)['key']
        one = '<nav id="one"><a href="/same/">通知公告</a><span>学生事务</span></nav>'
        two = '<nav id="two"><a href="/same/">通知公告</a><span>学生事务</span></nav>'
        self.assertNotEqual(material(one), material(two))
        self.assertNotEqual(material(one), material(one.replace('学生事务', '科研事务')))
        self.assertNotEqual(material(one), material(one.replace('/same/', '/new/')))
        self.assertNotEqual(material(one), material(one + one))

    def test_nav_link_is_not_a_region_and_parent_hierarchy_changes_invalidate_roles(self):
        from backend.scraper.discovery.navigation_roles import region_material
        from bs4 import BeautifulSoup
        def material(html):
            soup = BeautifulSoup(html, 'lxml')
            return region_material(soup.a, ROOT)['key']
        menu = '<nav><a class="nav-link" href="/same/">通知公告</a>' \
               '<a href="/jobs/">就业指导</a></nav>'
        self.assertNotEqual(material(menu), material(menu.replace('就业指导', '校友活动')))
        submenu = '<nav><li><h3>学生事务</h3><ul class="submenu"><li>' \
                  '<a href="/same/">通知公告</a></li></ul></li></nav>'
        self.assertNotEqual(material(submenu), material(submenu.replace('学生事务', '科研事务')))

    def test_role_citing_page_specific_material_cannot_enter_shared_navigation_cache(self):
        from backend.scraper.discovery.navigation_roles import supported, region_material
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<title>奖学金办理</title><nav><a href="/same/">通知公告</a></nav>', 'lxml')
        material = region_material(soup.a, ROOT)
        self.assertFalse(supported({'role': 'publishing', 'facts': [{'quote': '奖学金办理'}]}, material))
        self.assertFalse(supported({'role': 'unknown', 'facts': [{'quote': '通知公告'}]}, material))
        self.assertFalse(supported({'role': 'reference', 'facts': [{'quote': '通知公告'}]}, material))
        self.assertTrue(supported({'role': 'publishing', 'facts': [{'quote': '通知公告'}]}, material))

    def test_navigation_model_skill_and_school_versions_are_separate(self):
        from backend.scraper.discovery.navigation_roles import candidate_key
        link = {'url': ROOT + 'notices/', 'label': '通知公告', 'kind': 'channel'}
        material = {'key': 'real-region'}
        def key(root=ROOT, version=1, skill='skill-1'):
            return candidate_key({'root_url': root}, link, material,
                                 {'id': 1, 'version': version}, skill, 'valuable')
        self.assertNotEqual(key(), key(version=2))
        self.assertNotEqual(key(), key(skill='skill-2'))
        self.assertNotEqual(key(), key(root='https://another.edu.cn/'))

    def test_cached_navigation_still_uses_fair_batches_and_needs_no_budgeted_requests(self):
        from backend.ai.configuration import AIConfigError
        html = '<nav id="school-menu">' + ''.join(
            f'<a href="/space/{index}">成长空间{index}</a>' for index in range(9)) + '</nav>'
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        page = PrimaryNavigationTests().page()
        def run():
            parsed = route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
                page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')
            return assist({'root_url': ROOT}, page, html, parsed), parsed
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            with patch('backend.ai.runtime.run_skill', side_effect=lambda *a, **kw: reply(a[2], role='gateway')) as model:
                self.assertEqual(run()[0]['remaining_routes'], 1)
                self.assertEqual(run()[0]['remaining_routes'], 0)
                self.assertEqual(model.call_count, 2)
            page = dict(page, url=ROOT + 'index/teaching.html', path_json='["招生与培养"]')
            handle['checkpoint'] = {}
            with patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')) as model:
                first, _ = run()
                second, parsed = run()
                model.assert_not_called()
            self.assertEqual((first['status'], first['remaining_routes'], first['cached_routes']), ('pending', 1, 8))
            self.assertEqual((second['status'], second['remaining_routes'], second['cached_routes']), ('processed', 0, 9))
            self.assertNotIn('decision_id', first)
            self.assertEqual(sum(link.get('ai_reviewed', False) for link in parsed['links']), 9)

    def test_cached_roles_are_saved_when_an_uncached_candidate_waits_for_budget(self):
        from backend.ai.configuration import AIConfigError
        html = '<nav>' + ''.join(
            f'<a href="/space/{index}">成长空间{index}</a>' for index in range(8)) + '</nav>'
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        page = PrimaryNavigationTests().page()
        def parsed():
            return route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
                page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')
        def initial(*args, **kwargs):
            result = reply(args[2], role='gateway')
            result['output']['results'][-1]['role'] = 'unknown'
            return result
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            with patch('backend.ai.runtime.run_skill', side_effect=initial):
                assist({'root_url': ROOT}, page, html, parsed())
            page = dict(page, url=ROOT + 'another.html')
            handle['checkpoint'] = {}
            with patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')):
                for _ in range(2):
                    current = parsed()
                    result = assist({'root_url': ROOT}, page, html, current)
                    self.assertEqual(result['error_code'], 'budget_exhausted')
                    self.assertEqual(sum(link.get('ai_reviewed', False) for link in current['links']), 7)
                    self.assertEqual(len(handle['checkpoint']['admitted_routes']), 7)
                    self.assertEqual(handle['checkpoint']['cached_navigation_routes'], 7)
                    self.assertEqual(handle['checkpoint']['route_cursor'], 0)
                    self.assertNotIn('decision_id', result)

    def test_uncertain_request_keeps_its_evidence_when_another_page_populates_cache(self):
        html = '<title>测试大学</title><nav>' + ''.join(
            f'<a href="/space/{index}">成长空间{index}</a>' for index in range(8)) + '</nav>'
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        page = PrimaryNavigationTests().page()
        calls, charged_ids = [], set()
        def parsed():
            return route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
                page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')
        def uncertain(*args, **kwargs):
            calls.append((args[4], args[2]))
            charged_ids.add(args[4])
            return {'status': 'uncertain', 'error_code': 'timeout_result_unknown'}
        def other_page(*args, **kwargs):
            result = reply(args[2], role='reference')
            result['output']['results'][0]['role'] = 'gateway'
            return result
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            with patch('backend.ai.runtime.run_skill', side_effect=uncertain):
                assist({'root_url': ROOT}, page, html, parsed())
            first_page, first_html, saved = page, html, json.loads(json.dumps(handle['checkpoint']))
            page = dict(page, url=ROOT + 'another.html')
            html = html.replace('测试大学</title>', '本科生教育-测试大学</title>')
            handle['checkpoint'] = {}
            with patch('backend.ai.runtime.run_skill', side_effect=other_page):
                assist({'root_url': ROOT}, page, html, parsed())
            page, html, handle['checkpoint'] = first_page, first_html, saved
            with patch('backend.ai.runtime.run_skill', side_effect=uncertain):
                result = assist({'root_url': ROOT}, page, html, parsed())
            self.assertEqual(result['error_code'], 'timeout_result_unknown')
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(len(charged_ids), 1)
            with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 2}), \
                 patch('backend.ai.runtime.run_skill') as model:
                changed = assist({'root_url': ROOT}, page, html, parsed())
            self.assertEqual(changed['error_code'], 'execution_conflict')
            model.assert_not_called()

    def test_legacy_uncertain_execution_is_restored_before_shared_cache_filters_batch(self):
        from backend.ai.models import AIExecution, AIProfile
        from backend.ai.skill_loader import digest
        from backend.database.db import db
        html = '<title>测试大学</title><nav>' + ''.join(
            f'<a href="/space/{index}">成长空间{index}</a>' for index in range(8)) + '</nav>'
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        page = PrimaryNavigationTests().page()
        calls = []
        db.session.add(AIProfile(id=1, name='模拟服务', provider='deepseek', model='deepseek-flash',
                                 encrypted_key='not-a-real-key', version=1))
        db.session.commit()
        skill = load_skill('student-information', 'navigation')
        def parsed():
            return route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
                page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')
        def uncertain(*args, **kwargs):
            calls.append((args[4], args[2]))
            if db.session.get(AIExecution, args[4]) is None:
                db.session.add(AIExecution(execution_id=args[4], purpose='directory', profile_id=1,
                    config_version='1', provider='deepseek', model='deepseek-flash', skill_id=skill.id,
                    skill_version=skill.version, skill_digest=skill.resource_digest,
                    input_digest=digest({'evidence': args[2], 'repair_feedback': None}), mode='navigation',
                    status='uncertain', request_sent=True, usage={'known': False},
                    error_code='timeout_result_unknown'))
                with patch('backend.services.tasks.current_execution', return_value=None):
                    db.session.commit()
            return {'status': 'uncertain', 'error_code': 'timeout_result_unknown'}
        def other_page(*args, **kwargs):
            result = reply(args[2], role='reference')
            result['output']['results'][0]['role'] = 'gateway'
            return result
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
            with patch('backend.ai.runtime.run_skill', side_effect=uncertain):
                assist({'root_url': ROOT}, page, html, parsed())
            first_page, first_html = page, html
            page = dict(page, url=ROOT + 'another.html')
            html = html.replace('测试大学</title>', '本科生教育-测试大学</title>')
            handle['checkpoint'] = {}
            with patch('backend.ai.runtime.run_skill', side_effect=other_page):
                assist({'root_url': ROOT}, page, html, parsed())
            # Older releases had the durable AIExecution but no request plan.
            page, html, handle['checkpoint'] = first_page, first_html, {}
            with patch('backend.ai.runtime.run_skill', side_effect=uncertain):
                assist({'root_url': ROOT}, page, html, parsed())
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(AIExecution.query.count(), 1)
            handle['checkpoint'] = {}
            with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 2}), \
                 patch('backend.ai.runtime.run_skill') as model:
                changed = assist({'root_url': ROOT}, page, html, parsed())
            self.assertEqual(changed['error_code'], 'execution_conflict')
            model.assert_not_called()

    def navigation_job(self, html, identity):
        from backend.database.db import db
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; parent.capability = 'http'; db.session.commit()
        job = tasks.enqueue('navigation_review', identity, {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        return store, key, job

    def test_navigation_budget_wait_preserves_completed_batch_without_new_generation(self):
        from datetime import datetime
        from backend.ai.configuration import AIConfigError
        from backend.database.db import db
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav>' + ''.join(f'<a href="/space/{index}">成长空间{index}</a>' for index in range(9)) + '</nav>'
        _, _, job = self.navigation_job(html, 'budget-wait')
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs: reply(args[2], role='gateway')):
            handle = tasks.claim(capabilities=['directory'])
            try:
                with tasks.execution_scope(handle):
                    process_navigation(handle['payload'])
            except tasks.TaskDeferred as deferred:
                self.assertEqual(deferred.state, 'pending')
                tasks.handoff(handle, deferred)
        job.available_at = datetime.utcnow(); db.session.commit()
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('额度已用完', 'budget_exhausted')):
            handle = tasks.claim(capabilities=['directory'])
            try:
                with tasks.execution_scope(handle):
                    process_navigation(handle['payload'])
            except tasks.TaskDeferred as deferred:
                self.assertEqual((deferred.state, deferred.phase), ('waiting', 'ai_budget'))
                tasks.handoff(handle, deferred)
        db.session.refresh(job)
        self.assertEqual(job.state, 'waiting')
        self.assertEqual(job.error_code, 'budget_exhausted')
        self.assertEqual(job.generation, 1)
        self.assertEqual(job.checkpoint['route_cursor'], 8)
        self.assertEqual(len(job.checkpoint['admitted_routes']), 8)
        self.assertIsNone(tasks.claim(capabilities=['directory']))

    def test_changed_snapshot_skips_old_navigation_before_any_paid_call(self):
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import process_navigation
        store, key, _ = self.navigation_job('<nav><a href="/old/">成长空间</a></nav>', 'changed-before-call')
        store.finish(key, ROOT, state='fetched', html='<nav><a href="/new/">最新入口</a></nav>')
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), patch('backend.ai.runtime.run_skill') as model:
            result = process_navigation(handle['payload'])
        self.assertEqual((result['status'], result['error_code']), ('stale', 'material_changed'))
        model.assert_not_called()

    def test_result_from_old_snapshot_cannot_overwrite_new_observed_edges(self):
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import process_navigation
        store, key, _ = self.navigation_job('<nav><a href="/old/">成长空间</a></nav>', 'changed-during-call')
        handle = tasks.claim(capabilities=['directory'])
        def answer(*args, **kwargs):
            import hashlib
            new_html = '<nav><a href="/new/">最新入口</a></nav>'
            store.finish(key, ROOT, state='fetched', html=new_html)
            store.record_edges(key, ROOT, [{'url': ROOT + 'new/', 'label': '最新入口',
                'kind': 'gateway', 'decision': 'navigation_pending', 'path': [], 'locator': 'nav a'}],
                hashlib.sha256(new_html.encode()).hexdigest())
            return reply(args[2], role='gateway')
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=answer):
            result = process_navigation(handle['payload'])
        self.assertEqual((result['status'], result['error_code']), ('stale', 'material_changed'))
        self.assertIsNone(store.get_page(key, ROOT + 'old/'))
        with store.connect() as connection:
            urls = [row[0] for row in connection.execute('SELECT target_url FROM edges WHERE site_key=?', (key,))]
        self.assertEqual(urls, [ROOT + 'new/'])

    def test_branch_stops_after_two_empty_progress_windows_without_losing_remaining_links(self):
        from datetime import datetime
        from backend.database.db import db
        from backend.services import tasks
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav>' + ''.join(f'<a href="/space/{index}">成长空间{index}</a>' for index in range(100)) + '</nav>'
        store, key, job = self.navigation_job(html, 'no-progress')
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs: reply(args[2], role='gateway')) as model:
            for _ in range(9):
                handle = tasks.claim(capabilities=['directory'])
                try:
                    with tasks.execution_scope(handle):
                        result = process_navigation(handle['payload'])
                except tasks.TaskDeferred as deferred:
                    tasks.handoff(handle, deferred)
                    job.available_at = datetime.utcnow(); db.session.commit()
                else:
                    tasks.finish(handle, result)
                    break
        self.assertEqual(model.call_count, 8)
        self.assertEqual(result['status'], 'branch_stopped')
        self.assertGreater(result['remaining_routes'], 0)
        with store.connect() as connection:
            self.assertEqual(connection.execute('SELECT count(DISTINCT target_url) FROM edges WHERE site_key=?', (key,)).fetchone()[0], 100)
        db.session.refresh(job)
        self.assertTrue(job.checkpoint['navigation_progress']['stopped'])

    def test_ai_priority_reorders_real_columns_without_removing_low_value_and_survives_cache(self):
        from backend.database.db import db
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav><a href="/a/">一般通知</a><a href="/z/">学生通知</a></nav>'
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        fetch = lambda url: {'url': url, 'html': html, 'status': 200}
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable', fetcher=fetch)
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; db.session.commit()
        tasks.enqueue('navigation_review', 'priority', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        handle = tasks.claim(capabilities=['directory'])
        def answer(*args, **kwargs):
            output = reply(args[2])
            for candidate, row in zip(args[2]['candidates'], output['output']['results']):
                row['value'] = 'low' if candidate['url'].endswith('/a/') else 'relevant'
            return output
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=answer):
            result = process_navigation(handle['payload'])
        self.assertEqual(result['reviewed_routes'], 2)
        for _ in range(2):
            self.assertLess(store.get_page(key, ROOT + 'z/')['priority'], store.get_page(key, ROOT + 'a/')['priority'])
            with store.connect() as connection:
                connection.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (key, ROOT))
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as queue:
                crawl_site(store, key, max_pages=1, workers=1, focus='valuable', fetcher=fetch)
            queue.assert_not_called()
        first = store.claim(key, focus='valuable')
        self.assertEqual(first['url'], ROOT + 'z/')
        store.finish(key, first['url'], state='fetched')
        self.assertEqual(store.claim(key, focus='valuable')['url'], ROOT + 'a/')

    def test_confirmed_publication_list_outranks_a_model_role_mistake(self):
        from backend.database.db import db
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav><a href="/notices/">通知公告</a></nav>'
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        store.finish(key, ROOT + 'notices/', state='fetched', feed_json='{"list_selector":"li"}')
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; db.session.commit()
        tasks.enqueue('navigation_review', 'verified-list', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs: reply(args[2], role='unit')):
            result = process_navigation(handle['payload'])
        self.assertEqual(result['role_corrections'], 0)
        self.assertEqual(store.get_page(key, ROOT + 'notices/')['kind'], 'channel')
        self.assertEqual(store.get_page(key, ROOT + 'notices/')['state'], 'fetched')
        self.assertEqual({node['kind'] for node in store.structure(key) if node['url'] == ROOT + 'notices/'}, {'channel'})

    def test_installed_ai_column_without_generic_feed_cannot_become_directory(self):
        from backend.database.db import db
        from backend.database.models import Announcement, Department
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav><a href="/notices/">通知公告</a></nav>'
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        source = Department(school_id=self.fixture.school_id, name='通知公告',
                            list_url=ROOT + 'notices/', list_selector='.custom-card')
        db.session.add(source); db.session.flush()
        notice = Announcement(school_id=source.school_id, department_id=source.id,
                              title='已经接入的通知', url=ROOT + 'article/1')
        db.session.add(notice)
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; db.session.commit()
        tasks.enqueue('navigation_review', 'installed-list', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs: reply(args[2], role='directory')):
            result = process_navigation(handle['payload'])
        self.assertEqual(result['role_corrections'], 0)
        self.assertEqual(store.get_page(key, ROOT + 'notices/')['kind'], 'channel')
        self.assertEqual(db.session.get(Department, source.id).list_selector, '.custom-card')
        self.assertEqual(db.session.get(Announcement, notice.id).department_id, source.id)

    def test_role_correction_updates_existing_page_structure_and_cached_replay(self):
        from backend.database.db import db
        from backend.services import tasks
        from backend.services.discovery_cache import DiscoveryCache
        from backend.services.discovery_changes import baseline
        from backend.scraper.discovery.ai_navigation import process_navigation
        html = '<nav><a href="/office/">教务处</a></nav>'
        store = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = store.ensure_site('测试大学', ROOT)
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        self.assertEqual(store.get_page(key, ROOT + 'office/')['kind'], 'unit')
        parent = tasks.enqueue('discover', self.fixture.school_id, {'school_id': self.fixture.school_id})
        parent.state = 'done'; db.session.commit()
        job = tasks.enqueue('navigation_review', 'role-correction', {
            'parent_task_id': parent.id, 'parent_generation': parent.generation,
            'school_id': self.fixture.school_id, 'school_generation': parent.generation,
            'site': store.report(key)['site'], 'page': dict(store.get_page(key, ROOT), discovery_policy='valuable')})
        handle = tasks.claim(capabilities=['directory'])
        self.assertEqual(handle['id'], job.id)
        with tasks.execution_scope(handle), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=lambda *args, **kwargs: reply(args[2])):
            result = process_navigation(handle['payload'])
        self.assertEqual(result['reviewed_routes'], 1)
        self.assertEqual(result['role_corrections'], 1)
        self.assertEqual(store.get_page(key, ROOT + 'office/')['kind'], 'channel')
        nodes = [n for n in store.structure(key) if n['url'] == ROOT + 'office/']
        self.assertEqual({n['kind'] for n in nodes}, {'channel'})
        self.assertEqual({n['name'] for n in nodes}, {'教务处'})
        self.assertTrue(baseline(store.get_page(key, ROOT))['routes'][0]['ai_reviewed'])
        with store.connect() as connection:
            connection.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (key, ROOT))
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as queue:
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        queue.assert_not_called()
        self.assertEqual({n['kind'] for n in store.structure(key) if n['url'] == ROOT + 'office/'}, {'channel'})


if __name__ == '__main__':
    unittest.main()
