import assert from 'node:assert/strict';
import {buildAtlas,discoveryRoute,accessRoute,membershipRoute,explorationTrace,visibleEntries,
    atlasLayoutSignature,refreshAtlasRecords} from '../../frontend/static/js/discovery-atlas-model.js';

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
const relation={source:'college',target:'party',type:'navigation',evidence_url:nodes[1].url,locator:'nav > a.party',basis:'Observed website menu',access:true};
const model=buildAtlas({school,nodes,relations:[relation]});
assert.equal(model.links.length,1);
assert.equal(model.links[0].from.record.id,'college');
assert.equal(model.links[0].to.record.id,'party');
assert.equal(model.links[0].type,'navigation','A website menu must never become an administrative relationship');
assert.equal(model.actual.get('other-party').parent,null,'Same-named pages in another college stay separate');
assert.equal(model.actual.get('party').parent,model.actual.get('college'));
assert.equal(model.actual.get('college').parent,null,'A discovery path through student affairs cannot become ownership');
const cyclic=buildAtlas({school,nodes,relations:[relation,{...relation,source:'party',target:'college'}]});
const twoPredicates=buildAtlas({school,nodes,relations:[
    {...relation,type:'organization_membership',observed_relation:'attached_unit',access:false,assertions:[{text:'挂靠单位'}]},
    {...relation,type:'organization_membership',observed_relation:'sub_unit',access:false,assertions:[{text:'下属单位'}]},
    {...relation,type:'organization_membership',observed_relation:'attached_unit',access:false,assertions:[{text:'明确挂靠'}]}
]});
assert.equal(twoPredicates.links.length,2,'Affiliation and subordinate-unit assertions remain distinct facts');
assert.equal(twoPredicates.links.find(edge=>edge.observed_relation==='attached_unit').assertions.length,2,
    'Aggregation preserves every distinct assertion as well as its evidence');
assert.equal(cyclic.links.length,2,'Real reciprocal navigation is retained without creating a layout cycle');
assert.equal(cyclic.links.filter(link=>link.primary).length,1,'Only the evidenced acyclic backbone is marked primary; reciprocal links remain available for inspection');
assert.ok(cyclic.links.every(link=>link.evidence_url===relation.evidence_url),'Layout metadata cannot replace the underlying link evidence');
const unsafe=buildAtlas({school,nodes:[...nodes,{id:'bad',label:'Decoration',url:'javascript:alert(1)',path:[]}]});
assert.equal(unsafe.entries.size,nodes.length,'An entry without a usable webpage identity cannot become a sphere');
const complete=buildAtlas({school,nodes,relations:[relation,{source:'root',target:'college',type:'navigation',evidence_url:school.url,access:true}]});
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
const observed=(source,target,type='navigation')=>({source,target,type,evidence_url:associationNodes.find(node=>node.id===source).url,access:true});
const association=buildAtlas({school,nodes:associationNodes,relations:[
    observed('college','root'),observed('directory','college','directory'),
    observed('office','party'),observed('sibling','office'),observed('party','sibling')
]});
const connectedRoute=discoveryRoute(association,association.actual.get('directory'));
assert.equal(connectedRoute.mode,'partial','Outgoing links cannot be reversed into an access path');
assert.equal(connectedRoute.complete,false,'A connected association does not fill the missing directed discovery record');
assert.equal(connectedRoute.rootConnected,false);
assert.deepEqual(connectedRoute.nodes.map(node=>node.id),['directory']);
assert.deepEqual(connectedRoute.steps,[],'A reverse association cannot fill missing upstream evidence');
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
assert.deepEqual(discoveryRoute(equalLength,equalLength.actual.get('directory')).nodes.map(node=>node.id),['directory'],
    'Neither reversed alternative is an access route');
const direct=buildAtlas({school,nodes:associationNodes,relations:[observed('root','office'),observed('office','directory'),observed('root','directory')]});
assert.deepEqual(discoveryRoute(direct,direct.actual.get('directory')).nodes.map(node=>node.id),['root','directory'],
    'A recorded direct entrance wins over a longer route');
const membership=buildAtlas({school,nodes:associationNodes,relations:[{...observed('root','directory','directory'),access:false}]});
assert.equal(discoveryRoute(membership,membership.actual.get('directory')).complete,false,
    'Membership without a hyperlink does not grant website access');
