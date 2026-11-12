"""Lifecycle summaries and legacy subscription scope share honest read semantics."""
import unittest
from unittest.mock import patch

from flask import template_rendered
from sqlalchemy.orm import Session
from backend.database.db import db
from backend.database.models import BackgroundTask, Department, DepartmentDirectoryEntry, Subscription
from backend.database.source_governance_models import SourceValidation
from backend.services.directory_options import expand_directory_ids
from backend.services.source_lifecycle import set_source_state
from backend.services.structure_snapshot import read_structure
import test_structure_snapshot as fixture


class StructureLifecycleProgressTests(unittest.TestCase):
    setUp = fixture.StructureSnapshotTests.setUp
    tearDown = fixture.StructureSnapshotTests.tearDown
    source = fixture.StructureSnapshotTests.source
    get = fixture.StructureSnapshotTests.get

    def discovery(self):
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        response = reader.get(f'/api/subscriptions/{self.school.id}/discovery')
        self.assertEqual(response.status_code, 200)
        return response.json

    def lifecycle(self, source, state):
        response = self.client.post(f'/api/departments/{source.id}/lifecycle',
            json={'state': state}, headers={'X-CSRF-Token': 'token'})
        self.assertEqual(response.status_code, 200)

    def test_saved_and_verified_counts_remain_separate_through_lifecycle(self):
        source = self.source()
        self.assertEqual(SourceValidation.query.filter_by(department_id=source.id).count(), 0)
        for state, expected in [('active', 1), ('disabled', 0), ('removed', 0), ('active', 1)]:
            with self.subTest(state=state):
                self.lifecycle(source, state)
                choices, graph = self.discovery(), self.get()
                self.assertEqual(choices['active_source_count'], expected)
                self.assertEqual(choices['verified_source_count'], 0)
                self.assertEqual(choices['inactive_source_count'], 1 - expected)
                self.assertEqual(graph['source_count'], 0)
                self.assertEqual(graph['progress']['verified_source_count'], 0)
                self.assertEqual(graph['progress']['message'], choices['message'])
                self.assertEqual(graph['structure_version'], choices['structure_version'])
                if not expected:
                    self.assertNotIn('后续自动更新', choices['message'])
                    self.assertIn('历史通知', choices['message'])
                    self.assertIn('已停用' if state == 'disabled' else '已移除', choices['choices_html'])

    def test_source_page_counts_all_enabled_configurations_not_only_receipts(self):
        from bs4 import BeautifulSoup
        legacy = self.source('Existing configured source')
        checked = self.source('Source with retained verification', verified=True)
        disabled = self.source('Disabled configured source')
        self.lifecycle(disabled, 'disabled')
        self.source('Unconfigured candidate', selector='')
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        response = reader.get(f'/subscriptions/{self.school.id}')
        self.assertEqual(response.status_code, 200)
        html = BeautifulSoup(response.text, 'html.parser')
        poll = self.discovery()
        self.assertEqual(poll['active_source_count'], 2)
        self.assertEqual(poll['verified_source_count'], 1)
        self.assertIn('已启用2个信息来源', html.select_one('#discoveryCounts').get_text())
        self.assertIn('其中1个有完整验证记录', html.select_one('#discoveryValidationCounts').get_text())
        choices = html.select_one('#sourceChoices')
        ids = {int(node['value']) for node in choices.select('[name="department"]')}
        self.assertTrue({legacy.id, checked.id} <= ids)
        # Displaying all enabled sources must not upgrade legacy evidence.
        capture = read_structure(self.school)
        legacy_node = next(node for node in capture['facts']['nodes']
            if node.get('source_id') == legacy.id and node['entity_type'] == 'source')
        self.assertNotEqual(legacy_node['verification'], 'verified')

    def test_inactive_sources_do_not_inflate_mixed_active_progress(self):
        active, disabled, removed = (self.source(name, verified=True) for name in ('Active', 'Disabled', 'Removed'))
        self.lifecycle(disabled, 'disabled')
        self.lifecycle(removed, 'removed')
        choices, graph = self.discovery(), self.get()
        self.assertEqual(choices['active_source_count'], 1)
        self.assertEqual(choices['inactive_source_count'], 2)
        self.assertEqual(graph['source_count'], 1)
        self.assertEqual({node['source_id'] for node in graph['nodes'] if node['entity_type'] == 'source'}, {active.id})

    def test_waiting_ai_messages_do_not_promise_collection_when_all_sources_are_inactive(self):
        source = self.source()
        self.lifecycle(source, 'disabled')
        task = BackgroundTask(identity=f'discover:{self.school.id}', kind='discover',
            payload={'school_id': self.school.id}, state='waiting', phase='ai_budget',
            checkpoint={'discovery_progress': {'ai_error_code': 'budget_exhausted'}})
        db.session.add(task)
        db.session.commit()
        with patch('backend.services.onboarding_progress.ai_available', return_value=True):
            for phase, error in [('ai_budget', 'budget_exhausted'), ('ai_result', 'ai_result_unknown'),
                                 ('ai_connection', 'provider_connection_paused')]:
                with self.subTest(phase=phase):
                    task.phase, task.error_code = phase, error
                    task.checkpoint = {'discovery_progress': {'ai_error_code': error}}
                    db.session.commit()
                    choices, graph = self.discovery(), self.get()
                    self.assertEqual(choices['verified_source_count'], 0)
                    for message in [choices['message'], choices['ai_message'], graph['progress']['message'], graph['progress']['ai_message']]:
                        self.assertNotIn('继续更新', message)
                        self.assertIn('历史通知', message)

    def test_legacy_scope_is_read_only_and_new_choices_submit_unique_source_ids(self):
        source = self.source()
        owner = Department(school_id=self.school.id, name='Legacy directory', kind='unit',
            list_url=self.school.url + '/legacy/')
        db.session.add(owner)
        db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=owner.id, department_id=source.id,
            position=0, facts_json=[]))
        sub = Subscription.query.filter_by(user_id=self.fixture.fixture.reader.id, school_id=self.school.id).one()
        sub.department_ids = [owner.id]
        db.session.commit()
        before = expand_directory_ids(self.school.id, sub.department_ids)
        choices = self.discovery()
        self.assertTrue(choices['legacy_scope'])
        self.assertIn(source.id, before)
        self.assertEqual(sub.department_ids, [owner.id])
        self.assertEqual(expand_directory_ids(self.school.id, sub.department_ids), before)
        self.assertIn(f'value="{source.id}"', choices['choices_html'])
        self.assertNotIn(f'value="{owner.id}"', choices['choices_html'])
        self.assertFalse(any(edge['type'] == 'source_ownership' for edge in self.get()['relations']))
        contexts = []
        def rendered(sender, template, context, **extra):
            contexts.append(context)
        reader = self.fixture.fixture.client(self.fixture.fixture.reader)
        with template_rendered.connected_to(rendered, self.app):
            response = reader.get(f'/subscriptions/{self.school.id}')
        self.assertEqual(response.status_code, 200)
        self.assertTrue(contexts[-1]['legacy_scope'])
        with patch('backend.routes.subscriptions.start_background_scrape'):
            response = reader.post(f'/subscriptions/{self.school.id}',
                data={'mode': 'selected', 'department': [str(source.id), str(source.id)], 'csrf_token': 'token'})
        self.assertEqual(response.status_code, 302)
        db.session.refresh(sub)
        self.assertEqual(sub.department_ids, [source.id])
        self.assertFalse(self.discovery()['legacy_scope'])

    def test_interleaved_commit_keeps_snapshot_counts_at_its_captured_version(self):
        source = self.source(verified=True)
        original_source_count = Department.query.filter_by(school_id=self.school.id, kind='column').count()
        captured = None
        def capture_then_commit(school, **kwargs):
            nonlocal captured
            captured = read_structure(school, **kwargs)
            with Session(db.engine) as writer:
                set_source_state(writer.get(Department, source.id), 'disabled', session=writer)
                writer.add(Department(school_id=self.school.id, name='New unconfigured candidate',
                    kind='column', list_url=self.school.url + '/later/', list_selector=''))
                writer.commit()
            return captured
        with patch('backend.services.structure_snapshot.read_structure', side_effect=capture_then_commit):
            first = self.get()
        self.assertEqual(first['structure_version'], captured['version'])
        self.assertEqual(first['source_count'], 1)
        self.assertEqual(first['progress']['structure_counts_version'], captured['version'])
        self.assertEqual([first['progress'][key] for key in
            ('active_source_count', 'inactive_source_count', 'source_count')], [1, 0, original_source_count])
        self.assertEqual(first['progress']['runtime_progress_scope'], 'live_not_structure_versioned')
        # All three public consumers receive that same pinned capture, even
        # though the database already contains a newer source lifecycle.
        contexts = []
        def rendered(sender, template, context, **extra):
            contexts.append(context)
        with patch('backend.services.structure_snapshot.read_structure', return_value=captured):
            old_choices = self.discovery()
            reader = self.fixture.fixture.client(self.fixture.fixture.reader)
            with template_rendered.connected_to(rendered, self.app):
                self.assertEqual(reader.get(f'/subscriptions/{self.school.id}').status_code, 200)
        for progress in (old_choices, contexts[-1]['discovery']):
            self.assertEqual(progress['structure_counts_version'], captured['version'])
            self.assertEqual([progress[key] for key in
                ('active_source_count', 'inactive_source_count', 'source_count')], [1, 0, original_source_count])
        fresh, choices = self.get(), self.discovery()
        self.assertEqual(fresh['structure_version']['epoch'], captured['version']['epoch'])
        self.assertEqual(fresh['structure_version']['revision'], captured['version']['revision'] + 1)
        self.assertEqual(fresh['source_count'], 0)
        self.assertEqual(choices['structure_version'], fresh['structure_version'])
        for progress in (fresh['progress'], choices):
            self.assertEqual(progress['structure_counts_version'], fresh['structure_version'])
            self.assertEqual([progress[key] for key in
                ('active_source_count', 'inactive_source_count', 'source_count')], [0, 1, original_source_count + 1])


if __name__ == '__main__':
    unittest.main()
