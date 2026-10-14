"""A slow durable read remains visible and completes without repeated POSTs."""
import json,logging,os,sys,threading
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement
from backend.services.content_cache import fetch_content
from backend.scraper.acquisition import FetchResult
from playwright.sync_api import expect,sync_playwright
from werkzeug.serving import make_server

def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture=InboxSearchTests();fixture.use_file_database=True;fixture.setUp()
    server=None
    try:
        ann=db.session.get(Announcement,1)
        ann.content_html=None;ann.content_text=None;ann.content_cached_at=None;ann.published_at=None
        db.session.commit()
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        cookie=fixture.app.session_interface.get_signing_serializer(fixture.app).dumps({'user_id':1,'_csrf_token':'queue-fixture'})
        calls=[];errors=[];failed_get=[False]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'))
            context=browser.new_context()
            context.add_cookies([{'name':'session','value':cookie,'url':base}])
            context.route('**/*',lambda r:r.continue_() if r.request.url.startswith(base+'/') else r.abort())
            def observe(route):
                method=route.request.method;calls.append(method)
                if method=='GET' and not failed_get[0]:
                    failed_get[0]=True;route.abort()
                else:route.continue_()
            context.route('**/api/announcements/1/content',observe)
            context.route('**/api/inbox/sync',lambda r:r.fulfill(json={'sources':[],'active':0,'total':0,'scheduled':0}))
            context.route('**/api/inbox/refresh**',lambda r:r.fulfill(json={'sources':[],'active':0,'total':0,'tracked':False}))
            page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            page.clock.install()
            page.goto(base+'/?school=1&dept=11&period=all&selected=1')
            panel=page.locator('[data-content-id="1"]')
            expect(panel).to_have_attribute('data-load-state','loading')
            page.clock.fast_forward(65000);page.wait_for_timeout(100)
            page.clock.run_for(8000)
            expect(panel).to_have_attribute('data-load-state','loading')
            expect(panel.locator('.body-loader-status')).to_contain_text('队列')
            assert calls.count('POST')==1,calls
            html=f'<article><h1>{ann.title}</h1><p>发布日期：2026-10-13</p><p>这是成功读取的真实任务正文，等待完成后应自动显示给读者。</p></article>'
            with patch('backend.scraper.acquisition.coordinator.http_fetch',return_value=FetchResult(ann.url,status=200,html=html)):
                fetch_content(ann.id)
            page.clock.run_for(4000)
            expect(panel).to_have_attribute('data-cached','true')
            expect(panel).to_contain_text('等待完成后应自动显示')
            expect(page.locator('.reading-meta time')).to_have_text('2026年10月13日')
            assert calls.count('POST')==1 and not errors,(calls,errors)
            out=ROOT/'.local/content-queue-check';out.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(out/'recovered.png'))
            report={'wait_beyond_60_seconds_does_not_fail':True,'local_connection_recovers_automatically':True,
                'durable_body_and_date_arrive_without_retry_click':True,'post_count':calls.count('POST'),
                'page_errors':errors,'actual_model_requests':0,'official_website_requests':0}
            (out/'verified.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(report,ensure_ascii=False))
            browser.close()
    finally:
        if server:server.shutdown();server.server_close()
        fixture.tearDown()

if __name__=='__main__':check()
