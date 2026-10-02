"""Student value drives bounded exploration; expired opportunities remain useful."""
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.ai.skill_loader import load_skill, validate_input, validate_output, SkillValidationError, canonical
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import crawl_site
from backend.scraper.discovery.column_regions import materials
from backend.services.student_information import combine, deadline_view

ROOT = 'https://example.edu.cn/'


class LayeredDiscoveryTests(unittest.TestCase):
    def test_all_units_including_administration_and_multilevel_rosters_are_visited(self):
        pages = {
            ROOT: '<nav><a href="/org/">组织机构</a></nav>',
            ROOT + 'org/': '<main><a href="/teaching/">教学单位</a><a href="/finance/">财务处</a></main>',
            ROOT + 'teaching/': '<main><a href="/college/">工程学院</a></main>',
            ROOT + 'finance/': '<nav><a href="/money/">通知公告</a></nav>',
            ROOT + 'college/': '<nav><a href="/student/">通知公告</a></nav>',
            ROOT + 'money/': '<nav><a href="/unrelated/">新闻动态</a></nav><section><h2>财务公告</h2><ul>' + ''.join(
                f'<li><a href="/info/{i}.htm">关于学生申请补助材料的通知{i}</a><span>2026-09-01</span></li>' for i in range(3)) + '</ul></section>',
            ROOT + 'student/': '<main>学生公告</main>',
        }
        reads = []
        def fetch(url):
            reads.append(url)
            return {'html': pages[url], 'url': url, 'status': 200}
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Inventory(Path(tmp) / 'inventory.db')
            key = inventory.ensure_site('测试大学', ROOT)
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
                first = crawl_site(inventory, key, max_pages=2, workers=1, focus='layered', fetcher=fetch)
                self.assertGreater(first['states'].get('pending', 0), 0)
                result = crawl_site(inventory, key, max_pages=50, workers=1, focus='layered', fetcher=fetch)
            self.assertEqual(set(reads), set(pages))
            self.assertEqual(result['states'].get('pending', 0), 0)

    def test_ambiguous_gateway_is_reviewed_even_when_known_routes_exist(self):
        html = '<nav><a href="/org/">组织机构</a><a href="/grow/">成长空间</a></nav>'
        with tempfile.TemporaryDirectory() as tmp:
            inventory = Inventory(Path(tmp) / 'inventory.db')
            key = inventory.ensure_site('测试大学', ROOT)
            with patch('backend.scraper.discovery.ai_navigation.queue_navigation') as review:
                crawl_site(inventory, key, max_pages=1, workers=1, focus='layered',
                           fetcher=lambda url: {'html': html, 'url': url, 'status': 200})
            self.assertEqual(review.call_count, 1)
            self.assertEqual(review.call_args.args[1]['discovery_policy'], 'layered')
            self.assertIsNone(inventory.get_page(key, ROOT + 'grow/'))
            self.assertIsNotNone(inventory.get_page(key, ROOT + 'org/'))

    def test_large_page_is_split_with_selectors_valid_in_original_dom(self):
        html = '<html><body><nav>' + ''.join(f'<a href="/x{i}">其他入口{i}</a>' for i in range(2500)) + '</nav>'
        html += '<section><h2>申请材料</h2><ul><li><a href="/a">学生申请条件</a></li><li><a href="/b">往年材料清单</a></li></ul></section></body></html>'
        parts = materials(1, ROOT, html)
        self.assertTrue(parts)
        original = BeautifulSoup(html, 'lxml')
        for part in parts:
            self.assertLessEqual(len(canonical(part).encode()), 24 * 1024)
            root = part['evidence'][0]['original_root_selector']
            self.assertEqual(len(original.select(root)), 1)
            self.assertEqual(len(original.select(root + ' li')), 2)

    def test_navigation_resume_replays_admitted_links_before_remaining_batch(self):
        from backend.scraper.discovery.structure import extract_structure
        from backend.scraper.discovery.layered import route_structure, assist
        html = '<nav>' + ''.join(f'<a href="/space/{i}">成长空间{i}</a>' for i in range(9)) + '</nav>'
        page = {'url': ROOT, 'label': '测试大学', 'kind': 'root', 'path_json': '[]'}
        site = {'root_url': ROOT}
        handle = {'payload': {'school_id': 1}, 'checkpoint': {}}
        def save(data):
            handle['checkpoint'] = data
        def parse():
            return route_structure(extract_structure(html, ROOT, ROOT), page, html, ROOT)
        def response(*args, **kwargs):
            evidence = args[2]
            self.assertLessEqual(len(canonical(evidence).encode()), 24 * 1024)
            return {'status': 'succeeded', 'output': {'results': [{'candidate_id': c['candidate_id'],
                'role': 'publishing', 'value': 'unknown', 'historical': 'unknown', 'reason': '', 'facts': []}
                for c in evidence['candidates']]}}
        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=save), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=response) as ai:
            self.assertEqual(assist(site, page, html, parse())['status'], 'pending')
            restored = parse()
            self.assertEqual(assist(site, page, html, restored)['status'], 'processed')
        self.assertEqual(ai.call_count, 2)
        self.assertEqual(sum(l['decision'] == 'follow' for l in restored['links']), 9)


