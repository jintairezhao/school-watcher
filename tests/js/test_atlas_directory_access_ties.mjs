import assert from 'node:assert/strict';
import {buildAtlas,accessRoute} from '../../frontend/static/js/discovery-atlas-model.js';

const root='https://example.edu/',directory=root+'units/',programme=root+'programmes/',home=root+'college/';
const nodes=[
    {id:'root',entity_type:'page',kind:'root',url:root,label:'首页'},
    // Lexical IDs deliberately favour the ordinary programme link.
    {id:'a-programme',entity_type:'page',kind:'directory',url:programme,label:'本科专业设置'},
    {id:'z-directory',entity_type:'page',kind:'directory',url:directory,label:'组织机构'},
    {id:'home',entity_type:'page',kind:'unit',url:home,label:'学院主页'},
    {id:'unit',entity_type:'organization',kind:'unit',url:home,label:'学院'},
];
const proof=(url,locator='#units a',hash='current')=>({reference_url:url,locator,content_hash:hash,
    observed_at:'2026-11-06T00:00:00Z',material_available:true});
const edge=(source,target,type,evidence,access=false)=>({source,target,type,evidence:[evidence],access});
const routes=[
    edge('root','a-programme','navigation_link',proof(root,'nav .programmes'),true),
    edge('root','z-directory','navigation_link',proof(root,'nav .units'),true),
    edge('a-programme','home','navigation_link',proof(programme,'footer option'),true),
    edge('z-directory','home','navigation_link',proof(directory),true),
];
const listing=edge('z-directory','unit','directory_listing',proof(directory));
const binding=edge('unit','home','entity_page',proof(directory));
function select(relations){
    const scene=buildAtlas({nodes,relations},{deferLayout:true});
    const result=accessRoute(scene,scene.actual.get('home'));
    assert(result.steps.every(step=>!step.reversed&&step.edge.type==='navigation_link'));
    return {scene,result,ids:result.nodes.map(node=>node.id)};
}

const witnessed=select([...routes,listing,binding]);
assert.deepEqual(witnessed.ids,['root','z-directory','home'],
    'Equal-length access should use the evidenced directory entry before opaque identity order');
assert.equal(witnessed.scene.links.filter(edge=>edge.type==='navigation_link').length,4,
    'An alternative valid entrance stays available');
assert.equal(witnessed.scene.links.filter(edge=>edge.type==='directory_listing').length,1);

for(const changed of [
    {...listing,evidence:[proof(directory,'#different-anchor')]},
    {...listing,evidence:[proof(directory,'#units a','stale')]},
    {...listing,evidence:[{...proof(directory),observed_at:''}]},
    {...listing,evidence:[{...proof(directory),material_available:false}]},
    {...listing,type:'source_ownership'},
]){
    assert.deepEqual(select([...routes,changed,binding]).ids,['root','a-programme','home'],
        'A label, ownership claim, unavailable or nonmatching witness cannot manufacture directory preference');
}
assert.deepEqual(select([...routes,listing,binding,
    edge('root','home','navigation_link',proof(root,'nav .direct'),true)]).ids,['root','home'],
    'A real shorter direct entrance remains preferred');
const disconnected=routes.filter(edge=>edge.target!=='z-directory');
assert.deepEqual(select([...disconnected,listing,binding]).ids,['root','a-programme','home'],
    'A preferred but unreachable directory cannot hide a complete alternate route');
assert.deepEqual(select([...routes.map(edge=>edge.source==='z-directory'?{...edge,access:false}:edge),listing,binding]).ids,
    ['root','a-programme','home'],'Directory evidence never supplies access permission');
assert.deepEqual(select([...routes].reverse().concat([listing,binding])).ids,witnessed.ids,
    'Input insertion order does not change the directory tie');
console.log('Directory access ties passed: matching evidence, shortest direction, alternatives and gaps.');
