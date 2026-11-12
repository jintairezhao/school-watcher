/* Label precedence only: geometry parents and recorded relations stay intact. */
const hierarchyTypes=new Set(['directory_listing','directory','organization_membership','column_membership','source_ownership']);
const labelOrderTypes=new Set([...hierarchyTypes,'entity_page','source_on_page']);
const endpoints=edge=>[edge.from?.id??edge.source,edge.to?.id??edge.target];
function* hierarchyEdges(links,types=hierarchyTypes) {
    for(const edge of links){
        if(!types.has(edge.type))continue;
        const [from,to]=endpoints(edge);
        if(from&&to&&from!==to)yield [from,to];
    }
}
export function baseLabelLevel(item) {
    const record=item.record||item;
    if(record.kind==='root'||record.entity_type==='organization'&&record.organization_kind==='school')return 0;
    if(record.kind==='directory'||record.entity_type==='directory_group')return 1;
    if(record.entity_type==='organization')return 2;
    if(record.kind==='unit')return record.entity_type==='page'?3:2;
    if(record.kind==='channel'||record.entity_type==='source')return 4;
    return 5;
}
export function labelHierarchyLevels(items,links) {
    const nodes=new Map(items.map(item=>[item.id,item]));
    const children=new Map(items.map(item=>[item.id,new Set()]));
    // A bound webpage also yields its label slot to its organization. These
    // are display dependencies, not additional administrative relationships.
    for(const [from,to] of hierarchyEdges(links,labelOrderTypes))if(nodes.has(from)&&nodes.has(to))children.get(from).add(to);
    // Collapse genuine recorded cycles for display ordering. Choosing an
    // arbitrary edge from a directory loop would manufacture a parent level.
    const index=new Map(),low=new Map(),stack=[],active=new Set(),components=[],component=new Map();
    let sequence=0;
    function visit(id){
        index.set(id,sequence);low.set(id,sequence++);stack.push(id);active.add(id);
        for(const child of children.get(id)){
            if(!index.has(child)){visit(child);low.set(id,Math.min(low.get(id),low.get(child)));}
            else if(active.has(child))low.set(id,Math.min(low.get(id),index.get(child)));
        }
        if(low.get(id)!==index.get(id))return;
        const group=[];let member;
        do{member=stack.pop();active.delete(member);component.set(member,components.length);group.push(member);}while(member!==id);
        components.push(group);
    }
    for(const id of nodes.keys())if(!index.has(id))visit(id);
    const levels=components.map(group=>Math.max(...group.map(id=>baseLabelLevel(nodes.get(id)))));
    const successors=components.map(()=>new Set()),incoming=components.map(()=>0);
    for(const [from,next] of children)for(const to of next){
        const a=component.get(from),b=component.get(to);
        if(a!==b&&!successors[a].has(b)){successors[a].add(b);incoming[b]++;}
    }
    const queue=incoming.flatMap((count,id)=>count?[]:[id]);
    for(let offset=0;offset<queue.length;offset++){
        const id=queue[offset];
        for(const next of successors[id]){
            levels[next]=Math.max(levels[next],levels[id]+1);
            if(--incoming[next]===0)queue.push(next);
        }
    }
    return new Map([...nodes.keys()].map(id=>[id,levels[component.get(id)]]));
}
export function labelAncestors(links,selected,hovered) {
    const parents=new Map();
    for(const [from,to] of hierarchyEdges(links)){
        if(!parents.has(to))parents.set(to,new Set());parents.get(to).add(from);
    }
    const seeds=[selected?.id,hovered?.id].filter(Boolean),seen=new Set(seeds),queue=[...seeds];
    for(let offset=0;offset<queue.length;offset++)for(const id of parents.get(queue[offset])||[]){
        if(!seen.has(id)){seen.add(id);queue.push(id);}
    }
    for(const id of seeds)seen.delete(id);
    return seen;
}
