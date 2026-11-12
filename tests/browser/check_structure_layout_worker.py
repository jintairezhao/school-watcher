"""Functional worker/heartbeat test over explicit retained fixtures, no product UI.

Serves only named source modules and verified JSON on loopback. No application,
production database, university request, model, or screenshot polish pass.
"""
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit
import argparse
import hashlib
import json

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
MODULES = ('structure-layout.js', 'structure-appearance.js', 'structure-layout-protocol.js',
           'structure-layout-task.js', 'structure-layout-worker.js')
HTML = b'''<!doctype html><meta charset="utf-8"><title>Local layout worker check</title>
<script type="module">
import {StructureLayoutTask} from '/modules/structure-layout-task.js';
import {computeStructureLayout} from '/modules/structure-layout-protocol.js';
window.LayoutTask=StructureLayoutTask;window.compute=computeStructureLayout;window.ready=true;
</script>'''
MEASURE = r"""async name => {
    const data=await (await fetch('/'+name+'.json')).json();
    const expected=await (await fetch('/'+name+'-expected.json')).json();
    const task=new LayoutTask(),gaps=[];let last=performance.now();
    const heartbeat=setInterval(()=>{const next=performance.now();gaps.push(next-last);last=next;},20);
    const started=performance.now();
    const result=await task.run(data.input,{structureToken:data.structureToken,signature:data.signature,options:data.options});
    const elapsed=performance.now()-started;
    clearInterval(heartbeat);task.dispose();
    if(result.status!=='completed')throw new Error(JSON.stringify(result));
    const identities=Object.keys(result.result.positions);
    if(identities.length!==data.input.nodes.length)throw new Error('Identity count changed');
    let maximumDifference=0;
    for(const id of identities)for(const axis of ['x','y','z'])maximumDifference=Math.max(maximumDifference,
        Math.abs(expected[id][axis]-result.result.positions[id][axis]));
    if(maximumDifference>1e-7)throw new Error('Frozen geometry differs: '+maximumDifference);
    if(!gaps.length)throw new Error('No main-thread heartbeat while worker ran');
    if(name==='large'&&gaps.length<20)throw new Error('Large computation blocked heartbeat');
    const probeStarted=performance.now();
    const probe=new Promise(resolve=>setTimeout(()=>resolve(performance.now()-probeStarted),0));
    const directStarted=performance.now();
    const direct=compute({kind:'layout',generation:1,structureToken:data.structureToken,
        signature:data.signature,input:data.input,options:data.options});
    const directMilliseconds=performance.now()-directStarted;
    const mainThreadTimerDelay=await probe;
    if(direct.kind!=='completed'||JSON.stringify(direct.result)!==JSON.stringify(result.result))
        throw new Error('Worker result differs from direct computation');
    return {name,nodes:data.input.nodes.length,relations:data.input.relations.length,
        workerMilliseconds:elapsed,heartbeatCount:gaps.length,maxHeartbeatGap:Math.max(...gaps),
        directMilliseconds,mainThreadTimerDelay,maximumDifferenceFromFrozenNodePositions:maximumDifference,
        exactWorkerDirectMatch:true,physicalOverlaps:result.result.metrics.sphereOverlaps,
        allRelationIdsPreserved:result.result.constraints.participatingRelationIds.length===data.input.relations.length};
}"""
SUPERSEDE = r"""async () => {
    const small=await (await fetch('/small.json')).json(),large=await (await fetch('/large.json')).json();
    const started=async task=>{
        const deadline=performance.now()+30000;
        while(!task.pending?.started){
            if(!task.pending||performance.now()>deadline)throw new Error('Worker never acknowledged start');
            await new Promise(resolve=>setTimeout(resolve,10));
        }
    };
    const task=new LayoutTask();
    const old=task.run(large.input,{structureToken:'old-epoch:1',signature:large.signature,options:large.options});
    await started(task);
    const next=task.run(small.input,{structureToken:'new-epoch:1',signature:small.signature,options:small.options});
    const oldResult=await old,nextResult=await next;
    if(oldResult.status!=='superseded'||nextResult.status!=='completed'||nextResult.structureToken!=='new-epoch:1')
        throw new Error('Version supersession failed');
    if(Object.keys(nextResult.result.positions).length!==small.input.nodes.length)throw new Error('Old geometry leaked');
    const discarded=task.run(large.input,{structureToken:'new-epoch:2',signature:large.signature,options:large.options});
    await started(task);task.dispose();
    const discardedResult=await discarded;
    if(discardedResult.status!=='disposed'||task.pending!==null)throw new Error('Dispose left a live task');
    return {oldWorkerAcknowledged:true,oldStatus:oldResult.status,newStatus:nextResult.status,
        newToken:nextResult.structureToken,newNodes:Object.keys(nextResult.result.positions).length,
        disposedAfterAcknowledgement:true,disposedStatus:discardedResult.status};
}"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixtures', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for name, digest in json.loads((args.fixtures/'artifact-hashes.json').read_text('utf-8')).items():
        assert hashlib.sha256((args.fixtures/name).read_bytes()).hexdigest() == digest, name
    args.output.mkdir(parents=True, exist_ok=False)
    assets = {'/': (HTML, 'text/html')}
    for name in MODULES:
        assets['/modules/'+name] = ((ROOT/'frontend/static/js'/name).read_bytes(), 'text/javascript')
    for name in ('small', 'large', 'small-expected', 'large-expected'):
        assets['/'+name+'.json'] = ((args.fixtures/(name+'.json')).read_bytes(), 'application/json')
    requests, external, errors = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused):
            pass

        def do_GET(self):
            route = urlsplit(self.path).path
            requests.append(dict(method='GET', path=route))
            if route not in assets:
                self.send_error(404)
                return
            body, kind = assets[route]
            self.send_response(200)
            self.send_header('Content-Type', kind+'; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            requests.append(dict(method='POST', path=self.path))
            self.send_error(405)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    report = dict(websiteRequests=0, modelCalls=0, businessWrites=0, productIntegrated=False,
        inputSummary=json.loads((args.fixtures/'summary.json').read_text('utf-8')),
        codeHashes={name: hashlib.sha256(assets['/modules/'+name][0]).hexdigest() for name in MODULES}, cases=[])
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel='msedge', headless=True)
            report['browserVersion'] = browser.version
            try:
                context = browser.new_context()

                def guard(route):
                    if not route.request.url.startswith(base+'/'):
                        external.append(route.request.url)
                        route.abort()
                    else:
                        route.continue_()

                context.route('**/*', guard)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base)
                page.wait_for_function('window.ready===true')
                for name in ('small', 'large'):
                    report['cases'].append(page.evaluate(MEASURE, name))
                    (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
                report['supersession'] = page.evaluate(SUPERSEDE)
                context.close()
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        report.update(externalRequests=external, pageErrors=errors, servedRequests=requests)
        (args.output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    assert not external and not errors
    assert all(row['method'] == 'GET' for row in requests)
    assert all(row['allRelationIdsPreserved'] for row in report['cases'])
    print(json.dumps(dict(cases=report['cases'], supersession=report['supersession'],
        pageErrors=errors, externalRequests=external), ensure_ascii=False))


if __name__ == '__main__':
    main()
