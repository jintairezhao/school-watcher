"""Position-only WebGL comparison on a hash-verified, retained website snapshot.

No discovery, provider, source installation, or production database is used.
The application shell comes from an isolated fixture; its snapshot response is
replaced with the original retained API bytes. Only the model's positions are
overridden in the browser. This is a scripted lookup check, not a human study.
"""
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
from urllib.parse import urlsplit
import argparse
import hashlib
import json
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from test_structure_snapshot import StructureSnapshotTests
from playwright.sync_api import sync_playwright, expect


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


MEASURE = r"""() => {
    const root=document.getElementById('discoveryMonitor'),a=root.discoveryAtlas;
    const rect=n=>{const r=n.getBoundingClientRect();return {x:r.x,y:r.y,w:r.width,h:r.height};};
    const intersection=(a,b)=>Math.max(0,Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x))
        *Math.max(0,Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y));
    const rows=a.nodes.filter(n=>n.labelElement&&!n.labelElement.hidden).map(n=>({
        id:n.id,label:n.label,type:n.record.entity_type,box:rect(n.labelElement),
        ellipsized:n.labelElement.scrollWidth>n.labelElement.clientWidth+1,
        hitTarget:(()=>{const r=rect(n.labelElement);return document.elementFromPoint(r.x+r.w/2,r.y+r.h/2)===n.labelElement;})()}));
    const overlaps=[];
    for(let i=0;i<rows.length;i++)for(let j=i+1;j<rows.length;j++){
        const area=intersection(rows[i].box,rows[j].box);
        if(area>.5)overlaps.push({a:rows[i].id,b:rows[j].id,area});
    }
    const viewport=rect(document.getElementById('atlasViewport'));
    a.camera.updateMatrixWorld(true);
    const outside=a.nodes.filter(n=>{const p=a.camera.position.clone().copy(n.renderPosition||n.position).project(a.camera);
        return Math.abs(p.x)>1||Math.abs(p.y)>1||p.z< -1||p.z>1;}).map(n=>n.id);
    const sphereOverlaps=[];
    for(let i=0;i<a.nodes.length;i++)for(let j=i+1;j<a.nodes.length;j++){
        const first=a.nodes[i],second=a.nodes[j],p=first.renderPosition||first.position,q=second.renderPosition||second.position;
        const minimum=(first.drawRadius||first.radius)+(second.drawRadius||second.radius);
        if(Math.hypot(p.x-q.x,p.y-q.y,p.z-q.z)<minimum-1e-6)sphereOverlaps.push([first.id,second.id]);
    }
    return {visibleLabels:rows.length,byType:rows.reduce((o,n)=>(o[n.type]=(o[n.type]||0)+1,o),{}),
        overlaps,ellipsized:rows.filter(n=>n.ellipsized).map(n=>n.id),
        obstructed:rows.filter(n=>!n.hitTarget).map(n=>n.id),labels:rows,
        viewport,sceneNodes:a.nodes.length,sceneRelations:a.model.links.length,
        selected:a.current?.id||null,detailOpen:root.classList.contains('atlas-details-open'),
        pageOverflow:document.documentElement.scrollWidth>innerWidth+1,
        offscreenNodeCenters:outside,
        physicalSphereOverlaps:sphereOverlaps,
        camera:a.camera.position.toArray(),target:a.controls.target.toArray()};
}"""


def settle(page):
    # Wait for the real renderer, including its HTML label pass, to draw the
    # requested camera/state change. A frame count is observable completion.
    frame = page.evaluate("discoveryMonitor.discoveryAtlas.renderer.info.render.frame")
    page.evaluate("discoveryMonitor.discoveryAtlas.controls.dispatchEvent({type:'change'})")
    page.wait_for_function("before=>discoveryMonitor.discoveryAtlas.renderer.info.render.frame>before", arg=frame)


