"""Actual WebGL/Worker integration on a retained graph and disposable app shell.

Payload perturbations test geometry version fences; they are not real source
onboarding/removal events. No injected positions, fake solver, website or model.
"""
from pathlib import Path
from threading import Thread, Lock
from urllib.parse import urlsplit
import argparse
import copy
import hashlib
import json
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from test_structure_snapshot import StructureSnapshotTests
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server,WSGIRequestHandler
from check_layout_comparison_browser import MEASURE

TRACK_WORKERS=r"""(() => {
    const NativeWorker=Worker;window.geometryJobs=[];window.heldGeometry=[];window.holdGeometry=false;
    window.Worker=class extends NativeWorker {
        constructor(...args){super(...args);const row={url:String(args[0]),started:false,terminated:false};
            geometryJobs.push(row);this._row=row;
            this.addEventListener('message',event=>{
                if(event.data?.kind==='started')row.started=true;
                const deliver=()=>this._receiver?.(event);
                if(event.data?.kind==='completed'&&holdGeometry)heldGeometry.push(deliver);else deliver();
            });
        }
        set onmessage(receiver){this._receiver=receiver;}
        get onmessage(){return this._receiver;}
        terminate(){this._row.terminated=true;return super.terminate();}
    };
})();"""

def digest(raw):return hashlib.sha256(raw).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--expected',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--cases',nargs='*',help='Optional width:locale cases for a focused reproduction')
    parser.add_argument('--appearance',action='store_true',help='Check source-count radii, real instance colors and localized display evidence')
    parser.add_argument('--no-screenshots',action='store_true',help='Functional follow-up without a further visual review pass')
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    original=json.loads(args.snapshot.read_text('utf-8'))
    assert len(original['nodes'])==241 and len(original['relations'])==902
    expected=json.loads(args.expected.read_text('utf-8'))
    assert set(expected)=={node['id'] for node in original['nodes']}
    fixture=StructureSnapshotTests();fixture.setUp()
    current={'payload':copy.deepcopy(original)};lock=Lock()
    errors,external,writes,requests,results=[],[],[],[],[]
    server=thread=None
    tracked=['discovery-atlas.js','discovery-atlas-model.js','discovery-atlas-layout.js','discovery-atlas-view.js','structure-appearance.js','discovery-atlas-relations.js','discovery-monitor.js',
        'structure-layout.js','structure-layout-task.js','structure-layout-worker.js','structure-layout-protocol.js']
    code={name:digest((ROOT/'frontend/static/js'/name).read_bytes()) for name in tracked}
    report=dict(snapshotSha256=digest(args.snapshot.read_bytes()),codeHashes=code,results=results,
        officialRequests=0,modelCalls=0,businessWrites=0,positionsInjected=False,
        scope='Retained development graph, actual app/Worker; controlled DTO mutations are geometry protocol fixtures')
    def save():
        (args.output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    class Quiet(WSGIRequestHandler):
        def log(self,*unused):pass
    try:
        from flask import request,Response
        @fixture.app.before_request
        def serve_snapshot():
            requests.append(dict(method=request.method,path=request.path))
            if request.method!='GET':writes.append(request.path)
            if request.path==fixture.endpoint:
                with lock:body=json.dumps(current['payload'],ensure_ascii=False)
                return Response(body,content_type='application/json',headers={'Cache-Control':'no-store'})
        server=make_server('127.0.0.1',0,fixture.app,threaded=True,request_handler=Quiet)
        origin=f'http://127.0.0.1:{server.server_port}'
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        cookie=fixture.client.get_cookie(fixture.app.config['SESSION_COOKIE_NAME']);assert cookie
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True,
                args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            report['browserVersion']=browser.version
            try:
                cases=[(int(value.partition(':')[0]),value.partition(':')[2]) for value in args.cases] if args.cases else [(1440,'zh-CN'),(390,'en'),(1440,'en'),(390,'zh-CN')]
                for index,(width,locale) in enumerate(cases):
                    with lock:current['payload']=copy.deepcopy(original)
                    context=browser.new_context(viewport={'width':width,'height':1000},reduced_motion='reduce')
                    context.add_cookies([dict(name=cookie.key,value=cookie.value,url=origin)])
                    context.add_init_script(TRACK_WORKERS)
                    def guard(route):
                        if not route.request.url.startswith(origin+'/'):
                            external.append(route.request.url);route.abort()
                        else:route.continue_()
                    context.route('**/*',guard)
                    page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                    page.goto(origin+fixture.fixture.page)
                    expect(page.locator('#atlasStage')).to_have_attribute('data-layout','ready',timeout=60000)
                    page.locator('#monitorAuto').uncheck()
                    page.evaluate('(locale)=>I18n.setLocale(locale)',locale)
                    page.evaluate("document.documentElement.dataset.theme='dark'")
                    positions=page.evaluate('Object.fromEntries([...discoveryMonitor.discoveryAtlas.model.actual].map(([id,n])=>[id,n.position]))')
                    difference=max(abs(positions[ident][axis]-expected[ident][axis]) for ident in positions for axis in ('x','y','z'))
                    assert difference<1e-7,difference
                    assert page.evaluate('geometryJobs.length')==1
                    assert page.evaluate('discoveryMonitor.discoveryAtlas.layout.relationCount')==902
                    assert page.evaluate('discoveryMonitor.discoveryAtlas.layout.omitted.length')==0
                    appearance_audit=None
                    if args.appearance:
                        appearance_audit=[]
                        for theme in ('light','dark'):
                            page.evaluate('(theme)=>document.documentElement.dataset.theme=theme',theme)
                            page.evaluate('()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))')
                            audit=page.evaluate("""async theme=>{
                                const THREE=await import('three'),a=discoveryMonitor.discoveryAtlas;
                                const mesh=a.scene.getObjectByName('atlasNodes'),color=id=>{
                                    const c=new THREE.Color();mesh.getColorAt(a.nodes.findIndex(n=>n.id===id),c);return c.toArray();};
                                const source=a.model.actual.get('source:1440'),owner=source.presentation.groups[0];
                                const sources=[...a.model.actual.values()].filter(n=>n.record.entity_type==='source');
                                const unknown=sources.find(n=>n.presentation.groupStatus==='unknown');
                                const gray=new THREE.Color(theme==='dark'?'#98a2af':'#5f6b79').toArray();
                                const maxDifference=(x,y)=>Math.max(...x.map((n,i)=>Math.abs(n-y[i])));
                                return {theme,nodes:a.model.actual.size,drawnNodes:a.nodes.length,known:sources.filter(n=>n.presentation.groupStatus==='known').length,
                                    unknown:sources.filter(n=>n.presentation.groupStatus==='unknown').length,
                                    ownerColorDifference:maxDifference(color(source.id),color(owner)),
                                    unknownGrayDifference:maxDifference(color(unknown.id),gray),
                                    configuredSourceRadius:source.radius,
                                    zeroCountRadii:[...a.model.actual.values()].filter(n=>!n.presentation.connectedSourceCount).map(n=>n.radius),
                                    solverMatchesScene:[...a.model.actual.values()].every(n=>
                                        n.radius===n.layoutSize.radius&&n.presentation.connectedSourceCount===n.layoutSize.connectedSourceCount)};
                            }""",theme)
                            assert audit['nodes']==241 and audit['known']==4 and audit['unknown']==85,audit
                            assert audit['ownerColorDifference']<1e-6 and audit['unknownGrayDifference']<1e-6,audit
                            assert audit['configuredSourceRadius']==5 and set(audit['zeroCountRadii'])=={3},audit
                            assert audit['solverMatchesScene'],audit
                            appearance_audit.append(audit)
                        assert page.evaluate('geometryJobs.length')==1
                    page.locator('#monitorPathMode').select_option('membership')
                    page.locator('#atlasFullscreen').click()
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    page.locator('#atlasSearchToggle').click()
                    expect(page.locator('#monitorSearch')).to_be_visible()
                    target=next(node for node in original['nodes'] if node['id']=='source:1440')
                    page.locator('#monitorSearch').fill(target['label'])
                    page.locator('#atlasSearchToggle').click()
                    page.wait_for_timeout(150)
                    page.locator('#atlasOverview').click()
                    await_frame='()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)))'
                    page.evaluate(await_frame)
                    point=page.evaluate("""()=>{const a=discoveryMonitor.discoveryAtlas,n=a.model.actual.get('source:1440');
                        a.camera.updateMatrixWorld(true);
                        const p=a.camera.position.clone().copy(n.renderPosition||n.position).project(a.camera),r=atlasViewport.getBoundingClientRect();
                        return {x:r.x+(p.x+1)*r.width/2,y:r.y+(1-p.y)*r.height/2,z:p.z};}""")
                    assert 0<point['x']<width and 0<point['y']<1000 and -1<point['z']<1,point
                    before_click=page.evaluate("""async point=>{
                        const THREE=await import('three'),{pickSphere}=await import('/static/js/discovery-atlas-picking.js');
                        const a=discoveryMonitor.discoveryAtlas,r=atlasViewport.getBoundingClientRect(),ray=new THREE.Raycaster();
                        ray.setFromCamera(new THREE.Vector2((point.x-r.x)/r.width*2-1,-(point.y-r.y)/r.height*2+1),a.camera);
                        const hit=pickSphere(a.nodes,{origin:ray.ray.origin,direction:ray.ray.direction,near:a.camera.near,far:a.camera.far,
                            pixelFactor:2*Math.tan(THREE.MathUtils.degToRad(a.camera.fov/2))/r.height,minimumPixels:4});
                        return {point,frontSphere:hit?.id,top:document.elementsFromPoint(point.x,point.y).slice(0,3)
                            .map(n=>({tag:n.tagName,id:n.id,cls:n.className,node:n.dataset.atlasNode}))};
                    }""",point)
                    (args.output/f'click-before-{width}-{locale}.json').write_text(json.dumps(before_click,ensure_ascii=False,indent=2),'utf-8')
                    assert before_click['frontSphere']=='source:1440',before_click
                    assert before_click['top'][0]['tag']=='CANVAS',before_click
                    page.mouse.click(point['x'],point['y'])
                    page.wait_for_timeout(150)
                    if page.evaluate("discoveryMonitor.discoveryAtlas.current?.id!=='source:1440'"):
                        diagnostic=page.evaluate("""point=>({point,selected:discoveryMonitor.discoveryAtlas.current?.id,
                            targetLabelVisible:!discoveryMonitor.discoveryAtlas.model.actual.get('source:1440').labelElement.hidden,
                            top:document.elementsFromPoint(point.x,point.y).slice(0,5).map(n=>({tag:n.tagName,id:n.id,cls:n.className,node:n.dataset.atlasNode})),
                            nodes:discoveryMonitor.discoveryAtlas.nodes.map(n=>({id:n.id,label:n.label,position:n.renderPosition||n.position,radius:n.drawRadius})),
                            camera:discoveryMonitor.discoveryAtlas.camera.position.toArray(),target:discoveryMonitor.discoveryAtlas.controls.target.toArray()})""",point)
                        (args.output/f'click-failure-{width}-{locale}.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2),'utf-8')
                    page.wait_for_function("()=>discoveryMonitor.discoveryAtlas.current?.id==='source:1440'")
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    glow=page.evaluate("discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasRoutes').children.length")
                    assert glow>1,glow
                    if args.appearance:
                        page.locator('#atlasInspect').click()
                        expect(page.locator('#atlasDetailPanel')).to_be_visible()
                        expect(page.locator('#atlasAppearanceEvidence h3')).to_have_text('Graph display basis' if locale=='en' else '图谱显示依据')
                        copy_text=page.locator('#atlasAppearanceEvidence').inner_text()
                        assert '马克思主义学院' in copy_text,copy_text
                        assert ('Confirmed connected sources: 1' if locale=='en' else '已确认的接入信源数：1') in copy_text,copy_text
                        assert ('Color basis: Source ownership' if locale=='en' else '颜色依据：栏目归属') in copy_text,copy_text
                        disclosure=page.locator('#atlasPathEvidence .atlas-route-proof')
                        assert disclosure.count()>0
                        assert not page.locator('#atlasPathEvidence .atlas-route-proof[open]').count()
                        assert not disclosure.first.locator('a').first.is_visible()
                        expected_proofs=page.evaluate("""async()=>{
                            const {membershipRoute}=await import('/static/js/discovery-atlas-model.js'),a=discoveryMonitor.discoveryAtlas;
                            const route=membershipRoute(a.model,a.current);
                            return (route.paths||[route]).flatMap(path=>path.steps).reduce((n,step)=>n+(step.edge.evidence?.length||1),0);
                        }""")
                        assert page.locator('#atlasPathEvidence .atlas-route-proof a').count()==expected_proofs
                        if index<2 and not args.no_screenshots:
                            page.locator('#atlasAppearanceEvidence').scroll_into_view_if_needed()
                            page.locator('#atlasDetailPanel').screenshot(path=str(args.output/f'appearance-detail-{width}-{locale}.png'))
                        disclosure.first.locator('summary').click()
                        expect(disclosure.first.locator('a').first).to_be_visible()
                        opened_relation=disclosure.first.get_attribute('data-relation-id')
                        page.locator('#atlasInspect').click()
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    page.locator('#atlasSearchToggle').click()
                    page.locator('#atlasOptions > summary').click()
                    expect(page.locator('#atlasSearchToggle')).to_have_attribute('aria-expanded','false')
                    page.locator('#atlasNodeLabels').uncheck()
                    page.locator('#atlasOptions > summary').click()
                    entry=dict(width=width,locale=locale,initialNodes=len(positions),participatingRelations=902,
                        maxDifferenceFromFrozenPositions=difference,initialWorkerJobs=1,sphereClick=True,routeGlowObjects=glow,
                        detailsInitiallyClosed=True,frames={})
                    if args.appearance:entry['appearanceAudit']=appearance_audit
                    # Metadata-only update goes through the real refresh endpoint.
                    renamed=copy.deepcopy(original);renamed['structure_version']['revision']+=1
                    renamed['structure_token']='fixture-metadata-'+str(index)
                    for node in renamed['nodes']:
                        if node['id']=='source:1440':node['label']='Current fixture label'
                    with lock:current['payload']=renamed
                    # The refresh button is outside native fullscreen; click through
                    # its normal DOM action without bypassing any graph hit test.
                    page.locator('#monitorRefresh').evaluate('button=>button.click()')
                    page.wait_for_function('(token)=>discoveryMonitor.monitorSnapshot.structure_token===token',arg=renamed['structure_token'])
                    page.wait_for_timeout(100)
                    assert page.evaluate('geometryJobs.length')==1
                    assert page.evaluate("discoveryMonitor.discoveryAtlas.current.record.label==='Current fixture label'")
                    if args.appearance:
                        assert page.evaluate("""id=>[...document.querySelectorAll('#atlasPathEvidence .atlas-route-proof')]
                            .some(item=>item.dataset.relationId===id&&item.open)""",opened_relation)
                    assert not page.locator('#atlasNodeLabels').is_checked()
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    entry['metadataReusedWorker']=True
                    if index==0:
                        page.evaluate('window.holdGeometry=true')
                        shaped=copy.deepcopy(renamed);removed=next(node['id'] for node in shaped['nodes'] if node['entity_type']=='source' and node['id']!='source:1440')
                        shaped['nodes']=[node for node in shaped['nodes'] if node['id']!=removed]
                        shaped['relations']=[edge for edge in shaped['relations'] if edge['source']!=removed and edge['target']!=removed]
                        shaped['structure_token']='fixture-shape';shaped['structure_version']['revision']+=1
                        with lock:current['payload']=shaped
                        page.locator('#monitorRefresh').evaluate('button=>button.click()')
                        page.wait_for_function('()=>heldGeometry.length===1',timeout=60000)
                        reset=copy.deepcopy(renamed);reset['structure_version']={'epoch':'fixture-new-epoch','revision':1};reset['structure_token']='fixture-reset'
                        with lock:current['payload']=reset
                        page.locator('#monitorRefresh').evaluate('button=>button.click()')
                        page.wait_for_function('()=>heldGeometry.length===2',timeout=60000)
                        page.evaluate('heldGeometry.shift()()')
                        assert page.evaluate("atlasStage.dataset.layout==='pending'&&discoveryMonitor.discoveryAtlas.model.actual.size===241")
                        page.evaluate('window.holdGeometry=false;heldGeometry.shift()()')
                        expect(page.locator('#atlasStage')).to_have_attribute('data-layout','ready',timeout=60000)
                        assert page.evaluate("discoveryMonitor.discoveryAtlas.layout.token==='fixture-reset'")
                        assert page.evaluate("discoveryMonitor.discoveryAtlas.current?.id==='source:1440'")
                        assert not page.locator('#atlasNodeLabels').is_checked()
                        expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                        entry['oldCompletedReplyRejectedAfterEpochReset']=True
                    entry['finalWorkerJobs']=page.evaluate('geometryJobs.length')
                    entry['finalLayout']=page.evaluate('discoveryMonitor.discoveryAtlas.layout')
                    entry['frames']['focused']=page.evaluate(MEASURE)
                    results.append(entry);save()
                    context.close()
            finally:browser.close()
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(timeout=5)
        fixture.tearDown()
        report.update(pageErrors=errors,externalRequests=external,writes=writes,servedRequests=requests);save()
    assert not errors and not external and not writes,(errors,external,writes)
    assert len(results)==len(cases)
    print(json.dumps(dict(cases=len(results),jobs=[row['finalWorkerJobs'] for row in results],
        errors=errors,external=external,writes=writes),ensure_ascii=False))


if __name__=='__main__':main()
