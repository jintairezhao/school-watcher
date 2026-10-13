"""Desktop/mobile source settings using retained real public website evidence."""
import json, logging, os, sys, threading
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_direct_onboarding import DirectOnboardingTests
from test_source_presentation_replay import seed_cupk
from backend.database.db import db
from backend.database.models import School,User,Subscription,Department
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server

def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture=DirectOnboardingTests();fixture.setUp();server=None
    try:
        school=seed_cupk(fixture.app,existing_school=db.session.get(School,fixture.school_id))
        user=User.query.one();subscription=Subscription.query.one()
        subscription.department_ids=[1132,1143,1162];db.session.commit()
        before=[(d.id,d.name,d.list_url,d.list_selector) for d in Department.query.order_by(Department.id)]
        cookie=fixture.app.session_interface.get_signing_serializer(fixture.app).dumps({'user_id':user.id,'_csrf_token':'fixture'})
        server=make_server('127.0.0.1',0,fixture.app,threaded=True)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        base=f'http://127.0.0.1:{server.server_port}'
        out=ROOT/'.local/source-presentation-browser';out.mkdir(parents=True,exist_ok=True)
        checks=[];errors=[]
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
            for width,theme in ((1280,'dark'),(390,'light')):
                context=browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce')
                context.add_cookies([{'name':'session','value':cookie,'url':base}])
                context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base+'/') and route.request.method in ('GET','HEAD') else route.abort())
                page=context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
                response=page.goto(base+f'/subscriptions/{school.id}')
                assert response.status==200,response.status
                page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                expect(page.locator('.source-unit > summary').filter(has_text='化工与环境学院')).to_have_count(1)
                expect(page.locator('.source-entry').filter(has_text='教学运行 / 通知公告')).to_have_count(1)
                expect(page.locator('.source-entry[data-source-id="88"]')).to_have_count(0)
                aid=page.locator('.source-unit').filter(has=page.locator(':scope > summary').filter(has_text='学生资助管理中心'))
                expect(aid.locator('.source-entry[data-source-id="39"]')).to_have_count(1)
                parent=page.locator('.source-unit').filter(has=page.locator(':scope > summary').filter(has_text='学生工作与安全保卫部'))
                expect(parent).to_have_count(1)
                for ident in (35,36,37,38,39,40):
                    expect(parent.locator(f'.source-entry[data-source-id="{ident}"]')).to_have_count(1)
                expect(parent.locator('.source-entry[data-source-id="40"]')).to_contain_text('心理健康 / 通知公告')
                expect(page.locator('input[name="department"][value="1132"]')).to_be_checked()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),'Horizontal page overflow'
                page.screenshot(path=str(out/f'sources-{width}.png'),full_page=True)
                parent.screenshot(path=str(out/f'student-work-{width}.png'))
                response=page.goto(base+f'/schools/{school.id}/structure')
                assert response.status==200,response.status
                expect(page.get_by_text('官网来源依据',exact=True)).to_be_visible()
                assert '需要复查的来源' not in page.content()
                assert '官网页面存在模板错误' not in page.content()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.screenshot(path=str(out/f'structure-{width}.png'),full_page=True)
                checks.append({'width':width,'theme':theme,'success':True});context.close()
            browser.close()
        assert not errors,errors
        assert subscription.department_ids==[1132,1143,1162]
        assert before==[(d.id,d.name,d.list_url,d.list_selector) for d in Department.query.order_by(Department.id)]
        result={'checks':checks,'browser_errors':errors,'real_api_requests':0,'production_database_modified':False}
        (out/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
        print(json.dumps(result,ensure_ascii=False))
    finally:
        if server:server.shutdown()
        fixture.tearDown()

if __name__=='__main__':check()
