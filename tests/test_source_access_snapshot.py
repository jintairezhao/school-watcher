"""The same saved entry observation reaches graph, source list and descriptors."""
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.source_governance_models import SourceValidation
from backend.services.source_channels import channels_for
from backend.services.structure_snapshot import read_structure, source_choices
from tests import test_shared_page_mutations as shared_fixture


class SourceAccessSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.f = shared_fixture.SharedPageMutationTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        provider = patch('backend.ai.providers.complete', side_effect=AssertionError('No model'))
        provider.start(); self.addCleanup(provider.stop)

    def test_committed_response_updates_shared_views_and_delta_without_changing_admission(self):
        first = self.f.first
        before = self.f.get()
        saved = {key: value for key, value in self.f.retained().items() if key != 'source_validations'}
        event = SourceValidation.query.filter_by(department_id=first.id).order_by(SourceValidation.id.desc()).first()
        self.assertIsNotNone(event)
        detail = json.loads(event.validation_json)
        detail['list_snapshot']['status'] = 200
        detail['list_snapshot']['observed_at'] = detail['list_snapshot']['captured_at']
        event.validation_json = json.dumps(detail, ensure_ascii=False)
        db.session.commit()
        after = self.f.get()
        node = next(row for row in after['nodes'] if row['id'] == f'source:{first.id}')
        self.assertEqual((node['access'], node['status_code'], node['state']), ('responded', 200, 'connected'))
        self.assertEqual(node['access_observation']['record_id'], f'native:{event.id}')
        self.assertEqual({row['id'] for row in after['nodes']}, {row['id'] for row in before['nodes']})
        self.assertEqual(after['relations'], before['relations'])
        with self.f.f.fixture.app.test_request_context('/'):
            capture = read_structure(self.f.f.school)
            column = next(column for groups in source_choices(capture).values() for group in groups
                for column in group['own_columns'] if column['department'].id == first.id)
            descriptor = channels_for([first])[first.id]
            for view in (column, descriptor):
                self.assertEqual(view['access'], node['access'])
                self.assertEqual(view['access_observation'], node['access_observation'])
        delta = self.f.get(since=before['structure_token'])
        self.assertEqual(delta['update_mode'], 'delta')
        self.assertTrue(any(row['id'] == node['id'] and row['access'] == 'responded'
                            for row in delta['delta']['nodes']['upsert']))
        self.assertEqual({key: value for key, value in self.f.retained().items() if key != 'source_validations'}, saved)


if __name__ == '__main__':
    unittest.main()
