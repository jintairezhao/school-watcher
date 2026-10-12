"""Favorite → local attachment → trash → restore, on isolated real reader routes."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
from datetime import datetime, timedelta
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement,AnnouncementResource,UserAnnouncementState
from backend.services.saved_materials import save_resource
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server

PDF=b'%PDF-1.4\nfixture saved attachment\n%%EOF\n'


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture=InboxSearchTests();fixture.use_file_database=True;fixture.setUp()
    server=None
    try:
        for ann in Announcement.query.all():
            ann.content_html='<article><p>完整的奖学金申请正文。</p><a href="https://files.example.edu.cn/award.pdf">申请附件.pdf</a></article>'
            ann.content_text='完整的奖学金申请正文。';ann.content_cached_at=datetime.utcnow()
            ann.content_bytes=len(ann.content_html.encode())
        db.session.commit()
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        cookie=fixture.app.session_interface.get_signing_serializer(fixture.app).dumps({'user_id':1,'_csrf_token':'saved-browser'})
        output=ROOT/'.local/saved-materials-check';output.mkdir(parents=True,exist_ok=True)
        checks,errors=[],[]
        with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')),sync_playwright() as p:
            browser=p.chromium.launch(headless=True,channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'))
            for width in (1440,390):
                ann_id=1 if width==1440 else 2
                context=browser.new_context(viewport={'width':width,'height':950},reduced_motion='reduce')
                context.add_cookies([{'name':'session','value':cookie,'url':base}])
                context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else r.abort())
                context.route('**/api/inbox/sync',lambda r:r.fulfill(json={'sources':[],'active':0,'total':0,'scheduled':0}))
                context.route('**/api/inbox/refresh**',lambda r:r.fulfill(json={'sources':[],'active':0,'total':0,'tracked':False}))
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+f'/?school=1&period=all&selected={ann_id}')
                if width==390:page.evaluate("document.documentElement.dataset.theme='dark'")
                page.locator('#readerStar').click()
                expect(page.locator('#readerStar')).to_have_text('取消收藏')
                expect(page.locator('[data-saved-message]')).to_contain_text('正在后台保存')
                with fixture.app.app_context():
                    resource=AnnouncementResource.query.filter_by(announcement_id=ann_id).one();key=resource.url_key
                    with patch('backend.services.saved_materials.download_resource',return_value=(PDF,'application/pdf','申请附件.pdf')):
                        save_resource(ann_id,key)
                expect(page.locator('[data-saved-message]')).to_contain_text('已保存在本地',timeout=10000)
                link=page.locator('[data-saved-files] a')
                expect(link).to_have_text('申请附件.pdf')
                with page.expect_download() as pending:link.click()
                download=pending.value
                assert download.suggested_filename=='申请附件.pdf'
                assert Path(download.path()).read_bytes()==PDF
                page.screenshot(path=str(output/f'saved-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                page.locator('#readerStar').click()
                expect(page.locator('.toast').last).to_contain_text('30 天内可恢复收藏')
                page.goto(base+'/?view=trash&period=all')
                page.locator(f'[data-notice-link][data-announcement-id="{ann_id}"]').click()
                expect(page.locator('#readerStar')).to_have_text('恢复收藏')
                expect(page.locator('[data-saved-message]')).to_contain_text('回收站保留至')
                page.screenshot(path=str(output/f'trash-{width}.png'))
                page.locator('#readerStar').click()
                expect(page.locator(f'[data-notice-link][data-announcement-id="{ann_id}"]')).to_have_count(0)
                page.goto(base+f'/?view=saved&period=all&selected={ann_id}')
                expect(page.locator('.reader-content')).to_contain_text('完整的奖学金申请正文')
                expect(page.locator('[data-saved-message]')).to_contain_text('已保存在本地')
                # No live website is available, but the stored attachment still downloads.
                with patch('backend.services.article_images.fetch_article_image',side_effect=AssertionError('Offline')):
                    response=context.request.get(base+f'/api/announcements/{ann_id}/resources/{key}')
                    assert response.ok and response.body()==PDF
                checks.append({'width':width,'local_attachment_bytes':len(PDF),'trash_and_restore':True,'offline_copy':True,'no_overflow':True})
                context.close()
            assert not errors,errors
            browser.close()
        (output/'report.json').write_text(json.dumps({'checks':checks,'errors':errors,'actual_model_requests':0,'actual_official_requests':0},indent=2),'utf8')
        print(json.dumps({'checks':checks,'errors':errors}))
    finally:
        if server:server.shutdown();server.server_close()
        fixture.tearDown()


if __name__=='__main__':check()
