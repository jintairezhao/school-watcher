import assert from 'node:assert/strict';
import {buildAtlas,explorationTrace} from '../../frontend/static/js/discovery-atlas-model.js';
import {recordedGrowthPlan,growthAt} from '../../frontend/static/js/discovery-atlas-motion.js';

const school={url:'https://example.edu.cn/',name:'Local behavior fixture'};
const nodes=[{id:'home',entity_type:'page',kind:'root',label:'Home',url:school.url,path:[],sources:[]},
    {id:'directory',entity_type:'page',kind:'directory',label:'Directory',url:school.url+'departments/',path:[],sources:[]}];
const proof={reference_url:school.url,locator:'nav a',content_hash:'recorded-material'};
const event=(id,node,type,parent,second,extra={})=>({id,node_id:node,event_type:type,parent_event_id:parent,
    sequence:second,observed_at:`2026-10-22T01:00:${String(second).padStart(2,'0')}Z`,...extra});
const events=[event('start','home','exploration_started',null,1),
    event('discovered','directory','page_discovered','start',2,{evidence:[proof]}),
    event('home-end','home','page_inspection_finished','start',3),
    event('child-start','directory','page_inspection_started','discovered',4),
    event('child-end','directory','page_inspection_finished','child-start',5,{state:'fetched'})];
const snapshot={school,nodes,relations:[],exploration_events:events}, model=buildAtlas(snapshot);
const trace=explorationTrace(snapshot,model.actual.get('directory'),model);
assert.equal(trace.complete,true);
assert.equal(trace.renderComplete,true);
assert.deepEqual(trace.events.map(e=>e.id),['start','discovered','child-start','child-end']);
assert.deepEqual(trace.nodes.map(n=>n.id),['home','directory']);
assert.equal(trace.steps[0].edge.type,'discovered_from');
assert.equal(trace.steps[0].edge.evidence[0].content_hash,proof.content_hash);
assert.equal(model.links.length,0,'Historical discovery is not promoted into a current navigation or ownership fact');

const plan=recordedGrowthPlan(trace);
assert.ok(plan.entries.get('home').start<plan.entries.get('directory').start);
assert.equal(growthAt(plan,trace.nodes[1],0),0);
assert.equal(growthAt(plan,trace.nodes[1],1),1);
assert.deepEqual(recordedGrowthPlan(trace),plan,'Recorded ordering is deterministic');
assert.equal(recordedGrowthPlan({...trace,complete:false}),null,'Incomplete history never gets a fabricated replay');
assert.equal(recordedGrowthPlan({...trace,renderComplete:false}),null,'A hidden ancestor cannot be silently omitted from replay');

const missing=explorationTrace({...snapshot,exploration_events:events.slice(1)},'directory',model);
assert.equal(missing.gap,'missing_parent_event');
assert.equal(missing.complete,false);
const badProof=explorationTrace({...snapshot,exploration_events:events.map(e=>e.id==='discovered'?{...e,evidence:[]}:e)},'directory',model);
assert.equal(badProof.gap,'discovery_proof_incomplete');
assert.equal(badProof.complete,false);
const duplicate=explorationTrace({...snapshot,exploration_events:[...events,{...events[0],node_id:'directory'}]},'directory',model);
assert.equal(duplicate.gap,'event_identity_conflict');
assert.equal(duplicate.complete,false);
const partialModel=buildAtlas({...snapshot,nodes:[nodes[1]]});
assert.equal(explorationTrace(snapshot,'directory',partialModel).renderComplete,false);
assert.equal(explorationTrace(snapshot,'not-recorded',model).gap,'node_events_unavailable');
const chronology=explorationTrace({...snapshot,exploration_events:events.map(e=>e.id==='child-end'?{...e,observed_at:'2026-10-21T00:00:00Z'}:e)},'directory',model);
assert.equal(chronology.gap,'event_order_conflict');
console.log('Actual event trace: causal evidence, missing history, identity conflict, recorded growth order and read-only facts passed.');

const source={id:'source:7',entity_type:'source',source_id:7,config_hash:'saved-config',
    kind:'channel',label:'Source',url:nodes[1].url,path:[],sources:[]};
