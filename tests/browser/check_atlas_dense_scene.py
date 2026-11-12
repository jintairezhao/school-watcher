"""Measure the actual atlas on a retained large graph; no position injection.

Only the surrounding page status comes from a disposable app/retained shell.
Graph nodes/relations and their version stay exactly as supplied. This is one
local software-rendered observation, not a human study or a hardware benchmark.
"""
from pathlib import Path
from threading import Thread
import argparse
import copy
import hashlib
import json
import sys
import time

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_structure_snapshot import StructureSnapshotTests
from playwright.sync_api import sync_playwright,expect
from werkzeug.serving import make_server,WSGIRequestHandler

INSTRUMENT=r"""(()=>{
    window.atlasPhase='load';window.atlasFrames=[];window.atlasLongTasks=[];
    const nativeRAF=requestAnimationFrame;
    window.requestAnimationFrame=callback=>nativeRAF.call(window,time=>{
        const start=performance.now();try{return callback(time);}finally{
            if(callback.name==='draw')atlasFrames.push({phase:atlasPhase,at:start,duration:performance.now()-start});
        }
    });
    if(PerformanceObserver.supportedEntryTypes.includes('longtask'))new PerformanceObserver(list=>{
        for(const entry of list.getEntries())atlasLongTasks.push({at:entry.startTime,duration:entry.duration});
    }).observe({type:'longtask',buffered:true});
})();"""


