"""Isolated acceptance for school/account/admin navigation and responsive surfaces."""
import json
import logging
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from check_apple_ui_browser import seed
from backend import create_app
from backend.database.db import db
from backend.database.models import User
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check/supporting-redesign'
    output.mkdir(parents=True, exist_ok=True)
    checks, errors, external, layouts = [], [], [], []
    with tempfile.TemporaryDirectory(prefix='watcher-supporting-ui-') as scratch:
        folder = Path(scratch)
        app = create_app({'TESTING': True, 'SECRET_KEY': 'supporting-ui-fixture',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(folder / 'ui.db'),
            'SOURCE_CATALOG_PATH': str(folder / 'catalog.db'),
            'SOURCE_INVENTORY_PATH': str(folder / 'inventory.db'),
            'DISCOVERY_CACHE_PATH': str(folder / 'discovery.db'),
            'BACKUP_DIR': str(folder / 'backups'), 'BACKUP_COPY_DIR': '',
            'FETCH_EVIDENCE_DIR': str(folder / 'evidence'), 'BROWSER_ENABLED': False})
        cookie = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as runtime:
                browser = runtime.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width':1440,'height':960}, reduced_motion='reduce')
                context.add_cookies([{'name':'session','value':cookie,'url':base}])
                def route_external(route):
                    if route.request.url.startswith(base + '/'):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                context.route('**/*', route_external)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/me')
                expect(page.locator('#subscriptions')).to_be_visible()
                expect(page.locator('#security')).to_be_hidden()
                page.locator('[data-account-section="security"]').click()
                expect(page.locator('#security')).to_be_visible()
                expect(page.locator('#subscriptions')).to_be_hidden()
                expect(page.locator('#curPw')).to_be_visible()
                page.reload()
                expect(page.locator('#security')).to_be_visible()
                page.goto(base + '/me#security-title')
                expect(page.locator('#secQ')).to_be_visible()
                page.go_back()
                expect(page.locator('#security')).to_be_visible()
                page.locator('[data-account-section="subscriptions"]').click()
                expect(page.locator('.subscription-list')).to_be_visible()
                checks.append('account sections, refresh and original security anchor')
                for path, active in [('/admin','overview'),('/admin/storage','storage'),('/admin/sources','source-review'),('/admin/access-verification','verification')]:
                    response = page.goto(base + path)
                    assert response.status == 200, (path, response.status)
                    expect(page.locator('.admin-nav .admin-tab')).to_have_count(9)
                    expect(page.locator('.admin-nav [aria-current="page"]')).to_have_count(1)
                    expect(page.locator('.admin-main h1').first).to_be_visible()
                page.goto(base + '/admin#schools')
                expect(page.locator('#panel-schools')).to_be_visible()
                expect(page.locator('#adminPageTitle')).to_have_text('学校与部门')
                page.locator('[data-tab="platform"]').click()
                expect(page.locator('#panel-platform')).to_be_visible()
                expect(page.locator('#adminPageTitle')).to_have_text('平台设置')
                page.go_back()
                expect(page.locator('#panel-schools')).to_be_visible()
                checks.append('all admin subpages retain complete navigation and hash history')
                for path in ['/subscriptions/1','/schools/1/structure']:
                    response = page.goto(base + path)
                    assert response.status == 200, (path,response.status)
                    expect(page.locator('.school-tabs a')).to_have_count(3)
                    expect(page.locator('.school-tabs [aria-current]')).to_have_count(1)
                page.goto(base + '/subscriptions/1')
                expect(page.locator('.sources-page input[name="csrf_token"]')).to_have_count(2)
                page.locator('input[name="mode"][value="selected"]').check()
                expect(page.locator('input[name="department"]').first).to_be_enabled()
                checks.append('school section navigation and explicit subscription save stay available')
                targets = [('/explore?level=subscribed','directory'),('/me#security','account'),('/admin','admin'),
                    ('/admin#platform','platform'),('/admin/storage','storage'),('/admin/sources','source-review'),
                    ('/admin/access-verification','verification'),('/subscriptions/1','subscriptions'),('/schools/1/structure','structure')]
                for width, theme in [(1440,'light'),(1024,'light'),(390,'dark')]:
                    page.set_viewport_size({'width':width,'height':960})
                    page.evaluate('(theme)=>localStorage.setItem("theme",theme)',theme)
                    for path,name in targets:
                        response = page.goto(base + path)
                        assert response is None or response.status == 200
                        page.locator('h1').first.wait_for(state='visible')
                        page.evaluate('document.fonts.ready')
                        if name=='admin': expect(page.locator('#statGrid .stat-card').first).to_be_visible()
                        geometry=page.evaluate("""() => ({width:innerWidth,documentWidth:document.documentElement.scrollWidth,
                            headings:[...document.querySelectorAll('h1')].map(el=>({size:getComputedStyle(el).fontSize,text:el.textContent})),
                            offscreen:[...document.querySelectorAll('main h1, main h2, main button, main input, main select')].filter(el=>{
                                const r=el.getBoundingClientRect();return r.width&&r.height&&!el.closest('[hidden]')&&(r.left< -1||r.right>innerWidth+1);
                            }).map(el=>el.id||el.textContent.slice(0,40))})""")
                        assert geometry['documentWidth'] <= width+1,(path,geometry)
                        assert not geometry['offscreen'],(path,geometry)
                        layouts.append({'page':name,'theme':theme,**geometry})
                        if width!=1024 and name in ['directory','account','admin','platform','source-review']:
                            page.screenshot(path=str(output/f'{name}-{width}-{theme}.png'),full_page=True)
                checks.append('nine supporting pages fit 1440, 1024 and 390 with light/dark themes')
                def source_fixture(route):
                    if route.request.url.split('?')[0].endswith('/fixture'):
                        body = {'department_id':None,'candidate':{'name':'信息与通信工程学院 · 研究生招生'},'state':'needs_review',
                            'validation':{'errors':['pagination_unverified']},'evidence':[{'url':'https://synthetic.example.edu.cn/graduate/admissions','text':'隔离验收网页样本：学院研究生招生通知。'}]}
                    else:
                        body={'total':1,'pages':1,'items':[{'id':'fixture','school_name':'界面验收学校','candidate':{'name':'信息与通信工程学院 · 研究生招生'}}]}
                    route.fulfill(content_type='application/json',body=json.dumps(body,ensure_ascii=False))
                page.route('**/api/admin/source-proposals**',source_fixture)
                for width in [1440,390]:
                    page.set_viewport_size({'width':width,'height':960})
                    page.goto(base+'/admin/sources')
                    page.get_by_role('button',name='查看依据').click()
                    expect(page.locator('#proposalDetail')).to_be_visible()
                    expect(page.locator('#proposalErrors')).to_contain_text('分页')
                    expect(page.locator('#reviewActions button')).to_have_count(3)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                    if width==390:
                        assert page.locator('#proposalTitle').bounding_box()['y'] >= page.locator('.admin-sidebar').bounding_box()['height']
                    page.evaluate('window.scrollTo(0,0)')
                    page.screenshot(path=str(output/f'source-evidence-{width}.png'),full_page=True)
                checks.append('source evidence stays operable in split and stacked views')

                page.set_viewport_size({'width':1440,'height':960})
                page.route('**/api/admin/stats',lambda route:route.fulfill(status=503,content_type='application/json',body='{"error":"temporary"}'))
                page.goto(base+'/admin')
                expect(page.locator('#statGrid')).to_contain_text('加载失败')
                expect(page.locator('.admin-nav .admin-tab')).to_have_count(9)
                checks.append('overview read failure keeps navigation and explicit feedback')
                # The same rendered navigation does not grant access to a normal user.
                with app.app_context():
                    reader=User(username='ordinary-ui-reader',password_hash='unused',role='user')
                    db.session.add(reader);db.session.commit()
                    reader_cookie=app.session_interface.get_signing_serializer(app).dumps({'user_id':reader.id,'_csrf_token':'ui-reader'})
                reader=context.browser.new_context()
                reader.add_cookies([{'name':'session','value':reader_cookie,'url':base}])
                restricted=reader.new_page()
                for path in ['/admin','/admin/storage','/admin/sources','/admin/access-verification']:
                    assert restricted.goto(base+path).status==403,path
                restricted.goto(base+'/school/1')
                expect(restricted.locator('.school-tabs a')).to_have_count(3)
                restricted.goto(base+'/me')
                expect(restricted.locator('.account-heading').get_by_text('系统管理',exact=True)).to_have_count(0)
                reader.close()
                checks.append('normal users cannot access any administrator surface')
                browser.close()
        finally:
            server.shutdown();server.server_close();worker.join(timeout=5)
            with app.app_context(): db.session.remove();db.engine.dispose()
    report={'checks':checks,'errors':errors,'external_requests':external,'layouts':layouts}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    assert not errors and not external,report
    print(json.dumps({'checks':checks,'errors':errors,'external_requests':external},ensure_ascii=False))

if __name__=='__main__': check()
