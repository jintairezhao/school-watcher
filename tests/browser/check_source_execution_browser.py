"""Actual isolated inspections and source commit -> API -> WebGL replay."""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_source_execution_events import SourceExecutionEventTests
from backend.database.models import User
from backend.database.source_governance_models import SourceConfigVersion
from backend.services.structure_versions import current_version
from playwright.sync_api import sync_playwright,expect


def check(fixture_class=SourceExecutionEventTests,output=None,expected_event_text=None,expected_zh_event_text=None):
    output=Path(output) if output else ROOT/'.local/structure-facts-20261021/source-events-browser'
    output.mkdir(parents=True,exist_ok=True)
    fixture=fixture_class();fixture.setUp()
    results=[]
    try:
        saved=fixture.connect()
        source_id=f'source:{saved.department_id}'
        client=fixture.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=User.query.one().id,_csrf_token='source-events-browser')
        endpoint=f'/api/schools/{fixture.school.id}/structure-snapshot'
        baseline=client.get(endpoint).json
        terminal=next(e for e in baseline['exploration_events'] if e['node_id']==source_id)
        assert terminal['current_configuration']
        assert terminal['parent_event_id'] in {e['id'] for e in baseline['exploration_events']}
        assert baseline['source_count']==1
        saved_version=current_version(fixture.school.id)
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True,
                args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            try:
                for width in (1440,390):
                    page=browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
                    errors=[];writes=[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def serve(route):
                        req=route.request;url=urlsplit(req.url)
                        assert url.hostname=='localhost',req.url
                        if req.method!='GET':writes.append(dict(method=req.method,url=url.path))
                        if url.path.startswith('/_desktop/'):return route.fulfill(json={'phase':'ready'})
                        response=client.open(url.path+('?' + url.query if url.query else ''),method=req.method,
                            data=req.post_data,headers={key:value for key,value in req.headers.items()
                                if key.lower() in ('content-type','x-csrf-token')})
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    page.route('**/*',serve)
                    page.goto(f'http://localhost/subscriptions/{fixture.school.id}/discovery-monitor')
                    page.locator('#languageSelect').select_option('en')
                    expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready')
                    page.locator('#monitorAuto').uncheck()
                    expect(page.locator('#monitorRecent')).to_contain_text('Source validation and connection')
                    page.wait_for_function('id=>discoveryMonitor.monitorSnapshot?.exploration_events?.some(e=>e.node_id===id)',arg=source_id)
                    page.locator('#atlasFullscreen').click()
                    page.evaluate('id=>discoveryMonitor.discoveryAtlas.select(id)',source_id)
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    expect(page.locator('#atlasPlay')).to_be_enabled()
                    page.locator('#atlasInspect').click()
                    expect(page.locator('#atlasDetailPanel')).to_be_visible()
                    before=page.evaluate('''()=>({selected:discoveryMonitor.discoveryAtlas.current.id,
                        states:discoveryMonitor.monitorSnapshot.nodes.map(n=>[n.id,n.state]),
                        sources:discoveryMonitor.monitorSnapshot.source_count})''')
                    write_count=len(writes)
                    page.locator('#atlasPlay').click()
                    expect(page.locator('#monitorPathMode')).to_have_value('exploration')
                    expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                    expect(page.locator('#atlasPathEvidence')).to_contain_text('Source configuration saved')
                    if expected_event_text:
                        expect(page.locator('#atlasPathEvidence')).to_contain_text(expected_event_text)
                    assert 'source_configuration_saved' not in page.locator('#atlasPathEvidence').inner_text()
                    expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-progress','1.0000',timeout=10000)
                    expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','false',timeout=10000)
                    after=page.evaluate('''()=>({selected:discoveryMonitor.discoveryAtlas.current.id,
                        states:discoveryMonitor.monitorSnapshot.nodes.map(n=>[n.id,n.state]),
                        sources:discoveryMonitor.monitorSnapshot.source_count})''')
                    assert before==after,(before,after)
                    assert writes[write_count:]==[],'Replay must issue no write requests'
                    page.screenshot(path=str(output/f'source-save-en-{width}.png'))
                    page.locator('#languageSelect').select_option('zh-CN',force=True)
                    expect(page.locator('#atlasPathEvidence')).to_contain_text('栏目配置已保存')
                    if expected_zh_event_text:
                        expect(page.locator('#atlasPathEvidence')).to_contain_text(expected_zh_event_text)
                    page.screenshot(path=str(output/f'source-save-zh-{width}.png'))
                    page.locator('#atlasDetailClose').click()
                    page.evaluate('id=>discoveryMonitor.discoveryAtlas.select(id)',source_id)
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    page.locator('#atlasPlay').click()
                    expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                    page.evaluate('''()=>{
                        const root=document.getElementById('discoveryMonitor');
                        const next={...root.monitorSnapshot,structure_token:root.monitorSnapshot.structure_token+'-gap',
                            exploration_events:root.monitorSnapshot.exploration_events.filter(e=>e.event_type!=='exploration_started')};
                        root.monitorSnapshot=next;
                        root.dispatchEvent(new CustomEvent('monitor-data',{detail:next}));
                    }''')
                    expect(page.locator('#atlasPlay')).to_be_disabled()
                    expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','false')
                    assert page.evaluate('()=>discoveryMonitor.discoveryAtlas.current.id')==source_id
                    assert current_version(fixture.school.id)==saved_version
                    assert SourceConfigVersion.query.count()==1
                    assert not errors,errors
                    forbidden=[entry for entry in writes if entry['url']!='/api/preferences/localization']
                    assert not forbidden,forbidden
                    assert page.evaluate('()=>document.documentElement.scrollWidth-document.documentElement.clientWidth')<=1
                    results.append(dict(width=width,locales=['en','zh-CN'],source_configuration_replay=True,
                        fullscreen_closed_details_stay_closed=True,source_identity_and_states_preserved=True,
                        cross_stream_parent_reference=True,missing_ancestor_cancels_replay=True,
                        replay_writes=0,forbidden_writes=forbidden,errors=errors,
                        business_revision_unchanged=True,live_website_requests=0,model_calls=0,
                        navigation_application_ancestry=bool(expected_event_text),
                        model_usage_annotations=bool(expected_zh_event_text),
                        evidence_scope='Local behavior fixture, not independent real-site or threshold evidence'))
                    page.close()
            finally:
                browser.close()
    finally:
        fixture.doCleanups()
    (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':check()
