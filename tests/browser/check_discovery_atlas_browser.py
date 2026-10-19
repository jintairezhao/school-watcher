"""Real WebGL, recorded paths and task controls against isolated Flask state."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_discovery_monitor import DiscoveryMonitorTests
from backend.database.db import db
from backend.database.models import BackgroundTask, Department
from backend.services.runtime_catalog import pack
from playwright.sync_api import expect, sync_playwright


def check():
    output = ROOT / '.impeccable/review/discovery-atlas'
    output.mkdir(parents=True, exist_ok=True)
    scratch = ROOT / '.local/atlas-checks/tmp'
    scratch.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(scratch)
    report = []
    with tempfile.TemporaryDirectory(prefix='atlas-browser-') as temporary, \
            patch.dict(os.environ, {'WATCHER_DATA_DIR':temporary,
                'WATCHER_ENV_FILE':str(Path(temporary) / '.env'), 'FIELD_ENC_KEY':'isolated-atlas-key'}), \
            patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True,
            args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        fixture = DiscoveryMonitorTests(); fixture.setUp()
        try:
            base = fixture.school.url
            original_publish=fixture.publish
            def publish_linked(pages):
                original_publish(pages)
                with fixture.catalog.connect(write=True) as c:
                    for parent in pages:
                        records=[dict(node_key='identity',parent_key='',url=parent['url'],name=parent['label'],relation='page_identity')]
                        for child in pages:
                            if child.get('_parent_url')!=parent['url']:continue
                            records.append(dict(node_key=child['url'],parent_key='identity',url=child['url'],name=child['label'],
                                kind=child['kind'],relation='directory_entry' if parent['kind']=='directory' else 'navigation_entry',locator='nav a'))
                        c.execute('UPDATE catalog_pages SET nodes_gzip=? WHERE url=?',(pack(records),parent['url']))
            fixture.publish=publish_linked
            pages = [{'url':base, 'label':'学校官网', 'kind':'root', 'path_json':[], 'state':'fetched'}]
            pages += [dict(url=base+'/directory/'+key,label=label,kind='directory',path_json=['学校官网'],state='fetched',_parent_url=base) for key,label in [('org','组织机构'),('faculties','院系设置')]]
            for index, label in enumerate(['教务处','研究生院','学生工作处','招生就业','计算机学院','外国语学院','国际交流']):
                prefix = ['学校官网','组织机构' if index < 4 else '院系设置']
                pages.append({'url':base+'/unit/'+str(index), 'label':label, 'kind':'unit',
                    'path_json':prefix, '_parent_url':base+'/directory/'+('org' if index<4 else 'faculties'), 'state':'fetched', 'checked_at':'2026-10-18T03:00:00Z','status_code':200})
                for leaf, name in enumerate(['通知公告','教学安排','学生事务','办事指南','文件下载','培养工作']):
                    url = base+f'/unit/{index}/column/{leaf}'
                    pages.append({'url':url,'label':name,'kind':'channel','path_json':prefix+[label], '_parent_url':base+'/unit/'+str(index),
                        'state':'failed' if leaf == 4 and index == 0 else 'fetched',
                        'checked_at':'2026-10-18T03:10:00Z','status_code':503 if leaf == 4 and index == 0 else 200})
                    if leaf == 0:
                        db.session.add(Department(school_id=fixture.school.id, name=name,group_name=label,
                            list_url=url,list_selector='li'))
            pages.append({'url':base+'/unplaced','label':'路径未记录入口','kind':'channel','path_json':[], 'state':'observed'})
            fixture.publish(pages)
            task = fixture.task(state='running',lease_until=datetime.utcnow()+timedelta(hours=1),
                checkpoint={'discovery_progress':{'phase':'crawl','checked_pages':128,'pending_pages':12,
                    'updated_at':datetime.utcnow().isoformat()+'Z','current_label':'研究生院',
                    'current_pages':[{'url':base+'/unit/1','label':'研究生院','path_json':['学校官网','组织机构']}]}})
            db.session.commit()
            before = BackgroundTask.query.count()
            for width, theme in [(1586,'dark'),(1586,'light'),(390,'dark'),(390,'light')]:
                page = browser.new_page(viewport={'width':width,'height':992}, reduced_motion='reduce')
                errors, writes, requests = [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    request = route.request; parsed = urlsplit(request.url)
                    assert parsed.hostname == 'localhost', request.url
                    requests.append(parsed.path)
                    if parsed.path == '/_desktop/components':
                        return route.fulfill(json={'phase':'ready','message':'Ready'})
                    if request.method != 'GET': writes.append(parsed.path)
                    response = fixture.client.open(parsed.path+('?' + parsed.query if parsed.query else ''),
                        method=request.method, data=request.post_data,headers={key:value for key,value in request.headers.items()
                        if key.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                page.route('**/*',serve)
                page.add_init_script('localStorage.setItem("theme",'+json.dumps(theme)+')')
                page.goto('http://localhost'+fixture.page)
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready',timeout=30000)
                page.wait_for_function('() => document.getElementById("discoveryMonitor")?.discoveryAtlas?.model?.actual.size > 40')
                page.locator('#monitorAuto').uncheck()
                expect(page.locator('#monitorChecked')).to_have_text('128')
                expect(page.locator('#atlasViewport canvas')).to_have_count(1)
                page.locator('.school-heading p').evaluate("node => node.textContent = '隔离演示数据 · 实际界面'")
                count = page.locator('#discoveryMonitor').get_attribute('data-atlas-node-count')
                assert int(count) > 50
                assert page.evaluate('new Set([...discoveryMonitor.discoveryAtlas.model.entries.values()].map(n=>n.position.z)).size>5')
                page.wait_for_function('() => discoveryMonitor.dataset.atlasLabelCount!==undefined')
                assert 0<int(page.locator('#discoveryMonitor').get_attribute('data-atlas-label-count'))<=42
                assert page.evaluate('''() => {
                    const a=discoveryMonitor.discoveryAtlas,positions=a.scene.getObjectByName('atlasLinks').geometry.attributes.position;
                    // A link is one continuous segment; cross-links stay in the model.
                    return positions.count===a.model.links.length*2 && a.nodes.every(n=>n.record.url);
                }''')
                # Names are presentation preferences, not a filter on real nodes.
                expect(page.locator('#atlasNodeLabels')).to_be_checked()
                before_names=page.evaluate('''() => {const a=discoveryMonitor.discoveryAtlas;return {nodes:a.nodes.map(n=>n.id),positions:a.nodes.map(n=>n.position),camera:a.camera.position.toArray(),links:a.model.links.map(e=>e.id)}}''')
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasNodeLabels').uncheck()
                page.locator('#atlasOptions > summary').click()
                page.wait_for_function('''() => [...document.querySelectorAll('.atlas-node-label')].every(n=>n.hidden)''')
                assert page.evaluate('''() => {const a=discoveryMonitor.discoveryAtlas;return {nodes:a.nodes.map(n=>n.id),positions:a.nodes.map(n=>n.position),camera:a.camera.position.toArray(),links:a.model.links.map(e=>e.id)}}''')==before_names
                page.reload()
                page.wait_for_function('() => document.getElementById("discoveryMonitor")?.discoveryAtlas?.model?.actual.size>40')
                expect(page.locator('#atlasNodeLabels')).not_to_be_checked()
                # Saving another preference retains the hidden-name choice.
                page.locator('#atlasFollow').uncheck()
                assert page.evaluate('JSON.parse(localStorage.getItem("school-watcher:atlas:preferences")).nodeLabels===false')
                page.locator('#atlasFollow').check()
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.label==='研究生院').id)")
                selected_name=page.evaluate('discoveryMonitor.discoveryAtlas.current.id')
                expect(page.locator(f'[data-atlas-node="{selected_name}"]')).to_be_hidden()
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
                assert page.evaluate('''() => [...document.querySelectorAll('.atlas-node-label')].every(n=>n.hidden)''')
                page.locator('#atlasOverview').click()
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasNodeLabels').check()
                page.locator('#atlasOptions > summary').click()
                # Selected-path visibility is remembered; background tasks never choose a page.
                expect(page.locator('#atlasFollow')).to_be_visible()
                page.locator('#atlasFollow').check()
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current===null')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                page.reload()
                page.wait_for_function('() => document.getElementById("discoveryMonitor")?.discoveryAtlas?.model?.actual.size > 40')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#monitorAuto').uncheck()
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current===null')
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.label==='研究生院').id)")
                page.locator('.school-heading p').evaluate("node => node.textContent = '隔离演示数据 · 实际界面'")
                # Wheel gestures over either canvas or HTML labels never scroll the document.
                page.locator('#atlasStage').scroll_into_view_if_needed()
                canvas_point = page.locator('#atlasViewport').bounding_box()
                page.mouse.move(canvas_point['x']+canvas_point['width']*.55,canvas_point['y']+canvas_point['height']*.55)
                scroll_y=page.evaluate('scrollY')
                distance=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)')
                page.mouse.wheel(0,180)
                page.wait_for_timeout(200)
                assert abs(page.evaluate('scrollY')-scroll_y)<1
                assert page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)')!=distance
                page.locator('.atlas-node-label:visible').first.hover()
                scroll_y=page.evaluate('scrollY')
                page.mouse.wheel(0,-150)
                page.wait_for_timeout(200)
                assert abs(page.evaluate('scrollY')-scroll_y)<1
                expect(page.locator('#atlasFollow')).to_be_checked()
                # A real drag changes the camera, rather than moving a flat image.
                initial = page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                page.locator('#atlasViewport').scroll_into_view_if_needed()
                point = page.evaluate('''() => {
                    const canvas=document.querySelector('#atlasViewport canvas'), rect=canvas.getBoundingClientRect();
                    for(const [fx,fy] of [[.55,.75],[.6,.65],[.7,.8],[.4,.6]]) {
                        const x=rect.x+rect.width*fx,y=rect.y+rect.height*fy;
                        if(document.elementFromPoint(x,y)===canvas) return {x,y};
                    }
                    return null;
                }''')
                assert point, 'A clear canvas area is available for camera dragging'
                page.mouse.move(point['x'],point['y'])
                page.mouse.down();page.mouse.move(point['x']+75,point['y']+30,steps=8);page.mouse.up()
                assert initial != page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                expect(page.locator('#atlasFollow')).to_be_checked()
                # Middle drag translates camera and target together, without dollying or selecting.
                page.locator('#atlasOverview').click()
                before_pan=page.evaluate('''() => {const a=discoveryMonitor.discoveryAtlas;return {camera:a.camera.position.toArray(),target:a.controls.target.toArray(),distance:a.camera.position.distanceTo(a.controls.target)}}''')
                page.mouse.move(point['x'],point['y'])
                page.mouse.down(button='middle');page.mouse.move(point['x']+60,point['y']+25,steps=8);page.mouse.up(button='middle')
                after_pan=page.evaluate('''() => {const a=discoveryMonitor.discoveryAtlas;return {camera:a.camera.position.toArray(),target:a.controls.target.toArray(),distance:a.camera.position.distanceTo(a.controls.target)}}''')
                assert before_pan['target']!=after_pan['target']
                assert abs(before_pan['distance']-after_pan['distance'])<.01
                assert all(abs((after_pan['camera'][i]-before_pan['camera'][i])-(after_pan['target'][i]-before_pan['target'][i]))<.01 for i in range(3))
                label=page.locator('.atlas-node-label:visible').first
                label.hover()
                label_box=label.bounding_box()
                target_before=page.evaluate('discoveryMonitor.discoveryAtlas.controls.target.toArray()')
                page.mouse.move(label_box['x']+label_box['width']/2,label_box['y']+label_box['height']/2)
                page.mouse.down(button='middle');page.mouse.move(label_box['x']+label_box['width']/2+35,label_box['y']+label_box['height']/2+15,steps=6);page.mouse.up(button='middle')
                assert target_before!=page.evaluate('discoveryMonitor.discoveryAtlas.controls.target.toArray()')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#atlasOverview').click()
                # Manual selection shows the full evidenced upstream route; it has no sibling branches.
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='计算机学院').record.id)")
                expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-highlight','manual')
                expect(page.locator('#atlasFollow')).to_be_checked()
                route_info=page.evaluate("""() => {
                    const a=discoveryMonitor.discoveryAtlas,routes=a.scene.getObjectByName('atlasRoutes').children;
                    const manual=routes.filter(r=>r.userData.route==='manual');
                    return {ids:[...new Set(manual.map(r=>r.userData.entry))],current:a.current.id,
                        upstream:a.current.parent.id,live:routes.filter(r=>r.userData.route==='follow').length};
                }""")
                assert set(route_info['ids'])=={route_info['current'],route_info['upstream']},route_info
                assert route_info['live']==0,route_info
                expect(page.locator('#atlasPathEvidence')).to_contain_text('学校官网')
                page.locator('#atlasStage').screenshot(path=str(output/f'{theme}-{width}-manual-routes.png'))
                # Inspecting a node exits partial playback instead of hiding its fluorescent path.
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasScrubber').fill('0.35')
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='计算机学院').record.id)")
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1&&!discoveryMonitor.discoveryAtlas.playing')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#atlasOptions > summary').click()
                # Select a saved channel and inspect its real recorded ancestors.
                if width==1586 and theme=='dark':
                    # Interrupt a growing path and inspect another real page.
                    # Static highlights must meet the settled spheres afterward.
                    page.emulate_media(reduced_motion='no-preference')
                    page.locator('#atlasPlay').click()
                    page.wait_for_function('() => discoveryMonitor.discoveryAtlas.progress>.1&&discoveryMonitor.discoveryAtlas.progress<.8')
                    page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='计算机学院').id)")
                    assert page.evaluate('''() => {
                        const a=discoveryMonitor.discoveryAtlas;
                        return a.scene.getObjectByName('atlasRoutes').children.filter(r=>r.userData.route==='manual').every(r=>{
                            const end=r.localToWorld(a.camera.position.clone().set(0,.5,0)),n=a.model.actual.get(r.userData.entry);
                            return end.distanceTo(n.position)<.00001;
                        });
                    }''')
                    page.emulate_media(reduced_motion='reduce')
                page.evaluate('''() => {
                    const atlas=discoveryMonitor.discoveryAtlas;
                    const entry=[...atlas.model.actual.values()].find(n=>n.record.label==='通知公告'&&n.record.path.includes('研究生院'));
                    atlas.select(entry.record.id);
                }''')
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
                expect(page.locator('#atlasBreadcrumb')).to_contain_text('组织机构')
                assert page.evaluate('''() => {
                    const a=discoveryMonitor.discoveryAtlas;
                    const routes=a.scene.getObjectByName('atlasRoutes').children.filter(r=>r.userData.route==='manual');
                    return new Set(routes.map(r=>r.userData.entry)).size===3&&a.current.parent.label==='研究生院';
                }'''), 'A leaf highlights the entire recorded route from the university website'
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                page.locator('#atlasOptions > summary').click()
                page.locator('button[data-atlas-chapter="3"]').click()
                expect(page.locator('#atlasNarration')).to_have_attribute('data-stage','3')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)<170')
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasOverview').click()
                # Branch folding affects the graph while leaving the full accessible list intact.
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='研究生院').record.id)")
                page.locator('#atlasBranch').click()
                assert int(page.locator('#discoveryMonitor').get_attribute('data-atlas-node-count')) < int(count)
                page.locator('#atlasBranch').click()
                assert page.locator('#discoveryMonitor').get_attribute('data-atlas-node-count') == count
                page.locator('#monitorSearch').fill('路径未记录')
                expect(page.locator('.monitor-node')).to_have_count(1)
                assert page.evaluate('discoveryMonitor.discoveryAtlas.model.actual.get([...discoveryMonitor.discoveryAtlas.model.actual.keys()].find(k=>discoveryMonitor.discoveryAtlas.model.actual.get(k).label==="路径未记录入口")).parent===null')
                page.locator('#monitorSearch').fill('')
                page.locator('#atlasListPanel').evaluate('node => node.open=false')
                page.locator('#monitorFilter').select_option('failed')
                expect(page.locator('.monitor-node')).to_have_count(1)
                page.locator('#monitorFilter').select_option('all')
                page.locator('#atlasListPanel').evaluate('node => node.open=false')
                # Locate reveals a real target hidden by filters or a collapsed ancestor.
                page.locator('#monitorFilter').select_option('failed')
                page.locator('#atlasLocate').click()
                expect(page.locator('#monitorFilter')).to_have_value('all')
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='研究生院').parent.id)")
                page.locator('#atlasBranch').click()
                assert int(page.locator('#discoveryMonitor').get_attribute('data-atlas-node-count'))<int(count)
                page.locator('#atlasLocate').click()
                assert page.locator('#discoveryMonitor').get_attribute('data-atlas-node-count')==count
                # A just-completed task is still a useful real location between running steps.
                page.evaluate('''() => {
                    const snapshot=structuredClone(discoveryMonitor.monitorSnapshot);
                    snapshot.tasks=[];
                    for(const n of snapshot.nodes){n.tasks=[];if(n.state==='running')n.state='fetched';}
                    const target=snapshot.nodes.find(n=>n.label==='计算机学院');
                    target.state='connected';target.tasks=[{id:9000,state:'done',updated_at:'2099-01-01T00:00:00Z'}];
                    discoveryMonitor.monitorSnapshot=snapshot;
                    discoveryMonitor.dispatchEvent(new CustomEvent('monitor-data',{detail:snapshot}));
                }''')
                assert page.evaluate("discoveryMonitor.discoveryAtlas.current.record.label==='研究生院'"), 'New background work must not replace a selected page'
                page.locator('#atlasLocate').click()
                assert page.evaluate("discoveryMonitor.discoveryAtlas.current.record.label==='计算机学院'")
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                expect(page.locator('#atlasFollowStatus')).to_contain_text('所选路径')
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorDetail')).to_contain_text('计算机学院')
                # Tour is a local presentation, not a task control.
                page.locator('#atlasPlay').click()
                expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                expect(page.locator('#atlasFollow')).to_be_checked()
                selected_before=page.evaluate('discoveryMonitor.discoveryAtlas.current.id')
                camera_before=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                page.wait_for_function('() => !discoveryMonitor.discoveryAtlas.playing && discoveryMonitor.discoveryAtlas.progress===1',timeout=10000)
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id')==selected_before, 'Playback end must retain the selected page instead of jumping to the latest task'
                camera_after=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                assert max(abs(a-b) for a,b in zip(camera_before,camera_after))<1e-6,(camera_before,camera_after)
                expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','false')
                page.locator('#atlasOverview').click()
                # Repeated zoom out keeps actual sphere geometry visible and away from clipping.
                for _ in range(9): page.locator('#atlasZoomOut').click()
                page.wait_for_timeout(1200)
                assert page.evaluate('''() => {
                    const a=discoveryMonitor.discoveryAtlas;
                    return a.scene.fog===null && a.camera.far>a.camera.position.distanceTo(a.controls.target)
                        && a.nodes.every(n=>n.growth===1 && n.drawRadius>0);
                }''')
                page.locator('#atlasViewport').screenshot(path=str(output/f'{theme}-{width}-zoom.png'))
                page.locator('#atlasOverview').click()
                # No selected node means the whole saved graph grows continuously.
                page.emulate_media(reduced_motion='no-preference')
                page.locator('#atlasPlay').click()
                page.wait_for_function('() => discoveryMonitor.discoveryAtlas.progress>.1 && discoveryMonitor.discoveryAtlas.progress<.8')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current===null')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.nodes.some(n=>n.growth>0&&n.growth<1)')
                progress=page.evaluate('discoveryMonitor.discoveryAtlas.progress')
                # Wait for a subsequent intermediate frame. A fixed wall-clock
                # delay can observe the same frame on software-rendered CI hosts.
                page.wait_for_function('''previous => {
                    const atlas=discoveryMonitor.discoveryAtlas;
                    return atlas.playing && atlas.progress>previous && atlas.progress<1;
                }''', arg=progress, timeout=5000)
                page.locator('#atlasPlay').click()
                page.locator('#atlasOverview').click()
                page.emulate_media(reduced_motion='reduce')
                # Layout settings stay in the top-right popover; spacing really changes positions.
                page.locator('#atlasOptions > summary').click()
                expect(page.locator('#atlasOptions .atlas-options-panel')).to_be_visible()
                old_layout=page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>n.position.x)')
                page.locator('#atlasSpacing').fill('1.6')
                page.locator('#atlasSpacing').dispatch_event('change')
                assert old_layout!=page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>n.position.x)')
                page.locator('#atlasSpacing').fill('1')
                page.locator('#atlasSpacing').dispatch_event('change')
                page.locator('#atlasOptions > summary').click()
                if width==1586 and theme=='dark':
                    # New bridge evidence inserts a bubble between retained pages.
                    changed=[dict(item) for item in pages]
                    for item in changed:
                        if item['url']==base+'/unit/0' or item['url'].startswith(base+'/unit/0/'):
                            path=item['path_json'];item['path_json']=path[:2]+['新增导航桥梁']+path[2:]
                    next(item for item in changed if item['url']==base+'/unit/0')['_parent_url']=base+'/bridge'
                    changed.append({'url':base+'/bridge','label':'新增导航桥梁','kind':'navigation','path_json':['学校官网','组织机构'],'state':'fetched','_parent_url':base+'/directory/org'})
                    page.emulate_media(reduced_motion='no-preference')
                    fixture.publish(changed);page.locator('#monitorRefresh').click()
                    page.wait_for_function("() => discoveryMonitor.discoveryAtlas.nodes.some(n=>n.label==='新增导航桥梁'&&n.baseScale>0&&n.baseScale<1)")
                    page.wait_for_timeout(900)
                    assert page.evaluate("discoveryMonitor.discoveryAtlas.nodes.find(n=>n.label==='新增导航桥梁').baseScale===1")
                    fixture.publish(pages);page.locator('#monitorRefresh').click()
                    page.emulate_media(reduced_motion='reduce')
                # A close, unselected leaf still has its identity label; it is not restricted to a global shortlist.
                page.locator('#atlasOverview').click()
                leaf_id=page.evaluate("""() => {
                    const a=discoveryMonitor.discoveryAtlas,n=a.nodes.find(n=>n.record.url.endsWith('/unit/6/column/5'));
                    a.controls.target.copy(n.position);a.camera.position.copy(n.position);a.camera.position.z+=180;
                    a.controls.update();a.controls.dispatchEvent({type:'change'});return n.id;
                }""")
                expect(page.locator(f'[data-atlas-node="{leaf_id}"]')).to_be_visible()
                page.locator('#atlasStage').screenshot(path=str(output/f'{theme}-{width}-leaf-information.png'))
                page.locator('#atlasOverview').click()
                # Existing theme switch recolors the same WebGL scene.
                page.locator('#themeToggle').click()
                flipped = 'light' if theme=='dark' else 'dark'
                expect(page.locator('html')).to_have_attribute('data-theme',flipped)
                assert page.evaluate('discoveryMonitor.discoveryAtlas.renderer.domElement.width>0')
                page.locator('#themeToggle').click()
                page.locator('#atlasFullscreen').click()
                expect(page.locator('#atlasFullscreen')).to_have_attribute('aria-pressed','true')
                page.wait_for_function("document.fullscreenElement===document.querySelector('.monitor-workspace')")
                expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-fullscreen','native')
                page.wait_for_function('''() => {
                    const r=atlasStage.getBoundingClientRect();
                    return r.x===0&&r.y===0&&r.width===innerWidth&&r.height===innerHeight;
                }''')
                expect(page.locator('#atlasSearchPanel')).to_be_hidden()
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                expect(page.locator('#monitorMapHeading')).to_be_hidden()
                expect(page.locator('#atlasFollow')).to_be_checked()
                # Exercise real sphere picking with text hidden, rather than only
                # selecting through the diagnostics used by the other checks.
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasNodeLabels').uncheck()
                page.locator('#atlasOptions > summary').click()
                point=page.evaluate('''async () => {
                    const a=discoveryMonitor.discoveryAtlas,T=await import('/static/vendor/three/three.module.min.js');
                    a.camera.updateMatrixWorld(true);
                    const canvas=a.renderer.domElement,rect=canvas.getBoundingClientRect();
                    const mesh=a.scene.children.flatMap(g=>g.children).find(o=>o.isInstancedMesh);
                    for(const n of a.nodes.filter(n=>n.record.kind==='unit')) {
                        const p=new T.Vector3().copy(n.renderPosition).project(a.camera);
                        const x=rect.x+(p.x+1)*rect.width/2,y=rect.y+(1-p.y)*rect.height/2;
                        if(p.z<0||p.z>1||document.elementFromPoint(x,y)!==canvas)continue;
                        const ray=new T.Raycaster();ray.setFromCamera(new T.Vector2(p.x,p.y),a.camera);
                        const saved=mesh.boundingSphere;mesh.boundingSphere=null;
                        const hit=ray.intersectObject(mesh,false)[0];mesh.boundingSphere=saved;
                        if(hit&&a.nodes[hit.instanceId]===n)return {id:n.id,x,y};
                    }
                    return null;
                }''')
                assert point,'A genuine unobstructed sphere is available'
                camera_before=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                page.mouse.click(point['x'],point['y'])
                page.wait_for_function('id=>discoveryMonitor.discoveryAtlas.current?.id===id',arg=point['id'])
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                assert page.evaluate('''() => {const g=discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasRoutes');return g.visible&&g.children.some(n=>n.userData.selection);}''')
                assert max(abs(a-b) for a,b in zip(camera_before,page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')))<.000001
                page.locator('#atlasOptions > summary').click();page.locator('#atlasNodeLabels').check();page.locator('#atlasOptions > summary').click()
                page.locator('#atlasOverview').click()
                page.screenshot(path=str(output/f'{theme}-{width}-fullscreen.png'))
                page.locator('#atlasSearchToggle').click()
                expect(page.locator('#monitorSearch')).to_be_focused()
                page.locator('#monitorSearch').fill('研究生院')
                page.locator('#monitorSearch').fill('')
                page.locator('#atlasInspect').click()
                expect(page.locator('#atlasSearchPanel')).to_be_hidden()
                expect(page.locator('#atlasDetailPanel')).to_be_visible()
                page.locator('#atlasDetailClose').click()
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                page.keyboard.press('Escape')
                expect(page.locator('#atlasFullscreen')).to_have_attribute('aria-pressed','false')
                assert page.evaluate('document.fullscreenElement===null')
                # Hosts without the API still fill the window and describe that fallback accurately.
                page.evaluate("document.querySelector('.monitor-workspace').requestFullscreen=undefined")
                page.locator('#atlasFullscreen').click()
                expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-fullscreen','window')
                expect(page.locator('#atlasFullscreen')).to_have_text('退出沉浸')
                page.wait_for_function('''() => {
                    const r=atlasStage.getBoundingClientRect();
                    return !document.fullscreenElement&&r.x===0&&r.y===0&&r.width===innerWidth&&r.height===innerHeight;
                }''')
                page.locator('#atlasFullscreen').click()
                expect(page.locator('#atlasFullscreen')).to_have_attribute('aria-pressed','false')
                expect(page.locator('#atlasSearchPanel')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                assert not writes and BackgroundTask.query.count()==before
                assert not errors, errors
                page.evaluate('scrollTo(0,0)')
                page.screenshot(path=str(output/f'{theme}-{width}.png'),full_page=True)
                page.locator('#atlasStage').screenshot(path=str(output/f'{theme}-{width}-scene.png'))
                # Simulate GPU loss; the accessible tree remains functional.
                page.evaluate('document.querySelector("#atlasViewport canvas").dispatchEvent(new Event("webglcontextlost",{cancelable:true}))')
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','unavailable')
                expect(page.locator('#atlasListPanel')).to_have_attribute('open','')
                page.locator('#themeToggle').click()
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','unavailable')
                expect(page.locator('#atlasPlay')).to_be_disabled()
                changed = [dict(item) for item in pages]; changed[-1]['state'] = 'fetched'
                fixture.publish(changed)
                page.locator('#monitorRefresh').click()
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','unavailable')
                expect(page.locator('#atlasZoomIn')).to_be_disabled()
                expect(page.locator('#atlasListPanel')).to_have_attribute('open','')
                fixture.publish(pages)
                report.append({'width':width,'theme':theme,'sphere_nodes':int(count),'real_webgl':True,
                    'rotation_and_zoom':True,'theme_switch':True,'branch_fold':True,'real_evidence':True,
                    'local_tour_no_writes':True,'gpu_loss_fallback':True,'remembered_path_visibility':True,
                    'wheel_contained':True,'filtered_target_locate':True,'top_right_controls':True,
                    'branch_spacing':True,'full_evidenced_selected_route':True,'playback_retains_selection_and_camera':True,'close_leaf_label':True,'middle_drag_pan':True,
                    'native_fullscreen':True,'fullscreen_optional_panels':True,'window_fullscreen_fallback':True,'page_errors':errors})
                page.close()
            # Many branches must remain legible without dropping or flattening nodes.
            dense=[{'url':base,'label':'学校官网','kind':'root','path_json':[],'state':'fetched'}]
            dense.append(dict(url=base+'/dense-directory',label='院系设置',kind='directory',path_json=['学校官网'],state='fetched',_parent_url=base))
            for unit in range(40):
                label=f'院系 {unit+1:02d}'
                dense.append({'url':base+f'/dense/{unit}','label':label,'kind':'unit','path_json':['学校官网','院系设置'],'state':'fetched','_parent_url':base+'/dense-directory'})
                for leaf in range(28):
                    dense.append({'url':base+f'/dense/{unit}/{leaf}','label':f'信息栏目 {leaf+1:02d}','kind':'channel','path_json':['学校官网','院系设置',label],'state':'fetched','_parent_url':base+f'/dense/{unit}'})
            fixture.publish(dense)
            for width in [1586,390]:
                page=browser.new_page(viewport={'width':width,'height':992},reduced_motion='reduce')
                page.route('**/*',serve)
                page.add_init_script('localStorage.setItem("theme","dark")')
                page.goto('http://localhost'+fixture.page)
                page.wait_for_function('() => document.getElementById("discoveryMonitor")?.discoveryAtlas?.model?.actual.size>1100')
                page.locator('#monitorAuto').uncheck()
                page.locator('#atlasOverview').click()
                page.evaluate("scrollBy(0,document.getElementById('atlasStage').getBoundingClientRect().top-76)")
                page.locator('#atlasStage').screenshot(path=str(output/f'dense-{width}-scene.png'))
                assert page.evaluate('discoveryMonitor.discoveryAtlas.model.actual.size>=1161')
                assert page.evaluate('new Set(discoveryMonitor.discoveryAtlas.nodes.map(n=>n.cluster)).size>=40')
                assert int(page.locator('#discoveryMonitor').get_attribute('data-atlas-label-count'))<=42
                assert not writes and BackgroundTask.query.count()==before
                page.close()
            # The initial empty scene must not display demonstration nodes.
            fixture.publish([])
            for source in Department.query.all(): source.list_selector = None
            db.session.delete(task); db.session.commit()
            page = browser.new_page(viewport={'width':1440,'height':900})
            page.route('**/*',serve)
            page.goto('http://localhost'+fixture.page)
            expect(page.locator('#atlasStatus')).to_contain_text('还没有已记录',timeout=30000)
            assert page.evaluate('discoveryMonitor.discoveryAtlas.model.entries.size===0')
            page.close()
        finally:
            fixture.tearDown(); browser.close()
    (output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':
    check()
