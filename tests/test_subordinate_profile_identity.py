"""Retain a center's own introductory statement without inferring source success."""
from pathlib import Path
from unittest.mock import patch
import gzip
import hashlib
import json
import tempfile
import unittest

from bs4 import BeautifulSoup

from backend.scraper.discovery.structure import locator_element
from backend.services.publisher_identity import observations, saved_observation
from backend.services.source_inventory import canonical_url, Inventory
from backend.services.source_ownership import profile_identity_candidates

FIXTURE = Path(__file__).parent/'fixtures/structure/cupk-career-introduction-20261031.html.gz'
DIGEST = '296d97473a22a20569f853545c274d6bb8d846680c1f58cf611b13266855369a'
SCHOOL = '中国石油大学（北京）克拉玛依校区'
ROOT = 'https://www.cupk.edu.cn/'
HOME = 'https://career.cupk.edu.cn/CupkJY/Web'
PROFILE = HOME+'/Home/InfoShow?yqhRa3ZQ23_fhMUqBN83QA0NBc5MMvzAXQ-RXEqcU6Y=.shtml'
STATEMENT = '就业指导中心隶属于学生工作与安全保卫部，负责校区学生的就业创业工作。'


class SubordinateProfileIdentityTests(unittest.TestCase):
    def setUp(self):
        self.html = gzip.decompress(FIXTURE.read_bytes()).decode('utf8')
        self.assertEqual(hashlib.sha256(self.html.encode()).hexdigest(), DIGEST)

    def document(self):
        return BeautifulSoup(self.html, 'lxml')

    def test_retained_opening_identifies_its_subject_not_the_named_parent(self):
        soup = self.document()
        candidate, = profile_identity_candidates(soup)
        self.assertEqual(candidate['entity_name'], '就业指导中心')
        self.assertEqual(candidate['identity'], STATEMENT)
        self.assertEqual(locator_element(soup, candidate['locator']).get_text(' ', strip=True), STATEMENT)
        self.assertEqual(candidate['source'], 'unit_profile_self_description')
        self.assertNotIn('parent_id', candidate)
        self.assertNotIn('organization_membership', candidate.values())

    def test_real_intro_sidebar_link_is_recorded_as_a_profile_route(self):
        result = observations(self.document(), SCHOOL, PROFILE)
        route, = [r for r in result['routes'] if r['label'] == '中心介绍']
        self.assertEqual(route['target_url'], canonical_url(PROFILE))
        self.assertEqual(result['profile_names'][0]['entity_name'], '就业指导中心')
        self.assertEqual(result['nodes'], [])

    def test_quotation_historical_guidance_and_cooperation_are_not_self_descriptions(self):
        for replacement in ('据报道，'+STATEMENT, '“'+STATEMENT+'”',
                STATEMENT.replace('隶属于', '曾隶属于'),
                STATEMENT.replace('隶属于', '在'),
                STATEMENT.replace('隶属于', '与'),
                STATEMENT.replace('隶属于', '不隶属于')):
            with self.subTest(text=replacement):
                soup = self.document()
                soup.select_one('.sub-right p').string = replacement
                self.assertEqual(profile_identity_candidates(soup), [])

    def test_later_mentions_or_blockquotes_do_not_replace_an_opening_statement(self):
        for mode in ('later', 'blockquote'):
            with self.subTest(mode=mode):
                soup = self.document()
                paragraph = soup.select_one('.sub-right p')
                if mode == 'later':
                    lead = soup.new_tag('p')
                    lead.string = '本页记录本校就业工作的一般动态。'
                    paragraph.insert_before(lead)
                else:
                    paragraph.wrap(soup.new_tag('blockquote'))
                self.assertEqual(profile_identity_candidates(soup), [])

    def test_hidden_intro_or_empty_navigation_body_is_not_identity(self):
        for mode in ('hidden', 'empty'):
            with self.subTest(mode=mode):
                soup = self.document()
                if mode == 'hidden':
                    soup.select_one('.sub-right')['hidden'] = ''
                else:
                    soup.select_one('.sub-right').clear()
                self.assertEqual(profile_identity_candidates(soup), [])

    def test_an_ordinary_body_link_is_not_a_profile_navigation_route(self):
        soup = self.document()
        for anchor in list(soup.select('a[href]')):
            if anchor.get_text(' ', strip=True) == '中心介绍':
                anchor.extract()
        paragraph = soup.select_one('.sub-right p')
        anchor = soup.new_tag('a', href=PROFILE)
        anchor.string = '中心介绍'
        paragraph.insert_after(anchor)
        self.assertFalse(any(r['label'] == '中心介绍' for r in observations(soup, SCHOOL, PROFILE)['routes']))

    def test_inventory_retains_the_same_original_observation_without_source_success(self):
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.source_ownership import PROFILE_PREFIX
        from backend.services.source_relationships import SourceRelationships
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder)/'inventory.sqlite3')
            key = inventory.ensure_site(SCHOOL, ROOT)
            inventory.enqueue(key, PROFILE, '中心介绍', 'unit', 1, [], 'retained_fixture')
            url = canonical_url(PROFILE)
            with patch('socket.socket.connect', side_effect=AssertionError('No website')):
                inspect_page(inventory, inventory.report(key)['site'], inventory.get_page(key, url),
                    fetcher=lambda target: dict(url=target, status=200, html=self.html,
                        checked_at='2026-10-31T09:45:20.010173+00:00'), ai_assist=False)
            page = inventory.get_page(key, url)
            self.assertEqual(page['content_hash'], DIGEST)
            self.assertEqual(page['checked_at'], '2026-10-31T09:45:20.010173+00:00')
            self.assertTrue(any(note.startswith(PROFILE_PREFIX) for note in json.loads(page['notes_json'])))
            self.assertEqual(saved_observation(page, key)['profile_names'][0]['entity_name'], '就业指导中心')
            relations = SourceRelationships(inventory.report(key), inventory.structure(key))
            self.assertEqual(relations.publisher_declarations, [])

    def test_complete_existing_roster_proof_reexecutes_the_new_description(self):
        from types import SimpleNamespace
        from tests.test_profile_publisher_identity import ProfilePublisherIdentityTests, SOURCE
        from backend.services.school_structure import _publisher_assertion
        fixture = ProfilePublisherIdentityTests()
        fixture.setUp()
        try:
            soup = BeautifulSoup(fixture.profile, 'lxml')
            body = soup.select_one('.c-right-detail__content')
            body['class'] = ['sub-right']
            body.select_one('p').string = ('化工与环境学院隶属于示例大学，负责学院师生的教学科研工作，'
                '承担相关专业人才培养工作。')
            home = fixture.home.replace('学院概况', '中心介绍')
            relations, paths, proofs, bodies = fixture.case(profile=str(soup), home=home)
            self.assertTrue(any(proofs))
            self.assertTrue(any(p['profile_identity_witness']['unit_name'] == '化工与环境学院'
                for p in proofs if p))
            self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
                None, lambda url: bodies.get(url, ''), relations) for path in paths))
            bodies[next(u for u in bodies if 'xyjj' in u)] += 'changed'
            self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
                {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))
        finally:
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