class ValueContractTests(unittest.TestCase):
    def evidence(self):
        return {'school_id': 1, 'candidates': [{'candidate_id': 'one', 'name': '往年通知',
            'url': ROOT, 'kind': 'article', 'path': ''}],
            'evidence': [{'candidate_id': 'one', 'evidence_id': 'p0',
                          'text': '本科生申请截止2024年9月30日，请提交成绩单。'}]}

    def output(self):
        return {'results': [{'candidate_id': 'one', 'role': 'reference', 'value': 'relevant',
            'historical': 'high', 'reason': '可用于准备申请材料',
            'facts': [{'kind': 'deadline', 'text': '申请截止2024年9月30日', 'evidence_id': 'p0',
                       'quote': '本科生申请截止2024年9月30日，请提交成绩单。'}]}]}

    def test_expired_notice_keeps_student_and_historical_value(self):
        skill = load_skill('student-information', 'assess')
        validate_input(skill, self.evidence())
        output = validate_output(skill, self.output(), self.evidence())
        info = combine(output['results'])
        self.assertEqual((info['value'], info['historical']), ('relevant', 'high'))
        self.assertEqual(info['eligibility'], 'unknown')
        self.assertEqual(info['policy_validity'], 'unknown')
        self.assertEqual(deadline_view(info['facts'], datetime(2026, 10, 2)), '文中事项已到截止时间')

    def test_missing_year_or_multiple_actions_do_not_produce_false_expiry(self):
        self.assertEqual(deadline_view([{'kind': 'deadline', 'quote': '9月30日截止'}]), '参与时间待核实')
        facts = [{'kind': 'deadline', 'quote': '申请截止2024年9月30日'},
                 {'kind': 'deadline', 'quote': '补充材料截止2027年10月5日'}]
        self.assertEqual(deadline_view(facts), '含多个截止时间，请分别查看')
        self.assertEqual(deadline_view([facts[0]], datetime(2024, 9, 30, 9)), '文中截止时间尚未到')

    def test_fabricated_quote_or_year_is_rejected(self):
        skill = load_skill('student-information', 'assess')
        for field, value in [('quote', '保证保研加分'), ('text', '截止2026年9月30日')]:
            output = self.output(); output['results'][0]['facts'][0][field] = value
            with self.assertRaises(SkillValidationError):
                validate_output(skill, output, self.evidence())
        output = self.output(); output['results'][0]['url'] = ROOT + 'invented/'
        with self.assertRaises(SkillValidationError):
            validate_output(skill, output, self.evidence())

    def test_mixed_and_incomplete_material_remain_conservative(self):
        base = {'facts': [], 'value': 'low', 'historical': 'low'}
        useful = dict(base, value='relevant', historical='high')
        self.assertEqual(combine([base, useful])['value'], 'mixed')
        self.assertEqual(combine([base, dict(base, value='unknown')])['value'], 'unknown')


class StudentPipelineTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_manual_check_preserves_pending_units_and_probes_known_entrances(self):
        from backend.services.discovery_cache import DiscoveryCache, adapt_site
        from backend.scraper.discovery.layered import prepare
        from backend.services.runtime_catalog import RuntimeCatalog
        inventory = DiscoveryCache(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        key = inventory.ensure_site('测试大学', ROOT)
        prepare(inventory, key)
        inventory.finish(key, ROOT, state='fetched', html='<p>学校主页</p>')
        inventory.enqueue(key, ROOT + 'first/', '工程学院', 'unit', 1, [], 'school_domain')
        inventory.finish(key, ROOT + 'first/', state='fetched', html='<p>学院主页</p>')
        inventory.enqueue(key, ROOT + 'last/', '财务处', 'unit', 1, [], 'school_domain')
        RuntimeCatalog(self.fixture.app.config['SOURCE_CATALOG_PATH']).publish(inventory, key)
        def crawl(store, site, **kwargs):
            self.assertEqual(store.get_page(site, ROOT + 'first/')['state'], 'pending')
            self.assertEqual(store.get_page(site, ROOT + 'last/')['state'], 'pending')
            return store.report(site)
        with patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=crawl):
            result = adapt_site('测试大学', ROOT, monthly=True)
        self.assertTrue(result['continuation_required'])

    def test_connected_child_column_still_collects(self):
        from backend.database.db import db
        from backend.database.models import Department, DepartmentDirectoryEntry
        from backend.services.source_collection import collect_source
        parent = Department(school_id=self.fixture.school_id, name='财务处', kind='unit')
        source = Department(school_id=self.fixture.school_id, name='办事材料', list_url=ROOT, list_selector='li')
        db.session.add_all([parent, source]); db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=source.id, position=0))
        db.session.commit()
        with patch('backend.scraper.engine.scrape_department', return_value=(2, 10)) as scrape:
            result = collect_source(source)
        self.assertEqual(result['new_count'], 2)
        self.assertEqual(scrape.call_count, 1)

    def test_value_controls_polling_but_explicit_choice_and_history_win(self):
        from backend.database.db import db
        from backend.database.models import Department, Subscription
        from backend.database.student_information_models import StudentAssessment
        from backend.services.student_information import collection_policy
        sources = [Department(school_id=self.fixture.school_id, name=str(i), list_url=ROOT, list_selector='li') for i in range(3)]
        db.session.add_all(sources); db.session.flush()
        for source, value, history in zip(sources, ['low', 'low', 'mixed'], ['low', 'high', 'unknown']):
            db.session.add(StudentAssessment(subject_key='source:' + str(source.id), school_id=source.school_id,
                department_id=source.id, input_hash='x', state='ready', result={'value': value, 'historical': history}))
        db.session.commit()
        policy = collection_policy(sources)
        self.assertEqual([policy[s.id][1] for s in sources], [4, 1, 1])
        sources[0].name = '消息通知'; db.session.commit()
        self.assertEqual(collection_policy(sources)[sources[0].id], (1, 1))
        sub = Subscription.query.one(); sub.department_ids = [sources[0].id]; db.session.commit()
        self.assertEqual(collection_policy(sources)[sources[0].id], (0, 1))
        self.assertTrue(all(p == (0, 1) for p in collection_policy(sources, manual=True).values()))

    def test_bad_generic_rule_gets_ai_fallback(self):
        from tests.test_direct_onboarding import URL
        from backend.services.source_onboarding import onboard_page
        invalid = {'name': '通知公告', 'list_selector': '.absent'}
        valid = {'name': '通知公告', 'list_selector': '#notices li', 'title_selector': 'a',
                 'link_selector': 'a', 'date_selector': 'span', 'content_selector': '',
                 'container_selector': '#notices', 'heading_selector': '#notices h2'}
        with patch('backend.services.source_onboarding.publication_lists', return_value=[invalid]), \
             patch('backend.services.source_onboarding.recognize_column', return_value={'status': 'ready', 'columns': [valid]}) as ai:
            result = onboard_page({'school_id': self.fixture.school_id, 'url': URL}, fetcher=self.fixture.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(ai.call_count, 1)

    def test_assessment_resumes_long_body_and_rejects_stale_material(self):
        from backend.database.db import db
        from backend.database.models import Department, Announcement
        from backend.database.student_information_models import StudentAssessment
        from backend.services.student_information import queue_assessment, process, article_view
        from backend.services import tasks
        dept = Department(school_id=self.fixture.school_id, name='奖学金', list_url=ROOT, list_selector='li')
        db.session.add(dept); db.session.flush()
        ann = Announcement(school_id=dept.school_id, department_id=dept.id, title='往年申请', url=ROOT + 'old',
            content_text='申请截止2024年9月30日，请提交成绩单。' + '往年材料说明。' * 1000)
        db.session.add(ann); db.session.commit()
        def response(*args, **kwargs):
            batch = args[2]
            proof = batch['evidence'][0]
            return {'status': 'succeeded', 'output': {'results': [{'candidate_id': 'subject',
                'role': 'reference', 'value': 'relevant', 'historical': 'high', 'reason': '准备材料',
                'facts': [{'kind': 'history', 'text': '材料说明', 'quote': proof['text'][:60], 'evidence_id': proof['evidence_id']}]}]}}
        with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=response) as ai:
            job = queue_assessment('article', ann.id, requested_by=1)
            self.assertIsNotNone(job)
            for _ in range(10):
                job.available_at = datetime(2000, 1, 1); db.session.commit()
                handle = tasks.claim(capabilities=['directory'])
                if handle is None:
                    break
                try:
                    with tasks.execution_scope(handle):
                        result = process(handle['payload'])
                    tasks.finish(handle, result)
                except tasks.TaskDeferred as deferred:
                    tasks.handoff(handle, deferred)
                db.session.refresh(job)
                if job.state == 'done':
                    break
            self.assertEqual(job.state, 'done')
            self.assertGreater(ai.call_count, 1)
            self.assertEqual(StudentAssessment.query.one().state, 'ready')
            self.assertIsNotNone(article_view(ann))
            self.assertIsNone(queue_assessment('article', ann.id, requested_by=1))
            ann.content_text += ' 最新修订'; db.session.commit()
            self.assertIsNone(article_view(ann))
            self.assertIsNotNone(queue_assessment('article', ann.id, requested_by=1))


if __name__ == '__main__':
    unittest.main()
