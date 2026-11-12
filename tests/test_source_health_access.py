"""A due source-health read must publish the same attributed entry facts."""
from datetime import datetime, timedelta
from unittest.mock import patch
import unittest

from backend.database.db import db
from backend.database.models import BackgroundTask, ScrapeLog
from backend.database.structure_version_models import SourceAccessObservation
from backend.scraper.acquisition import FetchResult
from backend.services import tasks
from backend.services.source_channels import channels_for
from backend.services.structure_snapshot import read_structure, source_choices
from tests.test_collection_access_snapshot import CollectionAccessSnapshotTests


class SourceHealthAccessTests(unittest.TestCase):
    def setUp(self):
        self.f = CollectionAccessSnapshotTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app, self.source = self.f.app, self.f.source
        self.source.last_scraped_at = None
        db.session.commit()
        self.assertTrue(self.f.save(self.f.result(403)))
        tasks.finish(self.f.handle, result={})
        self.before = self.f.f.get()
        self.retained = self.f.f.retained()
        self.logs = ScrapeLog.query.count()

    def execute_health(self, status=200, *, parser_error=False):
        from backend.worker import execute
        job = tasks.enqueue('source_health', self.source.id,
            {'school_id': self.source.school_id, 'department_id': self.source.id})
        db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id != job.id)
            .values(available_at=datetime.utcnow() + timedelta(days=1)))
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'], job.id)
        html = self.f.f.f.pages[self.source.list_url] if status == 200 else '<h1>Not Found</h1>'
        response = FetchResult(self.source.list_url, status=status, html=html,
            observed_at=self.f.result(status, 10).observed_at,
            outcome='usable' if status == 200 else 'denied',
            error_code='' if status == 200 else f'http_{status}')
        with patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=response) as http, \
             patch('backend.services.desktop_maintenance.before_fetch'), \
             patch('backend.scraper.selector_monitor.evaluate_and_repair',
                   side_effect=ValueError('injected parser failure') if parser_error else None,
                   return_value={'action': 'healthy'}):
            execute(self.app, handle)
        return job.id, http.call_count

    def assert_shared_access(self, expected):
        graph = self.f.f.get()
        node = self.f.source_node(graph)
        self.assertEqual((node['access'], node['state']), (expected, 'connected'))
        self.assertEqual(graph['relations'], self.before['relations'])
        with self.app.test_request_context('/'):
            capture = read_structure(self.f.f.f.school)
            choice = next(c for groups in source_choices(capture).values() for group in groups
                for c in group['own_columns'] if c['department'].id == self.source.id)
            label = channels_for([self.source])[self.source.id]
            for view in (choice, label):
                self.assertEqual(view['access_observation'], node['access_observation'])
        self.assertEqual(self.f.f.retained(), self.retained)
        self.assertEqual(ScrapeLog.query.count(), self.logs)
        return graph, node

    def test_successful_health_worker_replaces_stale_restriction_in_all_readers(self):
        job_id, calls = self.execute_health()
        graph, node = self.assert_shared_access('responded')
        self.assertEqual(calls, 1)
        self.assertEqual(db.session.get(BackgroundTask, job_id).state, 'done')
        self.assertEqual(node['access_observation']['kind'], 'health_entry_response')
        delta = self.f.f.get(since=self.before['structure_token'])
        self.assertEqual(delta['update_mode'], 'delta')
        self.assertTrue(any(n['id'] == node['id'] and n['access'] == 'responded'
                            for n in delta['delta']['nodes']['upsert']))
        other = f'source:{self.f.f.second.id}'
        self.assertEqual(next(n for n in graph['nodes'] if n['id'] == other),
                         next(n for n in self.before['nodes'] if n['id'] == other))

    def test_failed_health_worker_records_entry_response_without_removing_source(self):
        _, calls = self.execute_health(404)
        _, node = self.assert_shared_access('not_found')
        self.assertEqual(calls, 1)
        self.assertEqual(node['status_code'], 404)

    def test_parser_failure_does_not_erase_the_completed_entry_observation(self):
        self.execute_health(parser_error=True)
        self.assert_shared_access('responded')

    def test_recent_collection_skip_makes_no_new_observation_or_request(self):
        self.source.last_scraped_at = datetime.utcnow()
        db.session.commit()
        row = SourceAccessObservation.query.one().observation_json
        job_id, calls = self.execute_health()
        after = self.f.f.get()
        self.assertEqual(calls, 0)
        self.assertEqual(after['structure_token'], self.before['structure_token'])
        self.assertEqual(SourceAccessObservation.query.one().observation_json, row)
        self.assertTrue(db.session.get(BackgroundTask, job_id).result['reused_recent_collection'])
