"""English application workflows use original source data; all traffic is isolated."""
import json,os,sys,tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_collection_setup import CollectionSetupTests
from backend.database.db import db
from backend.database.models import Department,Announcement,Subscription,BackgroundTask
from backend.auth.desktop import ensure_local_owner
from backend.services.localization import stage_preferences
from playwright.sync_api import sync_playwright,expect

def check():
    out=ROOT/'.local/localization-checks/expanded';out.mkdir(parents=True,exist_ok=True)
    report=[]
    with tempfile.TemporaryDirectory(prefix='english-workflows-') as scratch,patch.dict(os.environ,{'WATCHER_DATA_DIR':scratch,'WATCHER_ENV_FILE':str(Path(scratch)/'.env'),'FIELD_ENC_KEY':'isolated-english-key'}),patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')),sync_playwright() as pw:
        browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','chromium'),headless=True)
        for width,theme in ((1440,'dark'),(390,'light')):
            fixture=CollectionSetupTests();fixture.setUp()
            try:
                owner=ensure_local_owner()
                stage_preferences(owner.id,{'locale':'en','time_zone':'America/New_York'})
                dept=Department(school_id=fixture.school.id,name='学生事务',group_name='Academic Services',list_url='https://example.edu/notices',list_selector='li',title_selector='a',link_selector='a')
                db.session.add(dept);db.session.flush()
                ann=Announcement(school_id=fixture.school.id,department_id=dept.id,title='中文通知原题 — scholarship applications',url='https://example.edu/notice/1',published_at=datetime(2026,9,30),content_html='<p>原文内容不可翻译。Official notice content.</p>',content_text='原文内容不可翻译。Official notice content.',content_cached_at=datetime.utcnow())
                db.session.add_all([ann,Subscription(user_id=owner.id,school_id=fixture.school.id)]);db.session.commit()
                page=browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce');errors=[]
                page.on('pageerror',lambda value:errors.append(str(value)))
                def serve(route):
                    req=route.request;p=urlsplit(req.url)
                    assert p.hostname=='localhost','No external requests'
                    if p.path=='/_desktop/components':return route.fulfill(json={'phase':'ready'})
                    r=fixture.client.open(p.path+('?' + p.query if p.query else ''),method=req.method,data=req.post_data,headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=r.status_code,headers=dict(r.headers),body=r.data)
                page.route('**/*',serve);page.add_init_script('localStorage.setItem("theme",'+json.dumps(theme)+')')
                routes=[('inbox',f'/?school={fixture.school.id}&period=all&selected={ann.id}'),('directory','/explore'),('sources',f'/subscriptions/{fixture.school.id}'),('structure',f'/schools/{fixture.school.id}/structure'),('monitor',f'/subscriptions/{fixture.school.id}/discovery-monitor'),('admin','/admin'),('ai','/admin#platform'),('usage','/admin/ai-usage'),('storage','/admin/storage')]
                for name,url in routes:
                    response=page.goto('http://localhost'+url)
                    if response:assert response.status==200,(url,response.status)
                    expect(page.locator('html')).to_have_attribute('lang','en')
                    expect(page.locator('.language-switch-label')).to_have_text('Language')
                    page.wait_for_timeout(150)
                    if name=='inbox':expect(page.locator('.notice-copy strong')).to_have_text(ann.title)
                    labels=page.evaluate("""() => [...document.querySelectorAll('body *')].filter(n=>n.getClientRects().length&&!n.closest('script,style,.body-loader-content,.notice-copy,.source-school-name,.reading-header h1,.reader-source,.directory-school')).flatMap(n=>[...n.childNodes].filter(c=>c.nodeType===3&&/[\u4e00-\u9fff]/.test(c.textContent)).map(c=>({tag:n.tagName,id:n.id,cls:n.className,text:c.textContent.trim()}))).filter(n=>n.text)""")
                    import re
                    unexpected=[item for item in labels if re.search('[\u4e00-\u9fff]',item['text'].replace('测试大学','').replace('学生事务','').replace('Language / 语言',''))]
                    overflow=page.evaluate('document.documentElement.scrollWidth>innerWidth')
                    report.append({'width':width,'theme':theme,'page':name,'residual':labels,'overflow':overflow,'errors':list(errors)})
                    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
                    assert not unexpected,(name,unexpected)
                    attributes=page.evaluate('''() => [...document.querySelectorAll('[title],[aria-label],[placeholder],[alt]')]
                        .filter(n=>!n.closest('[data-official-content],.body-loader-content'))
                        .flatMap(n=>['title','aria-label','placeholder','alt'].filter(a=>n.hasAttribute(a)).map(a=>({id:n.id,attribute:a,text:n.getAttribute(a)})))''')
                    allowed=[ann.title,fixture.school.name,dept.name]+page.locator('.directory-school h2').all_text_contents()
                    residual=[item for item in attributes if re.search('[\u4e00-\u9fff]',__import__('functools').reduce(lambda s,v:s.replace(v,''),allowed,item['text']))]
                    assert not residual,(name,'attributes',residual)
                    assert not overflow,(width,name)
                    if name=='inbox':
                        expect(page.locator('[data-share-notice]')).to_have_attribute('title','Copy the title and official notice link')
                        page.evaluate("() => {const body=document.querySelector('.body-loader-content');const p=document.createElement('p');p.dataset.i18n='收藏';p.id='protectedOriginal';p.textContent='收藏';body.append(p);}")
                        page.locator('#languageSelect').select_option('zh-CN');expect(page.locator('html')).to_have_attribute('lang','zh-CN')
                        expect(page.locator('.language-switch-label')).to_have_text('语言')
                        expect(page.locator('[data-share-notice]')).to_have_attribute('title','复制标题和官网原文链接')
                        page.locator('#languageSelect').select_option('en');expect(page.locator('html')).to_have_attribute('lang','en')
                        expect(page.locator('[data-share-notice]')).to_have_attribute('title','Copy the title and official notice link')
                        expect(page.locator('.notice-copy strong')).to_have_text(ann.title)
                    if name=='ai':
                        page.locator('#aiLimitsForm').evaluate('(node) => {node.closest("details").open=true;}')
                        page.locator('#aiTotalLimit').fill('13579');page.locator('#aiModel').fill('unsaved-model')
                        page.locator('#languageSelect').select_option('zh-CN');expect(page.locator('html')).to_have_attribute('lang','zh-CN')
                        expect(page.locator('#aiTotalLimit')).to_have_value('13579');expect(page.locator('#aiModel')).to_have_value('unsaved-model')
                        page.locator('#languageSelect').select_option('en');expect(page.locator('html')).to_have_attribute('lang','en')
                        expect(page.locator('#aiTotalLimit')).to_have_value('13579')
                    if name=='storage':
                        page.locator('#policy-body_cache_days').fill('77')
                        page.locator('#languageSelect').select_option('zh-CN');expect(page.locator('html')).to_have_attribute('lang','zh-CN')
                        page.locator('#languageSelect').select_option('en');expect(page.locator('html')).to_have_attribute('lang','en')
                        expect(page.locator('#policy-body_cache_days')).to_have_value('77')
                    page.screenshot(path=str(out/f'{name}-{width}-{theme}.png'),full_page=True)
                assert not errors,errors
                page.close()
            finally:fixture.tearDown()
        browser.close()
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps({'pages_checked':len(report),'page_errors':0,'horizontal_overflow':0,'paid_requests':0,'locale_drafts_preserved':True},ensure_ascii=False))

if __name__=='__main__':check()
