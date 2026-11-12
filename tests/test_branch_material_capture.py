"""Trace real queue and native commits on isolated local fixtures, not website efficacy."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.ai.material_capture import capture_materials
from backend.database.db import db
from backend.database.source_governance_models import SourceConfigVersion
from backend.services.source_inventory import Inventory
from tests import test_source_execution_events as fixture


class BranchMaterialCaptureTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.SourceExecutionEventTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.root = Path(self.f.flow.f.temp.name) / 'branch-materials'

    def records(self, session):
        return [json.loads(p.read_text(encoding='utf-8')) for p in sorted(session.path.glob('event-*.json'))]

    def test_claim_freezes_complete_actual_inputs_before_later_pages_exist(self):
        store = Inventory(self.root.parent / 'frontier.db')
        school = self.f.school
        key = store.ensure_site(school.name, school.url)
        store.enqueue(key, school.url + 'known/', '已知入口', 'gateway', 1, [], 'school_domain')
        with capture_materials(self.root, label='frontier') as session:
            selected = store.claim(key, focus='valuable')
            store.enqueue(key, school.url + 'later/', '稍后才发现的入口', 'gateway', 1, [], 'school_domain')
        records = self.records(session)
        before, = [r for r in records if r['event'] == 'frontier_inputs']
        after, = [r for r in records if r['event'] == 'frontier_result']
        self.assertEqual(len(before['payload']['arguments']['pages']), 2)
        self.assertEqual(after['payload']['selected_url'], selected['url'])
        self.assertEqual(before['payload']['decision_id'], after['payload']['decision_id'])
        self.assertNotIn('稍后才发现的入口', json.dumps(before, ensure_ascii=False))
        self.assertEqual(len(store.report(key)['pages']), 3)
        self.assertTrue(session.complete)

    def test_native_validation_and_source_receipt_are_read_after_real_commit(self):
        with capture_materials(self.root, label='committed-native') as session:
            version = self.f.connect()
            version_id, source_id = version.id, version.department_id
        records = self.records(session)
        validations = [r for r in records if r['event'] == 'native_validation' and r['payload']['persistence'] == 'committed']
        saved, = [r for r in records if r['event'] == 'source_configuration' and r['payload']['persistence'] == 'committed']
        self.assertTrue(validations)
        self.assertEqual(saved['payload']['record_id'], version_id)
        self.assertEqual(saved['payload']['detail']['source_id'], source_id)
        self.assertEqual(saved['payload']['detail']['config_hash'], version.config_hash)
        self.assertEqual(saved['payload']['detail']['task_id'], version.execution_json['task_id'])
        self.assertTrue(saved['payload']['detail']['page_receipt']['events'])
        self.assertTrue(all(r['payload']['detail']['articles'] for r in validations))
        self.assertFalse(saved['payload']['detail']['establishes_ownership'])
        self.assertTrue(session.complete)

    def test_rolled_back_configuration_is_not_recorded_as_a_success(self):
        with capture_materials(self.root, label='rolled-back-native') as session:
            with patch('backend.services.school_structure.sync_official_structure', side_effect=RuntimeError('fixture rollback')):
                with self.assertRaisesRegex(RuntimeError, 'fixture rollback'):
                    self.f.connect()
            db.session.rollback()
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        saved, = [r for r in self.records(session) if r['event'] == 'source_configuration']
        self.assertEqual(saved['payload']['persistence'], 'not_visible_as_committed_at_capture_close')
        self.assertNotIn('detail', saved['payload'])

    def test_observer_does_not_flush_or_commit_pending_native_validation(self):
        from backend.services.source_validation import record_validation
        from backend.database.source_governance_models import SourceValidation
        with capture_materials(self.root, label='pending-native') as session:
            record_validation(self.f.school.id, {'list_url': self.f.school.url, 'name': 'pending'}, {'status': 'pending'})
            row, = [r for r in db.session.new if isinstance(r, SourceValidation)]
            self.assertIsNone(row.id)
        self.assertIsNone(row.id)
        self.assertIn(row, db.session.new)
        native, = [r for r in self.records(session) if r['event'] == 'native_validation']
        self.assertEqual(native['payload']['persistence'], 'not_visible_as_committed_at_capture_close')
        db.session.rollback()


if __name__ == '__main__':
    unittest.main()
