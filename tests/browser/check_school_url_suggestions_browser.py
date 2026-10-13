"""Real address API and form races, without creating schools or calling AI."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
from urllib.parse import parse_qs,urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_direct_onboarding import DirectOnboardingTests
from backend.database.db import db
from backend.database.models import BackgroundTask,School,User
from playwright.sync_api import expect,sync_playwright
from werkzeug.serving import make_server

PARENT='中国石油大学（北京）'
CAMPUS=PARENT+'克拉玛依校区'


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture=DirectOnboardingTests();fixture.setUp();server=None
    try:
        user=User.query.one();user.role='admin';db.session.commit()
        root='https://shared.example.edu.cn/'
        entries=[{'name':'示例大学南校区','url':root},{'name':'示例大学北校区','url':root},
                 {'name':'示例大学独立学院','url':'https://college.example.edu.cn/'}]
        path=Path(fixture.temp.name)/'catalog.json';path.write_text(json.dumps({'schools':entries}),'utf8')
        fixture.app.config['STARTER_CATALOG_PATH']=str(path)
        initial=(School.query.count(),BackgroundTask.query.count())
        cookie=fixture.app.session_interface.get_signing_serializer(fixture.app).dumps({'user_id':user.id,'_csrf_token':'fixture'})
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        output=ROOT/'.local/school-url-suggestion-check';output.mkdir(parents=True,exist_ok=True)
        checks,errors,writes=[],[],[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
            for width,theme in ((1280,'dark'),(390,'light')):
                context=browser.new_context(viewport={'width':width,'height':900},reduced_motion='reduce')
                context.add_cookies([{'name':'session','value':cookie,'url':base}])
                # Deliver late responses even after abort to test response fencing.
                context.add_init_script('''(() => {
                    const original = window.fetch;
                    window.fetch = (url, options) => {
                        if (String(url).includes('/api/schools/suggest-url') && options) {
                            options = {...options}; delete options.signal;
                        }
                        return original(url, options);
                    };
                })();''')
                state={'hold':False,'held':[]}
                def route_request(route):
                    request=route.request
                    if not request.url.startswith(base+'/'):route.abort();return
                    if request.method not in ('GET','HEAD'):
                        writes.append(request.url);route.abort();return
                    if urlsplit(request.url).path=='/api/schools/suggest-url' and state['hold']:
                        state['held'].append(route);return
                    route.continue_()
                context.route('**/*',route_request)
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/admin#schools')
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                add=page.get_by_role('button',name='添加学校',exact=True);add.click()
                name=page.locator('#schoolName');url=page.locator('#schoolUrl');hint=page.locator('#schoolUrlHint')
                name.fill(PARENT);expect(url).to_have_value('https://www.cup.edu.cn')
                name.fill(CAMPUS);expect(url).to_have_value('')
                expect(url).to_have_value('https://www.cupk.edu.cn');expect(hint).to_contain_text('克拉玛依校区')
                page.screenshot(path=str(output/f'campus-{width}.png'))
                name.fill('中国石油大学未知学院');expect(url).to_have_value('')
                expect(hint).to_contain_text('未找到确切匹配')
                for entry in entries:
                    name.fill(entry['name']);expect(url).to_have_value(entry['url']);expect(hint).to_contain_text(entry['name'])
                def hold():
                    state['hold']=True;name.fill(PARENT)
                    for _ in range(40):
                        if state['held']:break
                        page.wait_for_timeout(50)
                    assert state['held'],'Expected an in-flight address query'
                    state['hold']=False
                    return state['held'].pop(0)
                def deliver(route):
                    params=parse_qs(urlsplit(route.request.url).query)
                    client=fixture.app.test_client()
                    with client.session_transaction() as session:session['user_id']=user.id
                    response=client.get('/api/schools/suggest-url',query_string={'name':params['name'][0]})
                    with page.expect_response(lambda r:r.url==route.request.url):
                        route.fulfill(status=response.status_code,headers={'Content-Type':'application/json'},body=response.data)
                    page.wait_for_timeout(150)
                old=hold();name.fill(CAMPUS);expect(url).to_have_value('https://www.cupk.edu.cn')
                deliver(old);expect(url).to_have_value('https://www.cupk.edu.cn')
                old=hold();url.fill('https://manual.example.edu.cn/campus/')
                deliver(old);expect(url).to_have_value('https://manual.example.edu.cn/campus/')
                name.fill('其他独立学院');expect(url).to_have_value('https://manual.example.edu.cn/campus/')
                url.fill('');old=hold()
                page.get_by_role('button',name='取消',exact=True).first.click();add.click()
                deliver(old);expect(name).to_have_value('');expect(url).to_have_value('');expect(hint).to_have_text('')
                name.fill(CAMPUS);expect(url).to_have_value('https://www.cupk.edu.cn')
                name.fill('');expect(url).to_have_value('')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                checks.append({'width':width,'theme':theme,'actual_address_API':True,
                    'independent_and_shared_sites':True,'unknown_does_not_keep_parent_URL':True,
                    'late_response_fenced':True,'manual_URL_preserved':True,'reopened_dialog_fenced':True})
                context.close()
            browser.close()
        db.session.expire_all();assert initial==(School.query.count(),BackgroundTask.query.count())
        assert not errors and not writes,(errors,writes)
        report={'checks':checks,'page_errors':errors,'no_writes_or_model_calls':True,'official_requests':0}
        (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
        print(json.dumps(report))
    finally:
        if server:server.shutdown();server.server_close()
        fixture.tearDown()


if __name__=='__main__':check()
