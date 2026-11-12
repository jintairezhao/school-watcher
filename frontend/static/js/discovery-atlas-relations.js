/* A display projection over existing relations. Groups are never new entities or parents. */
import {displayLink} from './discovery-atlas-view.js';

const aliases={navigation:'navigation_link',website_navigation:'navigation_link',directory:'directory_listing',cross_department:'cross_entity_reference'};
const ends=edge=>[edge.from?.id||edge.source,edge.to?.id||edge.target];
function append(groups,edge,anchorId,direction,neighborId) {
    const type=aliases[edge.type]||edge.type,predicate=edge.observed_relation||null;
    const id=JSON.stringify([anchorId,direction,type,predicate]);
    if(!groups.has(id))groups.set(id,{id,anchorId,direction,type,predicate,links:[],neighbors:new Set()});
    const group=groups.get(id);group.links.push(edge);group.neighbors.add(neighborId);return id;
}
export function relationGroups(links,anchorId) {
    const groups=new Map();
    for(const edge of links){const [from,to]=ends(edge);if(!from||!to||from===to)continue;
        if(from===anchorId)append(groups,edge,anchorId,'outgoing',to);
        if(to===anchorId)append(groups,edge,anchorId,'incoming',from);
    }
    return [...groups.values()].sort((a,b)=>a.id.localeCompare(b.id));
}

export class AtlasRelationView {
    constructor(links,{limit=24}={}) {
        this.links=links;this.limit=limit;this.groups=new Map();this.edgeGroups=new Map();this.byAnchor=new Map();
        for(const edge of links){const [from,to]=ends(edge);if(!from||!to||from===to)continue;
            this.edgeGroups.set(edge.id,[append(this.groups,edge,from,'outgoing',to),append(this.groups,edge,to,'incoming',from)]);
        }
        for(const group of this.groups.values()){
            if(!this.byAnchor.has(group.anchorId))this.byAnchor.set(group.anchorId,[]);
            this.byAnchor.get(group.anchorId).push(group);
        }
    }
    groupsFor(id) {return this.byAnchor.get(id)||[];}
    dense(group) {return group.links.length>this.limit;}
    summaryCount(id,expanded=new Set()) {
        return new Set(this.groupsFor(id).filter(group=>this.dense(group)&&!expanded.has(group.id))
            .flatMap(group=>group.links.map(edge=>edge.id))).size;
    }
    project(entries,{selected=null,hovered=null,forced=new Set(),revealed=new Set(),expanded=new Set(),enabled=true}={}) {
        const available=new Map(entries.map(item=>[item.id,item])),collapsed=new Set(),anchors=new Set();
        if(enabled)for(const group of this.groups.values())if(this.dense(group)&&!expanded.has(group.id)){
            collapsed.add(group.id);anchors.add(group.anchorId);
        }
        const candidates=this.links.filter(edge=>{const [from,to]=ends(edge);return available.has(from)&&available.has(to);});
        const individual=candidates.filter(edge=>{
            const ids=this.edgeGroups.get(edge.id)||[];
            if(forced.has(edge.id)||(enabled&&ids.some(id=>expanded.has(id))))return true;
            if(enabled&&ids.some(id=>collapsed.has(id)))return false;
            return displayLink(edge,selected,hovered);
        });
        const touched=new Set(individual.flatMap(ends)),summarized=new Set(),boundPages=new Set();
        for(const edge of candidates){const [from,to]=ends(edge);
            if((this.edgeGroups.get(edge.id)||[]).some(id=>collapsed.has(id))){summarized.add(from);summarized.add(to);}
            if(edge.type==='entity_page'||edge.type==='source_on_page'&&available.get(to).record.configured)boundPages.add(edge.type==='entity_page'?to:from);
        }
        const nodes=entries.filter(item=>!enabled||!summarized.has(item.id)||touched.has(item.id)||anchors.has(item.id)||
            revealed.has(item.id)||item===selected||item===hovered||boundPages.has(item.id)||
            item.record.entity_type&&item.record.entity_type!=='page'||['root','unit','directory'].includes(item.record.kind));
        return {nodes,edges:individual,availableRelations:candidates.length,summarizedNodes:entries.length-nodes.length,
            collapsedGroups:collapsed.size};
    }
}
