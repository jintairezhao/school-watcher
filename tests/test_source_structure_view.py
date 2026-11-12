"""The official structure page consumes the same pinned, proven facts as the graph."""
from contextlib import ExitStack
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_discovery_monitor as monitor_fixture
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.routes.source_structure import evidence_tree, proven_page_relations
from backend.services.runtime_catalog import pack
from backend.services.structure_snapshot import read_structure
from backend.services.structure_versions import current_version


class SourceStructureViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture = monitor_fixture.DiscoveryMonitorTests()
        self.fixture.setUp()
        self.app, self.school, self.client = self.fixture.app, self.fixture.school, self.fixture.client
        self.endpoint = f'/schools/{self.school.id}/structure'
        self.guards = ExitStack()
        self.guards.enter_context(patch('backend.ai.providers.complete', side_effect=AssertionError('No model')))
        self.guards.enter_context(patch('backend.services.source_inventory.Inventory.__init__', side_effect=AssertionError('No inventory')))
        self.guards.enter_context(patch('backend.services.source_path_receipts.load_source_receipts', return_value=[]))
        self.url = self.school.url + '/directory'
        self.page = dict(url=self.url, label='Recorded directory', title='Recorded directory', kind='directory',
            state='fetched', content_hash='fixture-material', checked_at='2026-10-21T00:00:00Z',
            health='recent_publication', depth=1, notes_json='[]', final_url=self.url)
        self.records = [dict(node_key='parent', parent_key='', name='Parent', kind='unit', relation='directory_entry', url=''),
            dict(node_key='child', parent_key='parent', name='Child', kind='unit', relation='sub_unit', url='')]
        for i, row in enumerate(self.records):
            row.update(reference_url=self.url, locator=f'main li:nth-child({i+1})',
                       content_hash=self.page['content_hash'], observed_at=self.page['checked_at'])
        self.publish()

    def tearDown(self):
        self.guards.close()
        self.fixture.tearDown()

    def publish(self):
        self.fixture.publish([self.page])
        with self.fixture.catalog.connect(write=True) as connection:
            connection.execute('UPDATE catalog_sites SET report_gzip=?',
                (pack(dict(states={'fetched': 1}, reference_checks=[])),))
            connection.execute('UPDATE catalog_pages SET nodes_gzip=? WHERE url=?', (pack(self.records), self.url))

    def test_parser_enum_does_not_become_authoritative_relationship(self):
        for relation in ('sub_unit', 'attached_unit'):
            self.records[1]['relation'] = relation
            child = evidence_tree(self.records, {self.url: self.page})[0]['children'][0]
            self.assertEqual(child['relation_label'], '目录列示，隶属关系待核实')

    def test_duplicate_placements_keep_all_material_locators(self):
        self.records.append(dict(self.records[1], locator='footer li.child'))
        child = evidence_tree(self.records, {self.url: self.page})[0]['children'][0]
        self.assertEqual({row['locator'] for row in child['evidence']}, {'main li:nth-child(2)', 'footer li.child'})

    def test_optional_report_and_raw_nodes_share_the_pinned_reader_and_version_window(self):
        pinned = self.fixture.catalog
        with patch('backend.services.runtime_catalog.RuntimeCatalog.pin', return_value=pinned), \
                patch.object(pinned, 'report', wraps=pinned.report) as report, \
                patch.object(pinned, 'structure', wraps=pinned.structure) as structure:
            capture = read_structure(self.school, include_catalog_report=True)
        self.assertEqual(capture['version'], current_version(self.school.id))
        self.assertEqual(capture['catalog_report']['pages'][0]['content_hash'], 'fixture-material')
        self.assertEqual(capture['raw_structure_nodes'],
                         [dict(row, material_available=True) for row in self.records])
        report.assert_called_once()
        structure.assert_called_once()
        with patch('backend.services.runtime_catalog.RuntimeCatalog.report', side_effect=AssertionError('Unnecessary report read')):
            self.assertNotIn('catalog_report', read_structure(self.school))

    def test_page_does_not_claim_ownership_from_raw_nesting(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200)
        html = str(BeautifulSoup(response.get_data(as_text=True), 'html.parser').select_one('.structure-page'))
        self.assertIn('目录列示，隶属关系待核实', html)
        self.assertNotIn('id="provenStructureRelations"', html)
        self.assertIn('main li:nth-child(2)', html)
        self.assertIn('fixture-material', html)
        version = current_version(self.school.id)
        self.assertIn(f'data-structure-epoch="{version["epoch"]}"', html)
        self.assertIn(f'data-structure-revision="{version["revision"]}"', html)

    def test_proven_subordinate_and_attached_units_keep_distinct_predicates(self):
        parent = Department(school_id=self.school.id, name='Parent', kind='unit', structure_key='parent')
        child = Department(school_id=self.school.id, name='Child', kind='unit', structure_key='child')
        db.session.add_all([parent, child])
        db.session.flush()
        entry = DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id)
        db.session.add(entry)
        for observed, predicate, label in [('sub_unit', 'subordinate_unit', '下属单位'),
                                           ('attached_unit', 'attached_unit', '挂靠单位')]:
            entry.facts_json = [dict(type='organization_membership', observed_relation=observed,
                assertion=dict(kind='explicit_table_header', predicate=predicate, direction='parent_to_child',
                    text=label, locator='thead th', parent_locator='tbody .parent', child_locator='tbody .child'),
                evidence=[dict(self.records[1], locator='thead th'), dict(self.records[1], locator='tbody .child')])]
            db.session.commit()
            capture = read_structure(self.school, include_catalog_report=True)
            claims, _ = proven_page_relations(capture['facts'], self.page)
            self.assertEqual([claim['relation_label'] for claim in claims], [label])
            self.assertEqual(claims[0]['source_node']['label'], 'Parent')
            self.assertEqual(claims[0]['target_node']['label'], 'Child')
            self.assertEqual(len(claims[0]['evidence']), 2)
            html = self.client.get(self.endpoint).get_data(as_text=True)
            panel = BeautifulSoup(html, 'html.parser').select_one('#provenStructureRelations')
            self.assertIsNotNone(panel)
            self.assertIn(label, panel.get_text())
            self.assertIn('thead th', panel.get_text())
        claims, _ = proven_page_relations(capture['facts'], dict(self.page, content_hash='new-material'))
        self.assertEqual(claims, [], 'Evidence from a different material must not label the current page')

    def test_major_page_entrances_do_not_use_inferred_ownership_paths(self):
        self.page['notes_json'] = '["reviewed_major_directory"]'
        self.publish()
        with self.fixture.catalog.connect(write=True) as connection:
            incoming = dict(self.page, url=self.school.url, kind='root', label='Homepage',
                navigation_edges=[dict(parent_url=self.school.url, target_url=self.url, locator='nav a.directory',
                    content_hash='fixture-material', observed_at=self.page['checked_at'])])
            connection.execute('INSERT INTO catalog_pages VALUES(?,?,?,?,?,?)',
                (2, 1, self.school.url, pack(incoming), pack([]), b'NO HTML READ'))
        with patch('backend.services.runtime_catalog.relationships_for', side_effect=AssertionError('No inferred ownership')):
            response = self.client.get(self.endpoint, query_string={'reference': self.url})
        self.assertEqual(response.status_code, 200)
        page = BeautifulSoup(response.get_data(as_text=True), 'html.parser').select_one('.structure-page')
        self.assertIn('Homepage', page.select_one('#recordedPageEntrances').get_text())
        self.assertIsNone(page.select_one('#provenStructureRelations'))

    def test_report_read_crossing_version_retries_whole_capture_and_churn_is_503(self):
        stable = current_version(self.school.id)
        advanced = dict(stable, revision=stable['revision'] + 1)
        with patch('backend.services.structure_versions.current_version', side_effect=[stable, advanced, advanced, advanced]), \
                patch('backend.services.runtime_catalog.RuntimeCatalog.pin', return_value=self.fixture.catalog), \
                patch.object(self.fixture.catalog, 'report', wraps=self.fixture.catalog.report) as report:
            capture = read_structure(self.school, include_catalog_report=True)
        self.assertEqual(capture['version'], advanced)
        self.assertEqual(report.call_count, 2)
        with patch('backend.services.structure_versions.current_version', side_effect=[stable, advanced] * 3):
            self.assertEqual(self.client.get(self.endpoint).status_code, 503)

    def test_public_page_never_emits_credential_bearing_entry_urls(self):
        self.records[1]['url'] = self.school.url + '/private?token=do-not-display'
        self.records.append(dict(self.records[1], node_key='userinfo', url='https://user:do-not-display@example.edu/'))
        self.publish()
        response = self.client.get(self.endpoint)
        page = BeautifulSoup(response.get_data(as_text=True), 'html.parser').select_one('.structure-page')
        self.assertNotIn('do-not-display', str(page))

    def test_missing_original_material_is_explicit_in_evidence_details(self):
        self.publish()
        with self.fixture.catalog.connect(write=True) as connection:
            connection.execute("UPDATE catalog_pages SET evidence_gzip=X'' WHERE url=?", (self.url,))
        page = BeautifulSoup(self.client.get(self.endpoint).data, 'html.parser').select_one('.structure-page')
        self.assertIn('原始材料未保留', page.get_text())

    def test_english_metadata_uses_locale_markers_without_translating_observed_names(self):
        from backend.services.localization import stage_preferences
        self.records[1]['name'] = '官网记录的学院名称'
        self.records[1]['url'] = self.url
        self.publish()
        stage_preferences(self.fixture.fixture.admin.id, {'locale': 'en'})
        db.session.commit()
        page = BeautifulSoup(self.client.get(self.endpoint).data, 'html.parser').select_one('.structure-page')
        self.assertEqual(page.select_one('[data-i18n="目录列示，隶属关系待核实"]').get_text(),
                         'Directory listing; affiliation unverified')
        self.assertEqual(page.select_one('[data-i18n="有近期发布"]').get_text(), 'Recent publications found')
        self.assertIn('官网记录的学院名称', page.get_text())
        self.assertFalse(any(node.find_parent(attrs={'data-i18n': True}) for node in page.find_all(string='官网记录的学院名称')))


if __name__ == '__main__':
    unittest.main()
