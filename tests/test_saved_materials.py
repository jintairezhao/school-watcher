"""Favorite retention includes binary resources and each user's dated trash pin."""
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, AnnouncementResource, BackgroundTask, Department, User, UserAnnouncementState
from backend.services import saved_materials

PDF = b'%PDF-1.4\nfixture attachment\n%%EOF\n'
BODY = '<article><p>申请通知正文。</p><a href="https://files.example.edu.cn/award.pdf">申请附件.pdf</a><a href="https://example.edu.cn/news.htm">其他新闻</a></article>'


class SavedMaterialTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()
        source = Department(school_id=self.fixture.school_id,name='通知公告',list_url='https://example.edu.cn/notices/')
        db.session.add(source);db.session.flush()
        self.ann=Announcement(school_id=source.school_id,department_id=source.id,title='申请通知',url='https://example.edu.cn/info/1.htm',
            content_html=BODY,content_text='申请通知正文。',content_cached_at=datetime.utcnow(),content_bytes=len(BODY.encode()))
        db.session.add(self.ann);db.session.commit()
        self.uid=User.query.one().id
        self.client=self.fixture.app.test_client()
        with self.client.session_transaction() as s:s['user_id']=self.uid;s['_csrf_token']='test'

    def tearDown(self):self.fixture.tearDown()

    def star(self, value):
        response=self.client.put(f'/api/announcements/{self.ann.id}/state',json={'starred':value},headers={'X-CSRF-Token':'test'})
        self.assertEqual(response.status_code,200,response.json)
        return response.json

    def attachment_link(self):
        from bs4 import BeautifulSoup
        response = self.client.get(f'/api/announcements/{self.ann.id}/content')
        self.assertEqual(response.status_code, 200)
        return BeautifulSoup(response.json['content_html'], 'lxml').a['href']

    def test_viewing_attachment_redirects_immediately_and_queues_one_background_copy(self):
        original = self.ann.content_html
        link = self.attachment_link()
        self.assertTrue(link.startswith(f'/api/announcements/{self.ann.id}/attachments/'), link)
        self.assertEqual(AnnouncementResource.query.count(), 0)
        with patch('backend.services.saved_materials.download_resource', side_effect=AssertionError('Background only')):
            for _ in range(2):
                response = self.client.get(link)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response.location, 'https://files.example.edu.cn/award.pdf')
        row = AnnouncementResource.query.one()
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        self.assertTrue(job.payload['cache'])
        self.assertEqual(job.generation, 1)
        self.assertEqual(row.state, 'pending')
        self.assertFalse(saved_materials.is_pinned(self.ann.id))
        self.assertEqual(self.ann.content_html, original)

    def test_viewed_unstarred_attachment_is_available_offline_and_promoted_without_redownload(self):
        from backend.worker import dispatch
        link = self.attachment_link()
        self.client.get(link)
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        with patch('backend.services.saved_materials.download_resource', return_value=(PDF, 'application/pdf', '申请附件.pdf')) as fetch:
            self.assertTrue(dispatch(job.kind, job.payload)['saved'])
            self.assertEqual(fetch.call_count, 1)
        with patch('backend.services.saved_materials.download_resource', side_effect=AssertionError('Offline')):
            response = self.client.get(link)
            self.assertEqual(response.status_code, 302)
            self.assertIn('/resources/', response.location)
            self.assertEqual(self.client.get(response.location).data, PDF)
            self.star(True)
            self.assertEqual(BackgroundTask.query.filter_by(kind='saved_resource').count(), 1)
            self.assertEqual(saved_materials.material_status(self.ann)['state'], 'ready')

    def test_expiry_and_capacity_clear_viewed_copies_but_preserve_favorites(self):
        from backend.services.content_cache import prune_content
        from backend.services.storage_policy import save_policy
        link = self.attachment_link()
        self.client.get(link)
        row = AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource', return_value=(PDF, 'application/pdf', '申请附件.pdf')):
            saved_materials.save_resource(self.ann.id, row.url_key, cache=True)
        self.star(True)
        old = datetime.utcnow() - timedelta(days=400)
        self.ann.content_accessed_at = self.ann.content_cached_at = row.saved_at = old
        db.session.commit()
        save_policy({'body_cache_days': 1, 'body_cache_mb': 0})
        self.assertEqual(prune_content()['evicted'], 0)
        save_policy({'body_cache_mb': 1})
        row.byte_size = 2 * 1048576
        db.session.commit()
        self.assertEqual(prune_content(all_cache=True)['evicted'], 0)
        self.assertEqual(row.payload, PDF)
        # An ordinary viewed copy follows the same age and byte limits as its body.
        UserAnnouncementState.query.delete()
        db.session.commit()
        save_policy({'body_cache_days': 0})
        self.assertEqual(prune_content()['evicted'], 1)
        self.assertEqual(AnnouncementResource.query.count(), 0)

    def test_cleanup_during_viewed_download_cannot_recreate_attachment(self):
        from backend.worker import dispatch
        from backend.services.content_cache import prune_content
        self.client.get(self.attachment_link())
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        def download(*args):
            self.assertEqual(prune_content(all_cache=True)['evicted'], 1)
            return PDF, 'application/pdf', '申请附件.pdf'
        with patch('backend.services.saved_materials.download_resource', side_effect=download):
            self.assertTrue(dispatch(job.kind, job.payload)['skipped'])
        self.assertEqual(AnnouncementResource.query.count(), 0)

    def test_zero_days_keeps_viewed_attachment_until_time_limit_is_enabled(self):
        from backend.services.content_cache import prune_content
        from backend.services.storage_policy import save_policy
        self.client.get(self.attachment_link())
        row = AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource', return_value=(PDF, 'application/pdf', '申请附件.pdf')):
            saved_materials.save_resource(self.ann.id, row.url_key, cache=True)
        old = datetime.utcnow() - timedelta(days=400)
        self.ann.content_accessed_at = self.ann.content_cached_at = old
        db.session.commit()
        save_policy({'body_cache_days': 0, 'body_cache_mb': 0})
        self.assertEqual(prune_content()['evicted'], 0)
        self.assertEqual(row.payload, PDF)
        save_policy({'body_cache_days': 30})
        self.assertEqual(prune_content()['evicted'], 1)
        self.assertEqual(AnnouncementResource.query.count(), 0)

    def test_storage_failure_still_opens_official_attachment(self):
        from sqlalchemy.exc import OperationalError
        link = self.attachment_link()
        with patch('backend.services.saved_materials.queue_attachment', side_effect=OperationalError('insert', {}, Exception('disk full'))):
            response = self.client.get(link)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.location, 'https://files.example.edu.cn/award.pdf')
        self.assertEqual(AnnouncementResource.query.count(), 0)

    def test_attachment_link_rejects_unknown_urls_and_private_archives(self):
        key = saved_materials.resource_key('http://127.0.0.1/private.pdf')
        response = self.client.get(f'/api/announcements/{self.ann.id}/attachments/{key}',
            query_string={'url': 'http://127.0.0.1/private.pdf'})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(AnnouncementResource.query.count(), 0)
        link = self.attachment_link()
        self.ann.school.enabled = False
        db.session.commit()
        self.assertEqual(self.client.get(link).status_code, 404)
        self.assertEqual(BackgroundTask.query.filter_by(kind='saved_resource').count(), 0)

    def test_failed_viewed_attachment_keeps_original_link_and_resumes_after_cooldown(self):
        from backend.worker import dispatch
        link = self.attachment_link()
        self.client.get(link)
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        row = AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource', side_effect=ValueError('官网附件暂不可用')):
            with self.assertRaises(ValueError):
                dispatch(job.kind, job.payload)
        job.state = 'failed'
        job.next_run_at = datetime.utcnow() + timedelta(hours=1)
        generation = job.generation
        db.session.commit()
        self.assertEqual(self.client.get(link).location, row.url)
        self.assertEqual(job.generation, generation)
        self.assertEqual(row.state, 'failed')
        job.next_run_at = datetime.utcnow() - timedelta(seconds=1)
        accessed_at = self.ann.content_accessed_at
        db.session.commit()
        saved_materials.resume_saved_materials()
        db.session.refresh(job)
        self.assertEqual(job.state, 'pending')
        self.assertEqual(self.ann.content_accessed_at, accessed_at)
        with patch('backend.services.saved_materials.download_resource', return_value=(PDF, 'application/pdf', '申请附件.pdf')):
            self.assertTrue(dispatch(job.kind, job.payload)['saved'])

    def test_saved_body_and_attachments_survive_forced_cleanup_and_site_unavailability(self):
        self.star(True)
        row=AnnouncementResource.query.one()
        self.assertEqual(row.url,'https://files.example.edu.cn/award.pdf')
        with patch('backend.services.saved_materials.download_resource',return_value=(PDF,'application/pdf','申请附件.pdf')):
            saved_materials.save_resource(self.ann.id,row.url_key)
        from backend.services.content_cache import prune_content
        self.assertEqual(prune_content(all_cache=True)['evicted'],0)
        self.assertEqual(row.payload,PDF)
        with patch('backend.services.saved_materials.download_resource',side_effect=AssertionError('Offline')):
            response=self.client.get(f'/api/announcements/{self.ann.id}/resources/{row.url_key}')
        self.assertEqual(response.data,PDF)
        self.assertIn('attachment',response.headers['Content-Disposition'])
        self.assertEqual(saved_materials.material_status(self.ann)['state'],'ready')

    def test_spreadsheet_and_icon_save_through_binary_transport_and_remain_available_offline(self):
        from backend.scraper.acquisition import FetchResult
        from backend.services.article_images import image_sources

        spreadsheet_url = 'https://files.example.edu.cn/award.xls'
        icon_url = 'https://files.example.edu.cn/xls.gif'
        spreadsheet = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'fixture spreadsheet'
        icon = b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
        self.ann.content_html = (f'<article><a href="{spreadsheet_url}">'
            f'<img src="{icon_url}" alt="xls">奖学金公示名单</a></article>')
        db.session.commit()
        self.star(True)
        resources = AnnouncementResource.query.order_by(AnnouncementResource.kind).all()
        self.assertEqual({row.kind for row in resources}, {'attachment', 'image'})
        payloads = {spreadsheet_url: spreadsheet, icon_url: icon}

        def fetch(request, **kwargs):
            return FetchResult(request.url, status=200, outcome='usable',
                attachment={'payload': payloads[request.url]},
                headers={'Content-Type': 'application/octet-stream'})

        with patch('backend.scraper.acquisition.http_transport.fetch_http', side_effect=fetch) as transport:
            for row in resources:
                self.assertTrue(saved_materials.save_resource(self.ann.id, row.url_key)['saved'])
        self.assertEqual(transport.call_count, 2)
        self.assertTrue(all(call.kwargs['capture_binary'] for call in transport.call_args_list))
        status = self.client.get(f'/api/announcements/{self.ann.id}/saved-materials').json
        self.assertEqual((status['state'], status['saved'], status['total']), ('ready', 2, 2))
        self.assertEqual(next(row for row in resources if row.kind == 'image').mime_type, 'image/gif')
        with patch('backend.scraper.acquisition.http_transport.fetch_http', side_effect=AssertionError('Offline')), \
             patch('backend.routes.content.fetch_article_image', side_effect=AssertionError('Offline')):
            content = self.client.get(f'/api/announcements/{self.ann.id}/content').json['content_html']
            for row in resources:
                local_url = f'/api/announcements/{self.ann.id}/resources/{row.url_key}'
                response = self.client.get(local_url)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, payloads[row.url])
                if row.kind == 'attachment':
                    self.assertIn(local_url, content)
            image_key = next(iter(image_sources(self.ann.content_html, self.ann.url)))
            response = self.client.get(f'/api/announcements/{self.ann.id}/images/{image_key}')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, icon)
            self.assertEqual(response.mimetype, 'image/gif')

    def test_retry_recovers_resource_after_missing_module_error(self):
        from backend.scraper.acquisition import FetchResult

        self.star(True)
        row = AnnouncementResource.query.one()
        row.state = 'failed'
        row.error = "No module named 'backend.scraper.article_images'"
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        job.state, job.error = 'failed', row.error
        db.session.commit()
        self.assertEqual(saved_materials.material_status(self.ann)['state'], 'failed')
        response = self.client.post(f'/api/announcements/{self.ann.id}/saved-materials',
            headers={'X-CSRF-Token': 'test'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['state'], 'saving')
        db.session.refresh(job)
        self.assertEqual(job.state, 'pending')
        result = FetchResult(row.url, status=200, outcome='usable',
            attachment={'payload': PDF}, headers={'Content-Type': 'application/pdf'})
        with patch('backend.scraper.acquisition.http_transport.fetch_http', return_value=result):
            saved_materials.save_resource(self.ann.id, row.url_key)
        self.assertEqual(row.error, '')
        self.assertEqual(row.payload, PDF)
        self.assertEqual(saved_materials.material_status(self.ann)['state'], 'ready')

    def test_unfavorite_is_idempotent_and_preserves_copies_until_30_days_then_purges(self):
        self.star(True);row=AnnouncementResource.query.one()
        row.state='ready';row.payload=PDF;row.byte_size=len(PDF);db.session.commit()
        self.star(False)
        state=db.session.get(UserAnnouncementState,(self.uid,self.ann.id))
        original=state.trashed_at
        self.star(False);self.assertEqual(state.trashed_at,original)
        from backend.services.content_cache import prune_content
        state.trashed_at=datetime.utcnow()-timedelta(days=29);db.session.commit()
        self.assertEqual(prune_content(all_cache=True)['evicted'],0)
        self.star(True);self.assertIsNone(state.trashed_at)
        self.star(False);state.trashed_at=datetime.utcnow()-timedelta(days=31);db.session.commit()
        self.assertEqual(prune_content()['evicted'],1)
        self.assertFalse(self.ann.content_html)
        self.assertEqual(AnnouncementResource.query.count(),0)
        self.assertEqual(Announcement.query.count(),1)

    def test_known_module_failure_resumes_without_waiting_or_resetting_other_resource_cooldown(self):
        from backend.services import tasks

        self.star(True)
        row = AnnouncementResource.query.one()
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        missing = "No module named 'backend.scraper.article_images'"
        row.state, row.error = 'failed', missing
        job.state, job.error, job.attempts = 'failed', missing, 3
        job.next_run_at = datetime.utcnow() + timedelta(hours=6)
        generation = job.generation
        other_url = 'https://files.example.edu.cn/network.pdf'
        other_key = saved_materials.resource_key(other_url)
        other = AnnouncementResource(announcement_id=self.ann.id, url_key=other_key, url=other_url,
            kind='attachment', state='failed', error='官网读取超时')
        db.session.add(other)
        other_job = tasks.enqueue('saved_resource', f'{self.ann.id}:{other_key}',
            {'announcement_id': self.ann.id, 'url_key': other_key}, commit=False)
        other_job.state, other_job.error = 'failed', other.error
        other_job.next_run_at = datetime.utcnow() + timedelta(hours=6)
        other_generation, cooldown = other_job.generation, other_job.next_run_at
        db.session.commit()
        with patch('backend.scraper.acquisition.http_transport.fetch_http', side_effect=AssertionError('Enqueue only')):
            saved_materials.resume_saved_materials()
        db.session.refresh(job); db.session.refresh(row); db.session.refresh(other_job)
        self.assertEqual((job.state, job.generation, job.attempts), ('pending', generation + 1, 0))
        self.assertIsNone(job.next_run_at)
        self.assertEqual(row.state, 'pending')
        self.assertEqual((other_job.state, other_job.generation, other_job.next_run_at), ('failed', other_generation, cooldown))
        self.assertEqual(other.state, 'failed')
        saved_materials.resume_saved_materials()
        db.session.refresh(job)
        self.assertEqual(job.generation, generation + 1)
        self.assertEqual(BackgroundTask.query.filter_by(kind='saved_resource').count(), 2)

    def test_module_recovery_does_not_restart_active_ready_unpinned_or_unrelated_failures(self):
        self.star(True)
        row = AnnouncementResource.query.one()
        job = BackgroundTask.query.filter_by(kind='saved_resource').one()
        pin = db.session.get(UserAnnouncementState, (self.uid, self.ann.id))
        missing = "No module named 'backend.scraper.article_images'"
        cases = [dict(job_state=state) for state in ('pending', 'running', 'waiting', 'done')]
        cases += [dict(row_state='ready'), dict(pinned=False), dict(kind='content'),
            dict(job_error='官网读取超时'), dict(row_error='官网返回了登录页'),
            dict(job_error="No module named 'backend.scraper.unrelated'")]
        for case in cases:
            with self.subTest(case=case):
                row.state, row.error = case.get('row_state', 'failed'), case.get('row_error', missing)
                job.state, job.error = case.get('job_state', 'failed'), case.get('job_error', missing)
                job.kind, job.attempts = case.get('kind', 'saved_resource'), 3
                job.next_run_at = datetime.utcnow() + timedelta(hours=6)
                pin.starred = case.get('pinned', True)
                pin.trashed_at = None if pin.starred else datetime.utcnow() - timedelta(days=31)
                db.session.commit()
                expected = (row.state, row.error, job.state, job.error, job.generation, job.next_run_at, job.attempts)
                with patch('backend.services.tasks.enqueue', side_effect=AssertionError('No retry for this case')):
                    saved_materials.resume_saved_materials()
                db.session.refresh(job); db.session.refresh(row)
                self.assertEqual((row.state, row.error, job.state, job.error, job.generation, job.next_run_at, job.attempts), expected)

    def test_other_users_favorite_or_unexpired_trash_prevents_deletion(self):
        self.star(True);self.star(False)
        state=db.session.get(UserAnnouncementState,(self.uid,self.ann.id));state.trashed_at=datetime.utcnow()-timedelta(days=31)
        other=User(username='other-pin',password_hash='unused');db.session.add(other);db.session.flush()
        pin=UserAnnouncementState(user_id=other.id,announcement_id=self.ann.id,starred=True)
        db.session.add(pin);db.session.commit()
        from backend.services.content_cache import prune_content
        self.assertEqual(prune_content(all_cache=True)['evicted'],0)
        pin.starred=False;pin.trashed_at=datetime.utcnow()-timedelta(days=1);db.session.commit()
        self.assertEqual(prune_content(all_cache=True)['evicted'],0)

    def test_failed_resource_does_not_claim_complete_or_reset_retries_on_reads(self):
        self.star(True);row=AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource',side_effect=ValueError('官网返回登录页面')):
            with self.assertRaises(ValueError):saved_materials.save_resource(self.ann.id,row.url_key)
        self.assertEqual(row.state,'failed')
        job=BackgroundTask.query.filter_by(kind='saved_resource').one();job.state='failed';db.session.commit()
        self.assertEqual(saved_materials.material_status(self.ann)['state'],'failed')
        self.assertIsNone(row.payload)

    def test_offline_private_archive_survives_disabled_source_without_granting_others_access(self):
        self.star(True);row=AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource',return_value=(PDF,'application/pdf','通知.pdf')):
            saved_materials.save_resource(self.ann.id,row.url_key)
        self.ann.school.enabled=False;db.session.commit()
        with patch('backend.scraper.acquisition.http_transport.fetch_http',side_effect=AssertionError('No network')):
            self.assertEqual(self.client.get('/?view=saved&period=all').status_code,200)
            response=self.client.get(f'/api/announcements/{self.ann.id}/content')
            self.assertEqual(response.status_code,200)
            self.assertIn(f'/resources/{row.url_key}',response.json['content_html'])
            self.assertEqual(self.client.get(f'/api/announcements/{self.ann.id}/resources/{row.url_key}').data,PDF)
        other=User(username='no-private-access',password_hash='unused');db.session.add(other);db.session.commit()
        with self.client.session_transaction() as session:session['user_id']=other.id
        self.assertEqual(self.client.get(f'/api/announcements/{self.ann.id}/resources/{row.url_key}').status_code,404)

    def test_late_download_cannot_resurrect_an_expired_snapshot(self):
        self.star(True);key=AnnouncementResource.query.one().url_key
        def download(*args):
            state=db.session.get(UserAnnouncementState,(self.uid,self.ann.id))
            state.starred=False;state.trashed_at=datetime.utcnow()-timedelta(days=31);db.session.commit()
            from backend.services.content_cache import prune_content
            prune_content()
            return PDF,'application/pdf','通知.pdf'
        with patch('backend.services.saved_materials.download_resource',side_effect=download):
            self.assertTrue(saved_materials.save_resource(self.ann.id,key)['skipped'])
        self.assertEqual(AnnouncementResource.query.count(),0)

    def test_deleting_notice_cascades_resource_bytes(self):
        self.star(True)
        row = AnnouncementResource.query.one()
        with patch('backend.services.saved_materials.download_resource', return_value=(PDF,'application/pdf','通知.pdf')):
            saved_materials.save_resource(self.ann.id, row.url_key)
        db.session.delete(self.ann); db.session.commit()
        self.assertEqual(AnnouncementResource.query.count(), 0)
        self.assertEqual(UserAnnouncementState.query.count(), 0)

    def test_linked_inline_image_uses_same_offline_attachment_copy(self):
        import base64
        from backend.services.article_images import image_sources
        image_url = 'https://files.example.edu.cn/notice.png#preview'
        self.ann.content_html = f'<article><a href="{image_url}"><img src="{image_url}"></a></article>'
        db.session.commit(); self.star(True)
        row = AnnouncementResource.query.one()
        self.assertEqual(row.kind, 'attachment')
        png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1ioAAAAASUVORK5CYII=')
        with patch('backend.services.saved_materials.download_resource', return_value=(png,'image/png','notice.png')):
            saved_materials.save_resource(self.ann.id, row.url_key)
        image_key = next(iter(image_sources(self.ann.content_html, self.ann.url)))
        with patch('backend.routes.content.fetch_article_image', side_effect=AssertionError('Offline')):
            response = self.client.get(f'/api/announcements/{self.ann.id}/images/{image_key}')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, png)
        self.assertEqual(self.client.get(f'/api/announcements/{self.ann.id}/resources/{row.url_key}').data, png)

    def test_portable_backup_preserves_resource_bytes_without_exporting_personal_state(self):
        self.star(True);row=AnnouncementResource.query.one();key=row.url_key
        with patch('backend.services.saved_materials.download_resource',return_value=(PDF,'application/pdf','通知.pdf')):
            saved_materials.save_resource(self.ann.id,key)
        from backend.services.data_transfer import export_data,read_backup,merge_data
        stream=export_data();data=read_backup(stream,disk_backed=True)
        try:
            self.assertNotIn('user_announcement_states',data)
            self.assertEqual(len(data['announcement_resources']),1)
            AnnouncementResource.query.delete();db.session.commit()
            merge_data(data)
            self.assertEqual(db.session.get(AnnouncementResource,(self.ann.id,key)).payload,PDF)
            self.assertTrue(db.session.get(UserAnnouncementState,(self.uid,self.ann.id)).starred)
        finally:data.close();stream.close()

    def test_controlled_http_binary_capture_and_error_classification(self):
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from contextlib import ExitStack
        import threading
        from backend.scraper.acquisition import FetchRequest
        from backend.scraper.acquisition.http_transport import fetch_http
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path=='/bad.pdf':
                    self.send_response(503);self.send_header('Retry-After','120');self.end_headers();self.wfile.write(b'unavailable');return
                self.send_response(200);self.send_header('Content-Type','application/pdf')
                self.send_header('Content-Length',str(len(PDF)));self.end_headers();self.wfile.write(PDF)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        transport='backend.scraper.acquisition.http_transport.'
        try:
            with ExitStack() as stack:
                stack.enter_context(patch(transport+'public_addresses',return_value=['8.8.8.8']))
                stack.enter_context(patch(transport+'_pinned_url',side_effect=lambda url,address:'http://127.0.0.1:'+str(server.server_port)+__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url).path))
                stack.enter_context(patch(transport+'get_environ_proxies',return_value={}))
                result=fetch_http(FetchRequest('http://files.example.edu.cn/award.pdf',purpose='article'),capture_binary=True,max_bytes=1000)
                self.assertEqual(result.attachment['payload'],PDF)
                self.assertTrue(result.ok)
                too_large=fetch_http(FetchRequest('http://other.example.edu.cn/award.pdf',purpose='article'),capture_binary=True,max_bytes=4)
                self.assertFalse(too_large.ok);self.assertEqual(too_large.error_code,'response_too_large')
                failed=fetch_http(FetchRequest('http://third.example.edu.cn/bad.pdf',purpose='article'),capture_binary=True,max_bytes=1000)
                self.assertFalse(failed.ok);self.assertEqual(failed.status,503);self.assertEqual(failed.retry_after,120)
                self.assertNotIn('payload',failed.attachment)
        finally:server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
