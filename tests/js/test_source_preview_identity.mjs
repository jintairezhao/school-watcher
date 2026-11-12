import assert from 'node:assert/strict';
import {buildAtlas,membershipRoute,accessRoute} from '../../frontend/static/js/discovery-atlas-model.js';
import {atlasGeometryInput} from '../../frontend/static/js/discovery-atlas-layout.js';
import {layoutCompoundStructure} from '../../frontend/static/js/structure-layout.js';

const source=id=>({id:`source:${id}`,entity_type:'source',source_id:id,kind:'channel',configured:true,
    label:id===1?'原联合订阅':'通知公告',url:`https://example.edu.cn/${id}/`});
const proof={reference_url:'https://example.edu.cn/',locator:'section a',content_hash:'a'.repeat(64),observed_at:'2026-10-25T08:00:00Z'};
const snapshot={nodes:[source(1),source(23),source(73)],relations:[23,73].map(id=>({id:`preview:${id}`,
    source:'source:1',target:`source:${id}`,type:'source_preview',directed:true,evidence:[proof],
    assertions:[{kind:'configured_column_preview',full_column_equivalent:false}]}))};
const model=buildAtlas(snapshot,{deferLayout:true});
assert.equal(model.actual.size,3,'The combined scope and both columns retain their own source identities');
assert.equal(model.links.length,2,'Observed preview relationships reach the model');
assert.ok(model.links.every(edge=>!edge.primary),'Preview association cannot become a parent relation');
for(const id of [23,73]){
    const target=model.actual.get(`source:${id}`);
    assert.equal(target.parent,null);
    assert.equal(membershipRoute(model,target).links.length,0,'Preview links do not imply organizational membership');
    assert.equal(accessRoute(model,target).links.length,0,'Preview links cannot substitute for a recorded website route');
}
assert.equal(atlasGeometryInput(model,snapshot).relations.length,2,'Real associations remain available to the geometry view');
const layout=layoutCompoundStructure(atlasGeometryInput(model,snapshot),{iterations:4});
assert.deepEqual([...layout.constraints.participatingRelationIds].sort(),['preview:23','preview:73'],
    'Both actual preview associations participate without being turned into ownership');
console.log('Source preview identities and relationship semantics passed');
