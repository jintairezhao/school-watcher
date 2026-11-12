"""Isolated contracts for the shared structure read adapter and transport.

These cases use saved fixture documents only. They do not crawl, install
sources, touch a user profile, or call a model.
"""
from collections import deque
from contextlib import ExitStack
from pathlib import Path
import hashlib
import sys
import unittest
from unittest.mock import patch

from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_discovery_monitor as monitor_fixture

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, DepartmentDirectoryEntry, School, Subscription
from backend.database.structure_version_models import StructureChange
from backend.services.source_inventory import canonical_url, site_key
from backend.services.structure_snapshot import _transport, _transport_lock, read_structure, source_choices, source_paths
from backend.services.structure_versions import current_version, reset_epoch


class StructureSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.fixture = monitor_fixture.DiscoveryMonitorTests()
        self.fixture.setUp()
        self.app, self.school, self.client = self.fixture.app, self.fixture.school, self.fixture.client
        self.endpoint = f'/api/schools/{self.school.id}/structure-snapshot'
        self.guards = ExitStack()
        self.guards.enter_context(patch('backend.ai.providers.complete', side_effect=AssertionError('No model call')))
        self.guards.enter_context(patch('backend.services.source_inventory.Inventory.__init__',
                                       side_effect=AssertionError('No scratch inventory')))
        self.receipts = []
        self.guards.enter_context(patch('backend.services.source_path_receipts.load_source_receipts', return_value=self.receipts))
        with _transport_lock:
            _transport.clear()

    def tearDown(self):
        self.guards.close()
        with _transport_lock:
            _transport.clear()
        self.fixture.tearDown()

    def source(self, name='Notices', url=None, selector='li.notice', *, verified=False):
        row = Department(school_id=self.school.id, name=name, kind='column',
                         list_url=url or self.school.url + '/notices', list_selector=selector)
        db.session.add(row)
        db.session.commit()
        if verified:
            # This adapter fixture starts after the executed-receipt reader.
            # Keep its signature frozen so a changed configuration loses proof.
            from backend.services.source_governance import _hash, source_config
            self.receipts.append(dict(source_id=row.id, url=canonical_url(row.list_url),
                receipt_id='fixture:' + str(row.id), config_hash=_hash(source_config(row))))
        return row

    def document(self, url, *targets, label='Recorded page'):
        material = hashlib.sha256(('saved fixture document ' + url).encode()).hexdigest()
        return dict(url=url, label=label, kind='navigation', state='fetched',
                    content_hash=material, checked_at='2026-10-21T00:00:00Z',
                    navigation_edges=[dict(parent_url=url, target_url=target,
                        locator=f'nav a:nth-child({index + 1})', label='Recorded entrance',
                        content_hash=material, observed_at='2026-10-21T00:00:00Z')
                        for index, target in enumerate(targets)])

    def get(self, **query):
        response = self.client.get(self.endpoint, query_string=query)
        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        return response.json

    @staticmethod
    def apply_delta(base, update):
        result = dict(base, structure_token=update['structure_token'],
                      structure_version=update['structure_version'])
        for field in ('nodes', 'relations'):
            rows = {row['id']: row for row in result[field]}
            for ident in update['delta'][field]['remove']:
                rows.pop(ident, None)
            for row in update['delta'][field]['upsert']:
                rows[row['id']] = row
            result[field] = sorted(rows.values(), key=lambda row: row['id'])
        return result

    def test_same_url_keeps_one_page_and_two_independent_sources(self):
        first = self.source('Undergraduate notices', verified=True)
        second = self.source('Graduate notices', first.list_url, '.graduate li', verified=True)
        self.fixture.publish([self.document(self.school.url, first.list_url), self.document(first.list_url)])
        value = self.get()
        sources = [n for n in value['nodes'] if n['entity_type'] == 'source']
        self.assertEqual({n['id'] for n in sources}, {f'source:{first.id}', f'source:{second.id}'})
        pages = [n for n in value['nodes'] if n['entity_type'] == 'page' and n['url'] == first.list_url]
        self.assertEqual(len(pages), 1)
        mapped = [e for e in value['relations'] if e['type'] == 'source_on_page']
        self.assertEqual({e['source'] for e in mapped}, {pages[0]['id']})
        self.assertEqual({e['target'] for e in mapped}, {n['id'] for n in sources})
        self.assertTrue(all(not e['access'] for e in mapped))
        self.assertEqual(value['source_count'], 2)

    def test_legacy_directory_and_group_label_do_not_assert_ownership(self):
        group, unit, source = self.fixture.college()
        self.fixture.publish_college(group, unit, source)
        value = self.get()
        self.assertFalse(any(e['type'] in {'directory_listing', 'organization_membership', 'source_ownership'}
                             for e in value['relations']))
        self.assertGreaterEqual(value['structure_gaps']['counts']['legacy_membership_without_evidence'], 2)
        self.assertFalse(any(n['entity_type'] == 'organization' for n in value['nodes']))
        capture = read_structure(self.school)
        self.assertNotIn('有依据的栏目归属', source_choices(capture))
        self.assertEqual({p['relation_type'] for p in source_paths(capture)[source.id]}, {'source_on_page'})

    def test_home_direct_entrance_wins_without_reverse_or_admissions_detour(self):
        root, directory, admissions = self.school.url, self.school.url + '/departments', self.school.url + '/admissions'
        source = self.source(verified=True)
        self.fixture.publish([self.document(root, directory, admissions), self.document(admissions, directory),
                              self.document(directory, source.list_url), self.document(source.list_url)])
        value = self.get()
        pages = {n['url']: n for n in value['nodes'] if n['entity_type'] == 'page'}
        self.assertNotIn(admissions, pages)
        root_id, directory_id = pages[canonical_url(root)]['id'], pages[directory]['id']
        access = [e for e in value['relations'] if e['access']]
        self.assertIn((root_id, directory_id), {(e['source'], e['target']) for e in access})
        self.assertNotIn((directory_id, root_id), {(e['source'], e['target']) for e in access})
        by_source = {}
        for edge in access:
            by_source.setdefault(edge['source'], []).append(edge['target'])
        distances, queue = {root_id: 0}, deque([root_id])
        while queue:
            for target in by_source.get(queue.popleft(), []):
                if target not in distances:
                    distances[target] = 1
                    queue.append(target)
        self.assertIn(pages[source.list_url]['id'], distances)

    def test_default_scope_omits_unconnected_candidates_but_exploration_can_show_them(self):
        source = self.source(verified=True)
        candidate = self.school.url + '/candidate'
        self.source('Unverified candidate', candidate)
        self.fixture.publish([self.document(self.school.url, source.list_url, candidate),
                              self.document(source.list_url), self.document(candidate)])
        value = self.get()
        self.assertEqual(value['structure_scope']['mode'], 'connected')
        self.assertNotIn(candidate, {n['url'] for n in value['nodes']})
        exploration = self.get(scope='exploration')
        self.assertIn(candidate, {n['url'] for n in exploration['nodes']})
        self.assertEqual(exploration['exploration_events'], [])
        self.assertEqual(exploration['structure_gaps']['exploration_history'], 'not_recorded')

    def test_saved_configuration_without_current_verification_is_pending_not_connected(self):
        source = self.source()
        self.fixture.publish([self.document(self.school.url, source.list_url), self.document(source.list_url)])
        exploration = self.get(scope='exploration')
        node = next(n for n in exploration['nodes'] if n['id'] == f'source:{source.id}')
        self.assertTrue(node['configured'])
        self.assertEqual((node['state'], node['state_label']), ('pending', '待验证'))
        self.assertEqual(node['reason'], '已保存的栏目配置')
        self.assertEqual(exploration['source_count'], 0)
        self.assertEqual(exploration['progress']['active_source_count'], 1)
        self.assertEqual(exploration['progress']['verified_source_count'], 0)
        normal = self.get()
        self.assertNotIn(node['id'], {n['id'] for n in normal['nodes']})
        self.assertEqual(normal['source_count'], 0)
        self.assertEqual(db.session.get(Department, source.id).list_selector, 'li.notice')

    def test_changed_configuration_loses_connection_until_its_current_proof_is_valid(self):
        source = self.source(verified=True)
        ident = f'source:{source.id}'
        first = self.get()
        self.assertEqual(next(n for n in first['nodes'] if n['id'] == ident)['state'], 'connected')
        source.list_selector = 'li.changed'
        db.session.commit()
        update = self.get(since=first['structure_token'])
        self.assertIn(ident, update['delta']['nodes']['remove'])
        self.assertEqual(update['source_count'], 0)
        exploration = self.get(scope='exploration')
        self.assertEqual(next(n for n in exploration['nodes'] if n['id'] == ident)['state'], 'pending')
        self.assertEqual(db.session.get(Department, source.id).list_selector, 'li.changed')

    def test_old_projection_cursor_reloads_snapshot_without_changing_business_version(self):
        first = self.get()
        with _transport_lock:
            _transport[(self.school.id, 'connected', first['structure_token'])]['structure_projection_version'] -= 1
        after = self.get(since=first['structure_token'])
        self.assertEqual(after['update_mode'], 'snapshot')
        self.assertEqual(after['structure_version'], first['structure_version'])

    def test_duplicate_navigation_evidence_preserves_distinct_locators(self):
        source = self.source(verified=True)
        document = self.document(self.school.url, source.list_url, source.list_url)
        document['navigation_edges'].append(dict(document['navigation_edges'][0]))
        self.fixture.publish([document, self.document(source.list_url)])
        value = self.get()
        access = [e for e in value['relations'] if e['access']]
        self.assertEqual(len(access), 1)
        self.assertEqual({p['locator'] for p in access[0]['evidence']},
                         {'nav a:nth-child(1)', 'nav a:nth-child(2)'})
        self.assertEqual(len(access[0]['evidence']), 2)

    def test_page_labels_do_not_describe_pages_as_organization_entities(self):
        source = self.source(verified=True)
        unit_page = self.document(source.list_url, label='学院网站')
        unit_page['kind'] = 'unit'
        self.fixture.publish([self.document(self.school.url, source.list_url), unit_page])
        value = self.get()
        page = next(n for n in value['nodes'] if n['entity_type'] == 'page' and n['url'] == source.list_url)
        self.assertNotEqual(page['kind_label'], '组织实体')
        unit_page['kind'] = 'channel'
        self.fixture.publish([self.document(self.school.url, source.list_url), unit_page])
        value = self.get()
        page = next(n for n in value['nodes'] if n['entity_type'] == 'page' and n['url'] == source.list_url)
        self.assertNotEqual(page['kind_label'], '信息栏目')

    def test_repeated_snapshot_and_delta_are_idempotent_and_read_only(self):
        self.source()
        before = current_version(self.school.id)
        counts = (Department.query.count(), DepartmentDirectoryEntry.query.count(), BackgroundTask.query.count())
        first, repeat = self.get(), self.get()
        self.assertEqual(first['structure_token'], repeat['structure_token'])
        delta = self.get(since=first['structure_token'])
        self.assertEqual(delta['update_mode'], 'delta')
        self.assertEqual(delta['delta'], {key: {'upsert': [], 'remove': []} for key in ('nodes', 'relations')})
        self.assertEqual(current_version(self.school.id), before)
        self.assertEqual(counts, (Department.query.count(), DepartmentDirectoryEntry.query.count(), BackgroundTask.query.count()))

    def test_committed_rename_and_removal_apply_once_without_duplicate_nodes(self):
        source = self.source()
        first = self.get(scope='exploration')
        source.name = 'Student opportunities'
        db.session.commit()
        rename = self.get(scope='exploration', since=first['structure_token'])
        self.assertEqual(rename['update_mode'], 'delta')
        merged = self.apply_delta(first, rename)
        self.assertEqual(self.apply_delta(merged, rename), merged)
        renamed = next(n for n in merged['nodes'] if n['id'] == f'source:{source.id}')
        self.assertEqual(renamed['label'], source.name)
        self.assertEqual(merged['nodes'], self.get(scope='exploration')['nodes'])
        ident = f'source:{source.id}'
        db.session.delete(source)
        db.session.commit()
        removal = self.get(scope='exploration', since=rename['structure_token'])
        self.assertEqual(removal['update_mode'], 'delta')
        self.assertIn(ident, removal['delta']['nodes']['remove'])
        self.assertTrue(removal['delta']['relations']['remove'])
        merged = self.apply_delta(merged, removal)
        self.assertEqual(merged['nodes'], self.get(scope='exploration')['nodes'])
        self.assertEqual(merged['relations'], self.get(scope='exploration')['relations'])

    def test_rolled_back_change_has_no_optimistic_success_or_version_bump(self):
        source = self.source()
        first = self.get()
        source.name = 'Must never appear'
        db.session.flush()
        db.session.rollback()
        after = self.get(since=first['structure_token'])
        self.assertEqual(after['structure_token'], first['structure_token'])
        self.assertEqual(after['structure_version'], first['structure_version'])
        self.assertNotIn('Must never appear', str(after))
        self.assertEqual(after['delta']['nodes']['upsert'], [])

    def test_epoch_change_and_journal_gap_require_full_snapshot(self):
        source = self.source()
        first = self.get()
        reset_epoch(self.school.id, 'isolated restore')
        db.session.commit()
        restored = self.get(since=first['structure_token'])
        self.assertEqual(restored['update_mode'], 'snapshot')
        self.assertNotEqual(restored['structure_version']['epoch'], first['structure_version']['epoch'])
        source.name = 'After restore'
        db.session.commit()
        latest = current_version(self.school.id)
        StructureChange.query.filter_by(school_id=self.school.id, epoch=latest['epoch'],
                                       revision=latest['revision']).delete()
        db.session.commit()
        gap = self.get(since=restored['structure_token'])
        self.assertEqual(gap['update_mode'], 'snapshot')

    def test_tokens_are_bound_to_school_scope_and_permission(self):
        self.source()
        first = self.get()
        self.assertEqual(self.get(scope='exploration', since=first['structure_token'])['update_mode'], 'snapshot')
        other = School(name='Other school', url='https://other.example.edu', enabled=True)
        db.session.add(other); db.session.commit()
        other_endpoint = f'/api/schools/{other.id}/structure-snapshot'
        other_response = self.client.get(other_endpoint, query_string={'since': first['structure_token']})
        self.assertEqual(other_response.status_code, 200)
        self.assertEqual(other_response.json['update_mode'], 'snapshot')
        self.assertFalse(any(n.get('source_id') for n in other_response.json['nodes']))
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        self.assertEqual(reader.get(self.endpoint).status_code, 200)
        self.assertEqual(reader.get(other_endpoint, query_string={'since': first['structure_token']}).status_code, 403)
        Subscription.query.filter_by(school_id=self.school.id).delete(); db.session.commit()
        self.assertEqual(reader.get(self.endpoint, query_string={'since': first['structure_token']}).status_code, 403)
        self.assertIn(self.fixture.fixture.client().get(self.endpoint).status_code, (401, 403))
        self.school.enabled = False; db.session.commit()
        self.assertEqual(self.client.get(self.endpoint).status_code, 404)

    def test_url_credentials_are_not_exposed_in_nodes_evidence_or_source_paths(self):
        cases = [('/notices?token=PRIVATE_TOKEN_MARKER', 'PRIVATE_TOKEN_MARKER'),
                 ('/notices?password=PRIVATE_PASSWORD_MARKER', 'PRIVATE_PASSWORD_MARKER'),
                 ('/#/notices?token=PRIVATE_ROUTE_TOKEN', 'PRIVATE_ROUTE_TOKEN'),
                 ('https://PRIVATE_USER:PRIVATE_PASS@example.edu.cn/notices', 'PRIVATE_PASS')]
        for suffix, secret in cases:
            with self.subTest(secret=secret):
                address = suffix if suffix.startswith('https:') else self.school.url + suffix
                self.source('Private address', address)
                self.fixture.publish([self.document(self.school.url, address), self.document(address)])
                result = self.get(scope='exploration')
                self.assertNotIn(secret, str(result))
                self.assertNotIn(secret, str(source_paths(read_structure(self.school))))

    def test_source_choice_html_uses_the_public_url_projection(self):
        self.source('Private address', self.school.url + '/notices?token=PRIVATE_HTML_MARKER')
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        response = reader.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE_HTML_MARKER', response.json['choices_html'])

    def test_school_identity_is_refreshed_inside_the_versioned_read(self):
        original = self.school.url
        changed = 'https://new.example.edu.cn/'
        with Session(db.engine) as writer:
            row = writer.get(School, self.school.id)
            row.url = changed
            writer.commit()
        self.assertEqual(self.school.url, original, 'The fixture deliberately holds a stale ORM identity')
        capture = read_structure(self.school)
        self.assertEqual(capture['facts']['site_key'], site_key(changed))
        self.assertIn(changed, {n.get('url') for n in capture['facts']['nodes']})

    def test_source_lifecycle_is_shared_by_default_exploration_and_mutation_response(self):
        source = self.source(verified=True)
        ident = f'source:{source.id}'
        endpoint = f'/api/departments/{source.id}/lifecycle'
        initial = self.get()
        history = Announcement.query.count()
        subscription = Subscription.query.filter_by(user_id=self.fixture.fixture.reader.id, school_id=self.school.id).one()
        subscription.department_ids = [source.id]
        db.session.commit()
        for state, label in [('disabled', '已停用'), ('removed', '已移除')]:
            with self.subTest(state=state):
                response = self.client.post(endpoint, json={'state': state}, headers={'X-CSRF-Token': 'token'})
                self.assertEqual(response.status_code, 200)
                current = self.get()
                self.assertEqual(response.json['structure_version'], current['structure_version'])
                self.assertNotIn(ident, {n['id'] for n in current['nodes']})
                self.assertEqual(current['source_count'], 0)
                exploration = self.get(scope='exploration')
                source_node = next(n for n in exploration['nodes'] if n['id'] == ident)
                self.assertEqual((source_node['lifecycle'], source_node['state'], source_node['state_label']), (state, state, label))
                self.assertEqual(Announcement.query.count(), history)
                self.assertEqual(subscription.department_ids, [source.id])
        delta = self.get(since=initial['structure_token'])
        self.assertIn(ident, delta['delta']['nodes']['remove'])
        response = self.client.post(endpoint, json={'state': 'active'}, headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 200)
        restored = self.get()
        self.assertEqual(restored['source_count'], 1)
        self.assertEqual(next(n for n in restored['nodes'] if n['id'] == ident)['state'], 'connected')

    def test_source_choices_and_graph_report_identical_committed_version_and_status(self):
        source = self.source()
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        choices_endpoint = f'/api/subscriptions/{self.school.id}/discovery'
        source.name = 'Shared visible title'
        db.session.commit()
        choices = reader.get(choices_endpoint)
        self.assertEqual(choices.status_code, 200)
        graph = self.get(scope='exploration')
        self.assertEqual(choices.json['structure_version'], graph['structure_version'])
        self.assertIn(source.name, choices.json['choices_html'])
        self.assertEqual(next(n for n in graph['nodes'] if n['id'] == f'source:{source.id}')['label'], source.name)
        self.client.post(f'/api/departments/{source.id}/lifecycle', json={'state': 'disabled'}, headers={'X-CSRF-Token': 'token'})
        choices = reader.get(choices_endpoint)
        graph = self.get(scope='exploration')
        self.assertEqual(choices.json['structure_version'], graph['structure_version'])
        self.assertIn('已停用', choices.json['choices_html'])
        self.assertEqual(next(n for n in graph['nodes'] if n['id'] == f'source:{source.id}')['state'], 'disabled')

    def test_version_crossing_read_retries_and_persistent_churn_is_unavailable(self):
        self.source()
        stable = current_version(self.school.id)
        advanced = dict(stable, revision=stable['revision'] + 1)
        with patch('backend.services.structure_versions.current_version',
                   side_effect=[stable, advanced, advanced, advanced]) as cursor:
            capture = read_structure(self.school)
        self.assertEqual(cursor.call_count, 4)
        self.assertEqual(capture['version'], advanced)
        with patch('backend.services.structure_versions.current_version', side_effect=[stable, advanced] * 3):
            response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('nodes', response.json)


if __name__ == '__main__':
    unittest.main()
