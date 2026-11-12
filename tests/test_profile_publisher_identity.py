"""A real introductory self-description can support an exactly named roster unit."""
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
import gzip
import hashlib
import json
import tempfile
import unittest

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_assertion

FIXTURE = Path(__file__).parent / 'fixtures/structure/cupk-chemical-college-profile-20261024.html.gz'
ROOT = 'https://www.cupk.edu.cn/'
HOME = ROOT + 'gxy/'
PROFILE = HOME + 'xyjj/'
SOURCE = HOME + 'bkjy/jxtz/'
UNIT = '化工与环境学院'


class ProfilePublisherIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = gzip.decompress(FIXTURE.read_bytes()).decode('utf8')
        self.assertEqual(hashlib.sha256(self.profile.encode()).hexdigest(),
                         '949ce593656d6056ef778ac526c7fec2eb9fd0b052b359e4fe9e65771a9772d7')
        self.home = ('<title>中国石油大学(北京)克拉玛依校区化学与环境学院</title>'
                     '<nav><a href="/gxy/xyjj/">学院概况</a>'
                     '<a href="/gxy/bkjy/jxtz/">教学通知</a></nav>')

    def case(self, profile=None, home=None):
        inventory = Inventory(Path(self.tmp.name) / 'inventory.sqlite3')
        key = inventory.ensure_site('中国石油大学（北京）克拉玛依校区', ROOT)
        with inventory.connect() as connection:
            connection.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?", (key,))
        bodies = {ROOT: '<main><h4><a href="/gxy/">化工与环境学院</a></h4></main>',
                  HOME: self.home if home is None else home, PROFILE: self.profile if profile is None else profile}
        for url, label, kind in ((ROOT, '院系设置', 'directory'), (HOME, UNIT, 'unit'),
                                 (PROFILE, '学院概况', 'unit')):
            inventory.enqueue(key, url, label, kind, 1, [], 'fixture')
            inspect_page(inventory, inventory.report(key)['site'], inventory.get_page(key, url),
                fetcher=lambda target: dict(url=target, status=200, html=bodies[target]), ai_assist=False)
        relations = SourceRelationships(inventory.report(key), inventory.structure(key), directory_navigation=True)
        paths = relations.paths_for(SOURCE)
        source = SimpleNamespace(list_url=SOURCE)
        assertions = [_publisher_assertion(path, source, None, {'receipt_id': 'fixture'},
                                          lambda url: bodies.get(url, ''), relations) for path in paths]
        return relations, paths, assertions, bodies

    def test_real_profile_self_description_reaches_exact_roster_publisher(self):
        _, paths, assertions, _ = self.case()
        proof = next((proof for proof in assertions if proof), None)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['identity_reference_url'], PROFILE)
        self.assertEqual(proof['entry_reference_url'], HOME)
        self.assertEqual(proof['profile_identity_witness']['unit_name'], UNIT)
        self.assertEqual(proof['profile_identity_witness']['website_title'],
                         '中国石油大学(北京)克拉玛依校区化学与环境学院')
        self.assertTrue(any(path['unit_name'] == UNIT for path in paths))

    def test_directory_navigation_cannot_hide_or_inherit_publisher_proof(self):
        relations, paths, _, bodies = self.case()
        publisher = next(path for path in paths if path.get('website_identity'))
        navigation = deepcopy(publisher)
        navigation['basis'] = 'official_directory_navigation'
        navigation.pop('website_identity')
        extra = dict(url=HOME + 'unrelated/', content_hash='0' * 64,
                     checked_at='2026-10-24T06:00:00+00:00', locators=['nav a'])
        navigation['references'].append(extra)
        for entries in ([navigation, publisher], [publisher, navigation]):
            with self.subTest(first_basis=entries[0]['basis']):
                relations.entries[SOURCE] = entries
                actual = relations.paths_for(SOURCE)
                self.assertEqual(len(actual), 2)
                for path in actual:
                    proof = _publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
                        {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations)
                    if path['basis'] == 'official_website_entry':
                        self.assertIsNotNone(proof)
                        self.assertNotIn(extra, path['references'])
                    else:
                        self.assertIsNone(proof)

    def test_news_link_does_not_authorize_a_profile(self):
        self.assertFalse(any(self.case(home=self.home.replace('学院概况', '学院新闻'))[2]))

    def test_another_unit_self_description_does_not_join_by_similar_name(self):
        self.assertFalse(any(self.case(profile=self.profile.replace(
            '校区化工与环境学院依托', '校区化学与环境学院依托'))[2]))

    def test_general_mention_is_not_an_opening_self_description(self):
        self.assertFalse(any(self.case(profile=self.profile.replace(
            '校区化工与环境学院依托', '据报道，校区化工与环境学院依托'))[2]))

    def test_profile_without_its_own_home_link_is_not_website_identity(self):
        from bs4 import BeautifulSoup
        document = BeautifulSoup(self.profile, 'lxml')
        for anchor in document.select('a[href]'):
            if anchor['href'] == HOME:
                anchor['href'] = ROOT + 'other/'
        self.assertFalse(any(self.case(profile=str(document))[2]))

    def test_profile_outside_the_recorded_website_scope_is_not_used(self):
        self.assertFalse(any(self.case(home=self.home.replace('/gxy/xyjj/', '/other/xyjj/'))[2]))

    def test_missing_receipt_does_not_grant_source_ownership(self):
        relations, paths, _, bodies = self.case()
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None, None,
            lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_corrupt_original_profile_cannot_keep_its_saved_metadata(self):
        relations, paths, _, bodies = self.case()
        bodies[PROFILE] += 'changed'
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_consistent_hash_but_forged_body_claim_is_reexecuted(self):
        relations, paths, _, bodies = self.case()
        bodies[PROFILE] = bodies[PROFILE].replace('校区化工与环境学院依托', '据报道，校区化工与环境学院依托')
        digest = hashlib.sha256(bodies[PROFILE].encode()).hexdigest()
        relations.pages[PROFILE]['content_hash'] = digest
        for path in paths:
            for reference in path['references']:
                if reference['url'] == PROFILE:
                    reference['content_hash'] = digest
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_forged_profile_route_locator_is_rejected_on_original_dom(self):
        relations, paths, _, bodies = self.case()
        paths = deepcopy(paths)
        for path in paths:
            if path.get('website_identity'):
                path['website_identity']['profile_entry_locator'] = 'a.nonexistent'
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_review_capture_preserves_the_same_identity_observation(self):
        from backend.services.source_governance import _identity_observations
        from backend.services.source_ownership import PROFILE_PREFIX
        from backend.services.school_structure import _publisher_material_assertion
        relations, paths, _, bodies = self.case()
        captured = _identity_observations({'site': relations.site, 'pages': list(relations.pages.values())}, paths)
        self.assertTrue(any(PROFILE_PREFIX in page['notes_json'] for page in captured['identity_pages']))
        restored = SourceRelationships({'site': captured['identity_site'], 'pages': captured['identity_pages']}, [])
        self.assertTrue(any(_publisher_material_assertion(path, SimpleNamespace(list_url=SOURCE),
            lambda url: bodies.get(url, ''), restored) for path in paths))

    def test_known_department_profile_does_not_become_its_colleges_whole_site(self):
        relations, paths, _, _ = self.case()
        page = deepcopy(relations.pages[HOME])
        page['notes_json'] = json.dumps(json.loads(page['notes_json']) + ['unit_profile_page:{}'])
        self.assertIsNone(relations._profile_website_identity(page, paths[0]))


if __name__ == '__main__':
    unittest.main()
