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
            pages = [{'url':base, 'label':'学校官网', 'kind':'root', 'path_json':[], 'state':'fetched'}]
            for index, label in enumerate(['教务处','研究生院','学生工作处','招生就业','计算机学院','外国语学院','国际交流']):
                prefix = ['学校官网','组织机构' if index < 4 else '院系设置']
                pages.append({'url':base+'/unit/'+str(index), 'label':label, 'kind':'unit',
                    'path_json':prefix, 'state':'fetched', 'checked_at':'2026-10-18T03:00:00Z','status_code':200})
                for leaf, name in enumerate(['通知公告','教学安排','学生事务','办事指南','文件下载','培养工作']):
                    url = base+f'/unit/{index}/column/{leaf}'
                    pages.append({'url':url,'label':name,'kind':'channel','path_json':prefix+[label],
                        'state':'failed' if leaf == 4 and index == 0 else 'fetched',
                        'checked_at':'2026-10-18T03:10:00Z','status_code':503 if leaf == 4 and index == 0 else 200})
                    if leaf == 0:
                        db.session.add(Department(school_id=fixture.school.id, name=name,group_name=label,
                            list_url=url,list_selector='li'))
            pages.append({'url':base+'/unplaced','label':'路径未记录入口','kind':'navigation','path_json':[], 'state':'observed'})
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
                # Following is an independent remembered preference, not an animation stage.
                expect(page.locator('#atlasFollow')).to_be_visible()
                page.locator('#atlasFollow').check()
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                page.reload()
                page.wait_for_function('() => document.getElementById("discoveryMonitor")?.discoveryAtlas?.model?.actual.size > 40')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#monitorAuto').uncheck()
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
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
                # Manual paths use violet while the independently followed path stays blue.
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='计算机学院').record.id)")
                expect(page.locator('#discoveryMonitor')).to_have_attribute('data-atlas-highlight','manual')
                expect(page.locator('#atlasFollow')).to_be_checked()
                route_info=page.evaluate('''() => {
                    const routes=discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasRoutes').children;
                    const manual=routes.filter(r=>r.userData.route==='manual'),live=routes.filter(r=>r.userData.route==='follow');
                    const current=discoveryMonitor.discoveryAtlas.current;
                    const expected=[current,...current.children].map(n=>n.id).sort();
                    const camera=discoveryMonitor.discoveryAtlas.camera;
                    const inFrame=[current.parent,current,...current.children].filter(Boolean).every(n=>{
                        const p=camera.position.clone().copy(n.position).project(camera);return Math.abs(p.x)<.95&&Math.abs(p.y)<.95&&p.z<1;
                    });
                    return {manual:manual.length,live:live.length,manualColor:manual[0]?.material.color.toArray(),liveColor:live[0]?.material.color.toArray(),
                        actual:manual.map(r=>r.userData.entry).sort(),expected,inFrame};
                }''')
                assert route_info['manual']==7 and route_info['live']>0,route_info
                assert route_info['actual']==route_info['expected'],route_info
                assert route_info['inFrame'], 'Both the immediate parent and children must fit in the selected view'
                assert route_info['manualColor']!=route_info['liveColor'],route_info
                page.locator('#atlasStage').screenshot(path=str(output/f'{theme}-{width}-manual-routes.png'))
                # Inspecting a node exits partial playback instead of hiding its fluorescent path.
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasScrubber').fill('0.35')
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.label==='计算机学院').record.id)")
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1&&!discoveryMonitor.discoveryAtlas.playing')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#atlasOptions > summary').click()
                # Select a saved channel and inspect its real recorded ancestors.
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
                    return routes.length===1&&routes[0].userData.entry===a.current.id&&a.current.parent.label==='研究生院';
                }'''), 'A leaf highlights its direct parent only, without upstream ancestors'
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                page.locator('#atlasOptions > summary').click()
                page.locator('button[data-atlas-chapter="3"]').click()
                expect(page.locator('#atlasNarration')).to_have_attribute('data-stage','3')
                assert page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)<170')
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
                expect(page.locator('#atlasFollowStatus')).to_contain_text('筛选隐藏')
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
                assert page.evaluate("discoveryMonitor.discoveryAtlas.current.record.label==='计算机学院'")
                assert page.evaluate('discoveryMonitor.discoveryAtlas.progress===1')
                expect(page.locator('#atlasFollowStatus')).to_contain_text('最近进展')
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorDetail')).to_contain_text('研究生院')
                # Tour is a local presentation, not a task control.
                page.locator('#atlasPlay').click()
                expect(page.locator('#atlasPlay')).to_have_attribute('aria-pressed','true')
                expect(page.locator('#atlasFollow')).to_be_checked()
                page.locator('#atlasOptions > summary').click()
                page.locator('#atlasScrubber').fill('1')
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
                    changed.append({'url':base+'/bridge','label':'新增导航桥梁','kind':'navigation','path_json':['学校官网','组织机构'],'state':'fetched'})
                    page.emulate_media(reduced_motion='no-preference')
                    fixture.publish(changed);page.locator('#monitorRefresh').click()
                    page.wait_for_function("() => discoveryMonitor.discoveryAtlas.nodes.some(n=>n.label==='新增导航桥梁'&&n.baseScale>0&&n.baseScale<1)")
                    page.wait_for_timeout(900)
                    assert page.evaluate("discoveryMonitor.discoveryAtlas.nodes.find(n=>n.label==='新增导航桥梁').baseScale===1")
                    fixture.publish(pages);page.locator('#monitorRefresh').click()
                    page.emulate_media(reduced_motion='reduce')
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
                    'local_tour_no_writes':True,'gpu_loss_fallback':True,'remembered_follow':True,
                    'wheel_contained':True,'filtered_target_locate':True,'top_right_controls':True,
                    'branch_spacing':True,'manual_live_route_colors':True,'middle_drag_pan':True,
                    'native_fullscreen':True,'fullscreen_optional_panels':True,'window_fullscreen_fallback':True,'page_errors':errors})
                page.close()
            # Many branches must remain legible without dropping or flattening nodes.
            dense=[{'url':base,'label':'学校官网','kind':'root','path_json':[],'state':'fetched'}]
            for unit in range(40):
                label=f'院系 {unit+1:02d}'
                dense.append({'url':base+f'/dense/{unit}','label':label,'kind':'unit','path_json':['学校官网','院系设置'],'state':'fetched'})
                for leaf in range(28):
                    dense.append({'url':base+f'/dense/{unit}/{leaf}','label':f'信息栏目 {leaf+1:02d}','kind':'channel','path_json':['学校官网','院系设置',label],'state':'fetched'})
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
