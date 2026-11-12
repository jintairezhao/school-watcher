import assert from 'node:assert/strict';
import {buildAtlas,atlasLayoutSignature,refreshAtlasRecords} from '../../frontend/static/js/discovery-atlas-model.js';
import {layoutStructure} from '../../frontend/static/js/structure-layout.js';
import {appearanceColor} from '../../frontend/static/js/structure-appearance.js';

// Synthetic semantic guards, not independent university ownership evidence.
const proof={reference_url:'https://fixture.example/directory',locator:'main a',content_hash:'retained-fixture'};
const edge=(id,source,target,type,evidence=[proof])=>({id,source,target,type,evidence});
const org=id=>({id,entity_type:'organization',label:id});
const src=(id,configured=true)=>({id,entity_type:'source',label:id,configured});
const snapshot={nodes:[org('a'),org('b'),org('parent'),src('shared'),src('other'),src('unknown'),src('candidate',false),
    {id:'directory',entity_type:'directory_group',label:'Roster'},
    {id:'page',entity_type:'page',url:'https://fixture.example/a',label:'A website'},
    {id:'menu',entity_type:'page',url:'https://fixture.example/menu',label:'Menu'}],relations:[
    edge('owner-a','a','shared','source_ownership'),edge('owner-b','b','shared','source_ownership'),
    edge('owner-other','b','other','source_ownership'),edge('duplicate-owner','b','shared','source_ownership'),
    edge('candidate-owner','a','candidate','source_ownership'),
    edge('child-a','parent','a','organization_membership'),edge('child-b','parent','b','organization_membership'),
    edge('directory-b','directory','b','directory_listing'),edge('binding','a','page','entity_page'),
    edge('hosting','page','unknown','source_on_page'),edge('cross','a','other','cross_entity_reference'),
    edge('navigation','menu','b','navigation_link'),
    edge('incomplete','b','unknown','source_ownership',[{reference_url:proof.reference_url}])
]};
snapshot.nodes.find(node=>node.id==='unknown').display_group='b';
const before=JSON.stringify(snapshot),model=buildAtlas(snapshot,{deferLayout:true});
assert.equal(model.actual.get('directory').radius,3,'Directory collection must not imply a publisher size');
assert.equal(model.actual.get('a').radius,5,'One distinct configured source gives radius five, independent of kind');
assert.equal(model.actual.get('parent').radius,3+2*Math.log2(3),'Shared sources count once across all explicit descendant paths');
const item=id=>model.actual.get(id).presentation;
assert.equal(item('a').connectedSourceCount,1);
assert.equal(item('b').connectedSourceCount,2);
assert.equal(item('parent').connectedSourceCount,2);
assert.equal(item('unknown').connectedSourceCount,1,'A configured source counts itself without acquiring an owner');
assert.equal(item('candidate').connectedSourceCount,0,'A candidate is not an acquired source');
assert.deepEqual(item('shared').groups,['a','b'],'All proven owners are retained, no unique display parent is chosen');
assert.equal(item('shared').groupStatus,'shared');
assert.deepEqual(item('unknown').groups,[],'Directory labels, hosting and incomplete proof cannot supply a publisher');
assert.equal(item('unknown').groupStatus,'unknown');
assert.deepEqual(item('page').groups,['a'],'An explicit entity-page binding can share its organization color');
assert.deepEqual(item('page').groupRelationTypes,['entity_page'],'Page bindings remain distinct from publisher ownership');
assert.deepEqual(item('shared').groupRelationTypes,['source_ownership']);
assert.deepEqual(item('b').groups,['b'],'Organization identity does not inherit its navigation or administrative parent color');
assert.equal(item('shared').colorKey,'shared');
assert.equal(item('unknown').colorKey,'unknown');
assert.notEqual(item('a').colorKey,item('b').colorKey);
assert.equal(item('page').colorKey,item('a').colorKey);
assert.equal(appearanceColor(item('page'),'dark'),appearanceColor(item('a'),'dark'));
assert.notEqual(appearanceColor(item('shared'),'dark'),appearanceColor(item('unknown'),'dark'));
assert.notEqual(appearanceColor(item('a'),'dark'),appearanceColor(item('a'),'light'));
const geometry=layoutStructure(snapshot);
for(const [id,node] of model.actual) {
    assert.equal(node.presentation.connectedSourceCount,geometry.sizes[id].connectedSourceCount,'Scene and solver use the same source-count projection');
    assert.equal(node.radius,geometry.sizes[id].radius,'Solver and renderer agree on count-based radii');
}
const navigationChanged={...snapshot,relations:[...snapshot.relations,edge('extra-nav','page','b','navigation_link')]};
const changed=buildAtlas(navigationChanged,{deferLayout:true});
for(const [id,node] of model.actual)assert.deepEqual(changed.actual.get(id).presentation,node.presentation,
    'Changing the selected navigation forest cannot change factual color groups or sizes');
const reordered=buildAtlas({nodes:[...snapshot.nodes].reverse(),relations:[...snapshot.relations].reverse()},{deferLayout:true});
for(const [id,node] of model.actual)assert.deepEqual(reordered.actual.get(id).presentation,node.presentation,'Enumeration order is immaterial');
const renamed={...snapshot,nodes:snapshot.nodes.map(node=>({...node,label:'Renamed '+node.label,state:'failed'}))};
assert.equal(atlasLayoutSignature(renamed),atlasLayoutSignature(snapshot));
assert.equal(refreshAtlasRecords(model,renamed),true);
assert.deepEqual(item('shared').groups,['a','b'],'Metadata refresh preserves identities and count/color inputs');
assert.equal(JSON.stringify(snapshot),before,'Appearance is derived data, never written back into facts');
const sharedPage=buildAtlas({...snapshot,relations:[...snapshot.relations,edge('second-binding','b','page','entity_page')]},{deferLayout:true});
assert.deepEqual(sharedPage.actual.get('page').presentation.groups,['a','b']);
assert.deepEqual(sharedPage.actual.get('page').presentation.groupRelationTypes,['entity_page'],
    'A page bound to multiple organizations is not relabeled as an owned source');
const cyclic={nodes:[org('x'),org('y'),src('s')],relations:[edge('xy','x','y','organization_membership'),
    edge('yx','y','x','organization_membership'),edge('xs','x','s','source_ownership')]};
const cycleModel=buildAtlas(cyclic,{deferLayout:true});
assert.equal(cycleModel.actual.get('x').presentation.connectedSourceCount,1);
assert.equal(cycleModel.actual.get('y').presentation.connectedSourceCount,1,'Cycles terminate and do not multiply counts');
const many={nodes:[org('owner'),...Array.from({length:128},(_,i)=>src('s'+i))],
    relations:Array.from({length:128},(_,i)=>edge('owned'+i,'owner','s'+i,'source_ownership'))};
assert.equal(buildAtlas(many,{deferLayout:true}).actual.get('owner').radius,10,'Counts are logarithmic and capped');
const samePage={nodes:[org('owner'),{...src('one'),url:'https://fixture.example/list'},
    {...src('two'),url:'https://fixture.example/list'}],relations:[edge('one','owner','one','source_ownership'),
    edge('two','owner','two','source_ownership')]};
assert.equal(buildAtlas(samePage,{deferLayout:true}).actual.get('owner').presentation.connectedSourceCount,2,
    'Distinct configured source identities on one page must not be merged by URL');
console.log('Atlas appearance: shared owners, unknowns, distinct identities and bounded counts match the geometry projection.');
