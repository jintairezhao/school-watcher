/* Geometry follows recorded navigation names, never guessed administrative links. */
export function buildAtlas(snapshot, {spacing = 1} = {}) {
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
        const branch=lineage.find(part=>part.record?.kind==='unit')||lineage[Math.min(2,lineage.length-1)];
        item.cluster=branch.id;
        item.branch=hash(branch.id)%5;
        const axis=unit(heading), tangent=unit(cross(axis,Math.abs(axis.y)>.85?{x:1,y:0,z:0}:{x:0,y:1,z:0})), normal=cross(axis,tangent);
        const count=item.children.length;
        const reach=(item.depth===0?140:item.depth===1?96:40)+12*Math.sqrt(count)+8*Math.cbrt(item.weight);
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
        const distance=forest.length===1||item.id==='["@school"]'?0:160+24*Math.sqrt(forest.length);
        place(item,{x:vector.x*distance,y:vector.y*distance,z:vector.z*distance},vector);
    });
    // Keep each recorded branch together, then give whole branches breathing room.
    // This changes geometry only: it never creates or removes a recorded connection.
    const clusters = new Map();
    for (const item of entries.values()) {
        if (!clusters.has(item.cluster)) clusters.set(item.cluster, {id:item.cluster, items:[]});
        clusters.get(item.cluster).items.push(item);
    }
    for (const cluster of clusters.values()) {
        const members=cluster.items;
        cluster.anchor=entries.get(cluster.id);
        cluster.fixed=cluster.anchor.depth<2 && !cluster.anchor.orphan;
        // Local collision avoidance keeps dense sibling nodes from merging into a ball.
        for (let pass=0;pass<12;pass++) for (let i=0;i<members.length;i++) for (let j=i+1;j<members.length;j++) {
            const a=members[i],b=members[j],delta={x:b.position.x-a.position.x,y:b.position.y-a.position.y,z:b.position.z-a.position.z};
            const length=Math.hypot(delta.x,delta.y,delta.z)||.01, minimum=a.radius+b.radius+12;
            if (length>=minimum) continue;
            if (length===.01) {const seed=direction(i+j,members.length,hash(a.id)%31);for(const axis of ['x','y','z'])delta[axis]=seed[axis]*.01;}
            const step=(minimum-length)/length*.45;
            for (const axis of ['x','y','z']) {a.position[axis]-=delta[axis]*step;b.position[axis]+=delta[axis]*step;}
        }
        const low={x:Infinity,y:Infinity,z:Infinity},high={x:-Infinity,y:-Infinity,z:-Infinity};
        for (const item of members) for (const axis of ['x','y','z']) {
            low[axis]=Math.min(low[axis],item.position[axis]);high[axis]=Math.max(high[axis],item.position[axis]);
        }
        cluster.center={x:(low.x+high.x)/2,y:(low.y+high.y)/2,z:(low.z+high.z)/2};
        cluster.origin={...cluster.center};
        cluster.radius=Math.max(...members.map(item=>Math.hypot(item.position.x-cluster.center.x,item.position.y-cluster.center.y,item.position.z-cluster.center.z)+item.radius))+12;
    }
    const groups=[...clusters.values()].sort((a,b)=>a.id.localeCompare(b.id));
    const gap=64*Math.max(.7,Math.min(1.8,Number(spacing)||1));
    for (let pass=0;pass<48;pass++) {
        let crowded=false;
        for (let i=0;i<groups.length;i++) for (let j=i+1;j<groups.length;j++) {
            const a=groups[i],b=groups[j];if(a.fixed&&b.fixed)continue;
            const dx=b.center.x-a.center.x,dy=b.center.y-a.center.y,dz=(b.center.z-a.center.z)*.35;
            const length=Math.hypot(dx,dy,dz),minimum=a.radius+b.radius+gap;
            if(length>=minimum)continue;
            crowded=true;
            const angle=hash(a.id+'|'+b.id)%628/100;
            const axis=length>.01?{x:dx/length,y:dy/length,z:dz/length*.35}:{x:Math.cos(angle),y:Math.sin(angle),z:0};
            const push=(minimum-length)*.65;
            const share=a.fixed?0:b.fixed?1:.5;
            for(const key of ['x','y','z']){a.center[key]-=axis[key]*push*share;b.center[key]+=axis[key]*push*(1-share);}
        }
        if(!crowded)break;
    }
    for(const cluster of groups)for(const item of cluster.items)for(const axis of ['x','y','z'])
        item.position[axis]+=cluster.center[axis]-cluster.origin[axis];
    // Preserve useful spatial depth even when many clusters spread across the overview.
    const points=[...entries.values()],means={x:0,y:0,z:0},variance={x:0,y:0,z:0};
    for(const point of points)for(const axis of ['x','y','z'])means[axis]+=point.position[axis]/Math.max(1,points.length);
    for(const point of points)for(const axis of ['x','y','z'])variance[axis]+=(point.position[axis]-means[axis])**2;
    const depthScale=variance.z>0?Math.max(1,Math.sqrt((variance.x+variance.y)*.24/variance.z)):1;
    for(const point of points)point.position.z*=depthScale;
    return {entries, actual, forest};
}

/* The latest real work is a better target than the first alphabetically sorted node. */
export function trackingTarget(snapshot) {
    const nodes=snapshot?.nodes||[],byUrl=new Map(nodes.filter(n=>n.url).map(n=>[n.url,n]));
    const stamp=value=>Number.isFinite(Date.parse(value))?Date.parse(value):0;
    const work=[];
    for(const node of nodes)for(const task of node.tasks||[])work.push({node,state:task.state,at:stamp(task.updated_at)});
    for(const task of snapshot?.tasks||[])if(byUrl.has(task.url))work.push({node:byUrl.get(task.url),state:task.state,at:stamp(task.updated_at)});
    for(const node of nodes)if(node.state==='running'&&!work.some(w=>w.node===node&&w.state==='running'))work.push({node,state:'running',at:0});
    const running=work.filter(w=>w.state==='running');
    const candidates=running.length?running:work;
    candidates.sort((a,b)=>b.at-a.at||a.node.id.localeCompare(b.node.id));
    const latest=candidates[0];
    if(!latest)return null;
    const named=candidates.filter(w=>w.at===latest.at&&w.node.label===snapshot?.progress?.current_label);
    const target=named.length===1?named[0]:latest;
    return {...target,key:[target.node.id,target.state,target.at].join(':')};
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
