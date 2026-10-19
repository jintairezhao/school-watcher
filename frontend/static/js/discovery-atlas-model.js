/* Page identity and evidenced URL links are data; geometry never supplies facts. */
export function buildAtlas(snapshot, {spacing = 1} = {}) {
    const entries = new Map(), actual = new Map();
    for (const record of snapshot.nodes) {
        let url;
        try { url=new URL(record.url); } catch (_) { continue; }
        if(!record.id || !['http:','https:'].includes(url.protocol))continue;
        const item={id:record.id,label:record.label?.trim() || url.host+url.pathname,
            parent:null,children:[],records:[record],record,depth:0,orphan:record.kind!=='root'};
        entries.set(item.id,item);actual.set(record.id,item);
    }
    const links=[],seen=new Set();
    for(const relation of snapshot.relations || []) {
        const from=actual.get(relation.source),to=actual.get(relation.target);
        if(!from||!to||from===to||!['navigation','directory'].includes(relation.type)||!relation.evidence_url)continue;
        const key=JSON.stringify([from.id,to.id,relation.type]);
        if(seen.has(key))continue;
        seen.add(key);links.push({...relation,id:key,from,to});
    }
    const sourceRank=edge=>({root:0,directory:1,unit:2,channel:3}[edge.from.record.kind]??4);
    links.sort((a,b)=>Number(b.type==='directory')-Number(a.type==='directory')||sourceRank(a)-sourceRank(b)||a.id.localeCompare(b.id));
    // A spanning forest supplies positions only. All real links, including cross
    // links and cycles, remain in links; no page is attached by a matching name.
    for(const edge of links) {
        const {from,to}=edge;
        if(to.parent || to.record.kind==='root')continue;
        let cycle=false;
        for(let cursor=from;cursor;cursor=cursor.parent)if(cursor===to){cycle=true;break;}
        if(cycle)continue;
        to.parent=from;to.parentRelation=edge;to.orphan=false;from.children.push(to);
    }
    function depths(item,depth=0){item.depth=depth;item.children.forEach(child=>depths(child,depth+1));}
    [...entries.values()].filter(item=>!item.parent).forEach(item=>depths(item));
    const hash = text => {
        let value=[...text].reduce((value,char)=>(Math.imul(value,31)+char.charCodeAt(0))>>>0,7);
        value=Math.imul(value^(value>>>16),0x45d9f3b);value=Math.imul(value^(value>>>16),0x45d9f3b);
        return (value^(value>>>16))>>>0;
    };
    for (const item of entries.values()) item.children.sort((a,b)=>a.id.localeCompare(b.id));
    const forest=[...entries.values()].filter(item=>!item.parent).sort((a,b)=>a.id.localeCompare(b.id));
    function weight(item) {item.weight=1+item.children.reduce((n,child)=>n+weight(child),0);return item.weight;}
    forest.forEach(weight);
    const points=[...entries.values()].sort((a,b)=>a.id.localeCompare(b.id));
    const degree=new Map(points.map(item=>[item.id,new Set()]));
    for(const edge of links){
        edge.primary=edge.to.parentRelation===edge;
        degree.get(edge.from.id).add(edge.to.id);degree.get(edge.to.id).add(edge.from.id);
    }
    const clusters=new Map(), scale=Math.max(.7,Math.min(1.8,Number(spacing)||1));
    for(const item of points){
        const kind=item.record.kind,lineage=ancestors(item);
        // These are display groups along real links, never inferred ownership.
        const branch=lineage.find(part=>part.record.kind==='unit')||lineage[Math.min(2,lineage.length-1)];
        item.cluster=branch.id;item.branch=hash(branch.id)%5;item.degree=degree.get(item.id).size;
        item.radius=kind==='root'?14:kind==='directory'?10:kind==='unit'?9:kind==='channel'?4.3:item.children.length?5.5:3.4;
        item.salience=({root:1000,directory:800,unit:600,channel:220}[kind]??100)+Math.min(80,Math.log2(1+item.degree)*12);
        if(!clusters.has(item.cluster))clusters.set(item.cluster,{id:item.cluster,items:[],radius:0});
        clusters.get(item.cluster).items.push(item);
    }
    const groups=[...clusters.values()].sort((a,b)=>a.id.localeCompare(b.id));
    // Start inside a volume, not on a shell or a plane. Directions sample a
    // sphere evenly; independent radii fill its interior. Geometry is seeded by
    // identity so a snapshot refresh never randomly rearranges the same pages.
    function seedVolume(index,count,radius,seed){
        const polar=1-2*(index+.5)/Math.max(1,count), azimuth=index*2.399963229728653+(seed%6283)/1000;
        const radial=Math.sqrt(Math.max(0,1-polar*polar));
        const reach=radius*Math.cbrt(((index+.5)*.7548776662466927+(seed%997)/997)%1);
        return {x:Math.cos(azimuth)*radial*reach,y:polar*reach,z:Math.sin(azimuth)*radial*reach};
    }
    // Both local and whole-branch forces use all three coordinates. A spatial
    // grid bounds neighbour checks while isotropic gravity and springs preserve
    // a readable volume when the user rotates the scene.
    function relax(bodies,springs,{gap,iterations,attraction,gravity,cellSize}){
        bodies.forEach((body,index)=>{body.index=index;body.vx=body.vy=body.vz=0;});
        for(let pass=0;pass<iterations;pass++){
            const cells=new Map();
            for(const body of bodies){
                body.fx=-body.x*gravity;body.fy=-body.y*gravity;body.fz=-body.z*gravity;
                const key=Math.floor(body.x/cellSize)+':'+Math.floor(body.y/cellSize)+':'+Math.floor(body.z/cellSize);
                if(!cells.has(key))cells.set(key,[]);cells.get(key).push(body);
            }
            for(const a of bodies){
                const cx=Math.floor(a.x/cellSize),cy=Math.floor(a.y/cellSize),cz=Math.floor(a.z/cellSize);
                for(let gx=cx-1;gx<=cx+1;gx++)for(let gy=cy-1;gy<=cy+1;gy++)for(let gz=cz-1;gz<=cz+1;gz++)for(const b of cells.get(gx+':'+gy+':'+gz)||[]){
                    if(b.index<=a.index)continue;
                    let dx=b.x-a.x,dy=b.y-a.y,dz=b.z-a.z,distance=Math.hypot(dx,dy,dz);
                    const minimum=a.radius+b.radius+gap;
                    if(distance>=minimum)continue;
                    if(distance<.01){const direction=seedVolume(0,1,1,hash(a.id+'|'+b.id));dx=direction.x;dy=direction.y;dz=direction.z;distance=Math.hypot(dx,dy,dz)||1;}
                    const push=(minimum-distance)*.22/distance;
                    a.fx-=dx*push;a.fy-=dy*push;a.fz-=dz*push;b.fx+=dx*push;b.fy+=dy*push;b.fz+=dz*push;
                }
            }
            for(const {from:a,to:b} of springs){
                const dx=b.x-a.x,dy=b.y-a.y,dz=b.z-a.z,distance=Math.hypot(dx,dy,dz)||1;
                const target=a.radius+b.radius+gap*1.7;
                const force=(distance-target)*attraction/distance;
                a.fx+=dx*force;a.fy+=dy*force;a.fz+=dz*force;b.fx-=dx*force;b.fy-=dy*force;b.fz-=dz*force;
            }
            for(const body of bodies){
                if(body.fixed){body.x=body.y=body.z=0;continue;}
                body.vx=(body.vx+body.fx)*.68;body.vy=(body.vy+body.fy)*.68;body.vz=(body.vz+body.fz)*.68;
                const speed=Math.hypot(body.vx,body.vy,body.vz),limit=gap*.22;
                if(speed>limit){body.vx*=limit/speed;body.vy*=limit/speed;body.vz*=limit/speed;}
                body.x+=body.vx;body.y+=body.vy;body.z+=body.vz;
            }
        }
    }
    for(const group of groups){
        group.anchor=entries.get(group.id);
        const members=[group.anchor,...group.items.filter(item=>item!==group.anchor)];
        const local=members.map((item,index)=>{
            const position=index?seedVolume(index-1,members.length-1,28*scale*Math.cbrt(members.length-1),hash(group.id)):{x:0,y:0,z:0};
            return {id:item.id,item,...position,radius:item.radius,fixed:index===0};
        });
        const localById=new Map(local.map(body=>[body.id,body]));
        const springs=links.filter(edge=>edge.primary&&localById.has(edge.from.id)&&localById.has(edge.to.id))
            .map(edge=>({from:localById.get(edge.from.id),to:localById.get(edge.to.id)}));
        relax(local,springs,{gap:18*scale,iterations:100,attraction:.025,gravity:.001,cellSize:64*scale});
        for(const body of local){
            body.item.position={x:body.x,y:body.y,z:body.z};
            group.radius=Math.max(group.radius,Math.hypot(body.x,body.y,body.z)+body.radius);
        }
        group.radius+=8;
    }
    const volume=groups.reduce((sum,group)=>sum+(group.radius+32*scale)**3,0);
    const envelope=Math.cbrt(volume)*1.2;
    groups.forEach((group,index)=>{
        Object.assign(group,seedVolume(index,groups.length,envelope,hash('atlas-volume')));
        group.fixed=group.anchor.record.kind==='root';
        if(group.fixed)group.x=group.y=group.z=0;
    });
    const groupSprings=[],joined=new Set();
    for(const edge of links){
        if(!edge.primary||edge.from.cluster===edge.to.cluster)continue;
        const a=clusters.get(edge.from.cluster),b=clusters.get(edge.to.cluster),key=[a.id,b.id].sort().join('|');
        if(joined.has(key))continue;joined.add(key);groupSprings.push({from:a,to:b});
    }
    relax(groups,groupSprings,{gap:48*scale,iterations:180,attraction:.004,gravity:.0004,
        cellSize:Math.max(64,...groups.map(group=>group.radius*2+48*scale))});
    for(const group of groups){
        for(const item of group.items){item.position.x+=group.x;item.position.y+=group.y;item.position.z+=group.z;}
    }
    return {entries, actual, forest, links};
}