const isolated=discoveryRoute(association,association.actual.get('other-party'));
assert.deepEqual(isolated.nodes.map(node=>node.id),['other-party']);
assert.equal(isolated.mode,'partial');assert.equal(isolated.rootConnected,false);
assert.deepEqual(discoveryRoute(association,null).steps,[]);
const missingAccess=buildAtlas({nodes:associationNodes,relations:[{source:'root',target:'directory',type:'directory',evidence_url:school.url}]});
assert.equal(accessRoute(missingAccess,missingAccess.actual.get('directory')).complete,false,
    'Legacy directory records without explicit access=true cannot manufacture a clickable route');
for(const type of ['directory','directory_listing','organization_membership','source_ownership','column_membership',
    'source_on_page','entity_page','cross_entity_reference','cross_department','website_navigation']){
    const notClickable=buildAtlas({nodes:associationNodes,relations:[{...observed('root','directory',type),access:true}]});
    assert.equal(accessRoute(notClickable,notClickable.actual.get('directory')).complete,false,
        type+' cannot become a website click because an access flag was accidentally set');
}
for(const type of ['navigation','navigation_link']){
    const missingPermission=buildAtlas({nodes:associationNodes,relations:[{source:'root',target:'directory',type,evidence_url:school.url}]});
    assert.equal(accessRoute(missingPermission,missingPermission.actual.get('directory')).complete,false,
        type+' must explicitly prove access rather than inheriting a truthy default');
    const clickable=buildAtlas({nodes:associationNodes,relations:[observed('root','directory',type)]});
    assert.equal(accessRoute(clickable,clickable.actual.get('directory')).complete,true);
}
const proof=(locator,hash='material-1')=>({kind:'observed_link',reference_url:school.url,locator,
    content_hash:hash,observed_at:'2026-10-21T10:00:00Z'});
const duplicateRelations=[{...observed('root','directory'),evidence:[proof('nav a')]},
    {...observed('root','directory'),evidence:[proof('main a'),proof('nav a')]},
    {...observed('root','directory'),evidence:[proof('nav a','material-2')]}];
// Top-level legacy evidence is omitted here so each observation has one full identity.
duplicateRelations.forEach(edge=>{delete edge.evidence_url;});
const combined=buildAtlas({nodes:associationNodes,relations:duplicateRelations});
assert.equal(combined.links.length,1,'One drawing edge can represent multiple observations');
assert.equal(combined.links[0].evidence.length,3,'Distinct locators or material versions survive aggregation');
assert.deepEqual(new Set(combined.links[0].evidence.map(item=>item.locator)),new Set(['nav a','main a']));

const typedNodes=[{id:'page:home',entity_type:'page',kind:'root',url:school.url,label:'Home'},
    {id:'page:shared',entity_type:'page',url:school.url+'/shared',label:'Shared page'},
    {id:'org:a',entity_type:'organization',label:'A',url:school.url+'/a'},
    {id:'org:b',entity_type:'organization',label:'B',url:school.url+'/b'},
    {id:'group',entity_type:'directory_group',label:'Directory without its own URL',reference_url:school.url},
    {id:'source:1',entity_type:'source',label:'Source one',url:school.url+'/shared',state:'connected'},
    {id:'source:2',entity_type:'source',label:'Source two',url:school.url+'/shared',state:'connected'},
    {id:'observation',entity_type:'structure_observation',label:'Observed entity',reference_url:school.url}];
const typedEdge=(source,target,type,access=false)=>({source,target,type,access,evidence:[proof(source+':'+target)]});
const typedSnapshot={nodes:typedNodes,relations:[
    typedEdge('page:home','page:shared','navigation_link',true),
    typedEdge('page:shared','source:1','source_on_page'),
    typedEdge('page:shared','source:2','source_on_page'),
    typedEdge('group','org:a','directory_listing'),typedEdge('group','org:b','directory_listing'),
    typedEdge('org:a','source:1','source_ownership'),typedEdge('org:b','source:1','source_ownership'),
    typedEdge('source:1','source:2','cross_entity_reference')
]};
const typed=buildAtlas(typedSnapshot);
assert.equal(typed.entries.size,typedNodes.length,'Recorded entities without an independent page remain addressable');
assert.notEqual(typed.actual.get('source:1'),typed.actual.get('source:2'));
assert.notEqual(typed.actual.get('source:1'),typed.actual.get('page:shared'),'Shared URL never merges page and source identities');
assert.equal(typed.actual.get('org:a').record.kind,'unit');
assert.equal(typed.actual.get('observation').record.kind,'navigation','Unknown structure observations are not promoted to organizations');
const sourceAccess=accessRoute(typed,typed.actual.get('source:1'));
assert.deepEqual(sourceAccess.nodes.map(item=>item.id),['page:home','page:shared']);
assert.equal(sourceAccess.targetSource.id,'source:1');
assert.equal(sourceAccess.terminalGap,'source_configuration_not_click');
assert.ok(sourceAccess.steps.every(step=>step.edge.type==='navigation_link'));
const redirectedNodes=[...typedNodes,
    {id:'page:configured',entity_type:'page',url:'http://example.edu/shared',label:'Configured address'}];
