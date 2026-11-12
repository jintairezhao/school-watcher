import assert from 'node:assert/strict';
import {StructureLayoutTask,structureLayoutInput} from '../../frontend/static/js/structure-layout-task.js';
import {computeStructureLayout} from '../../frontend/static/js/structure-layout-protocol.js';

const workers=[];
const createWorker=()=>{const worker={terminated:false,postMessage(message){this.request=message;},
    terminate(){this.terminated=true;},reply(overrides={}){this.onmessage({data:{...this.request,kind:'completed',
        result:{positions:{a:{x:1,y:2,z:3}}},...overrides}});}};workers.push(worker);return worker;};
const task=new StructureLayoutTask({createWorker}),input={nodes:[{id:'a',entity_type:'page'}],relations:[]};
const before=JSON.stringify(input);
const first=task.run(input,{structureToken:'epoch-a:1',signature:'geometry-a'});
const firstWorker=workers[0];
const second=task.run(input,{structureToken:'epoch-a:2',signature:'geometry-b'});
assert.equal((await first).status,'superseded');
assert.equal(firstWorker.terminated,true);
firstWorker.reply();
workers[1].reply({kind:'started'});assert.equal(task.pending.started,true);
workers[1].reply();
const result=await second;
assert.equal(result.status,'completed');assert.equal(result.structureToken,'epoch-a:2');
assert.equal(workers[1].terminated,true);
assert.equal(task.pending,null);

for(const overrides of [{generation:0},{structureToken:'old-epoch:2'},{signature:'obsolete-layout'}]){
    const pending=task.run(input,{structureToken:'epoch-b:1',signature:'geometry-c'});
    workers.at(-1).reply(overrides);
    assert.equal((await pending).error,'layout_identity_mismatch');
}
for(const positions of [{},{ghost:{x:1,y:2,z:3}},{a:{x:NaN,y:1,z:2}}]){
    const pending=task.run(input,{structureToken:'epoch-b:1',signature:'geometry-c'});
    workers.at(-1).reply({result:{positions}});
    assert.equal((await pending).error,'invalid_layout_coordinates');
}
const failed=task.run(input,{structureToken:'epoch-b:2',signature:'geometry-c'});
workers.at(-1).onerror();assert.equal((await failed).status,'failed');
const cancelled=task.run(input,{structureToken:'epoch-b:3',signature:'geometry-c'});
task.cancel();assert.equal((await cancelled).status,'cancelled');
const disposed=task.run(input,{structureToken:'epoch-b:4',signature:'geometry-c'});
task.dispose();assert.equal((await disposed).status,'disposed');
assert.equal((await task.run(input,{structureToken:'epoch-b:5',signature:'geometry-c'})).status,'disposed');
const unavailable=new StructureLayoutTask({createWorker(){throw new Error('unsupported');}});
assert.equal((await unavailable.run(input,{structureToken:'x',signature:'y'})).error,'layout_worker_unavailable');
assert.equal((await unavailable.run(input,{})).error,'missing_snapshot_identity');
const noClone=new StructureLayoutTask({createWorker:()=>({postMessage(){throw new Error('clone');},terminate(){}})});
assert.equal((await noClone.run(input,{structureToken:'x',signature:'y'})).status,'failed');

const request={kind:'layout',generation:1,structureToken:'epoch:1',signature:'a',input};
const computed=computeStructureLayout(request);
assert.equal(computed.kind,'completed');
assert.deepEqual(Object.keys(computed.result.positions),['a']);
assert.equal(computed.structureToken,request.structureToken);
assert.equal(computeStructureLayout({...request,generation:-1}).kind,'failed');
assert.equal(computeStructureLayout({...request,input:{nodes:[{id:'a'},{id:'a'}],relations:[]}}).kind,'failed');
assert.equal(JSON.stringify(input),before);
const proof={reference_url:'https://example.edu/',locator:'nav>a',content_hash:'saved',observed_at:'2026-10-23'};
const full={nodes:[{id:'a',entity_type:'organization',label:'A',personal_status:'kept on page'},
    {id:'b',entity_type:'source',configured:true}],
    relations:[{id:'rel',source:'a',target:'b',type:'source_ownership',evidence:[proof,{...proof,locator:'footer>a'}]}],
    groups:[{id:'display',kind:'display',label:'View',members:['a','b'],relationIds:['rel']}],
    previousPositions:new Map([['a',{x:2,y:3,z:4}]]),exploration_events:[{large:'not needed for geometry'}]};
const geometry=structureLayoutInput(full);
assert.equal(full.relations[0].evidence.length,2);
assert.equal(geometry.relations[0].evidence.length,1);
assert.equal(geometry.nodes[0].personal_status,undefined);
assert.equal(geometry.exploration_events,undefined);
assert.deepEqual(computeStructureLayout({...request,input:geometry}).result.positions,
    computeStructureLayout({...request,input:full}).result.positions);
assert.deepEqual(geometry.previousPositions,{a:{x:2,y:3,z:4}});
console.log('Experimental layout tasks fence versions, supersede stale workers and settle failures/cancellation without mutating facts.');
