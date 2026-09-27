"""Exercise complete recent history and lazy archives in an isolated administrator UI."""
import json
import logging
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import User, School, ScrapeLog, AppConfig
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output=ROOT/'data/ui-check';output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='watcher-log-ui-') as scratch:
        app=create_app({'TESTING':True,'SECRET_KEY':'history-fixture',
            'SOURCE_CATALOG_PATH':str(Path(scratch)/'catalog.db'),
            'SQLALCHEMY_DATABASE_URI':'sqlite:///'+str(Path(scratch)/'ui.db')})
        with app.app_context():
            db.create_all()
            user=User(username='抓取记录测试管理员',password_hash='unused',role='admin')
            db.session.add_all([user,School(id=1,name='甲大学（界面测试）',url='https://example.edu.cn/'),
                School(id=2,name='乙大学（界面测试）',url='https://other.edu.cn/')])
            db.session.flush()
            now=datetime.utcnow()
            for i in range(23):
                start=now-timedelta(hours=i+1)
                db.session.add(ScrapeLog(school_id=1 if i%2==0 else 2,
                    source_name='医学院' if i%2==0 else '信息与通信工程学院',
                    started_at=start,finished_at=start+timedelta(seconds=12),
                    status='failed' if i==0 else 'success',new_count=i,total_count=30,
                    error_message='官网当前返回访问校验页面，暂时无法读取通知；已有消息仍保留。' if i==0 else None))
            for i in range(15):
                db.session.add(ScrapeLog(school_id=1 if i<12 else 2,started_at=now-timedelta(days=8+i),
                    status='failed',error_message='更早的完整原因：'+'细节说明。'*40+'<script>window.logXss=true</script>'))
            db.session.commit()
            cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':user.id,'_csrf_token':'fixture'})
        server=make_server('127.0.0.1',0,app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with patch('backend.services.scrape_logs.PAGE_SIZE',5),sync_playwright() as p:
                browser=p.chromium.launch(channel='msedge',headless=True)
                context=browser.new_context(viewport={'width':1440,'height':940},color_scheme='dark',reduced_motion='reduce')
                context.add_cookies([{'name':'session','value':cookie,'url':base}])
                page=context.new_page();errors=[];archives=[];retired_requests=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda r:archives.append(r.url) if '/api/admin/scrape-logs?' in r.url and 'period=archive' in r.url else None)
                page.on('request',lambda r:retired_requests.append(r.url) if '/api/selector-audit' in r.url else None)
                # Bookmarks from the retired technical panel still reach useful history.
                page.goto(base+'/admin#selectors')
                expect(page).to_have_url(base+'/admin#logs')
                expect(page.locator('[data-tab="selectors"]')).to_have_count(0)
                expect(page.locator('#panel-logs')).to_be_visible()
                expect(page.locator('#recentLogStatus')).to_have_text('已完整显示 23 条记录。')
                expect(page.locator('#recentLogSchools tr[data-log-id]')).to_have_count(23)
                expect(page.locator('#recentLogSchools .scrape-log-school')).to_have_count(2)
                assert not archives,'Archive was fetched before opening'
                expect(page.locator('#archiveLogSchools tr')).to_have_count(0)
                page.screenshot(path=str(output/'scrape-logs-desktop.png'))

                page.locator('#archiveLogSection summary').click()
                expect(page.locator('#archiveLogStatus')).to_have_text('已显示 5 / 15 条记录。')
                expect(page.locator('#archiveLogSchools .scrape-log-message').first).to_contain_text('<script>window.logXss=true</script>')
                assert page.evaluate('window.logXss === undefined')
                assert len(archives)==1
                for size in [10,15]:
                    page.locator('#loadMoreArchiveLogs').click()
                    expect(page.locator('#archiveLogSchools tr[data-log-id]')).to_have_count(size)
                expect(page.locator('#loadMoreArchiveLogs')).to_be_hidden()
                assert len({parse_qs(urlsplit(u).query)['as_of'][0] for u in archives})==1
                page.locator('#archiveLogSection summary').click()
                expect(page.locator('#archiveLogSchools')).to_be_hidden()

                page.locator('#logSchoolFilter').select_option('1')
                expect(page.locator('#recentLogStatus')).to_have_text('已完整显示 12 条记录。')
                expect(page.locator('#recentLogSchools .scrape-log-school')).to_have_count(1)
                expect(page.locator('#recentLogSchools h4')).to_contain_text('甲大学')
                expect(page.locator('#archiveLogCount')).to_have_text('· 12 条')
                assert not page.locator('#archiveLogSection').evaluate('(node) => node.open')
                assert len(archives)==3
                page.locator('#logRetentionDays').select_option('90')
                page.locator('#saveLogRetention').click()
                expect(page.locator('#logRetentionStatus')).to_contain_text('每日清理')
                with app.app_context():
                    assert AppConfig.get('scrape_log_retention_days')=='90'
                    assert ScrapeLog.query.count()==38
                page.locator('#logRetentionDays').select_option('0')
                page.locator('#saveLogRetention').click()
                expect(page.locator('#logRetentionStatus')).to_contain_text('永久保留')
                page.reload()
                expect(page.locator('#logRetentionDays')).to_have_value('0')
                expect(page.locator('#recentLogStatus')).to_have_text('已完整显示 23 条记录。')

                # Failed later pages keep the first batch and retry from its cursor.
                fail=[True]
                def fail_once(route):
                    if 'before_id=' in route.request.url and fail[0]:
                        fail[0]=False;route.fulfill(status=503,json={'error':'本次读取暂时失败'})
                    else:route.continue_()
                page.route('**/api/admin/scrape-logs?**',fail_once)
                page.locator('#reloadScrapeLogs').click()
                expect(page.locator('#recentLogStatus')).to_contain_text('已保留 5 条已加载记录')
                page.locator('#retryRecentLogs').click()
                expect(page.locator('#recentLogStatus')).to_have_text('已完整显示 23 条记录。')
                expect(page.locator('#recentLogSchools tr[data-log-id]')).to_have_count(23)
                page.set_viewport_size({'width':390,'height':844})
                page.evaluate('window.scrollTo(0, 0)')
                page.screenshot(path=str(output/'scrape-logs-mobile.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                # Admin overview and storage navigation work without the retired panel.
                page.locator('[data-tab="overview"]').click()
                expect(page.locator('#statGrid .stat-card')).to_have_count(5)
                expect(page.locator('#statGrid')).not_to_contain_text('选择器')
                page.get_by_role('link',name='存储管理',exact=True).click()
                expect(page).to_have_url(base+'/admin/storage')
                expect(page.get_by_role('heading',name='存储管理',exact=True)).to_be_visible()
                assert not retired_requests,retired_requests
                assert not errors,errors
                browser.close()
                print(json.dumps({'recent_rows':23,'archive_rows':15,'browser_errors':errors,
                    'checks':['complete automatic recent pages','school grouping and filter','archive lazy loading',
                    'archive pagination','full escaped errors','retention save and persistence',
                    'failed page retry without duplicates','mobile width','retired selector bookmark redirect',
                    'overview without technical statistics','storage navigation','no retired API requests']},ensure_ascii=False))
        finally:
            server.shutdown();server.server_close()
            with app.app_context():db.session.remove();db.engine.dispose()


if __name__=='__main__':check()
