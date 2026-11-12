"""Live source-choice order shares graph policy, while unsaved choices survive."""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_structure_snapshot import StructureSnapshotTests
from test_source_attention_flow import POLICY
from backend.database.db import db
from backend.database.models import Subscription
from playwright.sync_api import sync_playwright,expect


def check():
    output=ROOT/'.local/structure-facts-20261021/attention-browser'
    output.mkdir(parents=True,exist_ok=True)
    f=StructureSnapshotTests();f.setUp()
    results=[]
    try:
        platform=f.source('平台更新',f.school.url+'/platform/')
        teacher=f.source('教师发展',f.school.url+'/teacher/')
        funding=f.source('学生资助',f.school.url+'/funding/')
        db.session.add(Subscription(user_id=f.fixture.fixture.admin.id,school_id=f.school.id));db.session.commit()
        with patch('backend.services.discovery_policy.get_attention_policy',return_value=POLICY) as policy, sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                for width in (1440,390):
                    policy.return_value=POLICY
                    page=browser.new_page(viewport={'width':width,'height':1000})
                    errors=[];writes=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def serve(route):
                        req=route.request;url=urlsplit(req.url)
                        assert url.hostname=='localhost',req.url
                        if req.method!='GET':writes.append(url.path)
                        if url.path.startswith('/_desktop/'):return route.fulfill(json={'phase':'ready'})
                        response=f.client.open(url.path+('?' + url.query if url.query else ''),method=req.method,
                            data=req.post_data,headers={key:value for key,value in req.headers.items() if key.lower() in ('content-type','x-csrf-token')})
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    page.route('**/*',serve)
                    page.goto(f'http://localhost/subscriptions/{f.school.id}')
                    page.locator('#sourcePreferences').evaluate('(node)=>node.open=true')
                    page.locator('.discovery-details').evaluate('(node)=>node.open=true')
                    expect(page.locator('#discoveryRetry')).to_be_enabled()
                    def refresh():
                        expect(page.locator('#discoveryRetry')).to_be_enabled()
                        with page.expect_response('**/api/subscriptions/*/discovery') as received:
                            page.locator('#discoveryRefresh').click()
                        value=received.value.json()
                        expect(page.locator('#discoveryRetry')).to_be_enabled()
                        return value
                    order=lambda:page.locator('[name="department"]').evaluate_all('(nodes)=>nodes.map(node=>Number(node.value))')
                    expect(page.locator('[name="department"]').first).to_have_value(str(teacher.id))
                    assert order()==[teacher.id,platform.id,funding.id]
                    graph=f.get();version=graph['structure_version'];teacher_policy=graph['attention_policy_version']
                    expect(page.locator('.sources-page')).to_have_attribute('data-attention-policy-version',teacher_policy)
                    policy.return_value=dict(POLICY,weights={'personnel':5.0,'teaching':1.0})
                    same_order=refresh()
                    assert order()==[teacher.id,platform.id,funding.id]
                    assert same_order['attention_policy_version']!=teacher_policy
                    expect(page.locator('.sources-page')).to_have_attribute('data-attention-policy-version',same_order['attention_policy_version'])
                    policy.return_value=dict(POLICY,profiles=['undergraduate'],weights={'funding':6.0})
                    refresh()
                    expect(page.locator('[name="department"]').first).to_have_value(str(funding.id))
                    assert order()==[funding.id,platform.id,teacher.id]
                    graph=f.get();undergrad_policy=graph['attention_policy_version']
                    assert graph['structure_version']==version
                    assert undergrad_policy!=teacher_policy
                    expect(page.locator('.sources-page')).to_have_attribute('data-attention-policy-version',undergrad_policy)
                    page.locator('[name="mode"][value="selected"]').check()
                    page.locator(f'[name="department"][value="{platform.id}"]').uncheck()
                    policy.return_value=POLICY
                    pending=refresh()
                    assert pending['attention_policy_version']==teacher_policy
                    assert order()==[funding.id,platform.id,teacher.id]
                    expect(page.locator(f'[name="department"][value="{platform.id}"]')).not_to_be_checked()
                    expect(page.locator('.sources-page')).to_have_attribute('data-attention-policy-version',undergrad_policy)
                    assert not errors,errors
                    assert not writes,writes
                    assert page.evaluate('()=>document.documentElement.scrollWidth-document.documentElement.clientWidth')<=1
                    page.screenshot(path=str(output/f'choices-{width}.png'),full_page=True)
                    results.append(dict(width=width,new_policy_changes_order=True,graph_policy_matches_display=True,
                        same_order_policy_metadata_refreshed=True,
                        structural_version_unchanged=True,unsaved_selection_and_order_preserved=True,errors=errors,writes=writes,
                        evidence_scope='Isolated UI fixture; not website discovery or threshold evidence'))
                    page.close()
            finally:browser.close()
    finally:f.tearDown()
    (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':check()
