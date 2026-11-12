"""Bounded local hierarchy/recovery check; all website/model requests blocked."""
from pathlib import Path
import json, os, sys
from datetime import datetime
from unittest.mock import patch
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
OUT = ROOT / '.local/reader-branch-union-20261109/browser'
ABORT_ONLY = '--abort-only' in sys.argv
if ABORT_ONLY: OUT = OUT / 'abort-only'
OUT.mkdir(parents=True,exist_ok=True)
os.environ.update(WATCHER_ENV_FILE=str(OUT / 'no-env'), WATCHER_DATA_DIR=str(OUT / 'unused-data'), WATCHER_SEED_ON_START='0')
from test_structure_snapshot import StructureSnapshotTests
from backend.database.db import db
from backend.database.models import Announcement, Department, DepartmentDirectoryEntry, Subscription
from playwright.sync_api import sync_playwright, expect


def check():
    f = StructureSnapshotTests(); f.setUp()
    results = []; requests = []; errors = []
    try:
        projects = f.source('科研项目', f.school.url + '/projects/')
        outcomes = f.source('科研成果', f.school.url + '/outcomes/')
        excluded = f.source('未订阅的栏目', f.school.url + '/excluded/')
        projects.group_name = outcomes.group_name = excluded.group_name = '组织机构'
        unit = Department(school_id=f.school.id, kind='unit', name='科技与信息部', group_name='组织机构', list_url=f.school.url + '/science/')
        older = Department(school_id=f.school.id, kind='unit', name='科技与信息部', group_name='组织机构', list_url=f.school.url + '/science/')
        db.session.add_all([unit,older]); db.session.flush()
        db.session.add_all([DepartmentDirectoryEntry(parent_id=unit.id, department_id=row.id) for row in (projects,outcomes,excluded)])
        db.session.add(DepartmentDirectoryEntry(parent_id=older.id,department_id=projects.id))
        user = f.fixture.fixture.admin
        db.session.add(Subscription(user_id=user.id, school_id=f.school.id, department_ids=[projects.id,outcomes.id]))
        ann = Announcement(school_id=f.school.id, department_id=projects.id, title='隔离正文识别失败验收通知',
            url=projects.list_url+'one.htm', published_at=datetime.utcnow(), content_error='未找到配置要求的目标内容，需要核对来源规则')
        db.session.add(ann); db.session.commit()
        expected_ids = {str(projects.id), str(outcomes.id)}
        with patch('backend.routes.content.request_content', return_value=None), sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge',headless=True,timeout=15000)
            try:
                cases = ((390,'zh-CN','light'),) if ABORT_ONLY else ((1440,'zh-CN','dark'),(390,'zh-CN','light'),(1440,'en','light'))
                for width, locale, color in cases:
                    f.client.set_cookie('watcher_locale',locale)
                    page = browser.new_page(viewport={'width':width,'height':1000}, color_scheme=color, reduced_motion='reduce')
                    page.set_default_timeout(8000)
                    page.add_init_script(f"localStorage.setItem('theme', '{color}')")
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    recovery = {'injected':False,'persistent':False,'attempts':0}
                    def serve(route):
                        req = route.request; url = urlsplit(req.url)
                        assert url.hostname == 'localhost',req.url
                        if url.path in ('/api/inbox/sync','/api/inbox/refresh'):
                            return route.fulfill(json={'sources':[],'active':0,'total':0,'scheduled':0,'tracked':False})
                        if req.headers.get('x-inbox-fragment') == '1':
                            recovery['attempts'] += 1
                            if recovery['persistent'] or not recovery['injected']:
                                recovery['injected'] = True
                                return route.fulfill(status=503,headers={'X-Structure-Updating':'1','Retry-After':'1'},
                                    json={'error_code':'structure_updating'})
                        requests.append(dict(method=req.method,path=url.path,query=url.query))
                        response = f.client.open(url.path + ('?' + url.query if url.query else ''),method=req.method,
                            headers={key:value for key,value in req.headers.items() if key.lower() in ('x-inbox-fragment','x-csrf-token')})
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    page.route('**/*',serve)
                    page.goto(f'http://localhost/?school={f.school.id}&period=all&dept={projects.id}')
                    expect(page.locator('html')).to_have_attribute('data-theme',color)
                    page.locator('#openFilters').click()
                    panel=page.locator('#sourcePanel')
                    group=panel.locator('[data-source-group="组织机构"]')
                    branch=group.locator('[data-source-unit-name="科技与信息部"]')
                    expect(branch).to_have_count(1)
                    expect(branch).to_be_visible()
                    expect(branch.locator(f'[data-department][value="{projects.id}"]')).to_be_checked()
                    assert panel.locator(f'[data-department][value="{excluded.id}"]').count()==0
                    assert panel.locator('[data-recorded-group]').count()==0
                    assert branch.locator('[data-department]').count()==2
                    panel.locator('#sourceQuery').fill('科研成果')
                    expect(branch.locator(f'[data-department][value="{outcomes.id}"]')).to_be_visible()
                    expect(branch.locator(f'[data-department][value="{projects.id}"]')).to_be_hidden()
                    panel.locator('#sourceQuery').fill('')
                    expect(branch.locator(f'[data-department][value="{projects.id}"]')).to_be_visible()
                    toggle=branch.locator(':scope > .source-unit-row .source-unit-toggle')
                    toggle.focus();page.keyboard.press('Enter')
                    expect(toggle).to_have_attribute('aria-expanded','false')
                    page.keyboard.press('Enter')
                    expect(toggle).to_have_attribute('aria-expanded','true')
                    group.locator('[data-source-group-filter]').click()
                    page.wait_for_function('(ids) => { const got = new URL(location.href).searchParams.getAll("dept"); return got.length === ids.length && ids.every(id => got.includes(id)); }',arg=sorted(expected_ids))
                    assert not parse_qs(urlsplit(page.url).query).get('group')
                    assert set(page.locator('#sourcePanel [data-department]:checked').evaluate_all('(items)=>items.map(i=>i.value)'))==expected_ids
                    assert recovery['injected'] and recovery['attempts']>=2
                    page.screenshot(path=str(OUT/f'picker-{width}-{locale}.png'))
                    # Persistent changes keep the last applied list and filter
                    # together; after recovery the retry button can apply it.
                    applied = page.url
                    recovery['persistent'] = True
                    if ABORT_ONLY:
                        page.evaluate('''() => {const original=setTimeout; window.setTimeout=(callback,delay,...args)=>
                            original(callback,delay===15000?250:delay,...args);}''')
                    branch=page.locator('#sourcePanel [data-source-unit-name="科技与信息部"]')
                    branch.locator('[data-unit-select]').first.check()
                    branch.locator(f'.department-option:has([data-department][value="{projects.id}"])').click()
                    expect(page.locator('[data-retry-source]')).to_be_visible()
                    assert page.url==applied
                    assert set(page.locator('#sourcePanel [data-department]:checked').evaluate_all('(items)=>items.map(i=>i.value)'))==expected_ids
                    recovery['persistent'] = False
                    page.locator('[data-retry-source]').click()
                    page.wait_for_function('(id) => new URL(location.href).searchParams.getAll("dept").join()===id',arg=str(outcomes.id))
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(OUT/f'recovered-{width}-{locale}.png'))
                    results.append(dict(width=width,locale=locale,color=color,hierarchy=True,search=True,keyboard=True,
                        group_scope_exact=True,no_horizontal_overflow=True,
                        merged_partial_branches=True,transient_update_recovered=True,persistent_failure_keeps_applied_filter=True,
                        timeout_during_retry_checked=ABORT_ONLY))
                    page.close()
            finally:browser.close()
        assert not errors,errors
        report=dict(passed=True,cases=results,browser_errors=errors,requests=requests,external_requests=0,model_calls=0)
        (OUT/'browser-verification.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(dict(passed=True,cases=results,browser_errors=errors),ensure_ascii=False))
    finally:f.tearDown()


if __name__=='__main__':check()
