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
