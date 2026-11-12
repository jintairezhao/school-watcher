"""Display exact old publisher witnesses from a disposable replay.

This accepts only a workspace-local replay case. It does not collect sources,
contact a university, or change any production or replay business state.
"""
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import hashlib
import json
import socket
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import User
from backend.services.structure_versions import current_version
from playwright.sync_api import sync_playwright,expect


def check(case,output,include_source_identity=False,expected_home_hash=None,expected_publishers=None,source_ids=None):
    case,output = Path(case).resolve(),Path(output).resolve()
    assert case.is_relative_to(ROOT/'.local') and output.is_relative_to(ROOT/'.local')
    assert not output.exists()
    summary = json.loads((case/'summary.json').read_text('utf-8'))
    expected={ident:dict(publisher='组织人事部',material='7e17242a393a')
        for ident in ('source:1443','source:1444','source:1445')}
    if include_source_identity:
        expected['source:1440']=dict(publisher='马克思主义学院',material='8f032245c2b2')
    if expected_publishers:
        manifest=Path(expected_publishers).resolve()
        assert manifest.is_relative_to(ROOT/'.local')
        expected=json.loads(manifest.read_text('utf-8'))
        assert expected and all(ident.startswith('source:') and witness['publisher'] and
            len(witness['material'])>=12 for ident,witness in expected.items())
    assert summary.get('publisher_relations',summary.get('source_ownership',summary.get('publishers_after'))) == len(expected)
    if 'sources_with_publisher' in summary:assert summary['sources_with_publisher']==len(expected)
    assert summary.get('configured_sources',summary.get('source_count')) == 89
    displayed={ident:witness for ident,witness in expected.items() if not source_ids or ident in source_ids}
    assert displayed and (not source_ids or set(source_ids)<=set(expected))
    app = create_app(dict(TESTING=True,SECRET_KEY='isolated-retained-publisher-browser',
        SQLALCHEMY_DATABASE_URI='sqlite:///'+str(case/'business.sqlite3'),
        SOURCE_CATALOG_PATH=str(case/'catalog.sqlite3'),
        DISCOVERY_CACHE_PATH=str(case/'must-not-open-inventory.sqlite3'),
        SOURCE_GOVERNANCE_EVIDENCE_PATH=str(case/'no-new-governance-evidence')))
    output.mkdir()
    observations=[]
    access_checks=[]
    native_connect = socket.socket.connect
    def local_connect(sock,address):
        # Windows asyncio creates a loopback socket pair for its own IPC.
        # Permit that transport; any external backend connection still fails.
        if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1','localhost'):
            return native_connect(sock,address)
        raise AssertionError('Offline browser replay rejects external connections')
    with app.app_context(),patch('socket.socket.connect',new=local_connect), \
            patch('backend.ai.providers.complete',side_effect=AssertionError('No model call')):
        try:
            user = User.query.filter_by(role='admin').first()
            client = app.test_client()
            with client.session_transaction() as session:
                session.update(user_id=user.id,_csrf_token='retained-publisher-browser')
            baseline = client.get('/api/schools/1/structure-snapshot').json
            assert baseline['structure_version'] == summary['version_after']
            before_version = current_version(1)
            before_states = [(n['id'],n['state']) for n in baseline['nodes']]
            ownership = [r for r in baseline['relations'] if r['type']=='source_ownership']
            assert {r['target'] for r in ownership} == set(expected)
            assert not any(r['target']=='source:29' for r in ownership)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge',headless=True,
                    args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
                try:
                    for width in (1440,390):
                        page = browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
                        errors,writes,blocked=[],[],[]
                        page.on('pageerror',lambda error:errors.append(str(error)))
                        def serve(route):
                            req,url = route.request,urlsplit(route.request.url)
                            if url.hostname!='localhost':
                                blocked.append(req.url)
                                return route.abort()
                            if req.method!='GET':
                                writes.append(dict(method=req.method,path=url.path))
                                return route.fulfill(status=405,body='Read-only replay')
                            if url.path.startswith('/_desktop/'):
                                return route.fulfill(json={'phase':'ready'})
                            response=client.get(url.path+('?' + url.query if url.query else ''))
                            route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                        page.route('**/*',serve)
                        page.goto('http://localhost/subscriptions/1/discovery-monitor')
                        expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready')
                        try:
                            page.wait_for_function('(count)=>discoveryMonitor.discoveryAtlas?.model?.actual.size===count',arg=len(baseline['nodes']))
                        except Exception:
                            diagnostic=page.evaluate('''()=>({modelSize:discoveryMonitor.discoveryAtlas?.model?.actual.size,
                                snapshotNodes:discoveryMonitor.monitorSnapshot?.nodes.length,
                                version:discoveryMonitor.monitorSnapshot?.structure_version,
                                connection:document.getElementById('monitorConnection')?.textContent})''')
                            diagnostic.update(expected_nodes=len(baseline['nodes']),errors=errors,blocked=blocked)
                            (output/'loading-diagnostic.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),'utf-8')
                            raise
                        page.locator('#monitorAuto').uncheck()
                        if expected_home_hash:
                            actual=page.evaluate('''async()=>{
                                const a=discoveryMonitor.discoveryAtlas;
                                const {accessRoute}=await import('/static/js/discovery-atlas-model.js');
                                const root=a.nodes.find(n=>n.record.entity_type==='page'&&n.record.kind==='root');
                                return {hash:root.record.content_hash,routes:[
                                    'https://www.cupk.edu.cn/c/2021-06-29/508819.shtml',
                                    'https://www.cupk.edu.cn/c/2017-04-21/485097.shtml'].map(url=>{
                                        const node=a.nodes.find(n=>n.record.entity_type==='page'&&n.record.url===url);
                                        const route=accessRoute(a.model,node);
                                        return {url,complete:route.complete,urls:route.nodes.map(n=>n.record.url)};
                                    })};}''')
                            assert actual['hash']==expected_home_hash
                            assert all(r['complete'] and r['urls']==['https://www.cupk.edu.cn/',r['url']] for r in actual['routes'])
                            access_checks.append(dict(width=width,**actual))
                        page.locator('#monitorPathMode').select_option('membership')
                        page.locator('#atlasFullscreen').click()
                        for locale in ('zh-CN','en'):
                            page.evaluate('(locale)=>I18n.setLocale(locale)',locale)
                            for ident,witness in displayed.items():
                                page.evaluate('(id)=>discoveryMonitor.discoveryAtlas.select(id)',ident)
                                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                                page.locator('#atlasInspect').click()
                                expect(page.locator('#atlasPathEvidence')).to_contain_text(witness['publisher'])
                                # Proof rows are created on explicit expansion.
                                # Exercise that native interaction before checking
                                # the material, rather than requiring eager DOM.
                                group=page.locator('#monitorRelationGroups > details[data-relation-group*="source_ownership"]')
                                expect(group).to_have_count(1)
                                if group.get_attribute('open') is None:
                                    group.locator(':scope > summary').click()
                                material=group.locator('.monitor-relation-material')
                                expect(material).to_have_count(1)
                                if material.get_attribute('open') is None:
                                    material.locator(':scope > summary').click()
                                proofs=next(r['evidence'] for r in ownership if r['target']==ident)
                                expect(material.locator('p')).to_have_count(len(proofs))
                                expect(material).to_contain_text(witness['material'])
                                assert page.evaluate('''id=>{const a=discoveryMonitor.discoveryAtlas;
                                    return a.model.links.some(r=>r.type==='source_ownership'&&r.to.id===id); }''',ident)
                                access=None
                                if source_ids:
                                    access=page.evaluate('''async id=>{
                                        const a=discoveryMonitor.discoveryAtlas;
                                        const {accessRoute}=await import('/static/js/discovery-atlas-model.js');
                                        const route=accessRoute(a.model,a.model.actual.get(id));
                                        return {complete:route.complete,urls:route.nodes.map(n=>n.record.url),
                                            reversed:route.steps.some(s=>s.reversed)};}''',ident)
                                    assert access['complete'] and not access['reversed']
                                    assert len(access['urls'])==len(set(access['urls']))
                                text = page.locator('#atlasPathEvidence').inner_text()
                                observations.append(dict(width=width,locale=locale,source_id=ident,
                                    publisher=witness['publisher'],path_text=text,details_opt_in=True,
                                    native_proof_expansion=True,visible_proofs=len(proofs),access_path=access))
                                if not expected_publishers and ident in ('source:1443','source:1440'):
                                    page.screenshot(path=str(output/f'publisher-{ident.split(":")[1]}-{locale}-{width}.png'))
                                page.locator('#atlasDetailClose').click()
                        assert not errors and not writes and not blocked,(errors,writes,blocked)
                        after_states = page.evaluate('discoveryMonitor.monitorSnapshot.nodes.map(n=>[n.id,n.state])')
                        assert after_states == [list(pair) for pair in before_states]
                        assert page.evaluate('discoveryMonitor.monitorSnapshot.source_count') == 89
                        expect(page.locator('#atlasPlay')).to_be_disabled()
                        page.close()
                finally:browser.close()
            assert current_version(1) == before_version
            assert client.get('/api/schools/1/structure-snapshot').json['relations'] == baseline['relations']
        finally:
            db.session.remove()
            db.engine.dispose()
    result = dict(observations=observations,version_unchanged=before_version,source_count=89,
        checked_source_ids=list(displayed),
        expected_home_hash=expected_home_hash,direct_access_checks=access_checks,
        errors=[],writes=[],official_requests=0,model_calls=0,
        current_online_validity='not_checked',independent_threshold_sample=False)
    (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
    (output/'artifact-hashes.json').write_text(json.dumps({p.name:hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.iterdir() if p.is_file() and p.name!='artifact-hashes.json'},indent=2)+'\n','utf-8')
    print(json.dumps(dict(checks=len(observations),widths=[1440,390],locales=['zh-CN','en'],version=before_version,
        source_count=89,errors=[],writes=[]),ensure_ascii=False))


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--case',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--include-source-identity',action='store_true')
    parser.add_argument('--expected-home-hash')
    parser.add_argument('--expected-publishers')
    parser.add_argument('--source-ids',nargs='+')
    options=parser.parse_args()
    check(options.case,options.output,options.include_source_identity,options.expected_home_hash,options.expected_publishers,options.source_ids)
