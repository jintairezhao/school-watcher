"""Local inbox facts, policy refresh and failure recovery; no website/model IO."""
from copy import deepcopy
from datetime import datetime
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
from backend.database.models import Announcement,BackgroundTask,Department,DepartmentDirectoryEntry,Subscription
from playwright.sync_api import sync_playwright,expect


def check():
    output=ROOT/'.local/structure-facts-20261021/inbox-facts-browser'
    output.mkdir(parents=True,exist_ok=True)
    f=StructureSnapshotTests();f.setUp()
    results=[]
    try:
        platform=f.source('平台更新',f.school.url+'/platform/')
        teacher=f.source('教师发展',f.school.url+'/teacher/')
        funding=f.source('学生资助',f.school.url+'/funding/')
        platform.group_name=teacher.group_name=funding.group_name='保留的旧分类'
        for source in (platform,teacher,funding):
            db.session.add(Announcement(school_id=f.school.id,department_id=source.id,
                title=source.name+'：隔离验收通知',url=source.list_url+'notice',published_at=datetime.utcnow(),
                content_html='<p>隔离验收正文</p>',content_text='隔离验收正文',content_cached_at=datetime.utcnow()))
        db.session.add(Subscription(user_id=f.fixture.fixture.admin.id,school_id=f.school.id));db.session.commit()
        initial_tasks=BackgroundTask.query.count()
        with patch('backend.services.discovery_policy.get_attention_policy',return_value=deepcopy(POLICY)) as policy,sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True)
            try:
                for width in (1440,390):
                    for locale in ('zh-CN','en'):
                        policy.return_value=deepcopy(POLICY)
                        f.client.set_cookie('watcher_locale',locale)
                        page=browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
                        errors=[];writes=[];stubs=[];fail={'fragment':False}
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        def serve(route):
                            req=route.request;url=urlsplit(req.url)
                            assert url.hostname=='localhost',req.url
                            if url.path=='/api/inbox/sync':
                                stubs.append(url.path)
                                return route.fulfill(json={'sources':[],'active':0,'total':0,'scheduled':0,'tracked':False})
                            if req.method!='GET':
                                writes.append(url.path)
                                return route.fulfill(status=405,json={'error':'Read-only browser acceptance'})
                            if url.path.startswith('/_desktop/'):
                                return route.fulfill(json={'phase':'ready'})
                            if fail['fragment'] and url.path=='/' and req.headers.get('x-inbox-fragment')=='1':
                                fail['fragment']=False
                                return route.fulfill(status=503,body='Isolated response failure')
                            response=f.client.open(url.path+('?' + url.query if url.query else ''),method='GET',
                                headers={key:value for key,value in req.headers.items() if key.lower() in ('x-inbox-fragment','x-csrf-token')})
                            route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                        page.route('**/*',serve)
                        page.goto(f'http://localhost/?school={f.school.id}&period=all')
                        expect(page.locator('#openFilters')).to_be_visible()
                        page.locator('#openFilters').click()
                        if width==1440:page.locator('#pinSources').click()
                        expected_mode='pinned' if width==1440 else 'overlay'
                        group=page.locator('[data-source-group="栏目（归属待核实）"]')
                        if group.locator('.source-group-toggle').get_attribute('aria-expanded')!='true':
                            group.locator('.source-group-toggle').click()
                        expect(page.locator('#sourcePanel [data-source-unit]')).to_have_count(0)
                        expect(page.locator('#inboxLegacyScope')).to_be_hidden()
                        sources={platform.id,teacher.id,funding.id}
                        def order():
                            return [ident for ident in page.locator('#sourcePanel [data-department]').evaluate_all(
                                '(nodes)=>nodes.map(node=>Number(node.value))') if ident in sources]
                        def refresh():
                            page.evaluate('()=>window.refreshInboxView()')
                            expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy','true')
                        def row(ident):return page.locator(f'#sourcePanel .department-option:has([value="{ident}"])')
                        assert order()==[teacher.id,platform.id,funding.id]
                        graph=f.get();structure=graph['structure_version']
                        expect(page.locator('#sourcePanel')).to_have_attribute('data-attention-policy-version',graph['attention_policy_version'])
                        row(platform.id).click()
                        expect(page.locator(f'#sourcePanel [value="{platform.id}"]')).to_be_checked()
                        expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy','true')
                        page.wait_for_function('(id)=>new URL(location.href).searchParams.getAll("dept").includes(String(id))',arg=platform.id)
                        page.locator('#sourcePanel').evaluate('(node)=>node.__captureMarker="same-order"')
                        policy.return_value=dict(deepcopy(POLICY),weights={'personnel':5.0,'teaching':1.0})
                        refresh()
                        assert page.locator('#sourcePanel').evaluate('(node)=>node.__captureMarker')=='same-order'
                        assert order()==[teacher.id,platform.id,funding.id]
                        changed=f.get()
                        assert changed['attention_policy_version']!=graph['attention_policy_version']
                        assert changed['structure_version']==structure
                        expect(page.locator('#sourcePanel')).to_have_attribute('data-attention-policy-version',changed['attention_policy_version'])
                        page.locator('#sourceQuery').fill('平台 保留的旧分类')
                        expect(page.locator('#sourcePanel .department-option:visible')).to_have_count(1)
                        policy.return_value=dict(deepcopy(POLICY),profiles=['undergraduate'],weights={'funding':6.0})
                        refresh()
                        assert order()==[funding.id,platform.id,teacher.id]
                        expect(page.locator('#sourceQuery')).to_have_value('平台 保留的旧分类')
                        expect(page.locator(f'#sourcePanel [value="{platform.id}"]')).to_be_checked()
                        expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode',expected_mode)
                        graph=f.get()
                        assert graph['structure_version']==structure
                        expect(page.locator('#sourcePanel')).to_have_attribute('data-attention-policy-version',graph['attention_policy_version'])
                        page.locator('#sourceQuery').fill('')
                        before_url=page.url
                        fail['fragment']=True
                        row(funding.id).click()
                        expect(page.locator('#sourcePanelFeedback')).to_be_visible()
                        expect(page.locator(f'#sourcePanel [value="{funding.id}"]')).not_to_be_checked()
                        expect(page.locator(f'#sourcePanel [value="{platform.id}"]')).to_be_checked()
                        assert page.url==before_url
                        assert order()==[funding.id,platform.id,teacher.id]
                        expect(page.locator('#sourcePanel')).to_have_attribute('data-attention-policy-version',graph['attention_policy_version'])
                        page.locator('#sourcePanel [data-retry-source]').click()
                        expect(page.locator('#sourcePanelFeedback')).to_be_hidden()
                        expect(page.locator(f'#sourcePanel [value="{funding.id}"]')).to_be_checked()
                        count=row(platform.id).locator('.department-count')
                        expect(count).to_have_attribute('aria-label','1 saved notices' if locale=='en' else '1 条已保存通知')
                        expect(row(platform.id).locator('[data-recorded-group]')).to_have_text(
                            ('Saved display group: ' if locale=='en' else '记录分组：')+'保留的旧分类')
                        expect(page.locator('[data-historical-source-note]').first).to_have_text(
                            'Historical source; existing notices retained' if locale=='en' else '历史来源，保留已有通知')
                        # Switching the locale after a polling render still uses marked UI strings.
                        opposite='zh-CN' if locale=='en' else 'en'
                        f.client.set_cookie('watcher_locale',opposite)
                        page.evaluate('(value)=>window.I18n.setLocale(value)',opposite)
                        expect(row(platform.id).locator('[data-recorded-group]')).to_have_text(
                            ('Saved display group: ' if opposite=='en' else '记录分组：')+'保留的旧分类')
                        refresh()
                        f.client.set_cookie('watcher_locale',locale)
                        page.evaluate('(value)=>window.I18n.setLocale(value)',locale)
                        refresh()
                        expect(row(platform.id).locator('.department-name')).to_contain_text('平台更新')
                        expect(row(platform.id).locator('[data-source-status]')).to_have_text(
                            'Awaiting collection' if locale=='en' else '待采集')
                        assert page.locator('#clearDepartments').evaluate(
                            '(node)=>getComputedStyle(node).whiteSpace')=='nowrap'
                        assert page.evaluate('()=>document.documentElement.scrollWidth-document.documentElement.clientWidth')<=1
                        page.screenshot(path=str(output/f'inbox-{width}-{locale}.png'))
                        page.goto(f'http://localhost/?school={f.school.id}&period=all&group=保留的旧分类')
                        page.locator('#openFilters').click()
                        expect(page.locator('#inboxLegacyScope')).to_be_visible()
                        group=page.locator('[data-source-group="栏目（归属待核实）"]')
                        if group.locator('.source-group-toggle').get_attribute('aria-expanded')!='true':
                            group.locator('.source-group-toggle').click()
                        group.locator('[data-source-group-filter]').click()
                        expect(page.locator('#inboxLegacyScope')).to_be_hidden()
                        assert not writes,writes
                        assert not errors,errors
                        assert BackgroundTask.query.count()==initial_tasks
                        results.append(dict(width=width,locale=locale,shared_facts_and_policy=True,
                            same_order_metadata_refreshed=True,policy_reorders_without_structure_change=True,
                            selection_search_and_panel_mode_preserved=True,failed_filter_keeps_last_snapshot=True,
                            retry_and_legacy_filter_work=True,marked_ui_translates_official_names_unchanged=True,
                            errors=errors,writes=writes,stubbed_sync_requests=len(stubs),
                            scope='Synthetic local UI fixture; no website, publisher, threshold or crawl-cost evidence'))
                        page.close()
                # Explicitly evidenced fixture publishers may share a source.
                # These assertions exercise controls, not real website ownership.
                url=f.school.url+'/shared-owner-fixture/'
                document=f.document(url,label='Synthetic publisher identity fixture')
                f.fixture.publish([document])
                owners=[]
                for index in (1,2):
                    owner=Department(school_id=f.school.id,name=f'Fixture publisher {index}',kind='unit',
                        list_url=f.school.url+f'/publisher-{index}/')
                    db.session.add(owner);db.session.flush();owners.append(owner.id)
                    identity=f'main #owner-{index}';entrance=f'main #column-{index}'
                    assertion=dict(kind='explicit_publisher_identity',predicate='publication_owner',
                        direction='publisher_to_source',identity_text=owner.name,identity_reference_url=url,
                        identity_locator=identity,entry_reference_url=url,entry_locator=entrance,
                        entry_kind='column_link',entry_url=teacher.list_url)
                    proofs=[dict(reference_url=url,locator=locator,content_hash=document['content_hash'],
                        observed_at=document['checked_at']) for locator in (identity,entrance)]
                    db.session.add(DepartmentDirectoryEntry(parent_id=owner.id,department_id=teacher.id,
                        facts_json=[dict(type='source_ownership',assertion=assertion,evidence=proofs)]))
                db.session.commit()
                page=browser.new_page(viewport={'width':1440,'height':1000},reduced_motion='reduce')
                errors=[];writes=[];stubs=[];fail={'fragment':False}
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.route('**/*',serve)
                page.goto(f'http://localhost/?school={f.school.id}&period=all')
                page.locator('#openFilters').click()
                group=page.locator('[data-source-group="有依据的栏目归属"]')
                group.locator('.source-group-toggle').click()
                expect(group.locator('[data-source-unit]')).to_have_count(2)
                first=group.locator('[data-source-unit]').first
                first.locator('[data-unit-select]').check()
                expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy','true')
                expect(group.locator('[data-department]:checked')).to_have_count(2)
                expect(group.locator('[data-unit-select]:checked')).to_have_count(2)
                assert page.evaluate('()=>new URL(location.href).searchParams.getAll("dept")')==[str(teacher.id)]
                expect(page.locator('.source-scope-selection')).to_have_text('1 source selected')
                group.locator('[data-unit-select]').last.uncheck()
                expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy','true')
                expect(group.locator('[data-department]:checked')).to_have_count(0)
                expect(group.locator('[data-unit-select]:checked')).to_have_count(0)
                group.locator('[data-source-group-filter]').click()
                expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy','true')
                expect(page.locator('#noticeList [data-notice-link]')).to_have_count(1)
                assert not errors and not writes,(errors,writes)
                assert BackgroundTask.query.count()==initial_tasks
                results.append(dict(width=1440,locale='en',shared_publishers=2,unique_source=teacher.id,
                    mirrored_checks_and_unit_checks=True,selected_source_count=1,
                    group_filter_notices=1,errors=errors,writes=writes,
                    scope='Synthetic explicit assertions test multi-owner controls only; no independent publisher evidence.'))
                page.close()
            finally:browser.close()
    finally:f.tearDown()
    (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':check()
