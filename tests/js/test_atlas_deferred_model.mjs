import assert from 'node:assert/strict';
import {buildAtlas,atlasLayoutSignature} from '../../frontend/static/js/discovery-atlas-model.js';

const evidence=[{reference_url:'https://example.edu.cn/',locator:'nav>a',content_hash:'retained',observed_at:'2026-10-23'}];
const input={nodes:[{id:'root',entity_type:'page',kind:'root',url:'https://example.edu.cn/',label:'Home'},
    {id:'a',entity_type:'page',url:'https://example.edu.cn/a',label:'A'},
    {id:'b',entity_type:'source',configured:true,label:'B'}],relations:[
    {id:'a',source:'root',target:'a',type:'navigation_link',access:true,evidence},
    {id:'b',source:'a',target:'b',type:'source_on_page',evidence}]};
const previousPositions={root:{x:10,y:20,z:30},a:{x:40,y:50,z:60}};
const original=JSON.stringify(input),metadata=buildAtlas(input,{deferLayout:true,previousPositions});
assert.equal(metadata.layoutPending,true,'Deferred scene construction must not run the legacy position solver');
assert.deepEqual(metadata.actual.get('a').position,previousPositions.a);
assert.notEqual(metadata.actual.get('a').position,previousPositions.a,'Retained positions are copied');
assert.equal(metadata.links.length,2);
assert.ok([...metadata.entries.values()].every(item=>['x','y','z'].every(axis=>Number.isFinite(item.position[axis]))));
assert.equal(JSON.stringify(input),original);
assert.equal(atlasLayoutSignature(input),atlasLayoutSignature({...input,nodes:input.nodes.map(node=>({...node,label:'Changed label',state:'connected'}))}),
    'Progress and labels do not alter geometry');
assert.notEqual(atlasLayoutSignature(input),atlasLayoutSignature({...input,nodes:input.nodes.map(node=>node.id==='b'?{...node,configured:false}:node)}),
    'Configured source counts are part of the geometry input');
console.log('Deferred model preserves facts and retained positions while geometry signatures distinguish actual layout inputs.');
