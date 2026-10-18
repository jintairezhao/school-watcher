"""Actual locale switches preserve unsaved state and original source text."""
import json,os,sys,tempfile
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_collection_setup import CollectionSetupTests
from backend.database.models import BackgroundTask
from backend.database.db import db
from backend.ai.models import AIProfile
from backend.ai.providers import PROVIDER_NAMES, DEFAULT_REGIONS
from backend.services.localization import preferences
from backend.auth.desktop import ensure_local_owner
from playwright.sync_api import expect,sync_playwright


def check():
    output=ROOT/'.local/localization-checks'; output.mkdir(parents=True,exist_ok=True)
    results=[]
    with tempfile.TemporaryDirectory(prefix='locale-browser-') as scratch, \
         patch.dict(os.environ,{'WATCHER_DATA_DIR':scratch,'WATCHER_ENV_FILE':str(Path(scratch)/'.env'),'FIELD_ENC_KEY':'isolated-locale-key'}), \
         patch('backend.ai.providers.complete',side_effect=AssertionError('No paid request')),sync_playwright() as pw:
        browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
        for width,theme in ((1440,'dark'),(390,'light')):
            fixture=CollectionSetupTests(); fixture.setUp()
            try:
                for provider in ('dashscope','ark','bigmodel'):
                    db.session.add(AIProfile(name=PROVIDER_NAMES[provider],provider=provider,
                        model='fixture-model',region=DEFAULT_REGIONS[provider],encrypted_key='isolated-not-a-real-key',
                        enabled=True,version=1,tested_version=1))
                db.session.commit()
                page=browser.new_page(viewport={'width':width,'height':1100},reduced_motion='reduce')
                errors=[]; page.on('pageerror',lambda error:errors.append(str(error)))
                def serve(route):
                    req=route.request; parsed=urlsplit(req.url)
                    assert parsed.hostname=='localhost','No external requests'
                    if parsed.path=='/_desktop/components':return route.fulfill(json={'phase':'ready'})
                    result=fixture.client.open(parsed.path+('?' +parsed.query if parsed.query else ''),method=req.method,
                        data=req.post_data,headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=result.status_code,headers=dict(result.headers),body=result.data)
                page.route('**/*',serve)
                page.add_init_script('localStorage.setItem("theme",'+json.dumps(theme)+')')
                page.goto('http://localhost/collection/setup')
                expect(page.locator('#aiProvider option')).to_have_count(4)
                page.locator('#setupAIDetails').evaluate('(node) => {node.open=true;}')
                page.locator('#aiProvider').select_option('dashscope')
                page.locator('#aiRegion').select_option('us-east-1')
                page.locator('#aiModel').fill('unsaved-model')
                page.locator('#aiKey').fill('unsaved-fixture-key-never-sent')
                original_key_url=page.locator('#aiKeyLink').get_attribute('href')
                page.locator('[name="profiles"][value="graduate"]').check()
                page.locator('[name="profiles"][value="teacher"]').check()
                page.locator('#setupSinceDate').fill('2024-06-15'); page.locator('#setupInterval').fill('45')
                page.evaluate("() => {const p=document.createElement('p');p.id='officialSample';p.textContent='学校通知';document.body.append(p);}")
                page.locator('#languageSelect').select_option('en')
                expect(page.locator('html')).to_have_attribute('lang','en')
                expect(page.locator('#setupProfileTitle')).to_have_text('Your identities and interests')
                expect(page.locator('#setupProfileLabel')).to_contain_text('Graduate student + Faculty member')
                expect(page.locator('#setupSinceDate')).to_have_value('2024-06-15')
                expect(page.locator('#setupInterval')).to_have_value('45')
                expect(page.locator('#officialSample')).to_have_text('学校通知')
                page.evaluate("() => {const p=document.createElement('p');p.dataset.officialContent='';p.innerHTML='<span id=protectedText data-i18n=收藏>收藏</span>';document.body.append(p);const b=document.createElement('button');b.id='lateUI';b.dataset.i18n='收藏';document.body.append(b);}")
                expect(page.locator('#protectedText')).to_have_text('收藏')
                expect(page.locator('#lateUI')).to_have_text('Save')
                assert page.evaluate("I18n.t('{count} 个栏目',{count:1})")=='1 source'
                assert page.evaluate("I18n.t('{count} 个栏目',{count:2})")=='2 sources'
                expect(page.locator('#setupEducation')).to_have_value('cn')
                names={'dashscope':'Qwen','ark':'Doubao','bigmodel':'GLM'}
                for provider,name in names.items():
                    expect(page.locator('.setup-provider-guides a').filter(has_text=name)).to_have_count(1)
                    expect(page.locator('#aiProvider option[value="'+provider+'"]').first).to_have_text(name)
                    expect(page.locator('#aiProfiles')).to_contain_text(name+' · fixture-model')
                    expect(page.locator('#aiDirectory')).to_contain_text(name+' · fixture-model')
                expect(page.locator('#aiKeyLink')).to_contain_text(names['dashscope'])
                expect(page.locator('#aiModelHelp')).to_contain_text(names['dashscope'])
                expect(page.locator('#aiProvider')).to_have_value('dashscope')
                expect(page.locator('#aiRegion')).to_have_value('us-east-1')
                expect(page.locator('#aiModel')).to_have_value('unsaved-model')
                expect(page.locator('#aiKey')).to_have_value('unsaved-fixture-key-never-sent')
                expect(page.locator('#aiKeyLink')).to_have_attribute('href',original_key_url)
                expect(page.locator('#setupAIState')).to_contain_text('No discovery AI service is connected')
                page.locator('#languageSelect').select_option('zh-CN')
                expect(page.locator('#aiProvider option[value="dashscope"]')).to_have_text(PROVIDER_NAMES['dashscope'])
                expect(page.locator('.setup-provider-guides')).to_contain_text(PROVIDER_NAMES['bigmodel'])
                expect(page.locator('#aiModel')).to_have_value('unsaved-model')
                expect(page.locator('#aiKey')).to_have_value('unsaved-fixture-key-never-sent')
                page.locator('#languageSelect').select_option('en')
                expect(page.locator('#aiProvider option[value="dashscope"]')).to_have_text(names['dashscope'])
                page.locator('#setupAIDetails').evaluate('(node) => {node.open=false;}')
                page.locator('#setupEducation').select_option('global'); page.locator('#setupTimeZone').fill('America/New_York')
                page.locator('#setupStart').click()
                expect(page.locator('#setupFeedback')).to_contain_text('Setup saved')
                assert preferences(ensure_local_owner().id)['education_system']=='global'
                assert BackgroundTask.query.count()==0
                page.reload()
                expect(page.locator('html')).to_have_attribute('lang','en')
                expect(page.locator('#setupTimeZone')).to_have_value('America/New_York')
                expect(page.locator('#setupEducation')).to_have_value('global')
                value=page.evaluate("() => ({date:I18n.dateTime('2026-10-17'),wall:I18n.dateTime('2026-10-17T00:30:00'),before:I18n.dateTime('2026-03-08T06:30:00Z'),after:I18n.dateTime('2026-03-08T07:30:00Z')})")
                assert '17' in value['date'],value
                assert '17' in value['wall'] and '00:30' in value['wall'],value
                assert '01:30' in value['before'] and '03:30' in value['after'],value
                overflow=page.evaluate("() => ({width:innerWidth,scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll('body *')].filter(n=>n.getBoundingClientRect().right>innerWidth+1).slice(0,12).map(n=>({tag:n.tagName,id:n.id,cls:n.className,right:n.getBoundingClientRect().right}))})")
                if overflow['scroll']>width:
                    page.screenshot(path=str(output/f'overflow-{width}-{theme}.png'),full_page=True)
                assert overflow['scroll']<=width,overflow
                page.evaluate('scrollTo(0,0)'); page.screenshot(path=str(output/f'setup-en-{width}-{theme}.png'),full_page=True)
                page.locator('.setup-ai').screenshot(path=str(output/f'providers-en-{width}-{theme}.png'))
                page.goto('http://localhost/explore')
                expect(page.locator('html')).to_have_attribute('lang','en')
                expect(page.locator('#languageSelect')).to_have_value('en')
                if width==390:
                    page.locator('#navToggle').click()
                    expect(page.locator('#navLinks .mobile-admin-link')).to_be_visible()
                    page.locator('#navToggle').click()
                page.locator('#languageSelect').select_option('zh-CN')
                expect(page.locator('html')).to_have_attribute('lang','zh-CN')
                assert not errors,errors
                results.append({'width':width,'theme':theme,'unsaved_form_preserved':True,'preferences_persist':True,
                    'language_independent_of_education':True,'source_text_unchanged':True,'dst_and_date_only':True,
                    'provider_names_reversible':True,'provider_region_model_key_preserved':True,'provider_links_unchanged':True,
                    'no_tasks':True,'errors':errors})
                page.close()
            finally:fixture.tearDown()
        browser.close()
    (output/'report.json').write_text(json.dumps(results,indent=2),'utf8'); print(json.dumps(results))


if __name__=='__main__':check()
