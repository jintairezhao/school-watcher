/* One cancellable geometry task. Snapshot identity is fenced across every reply. */
export function structureLayoutInput(snapshot) {
    const input={
        nodes:(snapshot.nodes||[]).map(node=>({id:node.id,entity_type:node.entity_type,
            label:node.label,configured:node.configured})),
        relations:(snapshot.relations||[]).map(edge=>({id:edge.id,source:edge.source,target:edge.target,
            type:edge.type,observed_relation:edge.observed_relation,
            // The page keeps complete facts and proofs. Geometry needs only one
            // existing witness to admit this relation; extra locators add no force.
            evidence:(edge.evidence||[]).filter(proof=>proof.reference_url&&proof.locator&&proof.content_hash)
                .slice(0,1).map(proof=>({reference_url:proof.reference_url,locator:proof.locator,content_hash:proof.content_hash}))})),
    };
    if(snapshot.groups)input.groups=snapshot.groups.map(group=>({id:group.id,kind:group.kind,label:group.label,
        members:[...(group.members||[])],relationIds:[...(group.relationIds||[])]}));
    if(snapshot.previousPositions)input.previousPositions=Object.fromEntries(
        [...(snapshot.previousPositions instanceof Map?snapshot.previousPositions:Object.entries(snapshot.previousPositions))]
            .map(([id,point])=>[id,{x:point.x,y:point.y,z:point.z}]));
    return input;
}

export class StructureLayoutTask {
    constructor({createWorker=()=>new Worker(new URL('./structure-layout-worker.js',import.meta.url),{type:'module'})}={}) {
        this.createWorker=createWorker;
        this.generation=0;
        this.pending=null;
        this.disposed=false;
    }
    finish(pending,response) {
        if(this.pending!==pending)return;
        this.pending=null;
        pending.worker?.terminate();
        pending.resolve(response);
    }
    cancel(status='cancelled') {
        const pending=this.pending;
        if(pending)this.finish(pending,{status,generation:pending.generation,
            structureToken:pending.structureToken,signature:pending.signature});
    }
    run(input,{structureToken,signature,options={}}={}) {
        if(this.disposed)return Promise.resolve({status:'disposed'});
        if(typeof structureToken!=='string'||!structureToken||typeof signature!=='string'||!signature)
            return Promise.resolve({status:'failed',error:'missing_snapshot_identity'});
        this.cancel('superseded');
        const generation=++this.generation;
        return new Promise(resolve=>{
            const pending={generation,structureToken,signature,resolve,worker:null};
            this.pending=pending;
            const identity={generation,structureToken,signature};
            try {
                const geometry=structureLayoutInput(input),ids=new Set(geometry.nodes.map(node=>node.id));
                const worker=this.createWorker();
                pending.worker=worker;
                worker.onmessage=event=>{
                    if(this.pending!==pending)return;
                    const data=event.data;
                    if(!data||data.generation!==generation||data.structureToken!==structureToken||data.signature!==signature)
                        return this.finish(pending,{...identity,status:'failed',error:'layout_identity_mismatch'});
                    if(data.kind==='started'){pending.started=true;return;}
                    if(data.kind==='completed'){
                        const positions=data.result?.positions;
                        if(!positions||Object.keys(positions).length!==ids.size||Object.entries(positions).some(([id,p])=>
                            !ids.has(id)||!p||!['x','y','z'].every(axis=>Number.isFinite(p[axis]))))
                            return this.finish(pending,{...identity,status:'failed',error:'invalid_layout_coordinates'});
                        this.finish(pending,{...identity,status:'completed',result:data.result});
                    }
                    else this.finish(pending,{...identity,status:'failed',error:data.error||'layout_failed'});
                };
                worker.onerror=()=>this.finish(pending,{...identity,status:'failed',error:'layout_worker_failed'});
                worker.onmessageerror=()=>this.finish(pending,{...identity,status:'failed',error:'layout_message_failed'});
                worker.postMessage({kind:'layout',...identity,input:geometry,options});
            } catch {
                this.finish(pending,{...identity,status:'failed',error:'layout_worker_unavailable'});
            }
        });
    }
    dispose() {
        this.disposed=true;
        this.cancel('disposed');
    }
}
