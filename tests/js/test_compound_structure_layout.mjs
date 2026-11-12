import assert from 'node:assert/strict';
import {layoutCompoundStructure,structureCompoundGroups,directoryDisplayGroups} from '../../frontend/static/js/structure-layout.js';

// These fixtures verify solver boundaries, never website quality or facts.
const proof={reference_url:'https://fixture.example/roster',locator:'main a',content_hash:'retained-fixture'};
const edge=(id,source,target,type)=>({id,source,target,type,evidence:[proof]});
const org=id=>({id,entity_type:'organization'}),src=id=>({id,entity_type:'source',configured:true});
const input={nodes:[org('college'),org('department'),org('other-college'),src('shared'),src('local'),
    {id:'roster',entity_type:'directory_group'},{id:'page',entity_type:'page'}],relations:[
    edge('listed','roster','college','directory_listing'),
    edge('nested','college','department','organization_membership'),
    edge('owned-local','department','local','source_ownership'),
    edge('owned-shared-a','department','shared','source_ownership'),
    edge('owned-shared-b','other-college','shared','source_ownership'),
    edge('cross','other-college','local','cross_entity_reference'),
    edge('host','page','shared','source_on_page'),edge('nav','page','roster','navigation_link')]};
const original=JSON.stringify(input),first=layoutCompoundStructure(input);
assert.equal(JSON.stringify(input),original,'Layout cannot mutate facts/evidence');
assert.deepEqual(first,layoutCompoundStructure(input),'Fixed seed and input reproduce exactly');
assert.deepEqual(first.positions,layoutCompoundStructure({nodes:[...input.nodes].reverse(),relations:[...input.relations].reverse()}).positions);
assert.deepEqual(Object.keys(first.positions).sort(),input.nodes.map(n=>n.id).sort());
assert.deepEqual([...first.constraints.participatingRelationIds].sort(),input.relations.map(r=>r.id).sort());
assert.equal(first.metrics.sphereOverlaps,0);
assert.ok(first.constraints.coarseGeometryOnly);
assert.ok(first.constraints.coarseLinks.some(link=>link.relationIds.includes('cross')),'Cross-group evidence reaches the coarse solve');
assert.ok(first.metrics.nestedContainment.constraints>0,'Actual multi-level bounds, not only centroid cohesion');
assert.equal(first.metrics.labelOcclusion,'not_measured');
const groups=structureCompoundGroups(input),department=groups.find(g=>g.owner==='department');
assert.ok(department.parents.includes('compound:college'));
assert.equal(department.level,1);
assert.deepEqual(groups.filter(g=>g.members.includes('shared')).map(g=>g.owner).sort(),['college','department','other-college']);
assert.equal(Object.keys(first.positions).filter(id=>id==='shared').length,1,'There is only one shared node identity');
assert.equal(first.metrics.compoundContainment.outsideActual,0,'Reported envelopes enclose the actual node radii');
assert.equal(first.metrics.nestedContainment.violated,0,'Parent envelopes include nested envelopes');
assert.ok('compoundTargetExcess' in first.metrics,'Unmet soft targets are reported separately');
assert.ok(!first.constraints.disjointPairs.some(pair=>pair.includes('compound:department')&&pair.includes('compound:other-college')),
    'Shared compounds may overlap; no forced separation or unique parent');
assert.equal(first.sizes.college.connectedSourceCount,2);
assert.equal(first.sizes['other-college'].connectedSourceCount,1);
assert.equal(first.sizes.roster.connectedSourceCount,0);
assert.ok(groups.every(g=>g.geometryOnly && !g.establishesOwnership));
const explicit={...input,groups:directoryDisplayGroups(input)},withDisplay=layoutCompoundStructure(explicit);
assert.ok(withDisplay.constraints.groups.some(g=>g.origin==='explicit_display_group'));
assert.equal(withDisplay.sizes.roster.connectedSourceCount,0,'Display bounds never count as ownership');
assert.equal(structureCompoundGroups(input).some(g=>g.owner==='roster'),false);

const repeated={...input,relations:[...input.relations,{...input.relations.at(-1),id:'another-locator',
    evidence:[{...proof,locator:'footer a'}]}]};
assert.deepEqual(layoutCompoundStructure(repeated).positions,first.positions,'Repeated navigation does not strengthen attraction');
assert.notDeepEqual(layoutCompoundStructure({...input,relations:input.relations.filter(r=>r.id!=='cross')}).positions,first.positions);

const cycle={nodes:[org('a'),org('b'),src('s')],relations:[edge('ab','a','b','organization_membership'),
    edge('ba','b','a','organization_membership'),edge('as','a','s','source_ownership')]};
const cyclic=layoutCompoundStructure(cycle);
assert.equal(cyclic.constraints.relations,3,'Cycles are not deleted to manufacture a tree');
assert.ok(cyclic.constraints.groups.every(g=>g.cyclePeers.length===1));
assert.equal(cyclic.metrics.nestedContainment.constraints,0,'A membership cycle is not an impossible hard inclusion tree');
assert.ok(Object.values(cyclic.positions).every(p=>Object.values(p).every(Number.isFinite)));

const unproven=layoutCompoundStructure({nodes:input.nodes,relations:[{...input.relations[1],evidence:[]},
    {...input.relations[2],type:'topic_similarity'}]});
assert.equal(unproven.constraints.groups.length,0);
assert.equal(unproven.constraints.relations,0);
assert.equal(unproven.constraints.omitted.length,2);
const updated=layoutCompoundStructure({...input,nodes:[...input.nodes,src('new')],
    relations:[...input.relations,edge('new-owner','department','new','source_ownership')],
    previousPositions:first.positions},{maxDisplacement:12});
assert.equal(updated.metrics.updateMovement.count,input.nodes.length);
assert.ok(updated.metrics.updateMovement.max<=12+1e-9);
assert.equal(layoutCompoundStructure({nodes:[],relations:[]}).metrics.nodes,0);
assert.throws(()=>layoutCompoundStructure(input,{landmarkCount:-1}),/Invalid landmark/);
const displayRadii=Object.fromEntries(input.nodes.map((node,index)=>[node.id,index===0?14:9]));
const physical=layoutCompoundStructure(input,{nodeRadii:displayRadii});
for(const [id,radius] of Object.entries(displayRadii))assert.equal(physical.sizes[id].radius,radius,
    'Collision handling must use the actual requested display geometry');
for(let a=0;a<input.nodes.length;a++)for(let b=a+1;b<input.nodes.length;b++){
    const first=input.nodes[a].id,second=input.nodes[b].id,p=physical.positions[first],q=physical.positions[second];
    assert.ok(Math.hypot(p.x-q.x,p.y-q.y,p.z-q.z)>=displayRadii[first]+displayRadii[second]-1e-8,
        'Reported non-overlap must hold for actual displayed sphere sizes');
}
assert.equal(physical.sizes.college.connectedSourceCount,2,'Visual radii cannot alter factual source counts');
assert.throws(()=>layoutCompoundStructure(input,{nodeRadii:{college:NaN}}),/Invalid node radius/);
console.log('Compound 3D prototype: nested bounds, shared members, cross-group links, cycles, evidence and incremental stability passed.');