/* A discovery route follows recorded link directions. When that evidence cannot
 * reach the school entrance, a separately labelled association route can explain
 * how the same real pages are connected, retaining every original arrow. Neither
 * route claims administrative ownership or an observed crawl chronology. */
export function discoveryRoute(model,item) {
    const describe=(route,mode)=>({...route,mode,complete:mode==='discovery',
        rootConnected:route.nodes[0]?.record.kind==='root',
        steps:route.links.map((edge,index)=>({from:route.nodes[index],to:route.nodes[index+1],edge,
            reversed:edge.from!==route.nodes[index]}))});
    if(!item)return describe({nodes:[],links:[]},'partial');
    const incoming=new Map(),adjacent=new Map();
    for(const edge of model.links){
        if(!incoming.has(edge.to.id))incoming.set(edge.to.id,[]);incoming.get(edge.to.id).push(edge);
        for(const node of [edge.from,edge.to]){
            if(!adjacent.has(node.id))adjacent.set(node.id,[]);adjacent.get(node.id).push(edge);
        }
    }
    const anchorRank=node=>({directory:4,unit:3,channel:1}[node.record.kind]??0);
    for(const values of incoming.values())values.sort((a,b)=>Number(a.type==='directory')-Number(b.type==='directory')||anchorRank(b.from)-anchorRank(a.from)||a.id.localeCompare(b.id));
    const queue=[{nodes:[item],links:[]}],visited=new Set([item.id]);
    let partial=queue[0];
    for(let at=0;at<queue.length;at++){
        const route=queue[at],first=route.nodes[0];
        if(first.record.kind==='root')return describe(route,'discovery');
        // Missing root evidence is a gap. Repeated menus can form long cycles;
        // an arbitrary sibling page must not be promoted into the route's origin.
        if(route.links.length&&(!partial.links.length||anchorRank(first)>anchorRank(partial.nodes[0])))partial=route;
        for(const edge of incoming.get(first.id)||[]){
            if(visited.has(edge.from.id))continue;
            visited.add(edge.from.id);queue.push({nodes:[edge.from,...route.nodes],links:[edge,...route.links]});
        }
    }
    // A visible outgoing connection is still useful when upstream records are
    // absent. Search the real connected component, not the layout forest. The
    // shortest chain wins; equal-length chains prefer fewer reverse traversals,
    // then directory evidence. A visited set prevents repeated-menu cycles from
    // fabricating a longer or supposedly more complete origin story.
    const connected=[{nodes:[item],links:[],reversed:0,navigation:0}],settled=new Set();
    const compare=(a,b)=>a.links.length-b.links.length||a.reversed-b.reversed||a.navigation-b.navigation||
        a.nodes.map(node=>node.id).join('|').localeCompare(b.nodes.map(node=>node.id).join('|'));
    while(connected.length){
        connected.sort(compare);
        const route=connected.shift(),first=route.nodes[0];
        if(settled.has(first.id))continue;
        settled.add(first.id);
        if(first.record.kind==='root')return describe({nodes:route.nodes,links:route.links},'association');
        for(const edge of adjacent.get(first.id)||[]){
            const previous=edge.from===first?edge.to:edge.from;
            if(settled.has(previous.id))continue;
            connected.push({nodes:[previous,...route.nodes],links:[edge,...route.links],
                reversed:route.reversed+Number(edge.from!==previous),
                navigation:route.navigation+Number(edge.type==='navigation')});
        }
    }
    return describe(partial,'partial');
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

export function visibleEntries(model, {query = '', state = 'all', collapsed = new Set(), revealed = new Set()} = {}) {
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
        if(revealed.has(item.id))return true;
        if (filtering) return matching.has(item.id);
        return !ancestors(item).slice(0, -1).some(part => collapsed.has(part.id));
    });
}
