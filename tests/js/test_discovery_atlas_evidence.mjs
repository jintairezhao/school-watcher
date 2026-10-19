import assert from 'node:assert/strict';
import {buildAtlas,discoveryRoute,visibleEntries} from '../../frontend/static/js/discovery-atlas-model.js';

const school={name:'Example University',url:'https://example.edu'};
const nodes=[
    {id:'root',url:school.url,label:'Home',kind:'root',path:[]},
    {id:'college',url:school.url+'/math/',label:'Mathematics',kind:'unit',path:['Home','Student affairs']},
    {id:'party',url:school.url+'/math/party/',label:'Party building',kind:'navigation',path:['Home','Mathematics']},
    {id:'other-party',url:school.url+'/business/party/',label:'Party building',kind:'navigation',path:['Home','Mathematics']}
];
const unknown=buildAtlas({school,nodes});
assert.equal(unknown.entries.size,nodes.length,'Every visible node must correspond to a recorded page; path names cannot invent spheres');
assert.ok([...unknown.entries.values()].every(n=>n.record?.url),'No untraceable placeholder nodes');
assert.equal(unknown.links.length,0,'Matching path labels do not establish a relationship between page URLs');
const relation={source:'college',target:'party',type:'navigation',evidence_url:nodes[1].url,locator:'nav > a.party',basis:'Observed website menu'};
const model=buildAtlas({school,nodes,relations:[relation]});
assert.equal(model.links.length,1);
assert.equal(model.links[0].from.record.id,'college');
assert.equal(model.links[0].to.record.id,'party');
assert.equal(model.links[0].type,'navigation','A website menu must never become an administrative relationship');
assert.equal(model.actual.get('other-party').parent,null,'Same-named pages in another college stay separate');
assert.equal(model.actual.get('party').parent,model.actual.get('college'));
assert.equal(model.actual.get('college').parent,null,'A discovery path through student affairs cannot become ownership');
const cyclic=buildAtlas({school,nodes,relations:[relation,{...relation,source:'party',target:'college'}]});
assert.equal(cyclic.links.length,2,'Real reciprocal navigation is retained without creating a layout cycle');
assert.equal(cyclic.links.filter(link=>link.primary).length,1,'Only the evidenced acyclic backbone is marked primary; reciprocal links remain available for inspection');
assert.ok(cyclic.links.every(link=>link.evidence_url===relation.evidence_url),'Layout metadata cannot replace the underlying link evidence');
const unsafe=buildAtlas({school,nodes:[...nodes,{id:'bad',label:'Decoration',url:'javascript:alert(1)',path:[]}]});
assert.equal(unsafe.entries.size,nodes.length,'An entry without a usable webpage identity cannot become a sphere');
const complete=buildAtlas({school,nodes,relations:[relation,{source:'root',target:'college',type:'navigation',evidence_url:school.url}]});
const route=discoveryRoute(complete,complete.actual.get('party'));
assert.equal(route.complete,true);
assert.equal(route.mode,'discovery');
assert.equal(route.rootConnected,true);
assert.deepEqual(route.nodes.map(n=>n.id),['root','college','party'],'Selected route includes the full evidenced chain, not just immediate neighbours');
assert.ok(route.steps.every(step=>!step.reversed&&step.from===step.edge.from&&step.to===step.edge.to));
assert.deepEqual(visibleEntries(complete,{query:'absent',collapsed:new Set(['root']),revealed:new Set(route.nodes.map(n=>n.id))}).map(n=>n.id),['root','college','party'],'Filtering cannot silently remove the selected trace');
assert.equal(discoveryRoute(model,model.actual.get('party')).complete,false,'Missing upstream evidence must remain a gap');
const repeatedMenu=buildAtlas({school,nodes:[...nodes,{id:'leaf',url:school.url+'/math/news/',label:'News',kind:'navigation',path:[]}],relations:[relation,
    {...relation,source:'leaf',target:'party'}, {...relation,source:'other-party',target:'leaf'}]});
assert.deepEqual(discoveryRoute(repeatedMenu,repeatedMenu.actual.get('party')).nodes.map(n=>n.id),['college','party'],'An incomplete route starts at its evidenced directory/unit anchor, not an arbitrary chain of repeated sibling menus');
const associationNodes=[...nodes,
    {id:'directory',url:school.url+'/directory/',label:'Academic departments',kind:'directory',path:[]},
    {id:'office',url:school.url+'/office/',label:'Office',kind:'navigation',path:[]},
    {id:'sibling',url:school.url+'/sibling/',label:'Sibling',kind:'navigation',path:[]}];
const observed=(source,target,type='navigation')=>({source,target,type,evidence_url:associationNodes.find(node=>node.id===source).url});
const association=buildAtlas({school,nodes:associationNodes,relations:[
    observed('college','root'),observed('directory','college','directory'),
    observed('office','party'),observed('sibling','office'),observed('party','sibling')
]});
const connectedRoute=discoveryRoute(association,association.actual.get('directory'));
assert.equal(connectedRoute.mode,'association','Outgoing links cannot be silently reversed into a discovery or ownership path');
assert.equal(connectedRoute.complete,false,'A connected association does not fill the missing directed discovery record');
assert.equal(connectedRoute.rootConnected,true);
assert.deepEqual(connectedRoute.nodes.map(node=>node.id),['root','college','directory']);
assert.ok(connectedRoute.steps.every(step=>step.reversed&&step.edge.from===step.to&&step.edge.to===step.from),
    'An association exposes its traversal direction and retains the original evidenced edge');
assert.ok(connectedRoute.links.every(edge=>association.links.includes(edge)),'No association edge is synthesized');
const equalRankPartial=discoveryRoute(association,association.actual.get('party'));
assert.equal(equalRankPartial.mode,'partial');
assert.equal(equalRankPartial.rootConnected,false);
assert.deepEqual(equalRankPartial.nodes.map(node=>node.id),['office','party'],
    'A partial trace retains an immediate incoming page even when both pages have the same kind, without growing through a menu cycle');
const directedPreferred=buildAtlas({school,nodes:associationNodes,relations:[
    observed('root','college'),observed('college','directory'),observed('directory','root')
]});
const preferred=discoveryRoute(directedPreferred,directedPreferred.actual.get('directory'));
assert.equal(preferred.mode,'discovery','A complete directed route is preferred to a shorter reversed association');
assert.deepEqual(preferred.nodes.map(node=>node.id),['root','college','directory']);
const equalLength=buildAtlas({school,nodes:associationNodes,relations:[
    observed('college','root'),observed('directory','college'),observed('root','office'),observed('directory','office')
]});
assert.deepEqual(discoveryRoute(equalLength,equalLength.actual.get('directory')).nodes.map(node=>node.id),['root','office','directory'],
    'Equal-length association routes prefer fewer reverse traversals');
const isolated=discoveryRoute(association,association.actual.get('other-party'));
assert.deepEqual(isolated.nodes.map(node=>node.id),['other-party']);
assert.equal(isolated.mode,'partial');assert.equal(isolated.rootConnected,false);
assert.deepEqual(discoveryRoute(association,null).steps,[]);
console.log('Atlas evidence: real page identities, explicit URL relationships, same-name isolation and navigation semantics passed.');
