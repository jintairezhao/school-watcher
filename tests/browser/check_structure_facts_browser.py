"""Real WebGL interaction over isolated, versioned structure fixtures."""
from pathlib import Path
from urllib.parse import urlsplit
import json
import sys
import tempfile
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_structure_snapshot import StructureSnapshotTests
from backend.database.db import db
from backend.database.models import Subscription
from backend.services.structure_snapshot import read_structure
from playwright.sync_api import sync_playwright, expect


def check(output=None):
    output=Path(output) if output else ROOT/'.local/structure-facts-20261021/browser'
    output.mkdir(parents=True,exist_ok=True)
    results=[]
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True,
            args=['--enable-webgl','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
        fixture=StructureSnapshotTests();fixture.setUp()
        try:
            source=fixture.source('本科教学通知',verified=True)
            shared=fixture.source('研究生培养通知',source.list_url,'.graduate li',verified=True)
            directory=fixture.school.url+'/departments'
            pages=[fixture.document(fixture.school.url,directory,label='学校官网'),
                   fixture.document(directory,source.list_url,label='院系设置'),
                   fixture.document(source.list_url,label='教学信息页面'),
                   fixture.document(fixture.school.url+'/candidate',label='探索候选页面')]
            pages[0]['kind']='root';pages[1]['kind']='directory'
            fixture.fixture.publish(pages)
            for width in (1440,390):
                page=browser.new_page(viewport={'width':width,'height':1000},reduced_motion='reduce')
                # A browser may batch leaving and re-entering one viewport in
                # a single notification during fullscreen/layout changes.
                page.add_init_script('''
                    const NativeIntersectionObserver=window.IntersectionObserver;
                    window.IntersectionObserver=class extends NativeIntersectionObserver {
                        constructor(callback,options){super((entries,observer)=>{
                            if(entries.some(entry=>entry.target.id==='atlasViewport'))
                                window.atlasIntersectionBatch=records=>callback(records,observer);
                            callback(entries,observer);
                        },options);}
                    };
                ''')
                errors=[];updates=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                def serve(route):
                    req=route.request;url=urlsplit(req.url)
                    assert url.hostname=='localhost',req.url
                    if url.path.startswith('/_desktop/'):
                        return route.fulfill(json={'phase':'ready'})
                    response=fixture.client.open(url.path+('?' + url.query if url.query else ''),method=req.method,
                        data=req.post_data,headers={k:v for k,v in req.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                    if url.path.endswith('/structure-snapshot') and response.status_code==200:
                        updates.append(response.json['update_mode'])
                    route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                page.route('**/*',serve)
                page.goto('http://localhost'+fixture.fixture.page)
                page.locator('#languageSelect').select_option('zh-CN')
                page.locator('#monitorScope').select_option('exploration')
                expect(page.locator('#atlasStage')).to_have_attribute('data-renderer','ready')
                page.locator('#monitorAuto').uncheck()
                page.wait_for_function('()=>discoveryMonitor.monitorSnapshot?.nodes.filter(n=>n.entity_type==="source"&&n.configured).length===2')
                expect(page.locator('#atlasPlay')).to_be_disabled()
                page.locator('#atlasListPanel').evaluate('(node)=>node.open=true')
                page.locator(f'.monitor-node[data-node-id="source:{source.id}"]').click()
                expect(page.locator('#atlasPathEvidence')).to_contain_text('官网访问路径')
                expect(page.locator('#atlasPathEvidence')).to_contain_text('院系设置')
                expect(page.locator('#atlasPathEvidence')).to_contain_text('栏目所在页面')
                page.locator('#monitorPathMode').select_option('membership')
                expect(page.locator('#atlasPathEvidence')).to_contain_text('上游依据不完整')
                page.locator('#monitorPathMode').select_option('exploration')
                expect(page.locator('#atlasPathEvidence')).to_contain_text('尚无完整的真实探索记录')
                page.locator('#monitorPathMode').select_option('access')
                # A metadata update keeps the selected identity and receives a delta.
                source.name=f'本科教学通知（已更新 {width}）';db.session.commit()
                page.locator('#monitorRefresh').click()
                expect(page.locator('#monitorDetail h3').first).to_have_text(source.name)
                assert 'delta' in updates
                page.locator('#languageSelect').select_option('en')
                expect(page.locator('#monitorScope option:checked')).to_have_text('Exploration candidates')
                expect(page.locator('#atlasPathEvidence h3')).to_have_text('Website access path')
                expect(page.locator('#monitorConnection')).to_contain_text('Auto refresh is off')
                assert '数据读取于' not in page.locator('#monitorReadAt').inner_text()
                assert '本科' in page.locator('#monitorDetail').inner_text(),'Official names remain original'
                page.locator('#atlasOptions > summary').click();page.locator('#atlasNodeLabels').uncheck()
                page.locator('#atlasOptions > summary').click()
                assert page.locator('.atlas-label:not([hidden])').count()==0
                if width==1440:
                    before_frame=page.evaluate('''()=>{
                        const a=discoveryMonitor.discoveryAtlas;
                        const frame=a.renderer.info.render.frame;
                        atlasIntersectionBatch([{target:atlasViewport,isIntersecting:false},
                            {target:atlasViewport,isIntersecting:true}]);
                        a.controls.dispatchEvent({type:'change'});
                        return frame;
                    }''')
                    page.wait_for_function('before=>discoveryMonitor.discoveryAtlas.renderer.info.render.frame>before',arg=before_frame,timeout=3000)
                    page.locator('#atlasFullscreen').click()
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    point=page.evaluate('''async()=>{
                        const a=discoveryMonitor.discoveryAtlas,T=await import('/static/vendor/three/three.module.min.js');
                        a.camera.updateMatrixWorld(true);
                        const canvas=a.renderer.domElement,r=canvas.getBoundingClientRect();
                        const mesh=a.scene.children.flatMap(g=>g.children).find(o=>o.isInstancedMesh);
                        for(const n of a.nodes){
                            const p=new T.Vector3().copy(n.renderPosition).project(a.camera);
                            const x=r.x+(p.x+1)*r.width/2,y=r.y+(1-p.y)*r.height/2;
                            if(p.z<0||p.z>1||document.elementFromPoint(x,y)!==canvas)continue;
                            const ray=new T.Raycaster();ray.setFromCamera(new T.Vector2(p.x,p.y),a.camera);
                            const saved=mesh.boundingSphere;mesh.boundingSphere=null;
                            const hit=ray.intersectObject(mesh,false)[0];mesh.boundingSphere=saved;
                            if(hit&&a.nodes[hit.instanceId]===n)return {id:n.id,x,y};
                        }return null;
                    }''')
                    assert point,'A real sphere must be selectable without a label'
                    page.mouse.click(point['x'],point['y'])
                    page.wait_for_function('id=>discoveryMonitor.discoveryAtlas.current?.id===id',arg=point['id'])
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    assert page.evaluate("discoveryMonitor.discoveryAtlas.scene.getObjectByName('atlasRoutes').visible")
                    page.locator('#atlasInspect').click()
                    expect(page.locator('#atlasDetailPanel')).to_be_visible()
                    page.locator('#atlasDetailClose').click()
                    expect(page.locator('#atlasDetailPanel')).to_be_hidden()
                    page.keyboard.press('Escape')
                page.evaluate('window.scrollTo(0,0)')
                page.wait_for_timeout(300)
                page.screenshot(path=str(output/f'facts-{width}.png'),full_page=True)
                assert not errors,errors
                results.append(dict(width=width,locales=['zh-CN','en'],delta=True,paths=True,labels=True,errors=errors))
                page.close()
            # Synthetic ownership is used only to verify repeated controls for
            # one source identity. It is not evidence about a real university.
            db.session.add(Subscription(user_id=fixture.fixture.fixture.admin.id,school_id=fixture.school.id))
            db.session.commit()
            capture=read_structure(fixture.school)
            source_node=next(n for n in capture['facts']['nodes'] if n['id']==f'source:{source.id}')
            for owner in ('fixture-owner-a','fixture-owner-b'):
                capture['facts']['nodes'].append(dict(id=owner,entity_type='organization',label=owner))
                capture['facts']['relations'].append(dict(id=owner+'-source',source=owner,target=source_node['id'],
                    type='source_ownership',evidence=[]))
            page=browser.new_page(viewport={'width':1200,'height':900})
            page.route('**/*',serve)
            with patch('backend.services.structure_snapshot.read_structure',return_value=capture), \
                    patch('backend.routes.subscriptions.start_background_scrape'):
                page.goto(f'http://localhost/subscriptions/{fixture.school.id}')
                page.locator('#sourcePreferences').evaluate('(node)=>node.open=true')
                page.locator('[name="mode"][value="selected"]').check()
                page.locator('#sourceClearSelection').click()
                repeated=page.locator(f'[name="department"][value="{source.id}"]')
                expect(repeated).to_have_count(2)
                repeated.first.check();expect(repeated.nth(1)).to_be_checked()
                repeated.nth(1).uncheck();expect(repeated.first).not_to_be_checked()
                repeated.first.check()
                page.locator('.source-save button').click()
                page.wait_for_url('**/?school=*')
                sub=Subscription.query.filter_by(user_id=fixture.fixture.fixture.admin.id,school_id=fixture.school.id).one()
                db.session.refresh(sub)
                assert sub.department_ids==[source.id],sub.department_ids
                results.append(dict(shared_source_checkboxes=True,saved_once=True,synthetic_ui_fixture=True))
            page.close()
            browser.close()
        finally:fixture.tearDown()
    (output/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps(results,ensure_ascii=False))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--output')
    check(parser.parse_args().output)