def check(package, comparison, snapshot_file, output, require_overview_fit=False, variants=None, lookup_ids=None,
          require_physical_nonoverlap=False):
    package, comparison, snapshot_file, output = map(Path, (package, comparison, snapshot_file, output))
    assert not output.exists(), 'Preserve prior observations; choose a new output directory'
    manifest = json.loads((package/'manifest.json').read_text('utf-8'))
    for name, expected in manifest['files'].items():
        assert digest(package/name) == expected, name
    artifacts = json.loads((comparison/'artifact-hashes.json').read_text('utf-8'))
    for name, expected in artifacts.items():
        assert digest(comparison/name) == expected, name
    frozen_input = json.loads((package/'layout-input.json').read_text('utf-8'))
    snapshot_raw = snapshot_file.read_bytes()
    snapshot = json.loads(snapshot_raw)
    assert snapshot['nodes'] == frozen_input['nodes']
    assert snapshot['relations'] == frozen_input['relations']
    assert len(snapshot['nodes']) == 241 and len(snapshot['relations']) == 902
    model = (package/'current-model.js').read_text('utf-8')
    assert digest(ROOT/'frontend/static/js/discovery-atlas-model.js') == manifest['files']['current-model.js']
    assert model.count('export function buildAtlas(') == 1
    variants = tuple(variants or ('current', 'fixed-membership', 'explicit-directory-display'))
    assert variants and all(name and all(c.isalnum() or c=='-' for c in name) for name in variants)
    targets = []
    # Fixed real identities cover directory, organization, and source entities.
    # Repeated titles never identify a different node by name alone.
    for kind, label in (('directory_group', '院系设置'), ('organization', '数理与人工智能学院')):
        targets.append(next(n for n in snapshot['nodes'] if n['entity_type'] == kind and n['label'] == label))
    targets.append(next(n for n in snapshot['nodes'] if n['id'] == 'source:9'))
    for ident in lookup_ids or []:
        target=next(n for n in snapshot['nodes'] if n['id']==ident)
        if all(existing['id']!=ident for existing in targets):targets.append(target)
    tracked = ['frontend/static/js/discovery-atlas.js', 'frontend/static/js/discovery-atlas-view.js',
               'frontend/static/js/discovery-atlas-model.js', 'frontend/static/css/discovery-monitor.css',
               'frontend/templates/discovery_monitor.html', 'tests/browser/check_layout_comparison_browser.py']
    source_hashes = {name: digest(ROOT/name) for name in tracked}
    output.mkdir(parents=True)
    report = dict(snapshot_sha256=digest(snapshot_file), graph_sha256=digest(package/'layout-input.json'),
        code_manifest_sha256=digest(package/'manifest.json'), comparison_manifest_sha256=digest(comparison/'artifact-hashes.json'),
        source_hashes=source_hashes, input_node_types=dict(Counter(n['entity_type'] for n in snapshot['nodes'])),
        input_relation_types=dict(Counter(n['type'] for n in snapshot['relations'])),
        scope='One retained development-school snapshot; original renderer and common product radii/colors; positions only',
        limitations=['Not independent website acceptance', 'Not a human lookup or preference study',
                     'Frozen positions are precomputed; not a runtime layout performance measurement',
                     'No validation of prototype node-size or group-color integration',
                     'Visible label collision is measured; concealed labels are counted, not assumed readable'],
        official_website_requests=0, model_calls=0, product_layout_promoted=False, results=[])
    fixture = StructureSnapshotTests()
    fixture.setUp()
    try:
        with ExitStack() as guards:
            guards.enter_context(patch('requests.sessions.Session.request', side_effect=AssertionError('Offline comparison')))
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel='msedge', headless=True,
                    args=['--enable-webgl', '--use-angle=swiftshader', '--enable-unsafe-swiftshader'])
                report['browser_version'] = browser.version
                try:
                    for variant in variants:
                        positions = json.loads((comparison/(variant+'-positions.json')).read_text('utf-8'))
                        assert set(positions) == {n['id'] for n in snapshot['nodes']}
                        injected = model.replace('export function buildAtlas(', 'function productionBuildAtlas(') + '\n' + (
                            'const frozenPositions=' + json.dumps(positions, separators=(',', ':')) + ';\n'
                            'export function buildAtlas(...args){const model=productionBuildAtlas(...args);'
                            'for(const [id,node] of model.entries){if(!frozenPositions[id])throw new Error("Unfrozen identity");'
                            'node.position={...frozenPositions[id]};}return model;}\n')
                        for width in (1440, 390):
                            page = browser.new_page(viewport={'width':width, 'height':1000}, reduced_motion='reduce')
                            errors, writes, blocked, requests = [], [], [], []
                            page.on('pageerror', lambda error:errors.append(str(error)))
                            page.add_init_script("localStorage.setItem('school-watcher:theme','dark');")

                            def serve(route):
                                req, url = route.request, urlsplit(route.request.url)
                                requests.append(dict(method=req.method, path=url.path))
                                if url.hostname != 'localhost':
                                    blocked.append(req.url)
                                    return route.abort()
                                if req.method != 'GET':
                                    writes.append(dict(method=req.method, path=url.path))
                                    return route.fulfill(status=405, body='Read-only comparison')
                                if url.path.endswith('/structure-snapshot'):
                                    return route.fulfill(content_type='application/json', body=snapshot_raw)
                                if url.path == '/static/js/discovery-atlas-model.js':
                                    return route.fulfill(content_type='text/javascript', body=injected)
                                if url.path.startswith('/_desktop/'):
                                    return route.fulfill(json={'phase':'ready'})
                                response = fixture.client.get(url.path+('?' + url.query if url.query else ''))
                                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

                            page.route('**/*', serve)
                            page.goto('http://localhost'+fixture.fixture.page)
                            expect(page.locator('#atlasStage')).to_have_attribute('data-renderer', 'ready')
                            page.wait_for_function('discoveryMonitor.discoveryAtlas?.nodes.length===241')
                            page.locator('#monitorAuto').uncheck()
                            # Theme/locale presentation changes stay entirely in
                            # this page; no preferences POST is needed here.
                            page.evaluate("document.documentElement.dataset.theme='dark';I18n.setLocale('zh-CN')")
                            page.locator('#atlasFullscreen').click()
                            expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                            settle(page)
                            entry = dict(variant=variant, width=width, views=[], lookup=[])
                            entry['initialOverview'] = page.evaluate(MEASURE)
                            if require_overview_fit and entry['initialOverview']['offscreenNodeCenters']:
                                (output/'fit-failure.json').write_text(json.dumps(entry, ensure_ascii=False, indent=2), 'utf-8')
                                raise AssertionError(('Fullscreen overview clips node centers', variant, width,
                                    len(entry['initialOverview']['offscreenNodeCenters'])))
                            for locale in ('zh-CN', 'en'):
                                page.evaluate('(locale)=>I18n.setLocale(locale)', locale)
                                for view, direction in (('front', [.03,.035,1]), ('side', [1,0,0]), ('top', [0,1,.001])):
                                    page.evaluate('''direction=>{const a=discoveryMonitor.discoveryAtlas;
                                        const distance=a.camera.position.distanceTo(a.controls.target);
                                        const magnitude=Math.hypot(...direction);
                                        a.camera.position.set(...direction.map((n,i)=>a.controls.target.getComponent(i)+n/magnitude*distance));
                                        a.camera.lookAt(a.controls.target);a.controls.update();}''', direction)
                                    settle(page)
                                    entry['views'].append(dict(locale=locale, view=view, **page.evaluate(MEASURE)))
                                    if locale == 'zh-CN' and view == 'front':
                                        page.screenshot(path=str(output/f'{variant}-{width}.png'))
                            # Use real controls: search narrows the scene, then
                            # click a visible label or the projected real sphere.
                            # The list is hidden in immersive mode; do not force
                            # a click on an invisible list button or select by API.
                            page.locator('#atlasSearchToggle').click()
                            for target in targets:
                                page.locator('#monitorSearch').fill(target['label'])
                                page.locator('#atlasSearchToggle').click()
                                settle(page)
                                button = page.locator(f'.atlas-node-label[data-atlas-node="{target["id"]}"]')
                                method = 'visible_label'
                                if button.is_visible():
                                    button.click()
                                else:
                                    point = page.evaluate('''async id=>{
                                        const a=discoveryMonitor.discoveryAtlas,n=a.nodes.find(n=>n.id===id);
                                        if(!n)return null;
                                        const T=await import('/static/vendor/three/three.module.min.js');
                                        const {pickSphere}=await import('/static/js/discovery-atlas-picking.js');
                                        a.camera.updateMatrixWorld(true);
                                        const p=new T.Vector3().copy(n.renderPosition).project(a.camera);
                                        const r=a.renderer.domElement.getBoundingClientRect();
                                        const x=r.x+(p.x+1)*r.width/2,y=r.y+(1-p.y)*r.height/2;
                                        if(p.z< -1||p.z>1||document.elementFromPoint(x,y)!==a.renderer.domElement)return null;
                                        const ray=new T.Raycaster();ray.setFromCamera(new T.Vector2(p.x,p.y),a.camera);
                                        const hit=pickSphere(a.nodes,{origin:ray.ray.origin,direction:ray.ray.direction,
                                            near:a.camera.near,far:a.camera.far,
                                            pixelFactor:2*Math.tan(T.MathUtils.degToRad(a.camera.fov/2))/r.height,
                                            minimumPixels:4});
                                        return hit===n?{x,y}:null;
                                    }''', target['id'])
                                    method = 'sphere'
                                    if point:
                                        page.mouse.click(point['x'], point['y'])
                                    else:
                                        entry['lookup'].append(dict(id=target['id'],label=target['label'],
                                            entity_type=target['entity_type'],result='unreachable_after_search',
                                            selected_view=page.evaluate(MEASURE)))
                                        page.locator('#atlasSearchToggle').click()
                                        continue
                                page.wait_for_function('id=>discoveryMonitor.discoveryAtlas.current?.id===id', arg=target['id'])
                                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                                settle(page)
                                selected = page.evaluate(MEASURE)
                                page.locator('#atlasInspect').click()
                                expect(page.locator('#monitorDetail h3').first).to_have_text(target['label'])
                                route = page.locator('#atlasPathEvidence').inner_text()
                                page.locator('#atlasDetailClose').click()
                                entry['lookup'].append(dict(id=target['id'], label=target['label'],
                                    entity_type=target['entity_type'], result='selected_exact_identity',
                                    click_method=method,
                                    selected_label_visible=target['id'] in {n['id'] for n in selected['labels']},
                                    detail_opened_only_on_request=True, path_text=route, selected_view=selected))
                                page.locator('#atlasSearchToggle').click()
                            if require_overview_fit:
                                # Selected/local views preserve the camera;
                                # returning to overview resumes aspect fitting.
                                before = page.evaluate('''()=>{const a=discoveryMonitor.discoveryAtlas;
                                    return {camera:a.camera.position.toArray(),target:a.controls.target.toArray(),id:a.current?.id};}''')
                                page.set_viewport_size({'width': width+80, 'height':900})
                                settle(page)
                                after = page.evaluate('''()=>{const a=discoveryMonitor.discoveryAtlas;
                                    return {camera:a.camera.position.toArray(),target:a.controls.target.toArray(),id:a.current?.id};}''')
                                assert before['id'] == after['id']
                                assert all(abs(a-b)<1e-6 for key in ('camera','target') for a,b in zip(before[key],after[key])), \
                                    'Resizing must preserve the manually selected camera'
                                # The search panel was left open by the loop.
                                page.locator('#monitorSearch').fill('')
                                page.locator('#atlasSearchToggle').click()
                                page.locator('#atlasOverview').click()
                                settle(page)
                                page.set_viewport_size({'width': max(320,width//2), 'height':900})
                                settle(page)
                                resized = page.evaluate(MEASURE)
                                entry['resizedOverview'] = resized
                                if resized['offscreenNodeCenters']:
                                    (output/'fit-failure.json').write_text(json.dumps(entry,ensure_ascii=False,indent=2),'utf-8')
                                    raise AssertionError(('Resized overview clips node centers',variant,width,
                                        len(resized['offscreenNodeCenters'])))
                            assert not errors, errors
                            assert not writes, writes
                            assert not blocked, blocked
                            if require_physical_nonoverlap:
                                assert all(not view['physicalSphereOverlaps'] for view in entry['views']), \
                                    (variant,width,'Actual rendered sphere overlap',entry['views'])
                            assert page.evaluate('discoveryMonitor.monitorSnapshot.source_count') == 89
                            entry.update(errors=errors, writes=writes, blocked=blocked, request_count=len(requests))
                            report['results'].append(entry)
                            (output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
                            print(json.dumps(dict(variant=variant, width=width,
                                labels=[v['visibleLabels'] for v in entry['views']],
                                overlaps=sum(len(v['overlaps']) for v in entry['views']),
                                obstructed=sum(len(v['obstructed']) for v in entry['views']),
                                lookup=[t['result'] for t in entry['lookup']]), ensure_ascii=False), flush=True)
                            page.close()
                finally:
                    browser.close()
    finally:
        fixture.tearDown()
    assert {name:digest(ROOT/name) for name in tracked} == source_hashes
    assert digest(snapshot_file) == report['snapshot_sha256']
    (output/'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', 'utf-8')
    (output/'artifact-hashes.json').write_text(json.dumps({p.name:digest(p) for p in output.iterdir()
        if p.is_file() and p.name != 'artifact-hashes.json'}, indent=2)+'\n', 'utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--package', required=True)
    parser.add_argument('--comparison', required=True)
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--require-overview-fit', action='store_true')
    parser.add_argument('--variants', nargs='+')
    parser.add_argument('--lookup-node-ids', nargs='+')
    parser.add_argument('--require-physical-nonoverlap', action='store_true')
    options = parser.parse_args()
    check(options.package, options.comparison, options.snapshot, options.output, options.require_overview_fit,
          options.variants, options.lookup_node_ids, options.require_physical_nonoverlap)
