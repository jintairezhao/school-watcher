"""Current publication labels must use the graph's committed structure facts."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from flask import g

from backend.database.db import db
from backend.database.models import Announcement, Department, DepartmentDirectoryEntry
from backend.services.source_channels import channels_for
from backend.services.structure_snapshot import read_structure, source_paths
from tests.test_shared_page_mutations import SharedPageMutationTests
from tests.test_structure_facts import StructureFactsTests, DIRECTORY, SHARED


class StructureChannelFactsTests(unittest.TestCase):
    def setUp(self):
        self.f = SharedPageMutationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app, self.school = self.f.f.fixture.app, self.f.f.school
        provider = patch('backend.ai.providers.complete', side_effect=AssertionError('No paid model'))
        provider.start()
        self.addCleanup(provider.stop)

    def owners(self, capture, source):
        return {edge['source'] for edge in capture['facts']['relations']
            if edge['type'] == 'source_ownership' and edge['target'] == f'source:{source.id}'}

    def test_same_page_columns_and_paths_share_stable_proven_publisher_id(self):
        saved = self.f.retained()
        with self.app.test_request_context('/'):
            capture = read_structure(self.school)
            descriptors = channels_for(self.f.sources)
            for source in self.f.sources:
                item = descriptors[source.id]
                owner, = self.owners(capture, source)
                self.assertEqual(item['publisher_key'], owner)
                self.assertEqual({row['key'] for row in item['publishers']}, {owner})
                self.assertEqual(item['basis'], 'source_ownership')
                self.assertEqual(item['structure_version'], capture['version'])
                self.assertIn('source_ownership', {p['relation_type'] for p in source_paths(capture)[source.id]})
                item['publishers'][0]['label'] = 'Caller mutation'
                self.assertNotEqual(channels_for([source])[source.id]['publishers'][0]['label'], 'Caller mutation')
            self.assertNotEqual(descriptors[self.f.first.id]['source_id'], descriptors[self.f.second.id]['source_id'])
        self.assertEqual(self.f.retained(), saved)

    def test_legacy_directory_or_name_cannot_become_a_current_publisher(self):
        unit = Department(school_id=self.school.id, name='旧目录部门', kind='unit', list_url=self.f.url)
        source = Department(school_id=self.school.id, name='待确认栏目', kind='column',
            list_url=self.f.url, list_selector='#other li', group_name=unit.name)
        db.session.add_all([unit, source]); db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=unit.id, department_id=source.id, position=0))
        db.session.commit()
        saved = self.f.retained()
        with self.app.test_request_context('/'):
            capture = read_structure(self.school)
            self.assertEqual(self.owners(capture, source), set())
            item = channels_for([source])[source.id]
            self.assertEqual(item['publisher_label'], '')
            self.assertEqual(item['publishers'], [])
            self.assertEqual(item['units'], [])
            self.assertEqual(item['basis'], 'ownership_pending')
            self.assertEqual(item['display_group'], unit.name)
            self.assertIn('归属待核实', item['breadcrumb'])
            self.assertNotIn(unit.name, item['breadcrumb'])
            g.ui_locale = 'en'
            self.assertIn('ownership unverified', channels_for([source])[source.id]['breadcrumb'])
        self.assertEqual(self.f.retained(), saved)

    def test_successful_config_change_invalidates_request_cache_and_failed_change_does_not(self):
        source = self.f.first
        with self.app.test_request_context('/'):
            first = channels_for([source])[source.id]
            self.assertEqual(first['basis'], 'source_ownership')
            source.name = '改名后的本科生通知'
            db.session.commit()
            second = channels_for([source])[source.id]
            self.assertGreater(second['structure_version']['revision'], first['structure_version']['revision'])
            self.assertEqual(second['column_label'], source.name)
            self.assertEqual(second['basis'], 'ownership_pending')
            self.assertEqual(second['publishers'], [])
            self.assertEqual(channels_for([self.f.second])[self.f.second.id]['basis'], 'source_ownership')
            source.name = '未提交名称'
            db.session.flush()
            db.session.rollback()
            self.assertEqual(channels_for([source])[source.id], second)

    def test_two_proven_homonymous_publishers_are_not_merged_or_turned_into_a_chain(self):
        fixture = StructureFactsTests(); fixture.setUp()
        fixture.data['site']['name'] = self.school.name
        fixture.data['departments'] = [fixture.unit(1, name='共同名称'), fixture.unit(2, name='共同名称'), fixture.source(3)]
        fixture.data['directory_entries'] = [dict(parent_id=ident, department_id=3,
            relation_type='source_ownership', assertion=dict(kind='explicit_publisher_identity',
                predicate='publication_owner', direction='publisher_to_source', identity_text=f'Owner {ident}',
                identity_reference_url=DIRECTORY, identity_locator=f'main #owner-{ident}',
                entry_reference_url=DIRECTORY, entry_locator=f'main #column-{ident}', entry_kind='column_link', entry_url=SHARED),
            evidence=[fixture.proof(locator=f'main #owner-{ident}'), fixture.proof(locator=f'main #column-{ident}')])
            for ident in (1, 2)]
        capture = dict(facts=fixture.build(), version=dict(epoch='isolated-fixture', revision=1),
            department_records=deepcopy(fixture.data['departments']))
        source = SimpleNamespace(**fixture.data['departments'][-1])
        with self.app.test_request_context('/'), patch('backend.services.structure_snapshot.read_structure', return_value=capture):
            item = channels_for([source])[source.id]
            owners = self.owners(capture, source)
            self.assertEqual(len(owners), 2)
            self.assertEqual({owner['key'] for owner in item['publishers']}, owners)
            self.assertEqual(len(item['publishers']), 2)
            self.assertEqual(item['units'], [])  # Parallel owners are not successive ancestors.
            self.assertEqual(item['basis'], 'source_ownership')

    def test_reader_saved_list_and_search_preview_use_the_current_fact_labels(self):
        with self.app.test_request_context('/'):
            descriptors = channels_for(self.f.sources)
        source = self.f.first
        expected = descriptors[source.id]['breadcrumb']
        notice = Announcement.query.first()
        reader = self.f.client.get(f'/announcement/{notice.id}?source={source.id}')
        self.assertEqual(reader.status_code, 200)
        self.assertEqual(BeautifulSoup(reader.text, 'lxml').select_one('.reader-source').get_text(' ', strip=True), expected)
        saved = self.f.client.get(f'/?view=saved&school={self.school.id}&period=all')
        self.assertEqual(saved.status_code, 200)
        labels = [node.get_text(' ', strip=True) for node in BeautifulSoup(saved.text, 'lxml').select('.notice-source-label')]
        self.assertTrue(labels)
        self.assertTrue(all(any(row['breadcrumb'] in label for row in descriptors.values()) for label in labels))
        preview = self.f.client.get('/api/inbox/search-suggestions', query_string={
            'school': self.school.id, 'dept': source.id, 'period': 'all', 'q': notice.title})
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.json['items'])
        self.assertTrue(all(item['source'] == expected for item in preview.json['items']))


if __name__ == '__main__':
    unittest.main()
