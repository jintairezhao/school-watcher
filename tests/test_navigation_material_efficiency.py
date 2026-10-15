"""Repeated menus use their own evidence; footer resources stay observable."""
import json
import unittest
from unittest.mock import patch

from backend.ai.skill_loader import SkillValidationError, load_skill, validate_output, validate_schema
from backend.scraper.discovery.layered import (
    append_navigation_evidence, assist, navigation_candidates, route_structure,
)
from backend.scraper.discovery.navigation_roles import supported
from backend.scraper.discovery.structure import extract_structure
from tests import test_ai_primary_navigation as navigation_fixture

ROOT = navigation_fixture.ROOT
reply = navigation_fixture.reply


def page_fixture():
    return navigation_fixture.PrimaryNavigationTests().page()


def parse(page, html):
    return route_structure(extract_structure(html, page['url'], ROOT, page['kind'],
        page['label'], json.loads(page['path_json'])), page, html, ROOT, policy='valuable')


class NavigationMaterialEfficiencyTests(unittest.TestCase):
    def test_external_footer_resources_remain_observed_without_expanding_the_frontier(self):
        page = page_fixture()
        html = ('<main><a href="/office/">学生工作部</a></main>'
                '<div class="c-footer c-zizhu-footer"><div><a href="https://outside.example/">'
                '国家图书馆</a><a href="/notices/">通知公告</a></div></div>')
        parsed = parse(page, html)
        footer = next(link for link in parsed['links'] if link['url'] == 'https://outside.example/')
        self.assertEqual(footer['decision'], 'auxiliary_reference')
        candidates = {link['url'] for link, _ in navigation_candidates(html, parsed, page)}
        self.assertNotIn(footer['url'], candidates)
        self.assertIn(ROOT + 'office/', candidates)
        self.assertIn(ROOT + 'notices/', candidates)
        # The same external institution in a page's main body remains reviewable.
        body = parse(page, '<main><a href="https://outside.example/">国家图书馆</a></main>')
        self.assertIn('https://outside.example/', {
            link['url'] for link, _ in navigation_candidates(
                '<main><a href="https://outside.example/">国家图书馆</a></main>', body, page)})

    def test_menu_facts_use_local_text_and_keep_page_context_available(self):
        page = dict(page_fixture(), path_json='["奖学金办理"]')
        html = '<title>奖学金办理</title><nav><li><a href="/office/">学生事务中心</a></li></nav>'
        link, context = navigation_candidates(html, parse(page, html), page)[0]
        evidence = {'school_id': 1, 'candidates': [], 'evidence': []}
        append_navigation_evidence(evidence, link, context)
        proof = evidence['evidence'][0]
        self.assertNotIn('奖学金办理', proof['text'])
        self.assertIn('奖学金办理', proof['context'])
        skill = load_skill('student-information', 'navigation')
        validate_schema(evidence, skill.input_schema)
        output = reply(evidence, role='unit')['output']
        output['results'][0]['facts'][0]['quote'] = proof['text']
        validate_output(skill, output, evidence)
        self.assertTrue(supported(output['results'][0], link['_navigation_material']))
        output['results'][0]['facts'][0]['quote'] = '奖学金办理'
        with self.assertRaises(SkillValidationError):
            validate_output(skill, output, evidence)


class NavigationMenuReuseTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def test_external_footer_resource_is_persisted_as_an_auxiliary_edge(self):
        from pathlib import Path
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.services.source_inventory import Inventory
        store = Inventory(Path(self.fixture.temp.name) / 'footer-inventory.db')
        key = store.ensure_site('测试大学', ROOT)
        html = ('<main><a href="/office/">学生工作部</a></main>'
                '<div class="c-footer"><a href="https://outside.example/">国家图书馆</a></div>')
        with patch('backend.scraper.discovery.ai_navigation.queue_navigation'):
            crawl_site(store, key, max_pages=1, workers=1, focus='valuable',
                       fetcher=lambda url: {'url': url, 'html': html, 'status': 200})
        with store.connect() as connection:
            edge = connection.execute('SELECT decision FROM edges WHERE site_key=? AND target_url=?',
                                      (key, 'https://outside.example/')).fetchone()
        self.assertEqual(edge['decision'], 'auxiliary_reference')
        self.assertIsNone(store.get_page(key, 'https://outside.example/'))
        self.assertIsNotNone(store.get_page(key, ROOT + 'office/'))

    def test_old_unresolved_full_context_input_is_recovered_exactly(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest
        from backend.database.db import db
        from backend.scraper.discovery.layered import legacy_navigation_decision
        page = page_fixture()
        html = '<title>奖学金办理</title><nav><a href="/office/">学生事务中心</a></nav>'
        batch = navigation_candidates(html, parse(page, html), page)
        original = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        append_navigation_evidence(original, *batch[0], menu_evidence=False)
        skill = load_skill('student-information', 'navigation')
        db.session.add(AIExecution(execution_id='routes:old-format', purpose='directory', profile_id=None,
            config_version='1', provider='fixture', model='fixture', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': original, 'repair_feedback': None}), mode='navigation',
            status='uncertain', request_sent=True, usage={'known': False},
            error_code='timeout_result_unknown'))
        db.session.commit()
        recovered = legacy_navigation_decision(batch, self.fixture.school_id, 24 * 1024)
        self.assertEqual(recovered['fingerprint'], 'old-format')
        self.assertEqual(recovered['evidence'], original)

    def test_legacy_failed_or_partial_paid_result_with_unknown_usage_is_recovered(self):
        from backend.ai.models import AIExecution
        from backend.ai.skill_loader import digest
        from backend.database.db import db
        from backend.scraper.discovery.layered import legacy_navigation_decision
        page = page_fixture()
        html = '<nav><a href="/office/">学生事务中心</a></nav>'
        batch = navigation_candidates(html, parse(page, html), page)
        original = {'school_id': self.fixture.school_id, 'candidates': [], 'evidence': []}
        append_navigation_evidence(original, *batch[0], menu_evidence=False)
        skill = load_skill('student-information', 'navigation')
        row = AIExecution(execution_id='routes:old-unknown-usage', purpose='directory', profile_id=None,
            config_version='1', provider='fixture', model='fixture', skill_id=skill.id,
            skill_version=skill.version, skill_digest=skill.resource_digest,
            input_digest=digest({'evidence': original, 'repair_feedback': None}), mode='navigation',
            status='failed', request_sent=True, usage={'known': False})
        db.session.add(row)
        db.session.commit()
        for status in ('failed', 'partial'):
            row.status = status
            db.session.commit()
            recovered = legacy_navigation_decision(batch, self.fixture.school_id, 24 * 1024)
            self.assertEqual(recovered['fingerprint'], 'old-unknown-usage')
            self.assertEqual(recovered['evidence'], original)
        row.usage = {'known': True, 'total_tokens': 0}
        db.session.commit()
        self.assertIsNone(legacy_navigation_decision(batch, self.fixture.school_id, 24 * 1024))

    def test_quoting_the_entire_menu_proof_reuses_roles_across_page_titles_and_paths(self):
        handle = {'payload': {'school_id': self.fixture.school_id}, 'checkpoint': {}}
        first = dict(page_fixture(), path_json='["学校"]')
        second = dict(first, url=ROOT + 'teaching/', path_json='["人才培养"]')
        menu = '<nav id="school-menu"><li><a href="/office/">学生事务中心</a></li></nav>'

        def answer(*args, **kwargs):
            evidence = args[2]
            output = reply(evidence, role='unit')
            output['output']['results'][0]['facts'][0]['quote'] = evidence['evidence'][0]['text']
            return output

        with patch('backend.services.tasks.current_execution', return_value=handle), \
             patch('backend.services.tasks.checkpoint', side_effect=lambda value: handle.update(checkpoint=value)), \
             patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=answer) as model:
            html = '<title>学校首页</title>' + menu
            assist({'root_url': ROOT}, first, html, parse(first, html))
            handle['checkpoint'] = {}
            html = '<title>本科生教育</title>' + menu
            result = assist({'root_url': ROOT}, second, html, parse(second, html))
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['cached_routes'], 1)


if __name__ == '__main__':
    unittest.main()
