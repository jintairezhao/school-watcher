/* Geometry follows recorded navigation names, never guessed administrative links. */
export function buildAtlas(snapshot) {
    const entries = new Map(), actual = new Map();
    const cleanUrl = value => (value || '').replace(/\/$/, '');
    const roots = snapshot.nodes.filter(node => node.kind === 'root' || cleanUrl(node.url) === cleanUrl(snapshot.school.url));
    const rootNames = new Set(['学校官网', snapshot.school.name, ...roots.map(node => node.label)]);
    const ensure = (parts, orphan = false) => {
        const key = JSON.stringify(parts);
        if (entries.has(key)) return entries.get(key);
        const parent = parts.length > 1 && !orphan ? ensure(parts.slice(0, -1)) : null;
        const item = {id:key, label:parts.at(-1) === '@school' ? snapshot.school.name : parts.at(-1),
            parent, children:[], records:[], record:null, depth:parent ? parent.depth + 1 : 0, orphan};
        entries.set(key, item);
        if (parent) parent.children.push(item);
        return item;
    };
    for (const record of snapshot.nodes) {
        let parts = [...record.path];
        if (parts.at(-1) === record.label) parts.pop();
        if (parts.length && rootNames.has(parts[0])) parts[0] = '@school';
        const isRoot = roots.includes(record);
        const orphan = !parts.length && !isRoot;
        parts = isRoot ? ['@school'] : orphan ? ['@unplaced:' + record.id] : [...parts, record.label];
        const item = ensure(parts, orphan);
        if (orphan) item.label = record.label;
        item.records.push(record);
    }
    // Identical names on different URLs are separate pages, not a merged identity.
    for (const item of [...entries.values()]) {
        if (item.records.length === 1) {
            item.record = item.records[0]; actual.set(item.record.id, item);
        } else for (const record of item.records) {
            const child = {id:item.id + ':' + record.id, label:record.label, parent:item, children:[],
                records:[record], record, depth:item.depth + 1, orphan:item.orphan};
            item.children.push(child); entries.set(child.id, child); actual.set(record.id, child);
        }
    }
    const hash = text => [...text].reduce((value,char)=>(Math.imul(value,31)+char.charCodeAt(0))>>>0,7);
    const unit = vector => {const n=Math.hypot(vector.x,vector.y,vector.z)||1;return {x:vector.x/n,y:vector.y/n,z:vector.z/n};};
    const cross = (a,b) => ({x:a.y*b.z-a.z*b.y,y:a.z*b.x-a.x*b.z,z:a.x*b.y-a.y*b.x});
    const direction = (index,count,seed=0) => {
        const y=1-2*(index+.5)/Math.max(1,count), angle=index*2.399963229728653+seed;
        const radius=Math.sqrt(Math.max(0,1-y*y));
        return {x:Math.cos(angle)*radius,y,z:Math.sin(angle)*radius};
    };
    for (const item of entries.values()) item.children.sort((a,b)=>a.id.localeCompare(b.id));
    const forest=[...entries.values()].filter(item=>!item.parent).sort((a,b)=>a.id.localeCompare(b.id));
    function weight(item) {item.weight=1+item.children.reduce((n,child)=>n+weight(child),0);return item.weight;}
    forest.forEach(weight);
    function place(item,position,heading) {
        item.position=position;
        const kind=item.record?.kind;
        item.radius=kind==='root'||item.id==='["@school"]'?14:kind==='unit'?9:kind==='directory'?10:kind==='channel'?4.3:item.children.length?6:2.9;
        const lineage=ancestors(item);
        const branch=[...lineage].reverse().find(part=>part.record?.kind==='unit')||lineage[Math.min(2,lineage.length-1)];
        item.branch=hash(branch.id)%5;
        const axis=unit(heading), tangent=unit(cross(axis,Math.abs(axis.y)>.85?{x:1,y:0,z:0}:{x:0,y:1,z:0})), normal=cross(axis,tangent);
        const count=item.children.length;
        const reach=(item.depth===0?120:62)+16*Math.sqrt(count)+12*Math.cbrt(item.weight);
        item.children.forEach((child,index)=>{
            let vector;
            if (!item.parent) vector=direction(index,count,hash(item.id)%31);
            else {
                // A spherical cone follows each branch, rather than one global X axis.
                const cosine=.16+.76*(index+.5)/Math.max(1,count), sine=Math.sqrt(1-cosine*cosine);
                const angle=index*2.399963229728653+(hash(item.id)%37);
                vector={};
                for(const key of ['x','y','z']) vector[key]=axis[key]*cosine+sine*(tangent[key]*Math.cos(angle)+normal[key]*Math.sin(angle));
            }
            const distance=reach+10*Math.cbrt(child.weight);
            place(child,{x:position.x+vector.x*distance,y:position.y+vector.y*distance,z:position.z+vector.z*distance},vector);
        });
    }
    forest.forEach((item,index)=>{
        const vector=direction(index,forest.length,.6);
        const distance=forest.length===1?0:130+18*Math.sqrt(forest.length);
        place(item,{x:vector.x*distance,y:vector.y*distance,z:vector.z*distance},vector);
    });
    return {entries, actual, forest};
}

export function ancestors(item) {
    const path = [];
    for (let cursor = item; cursor; cursor = cursor.parent) path.unshift(cursor);
    return path;
}

export function visibleEntries(model, {query = '', state = 'all', collapsed = new Set()} = {}) {
    const matching = new Set();
    const filtering = Boolean(query) || state !== 'all';
    if (filtering) for (const item of model.entries.values()) {
        const record = item.record;
        if (record && (state === 'all' || record.state === state) &&
            (!query || [record.label, ...record.path].join(' ').toLocaleLowerCase().includes(query.toLocaleLowerCase()))) {
            ancestors(item).forEach(part => matching.add(part.id));
        }
    }
    return [...model.entries.values()].filter(item => {
        if (filtering) return matching.has(item.id);
        return !ancestors(item).slice(0, -1).some(part => collapsed.has(part.id));
    });
}
