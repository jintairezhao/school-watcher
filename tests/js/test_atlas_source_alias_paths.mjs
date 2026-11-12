import assert from 'node:assert/strict';
import {buildAtlas,accessRoute,membershipRoute,explorationTrace} from '../../frontend/static/js/discovery-atlas-model.js';

const root='https://school.example.edu.cn/',canonical=root+'college/notices/',cms=root+'zcms/catalog/42/pc/index_1.shtml';
const nodes=[
    {id:'root',entity_type:'page',kind:'root',url:root,label:'首页'},
    {id:'canonical',entity_type:'page',kind:'channel',url:canonical,label:'通知公告'},
    {id:'cms',entity_type:'page',kind:'channel',url:cms,label:'通知公告'},
    {id:'source:13',entity_type:'source',kind:'channel',url:canonical,label:'通知公告',config_hash:'canonical-config',configured:true},
    {id:'source:76',entity_type:'source',kind:'channel',url:cms,label:'通知公告',config_hash:'cms-config',configured:true},
];
const evidence=[{reference_url:canonical,locator:'#notices',content_hash:'canonical-original',observed_at:'2026-10-30T03:00:00Z'},
    {reference_url:cms,locator:'#pager-script',content_hash:'cms-original',observed_at:'2026-10-30T03:01:00Z'}];
const edge=(source,target,type,extra={})=>({source,target,type,evidence,...extra});
const bindings=[edge('canonical','source:13','source_on_page'),edge('cms','source:76','source_on_page')];
const alias=edge('canonical','source:76','source_on_page',{observed_relation:'validated_listing_alias',
    assertions:[{kind:'validated_listing_alias',configured_url:cms,alias_url:canonical,
        config_hash:'cms-config',receipt_id:'proposal:76'}]});
const model=relations=>buildAtlas({nodes,relations},{deferLayout:true});

{
    const scene=model([...bindings,alias,edge('root','canonical','navigation_link',{access:true})]);
    const route=accessRoute(scene,scene.actual.get('source:76'));
    assert(route.complete);
    assert.deepEqual(route.nodes.map(n=>n.id),['root','canonical']);
    assert.equal(route.steps.length,1,'Equivalent page binding must not invent a website click');
    assert(route.steps.every(s=>!s.reversed&&s.edge.type==='navigation_link'));
    assert.equal(route.configurationRelation.observed_relation,'validated_listing_alias');
    assert.equal(route.terminalGap,'source_configuration_not_click');
    assert.notEqual(scene.actual.get('source:13'),scene.actual.get('source:76'));
    assert.equal(scene.actual.get('source:76').record.url,cms,'Configured entrance is not rewritten');
    assert.equal(scene.links.filter(e=>e.type==='navigation_link').length,1);
    assert.equal(explorationTrace({nodes,relations:[]},scene.actual.get('source:76'),scene).complete,false,
        'An equivalent entrance and access path do not supply a historical exploration event');
}
{
    const scene=model([...bindings,alias,edge('root','cms','navigation_link',{access:true})]);
    const route=accessRoute(scene,scene.actual.get('source:76'));
    assert(route.complete,'A proven equivalent entrance must not hide a directly reachable configured URL');
    assert.equal(route.hostingPage.id,'cms');
}
for(const change of [{config_hash:'another-config'},{configured_url:canonical},{alias_url:cms},{receipt_id:''}]){
    const changed={...alias,assertions:[{...alias.assertions[0],...change}]};
    const scene=model([...bindings,changed,edge('root','canonical','navigation_link',{access:true})]);
    const route=accessRoute(scene,scene.actual.get('source:76'));
    assert.equal(route.complete,false,'An unbound alias assertion must not select a convenient entrance');
    assert.equal(route.gap,'multiple_hosting_pages');
}
{
    const scene=model([...bindings,edge('root','canonical','navigation_link',{access:true})]);
    const route=accessRoute(scene,scene.actual.get('source:76'));
    assert.equal(route.complete,false,'Same labels and source13 access do not provide source76 an alias');
    assert.equal(route.gap,'upstream_navigation_unknown');
}
{
    const scene=model([...bindings,alias,edge('canonical','root','navigation_link',{access:true})]);
    const route=accessRoute(scene,scene.actual.get('source:76'));
    assert.equal(route.complete,false,'An alias cannot reverse navigation');
    assert.equal(route.gap,'upstream_navigation_unknown');
}
{
    const publisher={id:'school:1',entity_type:'organization',organization_kind:'school',kind:'unit',label:'例校',url:root};
    const relations=[...bindings,edge('school:1','source:13','source_ownership')];
    const scene=buildAtlas({nodes:[...nodes,publisher],relations},{deferLayout:true});
    const route=membershipRoute(scene,scene.actual.get('source:13'));
    assert(route.complete&&route.rootConnected,'An evidenced saved School publisher is the school-scope membership root');
    assert.deepEqual(route.nodes.map(n=>n.id),['school:1','source:13']);
    assert.equal(accessRoute(scene,scene.actual.get('source:13')).complete,false,
        'A school publisher still supplies no website navigation');
    const ordinary=buildAtlas({nodes:[...nodes,{...publisher,organization_kind:'unit'}],relations},{deferLayout:true});
    assert.equal(membershipRoute(ordinary,ordinary.actual.get('source:13')).gap,'upstream_unknown',
        'A name, URL or layout unit cannot impersonate the saved School entity');
}
console.log('Source alias access-path checks passed; identities, direction and unknown history preserved.');
