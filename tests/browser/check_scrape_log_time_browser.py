"""Overview and history must show the same actual time, independent of host timezone."""
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import User, School, ScrapeLog
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT/'.local/log-time-audit/browser';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='log-time-browser-') as scratch:
        app = create_app({'TESTING':True,'SECRET_KEY':'isolated-log-time-browser',
            'SOURCE_CATALOG_PATH':str(Path(scratch)/'catalog.db'),
            'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(Path(scratch)/'fixture.db')})
        start = datetime.utcnow().replace(hour=3,minute=28,second=28,microsecond=0)-timedelta(days=1)
        rollover = start.replace(hour=18,minute=30,second=0)-timedelta(days=1)
        with app.app_context():
            db.create_all()
            user=User(username='time-admin',password_hash='unused',role='admin')
            school=School(name='时区测试大学',url='https://fixture.example.edu/')
            db.session.add_all([user,school]);db.session.flush()
            rows=[]
            for stamp in (start,rollover):
                row=ScrapeLog(school_id=school.id,source_name='通知公告',started_at=stamp,
                    finished_at=stamp+timedelta(seconds=2),status='success',new_count=0,total_count=3)
                db.session.add(row);rows.append(row)
            db.session.commit();ids=[row.id for row in rows]
            cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':user.id,'_csrf_token':'fixture'})
        server=make_server('127.0.0.1',0,app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as pw:
                browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
                checks=[]
                for width,zone,theme in [(1440,'Asia/Shanghai','dark'),(390,'America/New_York','light')]:
                    context=browser.new_context(viewport={'width':width,'height':900},timezone_id=zone,reduced_motion='reduce')
                    context.add_cookies([{'name':'session','value':cookie,'url':base}])
                    page=context.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(base+'/admin#overview')
                    page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                    expected=[(stamp+timedelta(hours=8)).strftime('%m-%d %H:%M') for stamp in (start,rollover)]
                    expect(page.locator('#recentLogs .log-item')).to_have_count(2)
                    for index,label in enumerate(expected):
                        expect(page.locator('#recentLogs .log-time').nth(index)).to_have_text(label)
                    expect(page.locator('#panel-overview')).to_contain_text('北京时间')
                    page.reload()
                    expect(page.locator('#recentLogs .log-time').first).to_have_text(expected[0])
                    page.locator('[data-tab="logs"]').click()
                    expect(page.locator('#recentLogSchools tr[data-log-id]')).to_have_count(2)
                    for ident,stamp in zip(ids,(start,rollover)):
                        time=page.locator(f'tr[data-log-id="{ident}"] time')
                        expect(time).to_have_attribute('datetime',stamp.isoformat()+'Z')
                        expect(time).to_contain_text((stamp+timedelta(hours=8)).strftime('%H:%M:%S'))
                    expect(page.locator('#panel-logs')).to_contain_text('北京时间')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+1')
                    page.screenshot(path=str(output/f'logs-{width}.png'))
                    assert not errors,errors
                    checks.append({'width':width,'browser_timezone':zone,'overview':expected,
                        'utc_0328_is_china_1128':True,'day_rollover':True,'history_matches':True,'page_errors':errors})
                    context.close()
                browser.close()
            with app.app_context():
                assert [row.started_at for row in ScrapeLog.query.order_by(ScrapeLog.id).all()]==[start,rollover]
            report={'checks':checks,'database_times_unchanged':True,'model_requests':0,'official_reads':0}
            (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
            print(json.dumps(report,ensure_ascii=False))
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)
            with app.app_context():db.session.remove();db.engine.dispose()


if __name__=='__main__':check()