const observedBinding={...typedEdge('page:shared','source:1','source_on_page'),observed_relation:'validated_response_page'};
const redirected=buildAtlas({nodes:redirectedNodes,relations:[
    typedEdge('page:home','page:shared','navigation_link',true),
    typedEdge('page:configured','source:1','source_on_page'),observedBinding]});
const redirectedAccess=accessRoute(redirected,redirected.actual.get('source:1'));
assert.equal(redirectedAccess.complete,true,'Use the validated response page while preserving the configured address');
assert.deepEqual(redirectedAccess.nodes.map(item=>item.id),['page:home','page:shared']);
assert.equal(redirectedAccess.configurationRelation.observed_relation,'validated_response_page');
assert.equal(redirected.links.filter(edge=>edge.type==='source_on_page').length,2);
const conflictingResponse=buildAtlas({nodes:redirectedNodes,relations:[
    typedEdge('page:home','page:shared','navigation_link',true),observedBinding,
    {...typedEdge('page:configured','source:1','source_on_page'),observed_relation:'validated_response_page'}]});
assert.equal(accessRoute(conflictingResponse,conflictingResponse.actual.get('source:1')).gap,'multiple_hosting_pages',
    'Conflicting observed pages cannot be resolved by URL similarity or layout order');
const organizationSnapshot={...typedSnapshot,relations:[...typedSnapshot.relations,
    typedEdge('org:a','page:shared','entity_page',true)]};
const organization=buildAtlas(organizationSnapshot), entityAccess=accessRoute(organization,organization.actual.get('org:a'));
assert.equal(entityAccess.complete,true,'The unique explicitly recorded organization page has a directed root route');
assert.deepEqual(entityAccess.nodes.map(item=>item.id),['page:home','page:shared']);
assert.equal(entityAccess.targetEntity.id,'org:a');
assert.equal(entityAccess.hostingPage.id,'page:shared');
assert.equal(entityAccess.entityPageRelation.type,'entity_page');
assert.equal(entityAccess.terminalGap,'entity_page_not_click');
assert.ok(entityAccess.steps.every(step=>step.edge.type==='navigation_link'));
assert.ok(!entityAccess.links.includes(entityAccess.entityPageRelation),
    'Entity correspondence is separately inspectable evidence, never a click or glow in the access route');
assert.equal(accessRoute(typed,typed.actual.get('org:b')).gap,'entity_page_unknown',
    'An organization URL is not enough to infer which page entity it represents');
const repeatedBinding=buildAtlas({...organizationSnapshot,relations:[...organizationSnapshot.relations,
    {...organizationSnapshot.relations.at(-1),evidence:[proof('another official entity locator')]}]});
assert.equal(accessRoute(repeatedBinding,repeatedBinding.actual.get('org:a')).complete,true,
    'Multiple observations of one binding do not become multiple hosting pages');
const ambiguous=buildAtlas({...organizationSnapshot,relations:[...organizationSnapshot.relations,
    typedEdge('org:a','page:home','entity_page')]});
const ambiguousRoute=accessRoute(ambiguous,ambiguous.actual.get('org:a'));
assert.equal(ambiguousRoute.complete,true,'Multiple explicit page bindings do not erase an evidenced route');
assert.deepEqual(ambiguousRoute.nodes.map(node=>node.id),['page:home'],
    'Choose the shortest directed entrance while keeping other pages and bindings');
assert.equal(ambiguousRoute.entityPageRelations.length,2);
assert.deepEqual(new Set(ambiguousRoute.hostingPages.map(page=>page.id)),new Set(['page:home','page:shared']));
const reversedBinding=buildAtlas({...typedSnapshot,relations:[...typedSnapshot.relations,
    typedEdge('page:shared','org:a','entity_page')]});
assert.equal(accessRoute(reversedBinding,reversedBinding.actual.get('org:a')).gap,'entity_page_unknown',
    'The defined entity-to-page direction cannot silently be reversed');
