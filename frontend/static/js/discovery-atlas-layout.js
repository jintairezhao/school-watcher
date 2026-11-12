/* Geometry only: the current scene keeps its own facts, selection and proofs. */
import {StructureLayoutTask} from './structure-layout-task.js';

const aliases={navigation:'navigation_link',website_navigation:'navigation_link',directory:'directory_listing',
    cross_department:'cross_entity_reference'};
export function atlasGeometryInput(model,snapshot) {
    const ids=new Set(model.actual.keys());
    return {nodes:[...model.actual.values()].map(item=>item.record),relations:(snapshot.relations||[])
        .filter(edge=>edge.source!==edge.target&&ids.has(edge.source)&&ids.has(edge.target))
        .map(edge=>({...edge,type:aliases[edge.type]||edge.type}))};
}

export class AtlasLayoutController {
    constructor({task=new StructureLayoutTask(),onReady=()=>{},onState=()=>{}}={}) {
        this.task=task;this.onReady=onReady;this.onState=onState;
        this.latest=null;this.job=null;this.settled=null;this.failedKey=null;this.state={status:'idle'};this.disposed=false;
    }
    setState(state) {this.state=state;this.onState(state);}
    readyState(settled,current) {
        return {status:'ready',token:current.token,computedToken:settled.token,signature:current.signature,
            relationCount:settled.result.constraints.relations,omitted:settled.result.constraints.omitted,
            metrics:settled.result.metrics};
    }
    update(model,snapshot,{signature,spacing=1}={}) {
        if(this.disposed)return;
        const epoch=String(snapshot.structure_version?.epoch||snapshot._transport?.version?.epoch||'legacy');
        const token=snapshot.structure_token||JSON.stringify(snapshot.structure_version||{});
        const latest={model,snapshot,signature,spacing,epoch,token};
        this.latest=latest;
        if(this.settled?.epoch===epoch&&this.settled.signature===signature){
            if(this.job){this.job=null;this.task.cancel('superseded');}
            if(this.settled.appliedModel===model)this.setState(this.readyState(this.settled,latest));
            else this.apply(this.settled,latest);
            return;
        }
        if(this.job?.epoch===epoch&&this.job.signature===signature){
            if(this.state.token!==token)this.setState({...this.state,token});
            return;
        }
        const key=JSON.stringify([epoch,signature]);
        if(this.failedKey===key){this.setState({...this.state,token});return;}
        const input=atlasGeometryInput(model,snapshot);
        if(this.settled?.epoch===epoch&&this.settled.spacing===spacing)input.previousPositions=this.settled.result.positions;
        const job={signature,spacing,epoch,token};this.job=job;this.failedKey=null;
        this.setState({status:'pending',token,signature});
        const nodeRadii=Object.fromEntries([...model.actual].map(([id,item])=>[id,item.radius]));
        job.promise=this.task.run(input,{structureToken:token,signature,
            options:{scale:60*spacing,seed:271828,iterations:90,landmarkCount:32,nodeRadii}}).then(response=>{
            if(this.disposed||this.job!==job)return;
            this.job=null;
            if(response.status!=='completed'){
                this.failedKey=key;
                this.setState({status:'failed',token:this.latest.token,signature,error:response.error||response.status});
                return;
            }
            const current=this.latest;
            if(current.epoch!==epoch||current.signature!==signature)return;
            const settled={...job,result:response.result};this.settled=settled;
            this.apply(settled,current);
        });
        return job.promise;
    }
    apply(settled,current) {
        const positions=settled.result.positions,ids=[...current.model.actual.keys()];
        if(ids.length!==Object.keys(positions).length||ids.some(id=>!positions[id])){
            this.setState({status:'failed',token:current.token,signature:current.signature,error:'layout_node_mismatch'});
            return;
        }
        for(const id of ids){const item=current.model.actual.get(id);item.position={...positions[id]};
            item.layoutSize=settled.result.sizes?.[id];}
        current.model.layoutPending=false;
        settled.appliedModel=current.model;
        this.setState(this.readyState(settled,current));
        this.onReady(current.model,this.state);
    }
    dispose() {this.disposed=true;this.job=null;this.task.dispose();}
}
