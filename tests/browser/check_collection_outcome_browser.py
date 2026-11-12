"""Private saved outcomes drive server rows, polling and reload consistently."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.local/collection-outcome-20261110'
OUT.mkdir(exist_ok=True)
os.environ.update(WATCHER_ENV_FILE=str(OUT/'no-env'),WATCHER_DATA_DIR=str(OUT/'browser-private'),
    WATCHER_SEED_ON_START='0',WATCHER_BROWSER='0',TEMP=str(ROOT/'.local/t-picker-1108'),TMP=str(ROOT/'.local/t-picker-1108'))
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from playwright.sync_api import sync_playwright,expect
from test_collection_outcomes import CollectionOutcomeTests
from backend.database.db import db
from backend.database.models import Announcement,UserRead
from backend.services.source_collection import _repair_progress
from types import SimpleNamespace

cases=[]
with patch('backend.ai.providers.complete',side_effect=AssertionError('No AI requests')),sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True,timeout=15000)
    try:
        for width,locale in [(390,'zh-CN'),(1440,'en')]:
            fixture=CollectionOutcomeTests();fixture.setUp()
            try:
                task=fixture.stored_result(_repair_progress(None))
                fixture.client.set_cookie('watcher_locale',locale)
                page=browser.new_page(viewport={'width':width,'height':920},reduced_motion='reduce')
                page.set_default_timeout(8000)
                errors=[];external=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def serve(route):
                    req=route.request;url=urlsplit(req.url)
                    if url.hostname!='localhost':
                        external.append(url.hostname);route.abort();return
                    if url.path=='/api/inbox/sync':
                        # Admission is stubbed; the saved result and every later
                        # status read come from the actual private Flask API.
                        return route.fulfill(json={**fixture.status(),'scheduled':1})
                    response=fixture.client.open(url.path+('?' +url.query if url.query else ''),
                        method=req.method,data=req.post_data_buffer,
                        headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token','x-inbox-fragment')})
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                page.route('**/*',serve)
                page.goto(f'http://localhost/?school={fixture.school.id}',timeout=15000)
                expected='0个已更新，1个仍待处理' if locale=='zh-CN' else '0 updated, 1 still need attention'
                expect(page.locator('#inboxRefreshStatus')).to_contain_text(expected)
                expect(page.locator('#inboxRefreshStatus')).not_to_contain_text('更新完成')
                expect(page.locator('#inboxRefreshSources')).to_contain_text(page.evaluate("uiT('等待重试')"))
                page.reload()
                expect(page.locator('#inboxRefreshStatus')).to_contain_text(expected)
                reads=UserRead.query.count()
                article=Announcement(school_id=fixture.school.id,department_id=fixture.a.id,
                    title='A notice retained during partial collection',url=fixture.a.list_url+'one.htm',
                    published_at=datetime.utcnow(),content_html='<p>Retained notice</p>',content_cached_at=datetime.utcnow())
                db.session.add(article)
                task.result=_repair_progress(SimpleNamespace(id=997,state='waiting'),new=1,total=2)
                db.session.commit()
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('#inboxRefreshSources')).to_contain_text(page.evaluate("uiT('待恢复读取')"))
                expect(page.locator('#inboxRefreshStatus')).to_contain_text(expected)
                assert fixture.status()['new_count']==1
                assert db.session.get(Announcement,article.id) is not None
                assert UserRead.query.count()==reads
                page.screenshot(path=str(OUT/f'pending-{width}-{locale}.png'))
                task.result={'new_count':0,'checked':1,'message':'更新完成，新增 0 条通知'}
                fixture.a.last_scraped_at=datetime.utcnow();db.session.commit()
                page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
                expect(page.locator('#inboxRefreshStatus')).to_contain_text('更新完成' if locale=='zh-CN' else 'Update complete')
                assert fixture.status()['incomplete']==0
                assert not errors and not external,(errors,external)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                cases.append({'width':width,'locale':locale,'incomplete_never_shown_as_success':True,
                    'reload_preserves_outcome':True,'partial_new_notices_retained':True,
                    'later_success_clears_attention':True,'browser_errors':errors,'external_requests':len(external),
                    'admission_stubbed':True})
                page.close()
            finally:fixture.tearDown()
    finally:browser.close()
(OUT/'browser-result.json').write_text(json.dumps({'status':'passed','cases':cases},ensure_ascii=False,indent=2)+'\n','utf-8')
print('Collection outcome browser checks passed.')
