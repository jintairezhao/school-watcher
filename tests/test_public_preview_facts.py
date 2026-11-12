"""The public school selector is a current source view with explicit history."""
from datetime import datetime
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department
from backend.services.source_channels import channels_for
from tests import test_shared_page_mutations as shared_fixture


class PublicPreviewFactsTests(unittest.TestCase):
    def setUp(self):
        self.f = shared_fixture.SharedPageMutationTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app, self.school = self.f.f.fixture.app, self.f.f.school
        self.client = self.app.test_client()  # No account or subscription session.
        provider = patch('backend.ai.providers.complete', side_effect=AssertionError('No provider'))
        provider.start(); self.addCleanup(provider.stop)

    def options(self, **query):
        response = self.client.get(f'/school/{self.school.id}', query_string=query)
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.text, 'lxml')
        return response, {int(row['value']): row for row in soup.select('#previewColumn option[value]') if row['value']}

    def test_selector_matches_connected_graph_and_uses_the_same_captions(self):
        pending = Department(school_id=self.school.id, name='未经验证的配置', kind='column',
            list_url=self.f.url, list_selector='#pending li', group_name='旧目录组')
        candidate = Department(school_id=self.school.id, name='待探索栏目', kind='column',
            list_url=self.f.url + 'candidate/', group_name='旧目录组')
        db.session.add_all([pending, candidate]); db.session.commit()
        saved, task_count = self.f.retained(), BackgroundTask.query.count()
        graph = self.f.get()
        expected_ids = {node['source_id'] for node in graph['nodes'] if node['entity_type'] == 'source'}
        self.assertEqual(expected_ids, {source.id for source in self.f.sources})
        with self.app.test_request_context('/'):
            captions = channels_for(self.f.sources)
        response, options = self.options()
        self.assertEqual(set(options), expected_ids)
        for ident, option in options.items():
            self.assertEqual(option.get_text(' ', strip=True), captions[ident]['breadcrumb'])
            self.assertEqual(option['data-source-scope'], 'current')
        self.assertEqual(self.f.retained(), saved)
        self.assertEqual(BackgroundTask.query.count(), task_count)
        self.assertEqual(self.f.get()['structure_version'], graph['structure_version'])

    def test_history_is_explicit_retains_old_ids_and_filters_without_subscription(self):
        legacy_unit = Department(school_id=self.school.id, name='旧部门通知范围', kind='unit',
            list_url=self.school.url + 'old-unit/')
        pending = Department(school_id=self.school.id, name='旧配置通知', kind='column',
            list_url=self.f.url + 'old/', list_selector='li', group_name='旧分组')
        db.session.add_all([legacy_unit, pending]); db.session.flush()
        for source in (legacy_unit, pending):
            db.session.add(Announcement(school_id=self.school.id, department_id=source.id,
                title=f'{source.name}保留消息', url=source.list_url + 'notice.htm',
                published_at=datetime(2026, 10, 1), content_text='保留的历史通知'))
        db.session.commit()
        self.assertEqual(self.f.lifecycle(self.f.first, 'disabled').status_code, 200)
        saved, task_count = self.f.retained(), BackgroundTask.query.count()
        _, options = self.options()
        self.assertEqual(set(options), {self.f.first.id, self.f.second.id, legacy_unit.id, pending.id})
        for ident in (self.f.first.id, legacy_unit.id, pending.id):
            self.assertEqual(options[ident]['data-source-scope'], 'historical')
            self.assertIn('历史通知范围', options[ident].get_text())
        self.assertEqual(options[self.f.second.id]['data-source-scope'], 'current')
        filtered, selected = self.options(dept=legacy_unit.id)
        self.assertTrue(selected[legacy_unit.id].has_attr('selected'))
        titles = [row.get_text(' ', strip=True) for row in BeautifulSoup(filtered.text, 'lxml').select('.preview-notice h2')]
        self.assertEqual(titles, [legacy_unit.name + '保留消息'])
        self.client.set_cookie('watcher_locale', 'en')
        _, english = self.options()
        self.assertIn('Historical notice sources', english[legacy_unit.id].get_text())
        self.assertNotIn('历史通知范围', english[legacy_unit.id].get_text())
        self.assertEqual(self.f.retained(), saved)
        self.assertEqual(BackgroundTask.query.count(), task_count)


if __name__ == '__main__':
    unittest.main()
