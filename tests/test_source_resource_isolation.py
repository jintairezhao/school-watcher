"""Independent article/file tasks cannot overwrite attributed source-entry facts."""
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementResource, AnnouncementSource,
    BackgroundTask, Department, School, ScrapeLog, User, UserAnnouncementState, UserRead)
from backend.database.structure_version_models import SourceAccessObservation
from backend.scraper.acquisition import FetchResult
from backend.services import tasks, content_cache, saved_materials
from backend.services.source_channels import channels_for
from backend.services.structure_snapshot import read_structure, source_choices
from tests.test_collection_access_snapshot import CollectionAccessSnapshotTests

TITLE = '关于研究生报名安排的通知'
BODY = ('<article><h1>' + TITLE + '</h1><p>请各位同学按照通知安排提交报名材料，'
        '并在规定时间内与学院负责老师核对个人信息及相关申请事项。</p></article>')
PDF = b'%PDF-1.7\ncontrolled resource-state fixture\n%%EOF\n'


class SourceResourceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.f = CollectionAccessSnapshotTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.app = self.f.app
        self.assertTrue(self.f.save(self.f.result(200)))
        tasks.finish(self.f.handle, result={})
        self.source_id, self.school_id = self.f.source.id, self.f.source.school_id
        ann = Announcement(school_id=self.school_id, department_id=self.source_id, title=TITLE,
            url='https://example.edu.cn/info/resource-isolation.htm')
        db.session.add(ann); db.session.flush()
        self.ann_id = ann.id
        user = User.query.first()
        db.session.add_all([UserRead(user_id=user.id, announcement_id=ann.id),
            UserAnnouncementState(user_id=user.id, announcement_id=ann.id, starred=True),
            AnnouncementSource(announcement_id=ann.id, department_id=self.source_id,
                list_url=self.f.source.list_url, article_url=ann.url)])
        db.session.commit()
        self.before = self.f.f.get()
        retained = self.f.f.retained()
        retained.pop('announcements')
        self.retained = retained
        self.original_entry = SourceAccessObservation.query.one().observation_json
        self.original_log_count = ScrapeLog.query.count()
        self.seen = []

    def ann(self):
        return db.session.get(Announcement, self.ann_id)

    def execute_job(self, job, response, boundary):
        from backend.worker import execute
        job_id = job.id
        db.session.execute(db.update(BackgroundTask).where(BackgroundTask.id != job_id)
            .values(available_at=datetime.utcnow() + timedelta(days=1)))
        db.session.commit()
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'], job_id)
        future = self.f.stamp + timedelta(hours=1)
        class ObservationClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return future.astimezone(tz) if tz else future.replace(tzinfo=None)
        def transport(request, *args, **kwargs):
            self.seen.append((request.purpose, request.source_id, request.url))
            return response
        with patch(boundary, side_effect=transport), \
             patch('backend.scraper.acquisition.coordinator.datetime', ObservationClock), \
             patch('backend.services.desktop_maintenance.before_fetch'), \
             patch('backend.worker.logger.exception'):
            execute(self.app, handle)
        db.session.expire_all()
        self.f.source = db.session.get(Department, self.source_id)
        self.f.f.f.school = db.session.get(School, self.school_id)
        return db.session.get(BackgroundTask, job_id)

    def assert_source_unchanged(self):
        graph = self.f.f.get()
        self.assertEqual(graph['nodes'], self.before['nodes'])
        self.assertEqual(graph['relations'], self.before['relations'])
        self.assertEqual(graph['structure_token'], self.before['structure_token'])
        node = self.f.source_node(graph)
        self.assertEqual((node['access'], node['state']), ('responded', 'connected'))
        with self.app.test_request_context('/'):
            capture = read_structure(self.f.f.f.school)
            choice = next(c for groups in source_choices(capture).values() for group in groups
                for c in group['own_columns'] if c['department'].id == self.source_id)
            label = channels_for([self.f.source])[self.source_id]
            for value in (choice, label):
                self.assertEqual(value['access_observation'], node['access_observation'])
        retained = self.f.f.retained(); retained.pop('announcements')
        self.assertEqual(retained, self.retained)
        self.assertEqual(SourceAccessObservation.query.one().observation_json, self.original_entry)
        self.assertEqual(ScrapeLog.query.count(), self.original_log_count)
        self.assertEqual((self.ann().department_id, self.ann().title, self.ann().url),
            (self.source_id, TITLE, 'https://example.edu.cn/info/resource-isolation.htm'))

    def test_article_failure_and_explicit_retry_keep_the_source_entry_and_personal_state(self):
        job = content_cache.request_content(self.ann())
        response = FetchResult(self.ann().url, status=404, outcome='denied', error_code='http_404')
        job = self.execute_job(job, response, 'backend.scraper.acquisition.coordinator.http_fetch')
        self.assertEqual(job.state, 'failed')
        self.assertEqual(content_cache.status(self.ann()), 'failed')
        self.assertTrue(self.ann().content_error)
        self.assert_source_unchanged()
        generation, ident = job.generation, job.id
        retry = content_cache.request_content(self.ann())
        self.assertEqual((retry.id, retry.generation), (ident, generation + 1))
        response = FetchResult(self.ann().url, status=200,
            html='<html><title>' + TITLE + '</title><body>' + BODY + '</body></html>')
        job = self.execute_job(retry, response, 'backend.scraper.acquisition.coordinator.http_fetch')
        self.assertEqual(job.state, 'done')
        self.assertEqual(content_cache.status(self.ann()), 'saved')
        self.assertIn('提交报名材料', self.ann().content_text)
        self.assertFalse(self.ann().content_error)
        self.assertEqual(len(self.seen), 2)
        self.assertTrue(all(purpose == 'article' and source_id == f'content:{self.ann_id}'
                            for purpose, source_id, _ in self.seen))
        self.assert_source_unchanged()

    def test_file_failure_and_explicit_retry_keep_entry_body_and_other_ready_file(self):
        ann = self.ann()
        ann.content_html = BODY + '<a href="https://example.edu.cn/notice.pdf">附件</a>'
        ann.content_text = '已保存的正文'
        ann.content_cached_at = datetime.utcnow()
        db.session.commit()
        saved_materials.queue_materials(ann)
        row = AnnouncementResource.query.filter_by(announcement_id=self.ann_id).one()
        key = row.url_key
        sibling = AnnouncementResource(announcement_id=self.ann_id,
            url_key=saved_materials.resource_key('https://example.edu.cn/already.pdf'),
            url='https://example.edu.cn/already.pdf', filename='already.pdf', kind='attachment',
            state='ready', payload=PDF, byte_size=len(PDF))
        db.session.add(sibling); db.session.commit()
        sibling_key = sibling.url_key
        body_before = (ann.content_html, ann.content_text, ann.content_cached_at)
        job = BackgroundTask.query.filter_by(identity=f'saved_resource:{self.ann_id}:{key}').one()
        failed = FetchResult(row.url, status=403, outcome='denied', error_code='http_403', message='controlled file denial')
        job = self.execute_job(job, failed, 'backend.scraper.acquisition.http_transport.fetch_http')
        self.assertEqual(job.state, 'failed')
        row = db.session.get(AnnouncementResource, (self.ann_id, key))
        self.assertEqual(row.state, 'failed')
        self.assertIsNone(row.payload)
        self.assertEqual(saved_materials.material_status(self.ann())['state'], 'failed')
        self.assert_source_unchanged()
        generation = job.generation
        saved_materials.queue_materials(self.ann(), retry=True)
        retry = db.session.get(BackgroundTask, job.id)
        self.assertEqual(retry.generation, generation + 1)
        success = FetchResult(row.url, status=200, outcome='usable',
            headers={'Content-Type': 'application/pdf'}, attachment={'payload': PDF})
        job = self.execute_job(retry, success, 'backend.scraper.acquisition.http_transport.fetch_http')
        self.assertEqual(job.state, 'done')
        row = db.session.get(AnnouncementResource, (self.ann_id, key))
        self.assertEqual((row.state, row.payload, row.error), ('ready', PDF, ''))
        self.assertEqual(saved_materials.material_status(self.ann())['state'], 'ready')
        self.assertEqual((self.ann().content_html, self.ann().content_text, self.ann().content_cached_at), body_before)
        self.assertEqual(db.session.get(AnnouncementResource, (self.ann_id, sibling_key)).payload, PDF)
        self.assertEqual(len(self.seen), 2)
        self.assertTrue(all(purpose == 'article' and source_id.startswith('saved-resource:')
                            for purpose, source_id, _ in self.seen))
        self.assert_source_unchanged()