const sourceSnapshot={...snapshot,nodes:[...nodes,source],exploration_events:[
    ...events.map(e=>({...e,url:e.node_id==='home'?school.url:nodes[1].url,
        stream_id:'page_observations:fixture',...(e.id==='child-end'?{content_hash:'page-material'}:{})})),
    event('source-config:1',source.id,'source_configuration_saved','child-end',1,{
        observed_at:'2026-10-22T01:01:00Z',stream_id:'source_config_versions',source_id:7,
        current_configuration:true,config_hash:source.config_hash,url:source.url,
        evidence:[{reference_url:source.url,locator:'#notices',content_hash:'page-material'}]})]};
const sourceModel=buildAtlas(sourceSnapshot);
const sourceTrace=explorationTrace(sourceSnapshot,source.id,sourceModel);
assert.equal(sourceTrace.complete,true,'Configuration sequence 1 follows page sequence 5 by explicit parent, not numeric order');
assert.equal(sourceTrace.renderComplete,true);
assert.deepEqual(sourceTrace.nodes.map(n=>n.id),['home','directory',source.id]);
assert.equal(sourceTrace.steps.at(-1).edge.type,'source_configuration_saved');
assert.equal(sourceModel.links.length,0,'Execution edges do not become current facts');
assert.ok(recordedGrowthPlan(sourceTrace));
const altered=explorationTrace({...sourceSnapshot,exploration_events:sourceSnapshot.exploration_events.map(e=>
    e.node_id===source.id?{...e,current_configuration:false}:e)},source.id,sourceModel);
assert.equal(altered.complete,false);
assert.equal(altered.gap,'configuration_changed');
assert.equal(recordedGrowthPlan(altered),null);
const materialMismatch=explorationTrace({...sourceSnapshot,exploration_events:sourceSnapshot.exploration_events.map(e=>
    e.id==='child-end'?{...e,content_hash:'different'}:e)},source.id,sourceModel);
assert.equal(materialMismatch.gap,'cross_stream_proof_incomplete');
console.log('Source save replay: separate sequence domains, exact material, current config identity and no fact mutation passed.');

const appliedEvents=[events[0],events[2],
    event('applied-start','home','navigation_application_started','home-end',4),
    event('applied-found','directory','page_discovered','applied-start',5,{evidence:[proof]}),
    event('applied-end','home','navigation_application_finished','applied-start',6),
    event('next-child-start','directory','page_inspection_started','applied-found',7),
    event('next-child-end','directory','page_inspection_finished','next-child-start',8)];
const applied=explorationTrace({...snapshot,exploration_events:appliedEvents},'directory',model);
assert.equal(applied.complete,true);
assert.equal(applied.renderComplete,true);
assert.deepEqual(applied.events.map(e=>e.id),['start','home-end','applied-start','applied-found','next-child-start','next-child-end']);
assert.equal(applied.steps[0].edge.type,'discovered_from');
assert.equal(model.links.length,0);
const noOrigin=explorationTrace({...snapshot,exploration_events:appliedEvents.map(e=>
    e.id==='applied-start'?{...e,parent_event_id:null}:e)},'directory',model);
assert.equal(noOrigin.complete,false);
assert.equal(noOrigin.gap,'upstream_event_unknown');
console.log('Navigation application: explicit origin, recorded first enqueue and legacy history gap passed.');

const reviewedEvents=[...appliedEvents.map(e=>({...e,
    sequence:e.sequence>=4?e.sequence+1:e.sequence,
    ...(e.id==='applied-start'?{parent_event_id:'model-use'}:{})})),
    event('model-use','home','navigation_model_observed','home-end',4,{
        model_usage:{executions:[{use_kind:'ledger_reuse',total_tokens:70,usage_status:'confirmed'}]}})];
const reviewedSnapshot={...snapshot,exploration_events:reviewedEvents};
const untouched=JSON.stringify(reviewedSnapshot);
const reviewedTrace=explorationTrace(reviewedSnapshot,'directory',model);
assert.equal(reviewedTrace.complete,true);
assert.equal(reviewedTrace.renderComplete,true);
assert.ok(reviewedTrace.events.some(e=>e.id==='model-use'));
assert.deepEqual(reviewedTrace.nodes.map(n=>n.id),applied.nodes.map(n=>n.id));
assert.equal(reviewedTrace.steps.length,applied.steps.length,'Invoices do not create fact/discovery edges');
assert.equal(JSON.stringify(reviewedSnapshot),untouched,'Replay never changes ledger usage');
assert.ok(recordedGrowthPlan(reviewedTrace));
console.log('Model usage ancestry: observation remains replayable without creating facts or modifying invoices.');
