/* Read-only presentation projection. Navigation, labels and URLs never grant ownership. */
const membershipTypes=new Set(['organization_membership','source_ownership','column_membership']);
const proved=edge=>(edge.evidence||[]).some(proof=>proof.reference_url&&proof.locator&&proof.content_hash);

export function configuredSourceSets(nodes,relations) {
    const byId=new Map(nodes.map(node=>[node.id,node])),outgoing=new Map(nodes.map(node=>[node.id,new Set()]));
    for(const edge of relations)if(membershipTypes.has(edge.type)&&proved(edge)&&edge.source!==edge.target
        &&byId.has(edge.source)&&byId.has(edge.target))outgoing.get(edge.source).add(edge.target);
    return new Map(nodes.map(node=>{
        const sources=new Set(),seen=new Set(),pending=[node.id];
        while(pending.length){const id=pending.pop();if(seen.has(id))continue;seen.add(id);
            const current=byId.get(id);
            if(current.entity_type==='source'&&current.configured)sources.add(id);
            for(const child of outgoing.get(id))pending.push(child);
        }
        return [node.id,sources];
    }));
}

export const sourceCountRadius=count=>3+Math.min(7,2*Math.log2(1+count));

export function structureAppearance({nodes=[],relations=[]}) {
    const byId=new Map(nodes.map(node=>[node.id,node])),counts=configuredSourceSets(nodes,relations);
    const owners=new Map(nodes.map(node=>[node.id,[]])),bindings=new Map(nodes.map(node=>[node.id,[]]));
    const parents=new Map(nodes.map(node=>[node.id,[]]));
    for(const edge of relations){
        if(!proved(edge)||edge.source===edge.target)continue;
        const a=byId.get(edge.source),b=byId.get(edge.target);if(!a||!b)continue;
        if(edge.type==='source_ownership'&&a.entity_type==='organization'&&b.entity_type==='source')owners.get(b.id).push(edge);
        if(edge.type==='entity_page'&&a.entity_type==='organization'&&b.entity_type==='page')bindings.get(b.id).push(edge);
        if(edge.type==='column_membership'&&a.entity_type==='source'&&b.entity_type==='source')parents.get(b.id).push(edge);
    }
    return new Map(nodes.map(node=>{
        const groups=new Set(),relationIds=new Set(),relationTypes=new Set();
        if(node.entity_type==='organization')groups.add(node.id);
        if(node.entity_type==='page')for(const edge of bindings.get(node.id)){
            groups.add(edge.source);relationIds.add(edge.id);relationTypes.add(edge.type);}
        if(node.entity_type==='source'){
            const pending=[node.id],seen=new Set();
            while(pending.length){const id=pending.pop();if(seen.has(id))continue;seen.add(id);
                for(const edge of owners.get(id)){groups.add(edge.source);relationIds.add(edge.id);relationTypes.add(edge.type);}
                for(const edge of parents.get(id)){pending.push(edge.source);relationIds.add(edge.id);relationTypes.add(edge.type);}
            }
        }
        const ordered=[...groups].sort(),connectedSourceCount=counts.get(node.id).size;
        const groupStatus=ordered.length>1?'shared':ordered.length?'known':'unknown';
        return [node.id,{connectedSourceCount,radius:sourceCountRadius(connectedSourceCount),
            sizeBasis:'unique_configured_sources_in_explicit_membership',groups:ordered,groupStatus,
            groupRelationIds:[...relationIds].filter(Boolean).sort(),
            groupRelationTypes:[...relationTypes].sort(),
            colorKey:groupStatus==='known'?'organization:'+ordered[0]:groupStatus}];
    }));
}

export function appearanceColor(presentation,theme='light') {
    const dark=theme==='dark',key=presentation?.colorKey||'unknown';
    if(key==='unknown')return dark?'#98a2af':'#5f6b79';
    if(key==='shared')return dark?'#c3ac82':'#866a3c';
    let value=2166136261;
    for(const char of key)value=Math.imul(value^char.charCodeAt(0),16777619);
    return `hsl(${(value>>>0)%360}, ${dark?35:45}%, ${dark?68:42}%)`;
}
