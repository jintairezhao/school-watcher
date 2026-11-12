import assert from 'node:assert/strict';
import {layoutStructure,measureStructureLayout} from '../../frontend/static/js/structure-layout.js';

// Synthetic fixtures test algorithm behavior only. They are not evidence about
// discovery coverage, good parameter values, or usability on university sites.
const proof={kind:'web_observation',reference_url:'https://example.edu/',locator:'main > a',
    content_hash:'saved-fixture',observed_at:'2026-10-21T00:00:00Z'};
const relation=(id,source,target,type)=>({id,source,target,type,evidence:[proof]});
const nodes=[{id:'a',entity_type:'organization'},{id:'b',entity_type:'organization'},
    {id:'shared',entity_type:'source',configured:true},{id:'page',entity_type:'page'},
    {id:'other',entity_type:'source',configured:true},{id:'unrelated',entity_type:'page'}];
const relations=[relation('owns-a','a','shared','source_ownership'),relation('owns-b','b','shared','source_ownership'),
    relation('host','page','shared','source_on_page'),relation('owns-other','b','other','source_ownership'),
    relation('cross','a','other','cross_entity_reference'),relation('entity-page','a','page','entity_page'),
    relation('nav','page','unrelated','navigation_link')];
const input={nodes,relations}, untouched=JSON.stringify(input), first=layoutStructure(input);
assert.equal(JSON.stringify(input),untouched,'Layout must never mutate facts or their evidence');
assert.deepEqual(Object.keys(first.positions).sort(),nodes.map(n=>n.id).sort());
assert.deepEqual(first,layoutStructure(input),'The same material and seed must reproduce exactly');
assert.deepEqual(first.positions,layoutStructure({nodes:[...nodes].reverse(),relations:[...relations].reverse()}).positions,
    'Input enumeration order must not change the outcome');
assert.equal(first.constraints.relations,relations.length,'Every supported evidenced relation participates');
assert.ok(first.constraints.participatingRelationIds.includes('cross'),'Cross-group facts cannot be dropped by a spanning tree');
assert.equal(first.constraints.groups.filter(group=>group.members.includes('shared')).length,2,
    'A shared source belongs to both explicit groups');
assert.equal(first.sizes.a.connectedSourceCount,1);
assert.equal(first.sizes.b.connectedSourceCount,2);
assert.ok(first.sizes.b.radius>=first.sizes.a.radius);
assert.equal(first.metrics.sphereOverlaps,0);
assert.equal(first.metrics.labelOcclusion,'not_measured');

const repeated={nodes,relations:[...relations,{...relations.at(-1),id:'nav-second-locator',
    evidence:[{...proof,locator:'footer > a'}]}]};
assert.deepEqual(layoutStructure(repeated).positions,first.positions,'More locators do not strengthen navigation attraction');
const withoutCross=layoutStructure({nodes,relations:relations.filter(r=>r.id!=='cross')});
assert.notDeepEqual(withoutCross.positions,first.positions,'A cross-group relation changes the optimization');
const unsafe=layoutStructure({nodes,relations:[{...relations[0],id:'unproven',evidence:[]},
    {...relations[1],id:'theme-only',type:'topic_similarity'}]});
assert.equal(unsafe.constraints.relations,0);
assert.equal(unsafe.constraints.groups.length,0,'Names and unproven links cannot create layout ownership groups');
assert.equal(unsafe.constraints.omitted.length,2);

const updated={nodes:[...nodes,{id:'new-source',entity_type:'source',configured:true}],
    relations:[...relations,relation('new-owner','a','new-source','source_ownership')],previousPositions:first.positions};
const result=layoutStructure(updated,{maxDisplacement:12});
assert.equal(result.metrics.updateMovement.count,nodes.length);
assert.ok(result.metrics.updateMovement.max<=12+1e-9,'Known nodes stay within the stated update displacement limit');
assert.ok(Number.isFinite(result.positions['new-source'].z));
assert.equal(measureStructureLayout(updated,result.positions).nodes,nodes.length+1);
const grouped=layoutStructure({...input,groups:[{id:'chosen',kind:'display',members:['page','unrelated']}]});
assert.ok(grouped.constraints.groups.some(group=>group.origin==='explicit_display_group'));
assert.equal(layoutStructure({nodes:[],relations:[]}).metrics.nodes,0);
assert.throws(()=>layoutStructure({nodes:[nodes[0],nodes[0]],relations:[]}),/Duplicate entity/);
console.log('Offline 3D layout prototype: deterministic facts, all relation types, shared groups, evidence deduplication and update bounds passed.');
