"""Real WebGL controls over current typed navigation; no invented ancestry.

Typed paths, actual-sphere picking, explicit details, source sharing and delta
updates have their own check_structure_facts_browser gate. This gate covers
camera controls, persistent labels, read-only filtering and GPU fallback.
"""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_structure_snapshot import StructureSnapshotTests
from backend.database.models import BackgroundTask
from playwright.sync_api import expect,sync_playwright


def check():
    output=ROOT/'.local/discovery-atlas-browser'
    output.mkdir(parents=True,exist_ok=True)
    report=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True,
            args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        fixture=StructureSnapshotTests();fixture.setUp()
        try:
            base=fixture.school.url.rstrip('/')
            directory=base+'/departments'
            homes=[base+f'/unit/{i}' for i in range(7)]
            labels=['教务处','研究生院','学生工作处','招生就业','计算机学院','外国语学院','国际交流']
            pages=[fixture.document(base,directory,label='学校官网'),fixture.document(directory,*homes,label='院系设置')]
            pages[0]['kind']='root';pages[1]['kind']='directory'
            for i,home in enumerate(homes):
                leaves=[home+f'/column/{n}' for n in range(6)]
                pages.append(fixture.document(home,*leaves,label=labels[i]))
                pages.extend(fixture.document(url,label=f'栏目 {n+1}') for n,url in enumerate(leaves))
            fixture.fixture.publish(pages)
            before=BackgroundTask.query.count()
            for width,theme in [(1440,'dark'),(1440,'light'),(390,'dark'),(390,'light')]:
                page=browser.new_page(viewport={'width':width,'height':992},reduced_motion='reduce')
                errors,writes=[],[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                def serve(route):
                    req=route.request;url=urlsplit(req.url)
                    assert url.hostname=='localhost',req.url
                    if req.method!='GET':writes.append(url.path)
                    if url.path.startswith('/_desktop/'):
                        return route.fulfill(json={'phase':'ready'})
                    response=fixture.client.open(url.path+('?' + url.query if url.query else ''),method=req.method,
                        data=req.post_data,headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                page.route('**/*',serve)
                page.add_init_script('localStorage.setItem("theme",'+json.dumps(theme)+')')
                page.goto('http://localhost'+fixture.fixture.page)
                page.locator('#monitorScope').select_option('exploration')
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready',timeout=30000)
                page.wait_for_function('()=>discoveryMonitor.discoveryAtlas?.model?.actual.size>=51')
                page.locator('#monitorAuto').uncheck()
                expect(page.locator('#atlasViewport canvas')).to_have_count(1)
                expect(page.locator('#atlasPlay')).to_be_disabled()
                assert page.evaluate('new Set(discoveryMonitor.discoveryAtlas.nodes.map(n=>n.position.z)).size>5')
                page.locator('#atlasStage').scroll_into_view_if_needed()
                point=page.evaluate('''()=>{
                    const c=document.querySelector('#atlasViewport canvas'),r=c.getBoundingClientRect();
                    for(const [fx,fy] of [[.6,.65],[.5,.7],[.7,.8],[.4,.6]]){
                        const x=r.x+r.width*fx,y=r.y+r.height*fy;
                        if(document.elementFromPoint(x,y)===c)return {x,y};
                    }return null;
                }''')
                assert point,'Camera controls need unobscured canvas'
                page.mouse.move(point['x'],point['y'])
                scroll=page.evaluate('scrollY')
                distance=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)')
                page.mouse.wheel(0,180)
                page.wait_for_function('d=>discoveryMonitor.discoveryAtlas.camera.position.distanceTo(discoveryMonitor.discoveryAtlas.controls.target)!==d',arg=distance)
                assert abs(page.evaluate('scrollY')-scroll)<1
                camera=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                page.mouse.down();page.mouse.move(point['x']+65,point['y']+20,steps=8);page.mouse.up()
                assert camera!=page.evaluate('discoveryMonitor.discoveryAtlas.camera.position.toArray()')
                pan=page.evaluate('discoveryMonitor.discoveryAtlas.controls.target.toArray()')
                page.mouse.down(button='middle');page.mouse.move(point['x']+100,point['y']+40,steps=6);page.mouse.up(button='middle')
                assert pan!=page.evaluate('discoveryMonitor.discoveryAtlas.controls.target.toArray()')
                page.locator('#atlasOptions > summary').click()
                positions=page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>[n.id,n.position.x,n.position.y,n.position.z])')
                page.locator('#atlasNodeLabels').uncheck();page.locator('#atlasOptions > summary').click()
                page.wait_for_function('()=>[...document.querySelectorAll(".atlas-node-label")].every(n=>n.hidden)')
                assert positions==page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>[n.id,n.position.x,n.position.y,n.position.z])')
                page.reload();page.locator('#monitorScope').select_option('exploration')
                page.wait_for_function('()=>discoveryMonitor.discoveryAtlas?.model?.actual.size>=51')
                expect(page.locator('#atlasNodeLabels')).not_to_be_checked()
                page.locator('#monitorAuto').uncheck()
                page.locator('#atlasOptions > summary').click();page.locator('#atlasNodeLabels').check()
                old=page.evaluate('discoveryMonitor.discoveryAtlas.nodes.map(n=>n.position.x)')
                page.locator('#atlasSpacing').fill('1.6');page.locator('#atlasSpacing').dispatch_event('change')
                page.wait_for_function('old=>JSON.stringify(discoveryMonitor.discoveryAtlas.nodes.map(n=>n.position.x))!==JSON.stringify(old)',arg=old)
                page.locator('#atlasOptions > summary').click()
                page.evaluate("discoveryMonitor.discoveryAtlas.select([...discoveryMonitor.discoveryAtlas.model.actual.values()].find(n=>n.record.url.endsWith('/unit/1')).id)")
                expect(page.locator('#atlasPathEvidence')).to_contain_text('官网访问路径')
                expect(page.locator('#atlasPathEvidence')).to_contain_text('院系设置')
                selected=page.evaluate('discoveryMonitor.discoveryAtlas.current.id')
                page.locator('#monitorRefresh').click()
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current.id')==selected
                # A navigation path alone does not authorize a growth replay.
                expect(page.locator('#atlasPlay')).to_be_disabled()
                page.locator('#atlasFullscreen').click()
                expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                page.locator('#atlasInspect').click();expect(page.locator('#atlasDetailPanel')).to_be_visible()
                page.locator('#atlasDetailClose').click();expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                page.keyboard.press('Escape')
                page.locator('#atlasOverview').click()
                assert page.evaluate('discoveryMonitor.discoveryAtlas.current===null')
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                page.locator('#atlasStage').screenshot(path=str(output/f'{width}-{theme}.png'))
                page.evaluate('document.querySelector("#atlasViewport canvas").dispatchEvent(new Event("webglcontextlost",{cancelable:true}))')
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','unavailable')
                expect(page.locator('#atlasListPanel')).to_have_attribute('open','')
                expect(page.locator('#atlasPlay')).to_be_disabled()
                assert not writes and BackgroundTask.query.count()==before
                assert not errors,errors
                report.append(dict(width=width,theme=theme,typed_navigation=True,rotation_zoom_pan=True,
                    labels_persisted=True,spacing=True,background_refresh_retains_selection=True,
                    no_synthetic_playback=True,explicit_details=True,gpu_fallback=True,errors=errors))
                page.close()
        finally:
            fixture.tearDown();browser.close()
    (output/'browser-report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':check()
