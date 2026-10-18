"""Responsive school rows, language triggers and native menu alignment."""
import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_collection_setup import CollectionSetupTests
from backend.auth.desktop import ensure_local_owner
from backend.services.localization import stage_preferences
from backend.database.db import db
from playwright.sync_api import sync_playwright,expect


def check():
    output=ROOT/'.impeccable/review/interface-fixes';output.mkdir(parents=True,exist_ok=True)
    report=[]
    with tempfile.TemporaryDirectory(prefix='interface-layout-') as scratch,patch.dict(os.environ,{'WATCHER_DATA_DIR':scratch,'WATCHER_ENV_FILE':str(Path(scratch)/'.env')}),patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')),sync_playwright() as pw:
        fixture=CollectionSetupTests();fixture.setUp()
        try:
            browser=pw.chromium.launch(headless=True,channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','chromium'))
            for width,locale in ((1920,'en'),(1000,'en'),(760,'en'),(390,'en'),(1440,'zh-CN')):
                stage_preferences(ensure_local_owner().id,{'locale':locale});db.session.commit()
                fixture.fixture.app.config['DESKTOP_FRAMELESS']=width>=1000
                page=browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce');errors=[]
                page.on('pageerror',lambda value:errors.append(str(value)))
                def serve(route):
                    request=route.request;p=urlsplit(request.url)
                    assert p.hostname=='localhost'
                    if p.path=='/_desktop/components':return route.fulfill(json={'phase':'ready'})
                    response=fixture.client.open(p.path+('?' +p.query if p.query else ''),method=request.method,data=request.post_data,headers={k:v for k,v in request.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                page.route('**/*',serve)
                page.goto('http://localhost/admin#schools')
                expect(page.locator('.school-manage-list')).to_be_visible()
                expect(page.locator('.language-switch-label')).to_have_text('Language' if locale=='en' else '语言')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,'overflow')
                assert page.evaluate('''() => [...document.querySelectorAll('.manage-meta>span')].every(n=>n.getBoundingClientRect().height<28)'''),(width,'split statistics')
                if width>=1000:
                    assert page.locator('.admin-theme-label').bounding_box()['height']<28
                    assert page.locator('.manage-card').first.bounding_box()['height']<155
                    page.locator('.desktop-menu summary').click()
                    menu=page.locator('.desktop-menu>div');expect(menu).to_be_visible()
                    metrics=menu.locator('button').evaluate_all('''nodes=>nodes.map(n=>{const s=getComputedStyle(n);return {align:s.textAlign,pad:s.paddingInlineStart,height:n.getBoundingClientRect().height,width:n.getBoundingClientRect().width,scroll:n.scrollWidth,client:n.clientWidth};})''')
                    assert len({(m['align'],m['pad']) for m in metrics})==1,metrics
                    assert all(m['height']>=44 and m['scroll']<=m['client'] for m in metrics),metrics
                    menu.screenshot(path=str(output/f'menu-{width}-{locale}.png'))
                    page.keyboard.press('Escape')
                page.screenshot(path=str(output/f'schools-{width}-{locale}.png'),full_page=True)
                assert not errors,errors
                report.append({'width':width,'locale':locale,'overflow':False,'atomic_statistics':True,'errors':errors})
                page.close()
            browser.close()
        finally:fixture.tearDown()
    (output/'report.json').write_text(json.dumps(report,indent=2),'utf8');print(json.dumps(report))

if __name__=='__main__':check()
