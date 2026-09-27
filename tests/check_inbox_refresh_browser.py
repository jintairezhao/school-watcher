"""Real browser + durable worker against isolated, explicitly synthetic official pages."""
import json
import logging
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, User, Subscription, BackgroundTask
from backend.services import tasks
from backend.worker import execute
from backend.services.source_collection import SourceAccessError
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    logging.getLogger('backend.worker').setLevel(logging.CRITICAL)
    output = ROOT / 'data/ui-check'
    with tempfile.TemporaryDirectory(prefix='watcher-refresh-ui-') as scratch:
        app = create_app({'TESTING':True,'SECRET_KEY':'refresh-fixture',
            'SOURCE_CATALOG_PATH':str(Path(scratch)/'catalog.db'),
            'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(Path(scratch)/'ui.db')})
        with app.app_context():
            db.create_all()
            user=User(username='刷新交互测试',password_hash='unused',role='admin')
            db.session.add_all([user,School(id=1,name='测试学校',url='https://example.edu.cn/',subscriber_count=1),
                School(id=2,name='另一所测试学校',url='https://other.edu.cn/',subscriber_count=1)])
            db.session.flush()
            recent=datetime.utcnow()
            db.session.add_all([Department(id=1,school_id=1,name='医学院',group_name='学院部门',list_url='https://med.example.edu.cn/',last_scraped_at=recent),
                Department(id=2,school_id=1,name='物理学院',group_name='学院部门',list_url='https://physics.example.edu.cn/',last_scraped_at=recent),
                Department(id=3,school_id=2,name='教务处',list_url='https://other.edu.cn/',last_scraped_at=recent)])
            db.session.add_all([Subscription(user_id=user.id,school_id=1),Subscription(user_id=user.id,school_id=2)])
            db.session.commit()
            cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':user.id,'_csrf_token':'fixture'})
        calls=[];blocked=[False];stop=threading.Event()
        def fetch(url):
            calls.append(url)
            if blocked[0] and 'med.' in url:
                raise SourceAccessError('官网当前返回访问校验页面，暂时无法读取通知；已有消息仍保留')
            time.sleep(.2)
            return '<h2>通知公告</h2><ul class="news-list">'+''.join(
                f'<li><a href="/info/1001/{i}.htm">第{i}期选课安排（交互测试数据）</a><span>{datetime.utcnow():%Y-%m-%d}</span></li>'
                for i in range(101,106))+'</ul>'
        def work():
            while not stop.wait(.15):
                with app.app_context():
                    handle=tasks.claim();db.session.remove()
                if handle: execute(app,handle)
        server=make_server('127.0.0.1',0,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with patch('backend.services.source_collection.fetch_source_page',side_effect=fetch):
                worker=threading.Thread(target=work,daemon=True);worker.start()
                with sync_playwright() as p:
                    browser=p.chromium.launch(channel='msedge',headless=True)
                    context=browser.new_context(viewport={'width':1440,'height':940},color_scheme='dark',reduced_motion='reduce')
                    context.add_cookies([{'name':'session','value':cookie,'url':base}])
                    page=context.new_page();errors=[];posts=[];syncs=[];documents=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('request',lambda r:posts.append(r.post_data_json) if r.url.endswith('/api/inbox/refresh') and r.method=='POST' else None)
                    page.on('request',lambda r:syncs.append(r.post_data_json) if r.url.endswith('/api/inbox/sync') and r.method=='POST' else None)
                    page.on('request',lambda r:documents.append(r.url) if r.resource_type=='document' else None)
                    with page.expect_response('**/api/inbox/sync'):
                        page.goto(base+'/?school=1&dept=1')
                    medical_row = page.locator('.department-option:has(input[value="1"])')
                    expect(page.locator('#inboxRefreshActivity')).to_be_hidden()
                    assert not calls and not posts and syncs == [{'scope':'all'}]
                    page.locator('#refreshCurrentSources').click()
                    expect(page.locator('#refreshCurrentSources')).to_be_enabled()
                    assert not calls and not posts and len(syncs)==1, 'Display refresh enqueued collection'
                    page.locator('#toggleNoticeActions').click()
                    page.locator('#collectCurrentSources').click()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('更新完成',timeout=15000)
                    expect(page.locator('[data-notice-link]')).to_have_count(5)
                    assert calls==['https://med.example.edu.cn/'],calls
                    expect(page.locator('.department-option:has(input[value="1"]) .department-count')).to_have_text('5')
                    expect(medical_row.locator('[data-source-status]')).to_be_hidden()
                    assert posts[0]['department_ids']==[1] and posts[0]['school_id']==1
                    page.locator('#toggleNoticeActions').click()
                    page.locator('#collectCurrentSources').click()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('更新完成')
                    assert len(calls)==1, 'Repeated refresh bypassed shared cooldown'
                    page.screenshot(path=str(output/'inbox-refresh-desktop.png'),full_page=True)
                    before=len(posts)
                    with page.expect_response('**/api/inbox/sync'):
                        page.reload()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('更新完成')
                    assert len(posts)==before and len(syncs)==2 and len(calls)==1, 'F5 bypassed the configured collection interval'
                    if not page.locator('#noticeActionsMenu').is_visible():
                        page.locator('#toggleNoticeActions').click()
                    expect(page.locator('#collectAllSources')).to_be_visible()
                    page.locator('#collectAllSources').click()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('更新完成',timeout=15000)
                    assert posts[-1]=={'scope':'all'}
                    assert set(calls)=={'https://med.example.edu.cn/','https://physics.example.edu.cn/','https://other.edu.cn/'}
                    assert 'dept=1' in page.url
                    # A source failure reports its cause and keeps already saved notices.
                    blocked[0]=True
                    with app.app_context():
                        task=BackgroundTask.query.filter_by(identity='collect:1').one()
                        task.updated_at=datetime.utcnow()-timedelta(minutes=2);db.session.commit()
                    page.locator('#toggleNoticeActions').click()
                    page.locator('#collectCurrentSources').click()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('暂未成功',timeout=15000)
                    expect(page.locator('#inboxRefreshSources')).to_contain_text('访问校验页面')
                    expect(page.locator('[data-notice-link]')).to_have_count(5)
                    expect(medical_row.locator('[data-source-status]')).to_have_text('访问受限')
                    expect(medical_row.locator('.department-count')).to_have_text('5')
                    page.screenshot(path=str(output/'inbox-source-status-desktop.png'),full_page=True)
                    page.set_viewport_size({'width':390,'height':844})
                    page.screenshot(path=str(output/'inbox-refresh-mobile.png'),full_page=True)
                    page.locator('#openFilters').click()
                    expect(medical_row.locator('[data-source-status]')).to_be_visible()
                    page.screenshot(path=str(output/'inbox-source-status-mobile.png'),full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert len(documents)==2 and not errors,(documents,errors)
                    print(json.dumps({'documents':len(documents),'browser_errors':errors,'collection_requests':len(posts),'interval_checks':len(syncs),
                        'checks':['current source only','global subscription scope','shared cooldown','browser reload',
                                  'display refresh does not collect','fresh interval does not collect',
                                  'live list and counts','failure reason','saved notices retained','mobile width']},ensure_ascii=False))
                    browser.close()
                stop.set();worker.join(timeout=10)
        finally:
            stop.set();server.shutdown();server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()


if __name__=='__main__':check()
