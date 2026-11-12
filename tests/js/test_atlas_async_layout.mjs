import assert from 'node:assert/strict';
import {AtlasLayoutController,atlasGeometryInput} from '../../frontend/static/js/discovery-atlas-layout.js';
import {buildAtlas,atlasLayoutSignature,refreshAtlasRecords} from '../../frontend/static/js/discovery-atlas-model.js';

// Protocol fixtures only; real graph/renderer acceptance is a separate browser run.
const evidence=[{reference_url:'https://example.edu.cn/',locator:'nav>a',content_hash:'saved'}];
const snapshot={structure_version:{epoch:'one',revision:1},structure_token:'one:1',nodes:[
    {id:'root',entity_type:'page',kind:'root',url:'https://example.edu.cn/',label:'Home'},
    {id:'source',entity_type:'source',label:'Notices',configured:true}],relations:[
    {id:'host',source:'root',target:'source',type:'source_on_page',evidence}]};
class Task {
    calls=[];pending=null;disposed=false;
    cancel(){if(this.pending){this.pending.resolve({status:'superseded'});this.pending=null;}}
    run(input,options){this.cancel();return new Promise(resolve=>{const call={input,options,resolve};this.calls.push(call);this.pending=call;});}
    complete(){const call=this.pending;this.pending=null;call.resolve({status:'completed',result:{
        positions:Object.fromEntries(call.input.nodes.map((node,index)=>[node.id,{x:10+index,y:20,z:30}])),
        constraints:{relations:call.input.relations.length,omitted:[]},metrics:{sphereOverlaps:0}}});}
    dispose(){this.disposed=true;this.cancel();}
}
const task=new Task(),ready=[],controller=new AtlasLayoutController({task,onReady:(model,state)=>ready.push({model,state})});
const model=buildAtlas(snapshot,{deferLayout:true}),before=JSON.stringify(snapshot),signature=atlasLayoutSignature(snapshot);
const first=controller.update(model,snapshot,{signature});
assert.equal(task.calls.length,1);
assert.equal(controller.state.status,'pending');
const renamed={...snapshot,structure_token:'one:2',structure_version:{epoch:'one',revision:2},
    nodes:snapshot.nodes.map(node=>({...node,label:node.id==='source'?'Current title':node.label,state:'connected'}))};
assert.equal(refreshAtlasRecords(model,renamed),true);
controller.update(model,renamed,{signature:atlasLayoutSignature(renamed)});
assert.equal(task.calls.length,1,'A label/status revision reuses pending geometry');
task.complete();await first;
assert.equal(controller.state.token,'one:2');assert.equal(controller.state.computedToken,'one:1');
assert.equal(model.actual.get('source').record.label,'Current title','Old result must never overwrite current facts');
assert.equal(model.layoutPending,false);
controller.update(model,renamed,{signature});
assert.equal(task.calls.length,1);assert.equal(ready.length,1,'Metadata refresh must not repaint/restart layout');

const changed={...renamed,relations:[...snapshot.relations,
    {id:'ownership',source:'root',target:'source',type:'cross_entity_reference',evidence}],structure_token:'one:3'};
const nextModel=buildAtlas(changed,{deferLayout:true});
const pending=controller.update(nextModel,changed,{signature:atlasLayoutSignature(changed)});
assert.deepEqual(task.calls.at(-1).input.previousPositions,{root:{x:10,y:20,z:30},source:{x:11,y:20,z:30}});
assert.equal(task.calls.at(-1).options.options.nodeRadii.source,nextModel.actual.get('source').radius);
controller.update(model,renamed,{signature});
await pending;
assert.equal(controller.state.status,'ready');assert.equal(controller.state.signature,signature);
assert.equal(task.pending,null,'Returning to cached geometry cancels obsolete work');

const reset={...renamed,structure_token:'two:1',structure_version:{epoch:'two',revision:1}};
const resetModel=buildAtlas(reset,{deferLayout:true});
const resetWork=controller.update(resetModel,reset,{signature});
assert.equal(task.calls.at(-1).input.previousPositions,undefined,'Epoch reset must not use old anchors');
task.complete();await resetWork;
const expanded=controller.update(resetModel,reset,{signature:atlasLayoutSignature(reset,1.8),spacing:1.8});
assert.equal(task.calls.at(-1).options.options.scale,108);
assert.equal(task.calls.at(-1).input.previousPositions,undefined,'Spacing reflow must not be pinned to old positions');
task.pending.resolve({status:'failed',error:'test failure'});task.pending=null;await expanded;
const calls=task.calls.length;
controller.update(resetModel,reset,{signature:atlasLayoutSignature(reset,1.8),spacing:1.8});
assert.equal(task.calls.length,calls,'Polling cannot restart an unavailable worker forever');
controller.dispose();assert.equal(task.disposed,true);
assert.equal(JSON.stringify(snapshot),before);
const aliases=atlasGeometryInput(model,{relations:[{...snapshot.relations[0],type:'navigation'}]});
assert.equal(aliases.relations[0].type,'navigation_link');
assert.equal(aliases.relations[0].id,'host');
console.log('Scene layout controller preserves current facts, reuses identical geometry and fences changed relations, epochs, spacing and failed workers.');
