import assert from 'node:assert/strict';
import {AtlasRelationView,relationGroups} from '../../frontend/static/js/discovery-atlas-relations.js';
import {buildAtlas} from '../../frontend/static/js/discovery-atlas-model.js';
const evidence=[{reference_url:'https://fixture.example/',locator:'main a',content_hash:'fixture'}];
const nodes=[{id:'root',entity_type:'page',kind:'root',url:'https://fixture.example/',label:'Home'},
    ...Array.from({length:30},(_,i)=>({id:'p'+i,entity_type:'page',url:'https://fixture.example/'+i,label:'Entry '+i})),
    {id:'org',entity_type:'organization',label:'Org'},{id:'source',entity_type:'source',configured:true,label:'Source'}];
const edge=(id,source,target,type='navigation_link',predicate)=>({id,source,target,type,access:true,evidence,observed_relation:predicate});
const snapshot={nodes,relations:[...Array.from({length:30},(_,i)=>edge('n'+i,'root','p'+i)),
    edge('back','p0','root'),edge('listing','root','org','directory_listing'),
    edge('owns','org','source','source_ownership'),edge('host','p1','source','source_on_page')]};
const untouched=JSON.stringify(snapshot),model=buildAtlas(snapshot,{deferLayout:true}),entries=[...model.actual.values()];
const view=new AtlasRelationView(model.links),root=model.actual.get('root');
const group=view.groupsFor('root').find(g=>g.direction==='outgoing'&&g.type==='navigation_link');
assert.equal(group.links.length,30);assert.equal(group.neighbors.size,30);
const overview=view.project(entries);
assert.equal(overview.summarizedNodes,29,'Only pages represented by collapsed facts are summarized; hosted source page stays visible');
assert.ok(overview.nodes.includes(root));assert.ok(overview.nodes.includes(model.actual.get('p1')));
assert.ok(overview.nodes.includes(model.actual.get('org'))&&overview.nodes.includes(model.actual.get('source')));
assert.equal(view.summaryCount('root'),30);
assert.ok(overview.edges.every(e=>e.type!=='navigation_link'),'Reciprocal navigation cannot accidentally expand the dense group');
const expanded=view.project(entries,{expanded:new Set([group.id])});
assert.equal(expanded.nodes.length,entries.length);
assert.ok(group.links.every(edge=>expanded.edges.includes(edge)),'Expanding one endpoint shows every underlying relation even if another endpoint is dense');
assert.equal(view.summaryCount('root',new Set([group.id])),0);
const forced=new Set([group.links[0].id]);
const path=view.project(entries,{forced,revealed:new Set(['p0'])});
assert.ok(path.nodes.includes(model.actual.get('p0'))&&path.edges.includes(group.links[0]),'Recorded paths survive density summarization');
assert.equal(view.project(entries,{enabled:false}).nodes.length,entries.length,'Search and status filters can reveal individual entries');
const focused=view.project(entries,{selected:root});
assert.ok(!group.links.some(edge=>focused.edges.includes(edge)),'Focusing a dense hub summarizes it before explicit expansion');
const page=model.actual.get('p2');
assert.ok(view.project(entries,{selected:page}).nodes.includes(page),'Selected real nodes always remain selectable/visible');
const parallel=[edge('sub','root','org','organization_membership','sub_unit'),
    edge('attached','root','org','organization_membership','attached_unit'),edge('incoming','org','root')];
assert.equal(relationGroups(parallel,'root').length,3,'Direction and factual predicates are distinct groups, not an administrative tree');
assert.deepEqual(relationGroups(snapshot.relations,'root').find(g=>g.direction==='outgoing'&&g.type==='navigation_link').links.length,30);
assert.equal(JSON.stringify(snapshot),untouched);
assert.equal(model.actual.size,nodes.length);assert.equal(model.links.length,snapshot.relations.length);
console.log('Relation summaries retain every typed fact, both directions, shared endpoints, hosted sources, selected nodes and recorded paths.');
