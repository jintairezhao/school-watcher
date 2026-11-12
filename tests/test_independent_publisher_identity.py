"""An officially reached publisher need not be an invented directory child."""
from pathlib import Path
from types import SimpleNamespace
import gzip
import json
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import patch

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_assertion

FIXTURES = Path(__file__).parent / 'fixtures/structure'
ROOT = 'https://www.cupk.edu.cn/'
HOME = ROOT + 'chem/'
PROFILE = HOME + 'c/2023-06-28/517808.shtml'
SOURCE = HOME + 'glzd/'
NAME = '化学与化工实验教学中心'
SCHOOL = '中国石油大学（北京）克拉玛依校区'


class IndependentPublisherIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = gzip.decompress((FIXTURES / 'cupk-chem-home-20261023.html.gz').read_bytes()).decode('utf8')
        self.profile = gzip.decompress((FIXTURES / 'cupk-chem-profile-20261024.html.gz').read_bytes()).decode('utf8')
        self.root = '<title>' + SCHOOL + '</title><nav><a href="/chem/">示范中心</a></nav>'

    def case(self, root=None, home=None, profile=None):
        inventory = Inventory(Path(self.tmp.name) / 'inventory.sqlite3')
        key = inventory.ensure_site(SCHOOL, ROOT)
        bodies = {ROOT: self.root if root is None else root, HOME: self.home if home is None else home,
                  PROFILE: self.profile if profile is None else profile}
        for url, label, kind in ((ROOT, SCHOOL, 'root'), (HOME, '示范中心', 'unit'),
                                 (PROFILE, '中心概况', 'unit')):
            inventory.enqueue(key, url, label, kind, 1, [], 'fixture')
            inspect_page(inventory, inventory.report(key)['site'], inventory.get_page(key, url),
                fetcher=lambda target: dict(url=target, status=200, html=bodies[target]), ai_assist=False)
        relations = SourceRelationships(inventory.report(key), inventory.structure(key), directory_navigation=True)
        paths = relations.paths_for(SOURCE)
        source = SimpleNamespace(list_url=SOURCE)
        assertions = [_publisher_assertion(path, source, None, {'receipt_id': 'fixture'},
            lambda url: bodies.get(url, ''), relations) for path in paths]
        return inventory, key, relations, paths, assertions, bodies

    def test_center_is_an_independent_publisher_without_a_roster_parent(self):
        _, _, _, paths, assertions, _ = self.case()
        self.assertTrue(any(assertions))
        selected = next(path for path, assertion in zip(paths, assertions) if assertion)
        self.assertEqual(selected['unit_name'], NAME)
        self.assertEqual([node['name'] for node in selected['nodes']], [NAME])
        self.assertEqual(selected['nodes'][0]['relation'], 'website_publisher_declaration')

    def test_same_domain_without_an_official_entrance_does_not_authorize_identity(self):
        self.assertFalse(any(self.case(root='<title>' + SCHOOL + '</title>')[4]))

    def test_profile_mention_without_its_matching_chart_caption_is_not_identity(self):
        self.assertFalse(any(self.case(profile=self.profile.replace(
            NAME + '总体架构图', '有关实验教学的总体架构图'))[4]))

    def test_title_alone_cannot_replace_missing_profile(self):
        self.assertFalse(any(self.case(profile='<title>' + SCHOOL + NAME + '</title>')[4]))

    def footer_home(self):
        return ('<title>' + SCHOOL + '</title><nav><a href="' + PROFILE + '">中心概况</a>'
            '<a href="' + SOURCE + '">管理制度</a></nav><div class="footer">'
            '<div class="school">' + SCHOOL + NAME + '</div></div>')

    def test_declared_footer_identity_reaches_the_same_profile_and_original_checks(self):
        _, _, _, paths, assertions, _ = self.case(home=self.footer_home())
        self.assertTrue(any(assertions))
        chosen = next(path for path, assertion in zip(paths, assertions) if assertion)
        self.assertEqual(chosen['website_identity']['source'], 'footer_signature')
        self.assertEqual(chosen['unit_name'], NAME)
        self.assertEqual(chosen['nodes'][0]['relation'], 'website_publisher_declaration')

    def test_footer_identity_cannot_replace_profile_or_current_receipt(self):
        self.assertFalse(any(self.case(home=self.footer_home(), profile='<title>中心概况</title>')[4]))
        _, _, relations, paths, _, bodies = self.case(home=self.footer_home())
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            None, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_footer_declaration_does_not_upgrade_an_ordinary_roster_or_another_locator(self):
        inventory, key, relations, _, _, _ = self.case(home=self.footer_home())
        node = next(n for n in inventory.structure(key) if n['relation'] == 'website_publisher_declaration')
        owner = dict(unit_key=node['node_key'], unit_name=node['name'], nodes=[relations._node(node)])
        page = relations.pages[HOME]
        self.assertIsNotNone(relations._unit_website_identity(page, owner))
        self.assertIsNone(relations._unit_website_identity(page, dict(owner, unit_key='another-observation')))
        roster = deepcopy(owner)
        roster['nodes'][0]['relation'] = 'directory_entry'
        self.assertIsNone(relations._unit_website_identity(page, roster))
        copied = deepcopy(page)
        from backend.services.publisher_identity import PREFIX
        notes = json.loads(copied['notes_json'])
        for i, note in enumerate(notes):
            if note.startswith(PREFIX):
                saved = json.loads(note[len(PREFIX):])
                saved['nodes'][0]['locator'] = 'a.friend'
                notes[i] = PREFIX + json.dumps(saved)
        copied['notes_json'] = json.dumps(notes)
        self.assertIsNone(relations._unit_website_identity(copied, owner))

    def test_changed_footer_bytes_cannot_reuse_saved_declaration(self):
        import hashlib
        _, _, relations, paths, _, bodies = self.case(home=self.footer_home())
        bodies[HOME] = bodies[HOME].replace('<div class="school">', '<div class="friend-links">')
        digest = hashlib.sha256(bodies[HOME].encode()).hexdigest()
        relations.pages[HOME]['content_hash'] = digest
        for path in paths:
            for reference in path['references']:
                if reference['url'] == HOME:
                    reference['content_hash'] = digest
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_different_unit_name_is_not_merged_by_similar_website_address(self):
        self.assertFalse(any(self.case(home=self.home.replace(NAME, '化学实验中心'))[4]))

    def test_original_material_is_reexecuted_even_with_consistent_saved_claims(self):
        import hashlib
        _, _, relations, paths, _, bodies = self.case()
        bodies[PROFILE] = bodies[PROFILE].replace(NAME + '总体架构图', '有关实验教学的总体架构图')
        digest = hashlib.sha256(bodies[PROFILE].encode()).hexdigest()
        relations.pages[PROFILE]['content_hash'] = digest
        for path in paths:
            for reference in path['references']:
                if reference['url'] == PROFILE:
                    reference['content_hash'] = digest
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_missing_current_receipt_does_not_install_ownership(self):
        _, _, relations, paths, _, bodies = self.case()
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None, None,
            lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_wrong_school_title_cannot_supply_root_trust(self):
        self.assertFalse(any(self.case(root=self.root.replace(SCHOOL, '其他大学'))[4]))

    def test_forged_profile_link_locator_is_not_accepted(self):
        _, _, relations, paths, _, bodies = self.case()
        paths = deepcopy(paths)
        for path in paths:
            path['publisher_declaration']['profile_entry_locator'] = 'a.nonexistent'
        self.assertFalse(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_optional_sibling_navigation_without_raw_body_cannot_erase_identity(self):
        _, _, relations, paths, _, bodies = self.case()
        paths = deepcopy(paths)
        for path in paths:
            path['references'].append(dict(url=HOME + 'unrelated/', content_hash='old-observation',
                checked_at='2026-10-01T00:00:00+00:00', locators=['a']))
        self.assertTrue(any(_publisher_assertion(path, SimpleNamespace(list_url=SOURCE), None,
            {'receipt_id': 'fixture'}, lambda url: bodies.get(url, ''), relations) for path in paths))

    def test_identity_persists_without_merging_existing_names_or_creating_administration(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.database.db import db
        from backend.database.models import Department
        from backend.services import source_governance as g
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.structure_snapshot import read_structure, source_choices, graph_projection
        from backend.services.structure_versions import current_version
        fixture = SourceGovernanceTests()
        fixture.setUp()
        try:
            fixture.school.name, fixture.school.url = SCHOOL, ROOT
            source = fixture.dept
            source.name, source.list_url, source.group_name = '管理制度', SOURCE, ''
            unrelated = Department(school_id=fixture.school.id, kind='unit', name=NAME, list_url=HOME)
            db.session.add(unrelated)
            db.session.commit()
            old_id, source_id = unrelated.id, source.id
            inventory, key, _, _, _, _ = self.case()
            receipt = dict(source_id=source.id, receipt_id='native:fixture',
                config_hash=g._hash(g.source_config(source)), url=SOURCE, content_hash='fixture', region={})
            with patch('backend.services.source_path_receipts.load_source_receipts', return_value=[receipt]):
                catalog = RuntimeCatalog(fixture.app.config['SOURCE_CATALOG_PATH'])
                from backend.database.models import CatalogPublication
                before = current_version(fixture.school.id)
                with patch.object(db.session, 'commit', side_effect=RuntimeError('identity commit failure')):
                    with self.assertRaisesRegex(RuntimeError, 'identity commit failure'):
                        catalog.publish(inventory, key)
                self.assertIsNone(Department.query.filter(Department.identity_json.is_not(None)).first())
                self.assertIsNone(db.session.get(CatalogPublication, key))
                self.assertEqual(current_version(fixture.school.id), before)
                catalog.publish(inventory, key)
                identity = Department.query.filter(Department.identity_json.is_not(None)).one()
                self.assertNotEqual(identity.id, old_id)
                self.assertEqual(identity.name, NAME)
                self.assertFalse(identity.identity_json['administrative_parent_asserted'])
                self.assertEqual(unrelated.structure_key, None)
                capture = read_structure(fixture.school)
                facts = capture['facts']
                self.assertFalse(any(edge['type'] == 'organization_membership' for edge in facts['relations']))
                owner = next(node for node in facts['nodes'] if node.get('department_id') == identity.id)
                self.assertTrue(any(edge['source'] == owner['id'] and edge['target'] == 'source:' + str(source_id)
                    and edge['type'] == 'source_ownership' for edge in facts['relations']))
                choices = source_choices(capture)
                self.assertTrue(any(unit['name'] == NAME and any(c['department'].id == source_id
                    for c in unit['own_columns']) for group in choices.values() for unit in group))
                self.assertIn(owner['id'], {node['id'] for node in graph_projection(capture, fixture.school)['nodes']})
                version, identity_id = current_version(fixture.school.id), identity.id
                catalog.publish(inventory, key)
                self.assertEqual(current_version(fixture.school.id), version)
                self.assertEqual(Department.query.filter(Department.identity_json.is_not(None)).one().id, identity_id)
                identity.identity_json = dict(identity.identity_json, writer='imported_history')
                db.session.commit()
                history = read_structure(fixture.school)['facts']
                self.assertFalse(any(edge['type'] == 'source_ownership' and edge['source'] == owner['id']
                                     for edge in history['relations']))
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