def sha(data):return hashlib.sha256(data).hexdigest()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--shell',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--width',type=int,default=1440)
    parser.add_argument('--summary-check',action='store_true')
    parser.add_argument('--capture',action='store_true',help='One bounded functional screenshot set')
    parser.add_argument('--locale',default='zh-CN')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=False)
    raw=args.snapshot.read_bytes();graph=json.loads(raw)
    shell=copy.deepcopy(json.loads(args.shell.read_bytes()));payload={**shell,**graph}
    payload['structure_token']='retained-dense:'+sha(raw)
    payload['tasks']=[];payload['recent']=[]
    payload['progress'].update(current_label='',state='paused',can_pause=False,can_resume=False)
    payload['progress']['verified_source_count']=graph['source_count']
    assert payload['nodes']==graph['nodes'] and payload['relations']==graph['relations']
    encoded=json.dumps(payload,ensure_ascii=False).encode('utf-8')
    fixture=StructureSnapshotTests();fixture.setUp()
    tracked=('discovery-atlas.js','discovery-atlas-model.js','discovery-atlas-view.js','discovery-atlas-layout.js',
             'structure-layout.js','structure-layout-task.js','structure-layout-worker.js',
             'structure-layout-protocol.js','structure-appearance.js','discovery-atlas-picking.js','discovery-atlas-relations.js','discovery-monitor.js')
    report=dict(snapshotSha256=sha(raw),shellSha256=sha(args.shell.read_bytes()),payloadBytes=len(encoded),
        graphNodes=len(graph['nodes']),graphRelations=len(graph['relations']),graphVersion=graph['structure_version'],
        codeHashes={name:sha((ROOT/'frontend/static/js'/name).read_bytes()) for name in tracked},
        width=args.width,scope='Retained partial exploration graph, disposable page status, actual Worker/WebGL on SwiftShader',
        positionsInjected=False,officialRequests=0,modelCalls=0,businessWrites=0,
        samples={},errors=[],external=[],writes=[])
    def save():(args.output/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    save();server=thread=None
    class Quiet(WSGIRequestHandler):
        def log(self,*unused):pass
    try:
        from flask import request,Response
        @fixture.app.before_request
        def retained():
            if request.method!='GET':report['writes'].append(request.path)
            if request.path==fixture.endpoint:return Response(encoded,content_type='application/json',headers={'Cache-Control':'no-store'})
        server=make_server('127.0.0.1',0,fixture.app,threaded=True,request_handler=Quiet)
        origin=f'http://127.0.0.1:{server.server_port}'
        thread=Thread(target=server.serve_forever,daemon=True);thread.start()
        cookie=fixture.client.get_cookie(fixture.app.config['SESSION_COOKIE_NAME']);assert cookie
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='msedge',headless=True,
                args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
            report['browserVersion']=browser.version
            try:
                context=browser.new_context(viewport={'width':args.width,'height':1000},reduced_motion='reduce')
                context.add_cookies([dict(name=cookie.key,value=cookie.value,url=origin)])
                context.add_init_script(INSTRUMENT)
                def guard(route):
                    if not route.request.url.startswith(origin+'/'):
                        report['external'].append(route.request.url);route.abort()
                    else:route.continue_()
                context.route('**/*',guard)
                page=context.new_page();page.on('pageerror',lambda error:report['errors'].append(str(error)))
                session=context.new_cdp_session(page);session.send('Performance.enable')
                page.set_default_timeout(120000)
                start=time.perf_counter();page.goto(origin+fixture.fixture.page,wait_until='domcontentloaded')
                expect(page.locator('#atlasStage')).to_have_attribute('data-layout','ready',timeout=180000)
                report['loadToLayoutReadyMs']=(time.perf_counter()-start)*1000
                page.locator('#monitorAuto').uncheck()
                page.evaluate('(locale)=>I18n.setLocale(locale)',args.locale)
                page.evaluate("document.documentElement.dataset.theme='dark'")
                page.locator('#monitorScope').select_option('exploration')
                page.locator('#atlasFullscreen').click()
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                page.wait_for_timeout(400)
                report['loaded']=page.evaluate("""()=>{const a=discoveryMonitor.discoveryAtlas;return {
                    nodes:a.model.actual.size,relations:a.model.links.length,drawnNodes:a.nodes.length,
                    labelElements:atlasLabels.children.length,solverRelations:a.layout.relationCount,
                    omitted:a.layout.omitted.length,metrics:a.layout.metrics,
                    geometry:a.scene.getObjectByName('atlasNodes').geometry.attributes.position.count,
                    domElements:document.querySelectorAll('*').length};}""")
                report['footer']=page.evaluate("""()=>{const s=atlasStatus.getBoundingClientRect(),h=atlasHelp.getBoundingClientRect();
                    return {overlapHeight:Math.max(0,Math.min(s.bottom,h.bottom)-Math.max(s.top,h.top)),
                        statusBottom:s.bottom,helpTop:h.top};}""")
                if args.summary_check:assert report['footer']['overlapHeight']==0
                save();print(json.dumps({'phase':'loaded','readyMs':report['loadToLayoutReadyMs'],
                    'nodes':report['loaded']['nodes'],'relations':report['loaded']['relations']}),flush=True)

                def rotate(phase):
                    page.evaluate('(phase)=>window.atlasPhase=phase',phase)
                    page.mouse.move(args.width*.42,500);page.mouse.down()
                    page.mouse.move(args.width*.60,535,steps=12);page.mouse.up()
                    page.mouse.move(1,1);page.wait_for_timeout(250)
                    data=page.evaluate("""phase=>{const rows=atlasFrames.filter(r=>r.phase===phase).map(r=>r.duration).sort((a,b)=>a-b),
                        q=p=>rows.length?rows[Math.floor((rows.length-1)*p)]:null,a=discoveryMonitor.discoveryAtlas;
                        return {drawCallbacks:rows.length,p50ms:q(.5),p95ms:q(.95),maxMs:rows.at(-1),
                            durations:rows,visibleLabels:Number(discoveryMonitor.dataset.atlasLabelCount),
                            highlightObjects:a.scene.getObjectByName('atlasRoutes').children.length,
                            selected:a.current?.id||null};}""",phase)
                    report['samples'][phase]=data;save();print(json.dumps({'phase':phase,**{k:v for k,v in data.items() if k!='durations'}}),flush=True)
                rotate('overview_labels_on')
                if args.capture:page.locator('#atlasStage').screenshot(path=str(args.output/'overview.png'))
                page.locator('#atlasOptions > summary').click();page.locator('#atlasNodeLabels').uncheck()
                page.locator('#atlasOptions > summary').click();rotate('overview_labels_off')
                counts={}
                for edge in graph['relations']:
                    for key in ('source','target'):counts[edge[key]]=counts.get(edge[key],0)+1
                hub=max(counts,key=counts.get)
                page.evaluate("""id=>{const node=discoveryMonitor.monitorSnapshot.nodes.find(n=>n.id===id);
                    discoveryMonitor.dispatchEvent(new CustomEvent('monitor-select-node',{detail:{id,node,focus:false}}));}""",hub)
                report['hub']=dict(id=hub,incidentRawRelations=counts[hub],selection='Controlled list-selection event; not a physical sphere-hit claim')
                rotate('selected_hub_labels_off')
                if args.summary_check:
                    assert report['loaded']['drawnNodes']<len(graph['nodes'])
                    assert page.locator('#monitorTree .monitor-node').count()==0
                    page.locator('#atlasInspect').click();expect(page.locator('#atlasDetailPanel')).to_be_visible()
                    details=page.locator('#monitorRelationGroups > details')
                    report['groupPanel']=page.evaluate("""()=>{const p=atlasDetailPanel.getBoundingClientRect(),g=monitorRelationGroups.getBoundingClientRect();
                        return {top:g.top,panelTop:p.top,panelBottom:p.bottom,firstScreen:g.top>=p.top&&g.top<p.bottom};}""")
                    assert report['groupPanel']['firstScreen']
                    assert not page.locator('#monitorRelationGroups > details[open]').count()
                    assert page.locator('#monitorRelationGroups .monitor-related-node').count()==0
                    largest=details.evaluate_all('(items)=>items.map(n=>Number(n.dataset.relationCount)).indexOf(Math.max(...items.map(n=>Number(n.dataset.relationCount))))')
                    disclosure=details.nth(largest);group_id=disclosure.get_attribute('data-relation-group')
                    count=int(disclosure.get_attribute('data-relation-count'));assert count>24
                    if args.capture:page.locator('#atlasDetailPanel').screenshot(path=str(args.output/'relations-collapsed.png'))
                    before_positions=page.evaluate('JSON.stringify([...discoveryMonitor.discoveryAtlas.model.actual].map(([id,n])=>[id,n.position]))')
                    disclosure.locator(':scope > summary').click()
                    expected=page.evaluate("""async({id,hub})=>{
                        const {relationGroups}=await import('/static/js/discovery-atlas-relations.js');
                        return relationGroups(discoveryMonitor.discoveryAtlas.model.links,hub).find(g=>g.id===id).links.map(e=>e.id);
                    }""",{'id':group_id,'hub':hub})
                    page.wait_for_function("""ids=>{const mesh=discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasLinks');
                        return ids.every(id=>mesh.userData.relationIds.includes(id));}""",arg=expected)
                    assert len(expected)==count
                    expect(disclosure.locator('.monitor-related-node')).to_have_count(count)
                    assert not disclosure.locator('.monitor-relation-material[open]').count()
                    assert page.evaluate('JSON.stringify([...discoveryMonitor.discoveryAtlas.model.actual].map(([id,n])=>[id,n.position]))')==before_positions
                    disclosure.locator(':scope > summary').click()
                    page.wait_for_function("""ids=>{const mesh=discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasLinks');
                        return ids.filter(id=>mesh.userData.relationIds.includes(id)).length<ids.length;}""",arg=expected)
                    report['summaryInteraction']=dict(group=group_id,underlyingRelations=count,allExpandedEdgesMatchedActualIds=True,
                        collapsedAgain=True,coordinatesUnchanged=True,modelNodes=page.evaluate('discoveryMonitor.discoveryAtlas.model.actual.size'))
                    disclosure.locator(':scope > summary').click()
                    other=disclosure.locator('.monitor-related-node').first.get_attribute('data-related-node')
                    disclosure.locator('.monitor-related-node').first.click()
                    page.wait_for_function('id=>discoveryMonitor.discoveryAtlas.current?.id===id',arg=other)
                    expect(page.locator('#atlasDetailPanel')).to_be_visible()
                    assert page.evaluate('discoveryMonitor.discoveryAtlas.model.actual.size')==len(graph['nodes'])
                    report['summaryInteraction']['nativeNeighborButtonSelected']=other
                    page.locator('#atlasInspect').click();page.locator('#atlasFullscreen').click()
                    page.locator('#atlasListPanel > summary').click()
                    expect(page.locator('#monitorTree .monitor-node')).to_have_count(len(graph['nodes']))
                    page.locator('#atlasListPanel > summary').click()
                    expect(page.locator('#monitorTree .monitor-node')).to_have_count(0)
                    report['lazyList']=dict(defaultRows=0,expandedRows=len(graph['nodes']),closedRows=0)
                    save()
                metrics=session.send('Performance.getMetrics')
                report['browserMetrics']={item['name']:item['value'] for item in metrics.get('metrics',[])
                    if item['name'] in ('JSHeapUsedSize','JSHeapTotalSize','Nodes','LayoutCount','RecalcStyleCount')}
                report['domCounters']=session.send('Memory.getDOMCounters')
                report['frames']=page.evaluate('atlasFrames');report['longTasks']=page.evaluate('atlasLongTasks')
                assert report['loaded']['nodes']==len(graph['nodes'])
                assert report['loaded']['solverRelations']==len(graph['relations'])
                assert report['loaded']['omitted']==0
                assert not report['errors'] and not report['external'] and not report['writes']
                save();context.close()
            finally:browser.close()
    finally:
        if server:server.shutdown();server.server_close()
        if thread:thread.join(timeout=5)
        fixture.tearDown();save()
    print(json.dumps({'samples':{key:{k:v for k,v in value.items() if k!='durations'} for key,value in report['samples'].items()},
        'errors':report['errors'],'external':report['external'],'writes':report['writes']},ensure_ascii=False))


if __name__=='__main__':main()