const unreachableEntity=buildAtlas({nodes:typedNodes,relations:[typedEdge('org:a','page:shared','entity_page')]});
assert.equal(accessRoute(unreachableEntity,unreachableEntity.actual.get('org:a')).complete,false,
    'A known organization page alone does not establish a route from the school root');
const falsePageBridge=buildAtlas({nodes:typedNodes,relations:[typedEdge('page:home','org:a','navigation_link',true),
    typedEdge('org:a','page:shared','navigation_link',true)]});
assert.equal(accessRoute(falsePageBridge,falsePageBridge.actual.get('page:shared')).complete,false,
    'A typed non-page entity cannot serve as an intermediate website click');
const ownership=membershipRoute(typed,typed.actual.get('source:1'));
assert.equal(ownership.paths.length,2,'Shared ownership must retain both upstream chains');
assert.deepEqual(ownership.paths.map(path=>path.nodes.map(node=>node.id)),[
    ['group','org:a','source:1'],['group','org:b','source:1']]);
assert.ok(ownership.paths.every(path=>path.steps.every(step=>['directory_listing','source_ownership'].includes(step.edge.type))));
assert.equal(ownership.gap,'upstream_unknown','A valid partial directory does not prove a complete school-root chain');
assert.equal(membershipRoute(typed,typed.actual.get('source:2')).complete,false,
    'Cross-entity references and page configuration cannot supply ownership');

const position=typed.actual.get('source:1').position,sourceObject=typed.actual.get('source:1'),edgeObject=typed.links[0];
sourceObject.renderPosition={x:1,y:2,z:3};sourceObject.labelElement={local:'retained'};
const changed=structuredClone(typedSnapshot);
changed.nodes.find(n=>n.id==='source:1').label='Corrected source';
changed.nodes.find(n=>n.id==='source:1').state='failed';
changed.nodes.find(n=>n.id==='source:1').sources=[{id:7,name:'Updated'}];
changed.relations[0].evidence.push(proof('additional locator'));
assert.equal(atlasLayoutSignature(changed),atlasLayoutSignature(typedSnapshot),
    'Names, states, source metadata and additional evidence do not change topology');
assert.equal(refreshAtlasRecords(typed,changed),true);
assert.equal(typed.actual.get('source:1'),sourceObject,'Selection and hover can retain their exact entity objects');
assert.equal(typed.actual.get('source:1').position,position,'Metadata refresh must never recompute positions');
assert.deepEqual(sourceObject.renderPosition,{x:1,y:2,z:3});
assert.deepEqual(sourceObject.labelElement,{local:'retained'});
assert.equal(sourceObject.record.label,'Corrected source');assert.equal(sourceObject.record.state,'failed');
assert.equal(typed.links[0],edgeObject,'Highlight references retain their relationship object');
assert.equal(typed.links.find(edge=>edge.type==='navigation_link').evidence.length,2);
const changedTopology=structuredClone(changed);changedTopology.nodes.push({id:'source:new',entity_type:'source',label:'New'});
assert.notEqual(atlasLayoutSignature(changed),atlasLayoutSignature(changedTopology));
assert.equal(refreshAtlasRecords(typed,changedTopology),false,'A structural update explicitly requires rebuilding the model');

const missingHistory=explorationTrace({nodes:typedNodes,tasks:[{url:school.url,state:'done'}],recent:[{at:'2026-10-21'}]},sourceObject);
assert.equal(missingHistory.gap,'exploration_events_unavailable');assert.equal(missingHistory.complete,false);
const history={exploration_events:[
    {id:'event:1',node_id:'page:home',sequence:1,observed_at:'2026-10-21T10:00:00Z',event_type:'exploration_started'},
    {id:'event:2',node_id:'page:shared',sequence:2,observed_at:'2026-10-21T10:00:01Z',event_type:'page_fetched',parent_event_id:'event:1'},
    {id:'event:3',node_id:'source:1',sequence:3,observed_at:'2026-10-21T10:00:02Z',event_type:'source_validated',parent_event_id:'event:2'}
]};
assert.equal(explorationTrace(history,sourceObject).complete,true);
assert.deepEqual(explorationTrace(history,sourceObject).events.map(event=>event.id),['event:1','event:2','event:3']);
assert.equal(explorationTrace({exploration_events:history.exploration_events.slice(1)},sourceObject).gap,'missing_parent_event');
assert.equal(explorationTrace({exploration_events:[history.exploration_events[0],{...history.exploration_events[1],sequence:0}]},'page:shared').gap,'event_order_conflict');
console.log('Atlas evidence: real page identities, explicit URL relationships, same-name isolation and navigation semantics passed.');
