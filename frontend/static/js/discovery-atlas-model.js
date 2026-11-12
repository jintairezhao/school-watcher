/* Entity identities and evidenced relationships are data; geometry never supplies facts. */
import {structureAppearance} from './structure-appearance.js';

const membershipTypes=new Set(['directory_listing','organization_membership','column_membership','source_ownership']);
const relationTypes=new Set(['navigation','directory','navigation_link','website_navigation',
    ...membershipTypes,'source_on_page','entity_page','cross_entity_reference','cross_department','source_preview']);
const entityKinds={source:'channel',organization:'unit',directory_group:'directory',structure_observation:'navigation',page:'navigation'};
const stable=value=>JSON.stringify(value,(_,item)=>item&&typeof item==='object'&&!Array.isArray(item)
    ? Object.fromEntries(Object.keys(item).sort().map(key=>[key,item[key]])):item);
function webUrl(value){try{const url=new URL(value);return ['http:','https:'].includes(url.protocol)?url:null;}catch{return null;}}
function records(snapshot){
    return (snapshot.nodes||snapshot.entities||[]).flatMap(original=>{
        if(!original.id)return [];
        const address=webUrl(original.url),type=original.entity_type;
        if(original.url&&!address)return [];
        // Non-page entities have their own recorded identity and can have no URL.
        // A URL alone never merges sources with each other or with a page.
        if(!address&&(!type||type==='page'||!entityKinds[type]))return [];
        const kind=['root','directory','unit','major','channel','navigation'].includes(original.kind)
            ? original.kind : entityKinds[type]||'navigation';
        const label=original.label?.trim()||original.name?.trim()|| (address?address.host+address.pathname:'');
        if(!label)return [];
        return [{...original,kind,label,path:original.path||[],sources:original.sources||[]}];
    });
}
function observedEvidence(relation){
    const values=[...(relation.evidence||[])];
    if(relation.evidence_url&&!values.some(item=>(item.reference_url||item.evidence_url)===relation.evidence_url
        &&item.locator===relation.locator&&item.content_hash===relation.content_hash&&item.observed_at===relation.observed_at)){
        values.push({reference_url:relation.evidence_url,locator:relation.locator||'',
            content_hash:relation.content_hash,observed_at:relation.observed_at,basis:relation.basis});
    }
    return values.filter(item=>webUrl(item.reference_url||item.evidence_url));
}
function relations(snapshot,actual){
    const grouped=new Map();
    for(const relation of snapshot.relations||[]){
        const from=actual.get(relation.source),to=actual.get(relation.target),evidence=observedEvidence(relation);
        if(!from||!to||from===to||!relationTypes.has(relation.type)||!evidence.length)continue;
        const key=JSON.stringify([from.id,to.id,relation.type,relation.observed_relation||null]);
        if(!grouped.has(key))grouped.set(key,{...relation,id:key,from,to,access:false,evidence:[]});
        const edge=grouped.get(key);
        edge.access ||= relation.access===true;
        const unique=new Map([...edge.evidence,...evidence].map(item=>[stable(item),item]));
        edge.evidence=[...unique].sort(([a],[b])=>a.localeCompare(b)).map(([,item])=>item);
        const assertions=new Map([...(edge.assertions||[]),...(relation.assertions||[])].map(item=>[stable(item),item]));
        if(assertions.size)edge.assertions=[...assertions].sort(([a],[b])=>a.localeCompare(b)).map(([,item])=>item);
        const proof=edge.evidence[0];
        edge.evidence_url=proof.reference_url||proof.evidence_url;
        edge.locator=proof.locator||'';edge.content_hash=proof.content_hash;edge.observed_at=proof.observed_at;
    }
    return [...grouped.values()];
}
export function atlasLayoutSignature(snapshot,spacing=1){
    const items=records(snapshot),actual=new Map(items.map(item=>[item.id,item]));
    return stable([spacing,items.map(item=>[item.id,item.kind,item.entity_type,Boolean(item.configured)]).sort(([a],[b])=>a.localeCompare(b)),
        relations(snapshot,actual).map(edge=>[edge.id,edge.evidence.some(proof=>proof.reference_url&&proof.locator&&proof.content_hash)])
            .sort(([a],[b])=>a.localeCompare(b))]);
}
export function refreshAtlasRecords(model,snapshot){
    const items=records(snapshot);
    if(items.length!==model.actual.size||items.some(item=>!model.actual.has(item.id)||model.actual.get(item.id).record.kind!==item.kind))return false;
    const updated=relations(snapshot,model.actual),byId=new Map(updated.map(edge=>[edge.id,edge]));
    if(updated.length!==model.links.length||model.links.some(edge=>!byId.has(edge.id)))return false;
    for(const record of items){const item=model.actual.get(record.id);item.record=record;item.records=[record];item.label=record.label;}
    for(const edge of model.links){
        const {primary,from,to,id}=edge;
        for(const key of Object.keys(edge))delete edge[key];
        Object.assign(edge,byId.get(id),{primary,from,to,id});
    }
    return true;
}
export function buildAtlas(snapshot, {spacing = 1,deferLayout=false,previousPositions={}} = {}) {
    const entries = new Map(), actual = new Map();
    for (const record of records(snapshot)) {
        const item={id:record.id,label:record.label,
            parent:null,children:[],records:[record],record,depth:0,orphan:record.kind!=='root'};
        entries.set(item.id,item);actual.set(record.id,item);
    }
    const links=relations(snapshot,actual);
    const sourceRank=edge=>({root:0,directory:1,unit:2,channel:3}[edge.from.record.kind]??4);
    links.sort((a,b)=>Number(b.type==='directory')-Number(a.type==='directory')||sourceRank(a)-sourceRank(b)||a.id.localeCompare(b.id));
    // A spanning forest supplies positions only. All real links, including cross
    // links and cycles, remain in links; no page is attached by a matching name.
    for(const edge of links) {
        if(edge.type==='source_preview')continue;
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
    const presentation=structureAppearance({nodes:points.map(item=>item.record),relations:links});
    for(const item of points){
        const kind=item.record.kind,lineage=ancestors(item);
        // These are display groups along real links, never inferred ownership.
        const branch=lineage.find(part=>part.record.kind==='unit')||lineage[Math.min(2,lineage.length-1)];
        item.cluster=branch.id;item.branch=hash(branch.id)%5;item.degree=degree.get(item.id).size;
        item.presentation=presentation.get(item.id);item.radius=item.presentation.radius;
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
    if(deferLayout){
        const retained=previousPositions instanceof Map?previousPositions:new Map(Object.entries(previousPositions));
        const reach=60*scale*Math.max(1,Math.cbrt(points.length))*.65;
        for(let index=0;index<points.length;index++){
            const item=points[index],previous=retained.get(item.id);
            item.position=previous&&['x','y','z'].every(axis=>Number.isFinite(previous[axis]))
                ?{x:previous.x,y:previous.y,z:previous.z}:seedVolume(index,points.length,reach,hash('pending-atlas-volume'));
        }
        return {entries,actual,forest,links,layoutPending:true};
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

/* A website access route follows observed hyperlinks in their original direction.
 * A connected component is not proof of reachability or crawl chronology. */
export function accessRoute(model,item) {
    const describe=(route,mode)=>({...route,mode,complete:mode==='discovery',
        rootConnected:route.nodes[0]?.record.kind==='root',
        steps:route.links.map((edge,index)=>({from:route.nodes[index],to:route.nodes[index+1],edge,
            reversed:edge.from!==route.nodes[index]}))});
    if(!item)return describe({nodes:[],links:[]},'partial');
    if(item.record.entity_type==='source'){
        const bindings=model.links.filter(edge=>edge.type==='source_on_page'&&edge.to===item&&edge.from.record.entity_type==='page');
        const aliases=bindings.filter(edge=>edge.observed_relation==='validated_listing_alias'
            &&edge.assertions?.some(proof=>proof.kind==='validated_listing_alias'
                &&proof.configured_url===item.record.url&&proof.alias_url===edge.from.record.url
                &&proof.config_hash===item.record.config_hash&&proof.receipt_id));
        if(aliases.length){
            const entrances=[...aliases,...bindings.filter(edge=>edge.from.record.url===item.record.url)];
            const routes=entrances.map(configurationRelation=>({...accessRoute(model,configurationRelation.from),
                targetSource:item,hostingPage:configurationRelation.from,configurationRelation,
                terminalGap:'source_configuration_not_click'}));
            routes.sort((a,b)=>Number(b.complete)-Number(a.complete)||a.steps.length-b.steps.length
                ||a.hostingPage.id.localeCompare(b.hostingPage.id));
            return {...routes[0],equivalentHostingPages:[...new Set(entrances.map(edge=>edge.from))]};
        }
        const observed=bindings.filter(edge=>edge.observed_relation==='validated_response_page');
        const hosting=observed.length?observed:bindings;
        if(hosting.length!==1)return {...describe({nodes:[],links:[]},'partial'),targetSource:item,
            gap:hosting.length?'multiple_hosting_pages':'hosting_page_unknown',hostingPages:hosting.map(edge=>edge.from)};
        const configurationRelation=hosting[0],hostingPage=configurationRelation.from;
        return {...accessRoute(model,hostingPage),targetSource:item,hostingPage,configurationRelation,
            terminalGap:'source_configuration_not_click'};
    }
    if(['organization','directory_group','structure_observation'].includes(item.record.entity_type)){
        const hosting=model.links.filter(edge=>edge.type==='entity_page'&&edge.from===item&&edge.to.record.entity_type==='page');
        if(!hosting.length)return {...describe({nodes:[],links:[]},'partial'),targetEntity:item,
            gap:'entity_page_unknown',hostingPages:[]};
        // Reaching the corresponding page is a website visit. The final entity
        // binding remains separate evidence, never an extra website click.
        // Several explicit bindings are alternative entrances, not a missing
        // identity or permission to merge pages. Keep all original witnesses.
        const routes=hosting.map(entityPageRelation=>({
            ...accessRoute(model,entityPageRelation.to),targetEntity:item,
            hostingPage:entityPageRelation.to,entityPageRelation,terminalGap:'entity_page_not_click'}));
        const preferred=route=>Number([item.record.url,item.record.reference_url].filter(Boolean)
            .includes(route.hostingPage.record.url));
        routes.sort((a,b)=>Number(b.complete)-Number(a.complete)||a.steps.length-b.steps.length
            ||preferred(b)-preferred(a)||a.hostingPage.id.localeCompare(b.hostingPage.id)
            ||a.entityPageRelation.id.localeCompare(b.entityPageRelation.id));
        return {...routes[0],hostingPages:[...new Set(hosting.map(edge=>edge.to))].sort((a,b)=>a.id.localeCompare(b.id)),
            entityPageRelations:[...hosting].sort((a,b)=>a.id.localeCompare(b.id))};
    }
    const incoming=new Map();
    for(const edge of model.links){
        if(edge.access!==true||!['navigation_link','navigation'].includes(edge.type))continue;
        if([edge.from,edge.to].some(node=>node.record.entity_type&&node.record.entity_type!=='page'))continue;
        if(!incoming.has(edge.to.id))incoming.set(edge.to.id,[]);incoming.get(edge.to.id).push(edge);
    }
    const anchorRank=node=>({directory:4,unit:3,channel:1}[node.record.kind]??0);
    // A directory listing can explain which of two equally short hyperlinks
    // to show. It never creates a click, an administrative parent or a route.
    const witnessKey=proof=>{
        const address=proof.reference_url||proof.evidence_url;
        return address&&proof.locator&&proof.content_hash&&proof.observed_at&&proof.material_available!==false
            ? stable([address,proof.locator,proof.content_hash,proof.observed_at]) : null;
    };
    const directoryWitnesses=new Set(model.links.filter(edge=>edge.type==='directory_listing')
        .flatMap(edge=>edge.evidence.map(witnessKey)).filter(Boolean));
    const directoryEntrance=edge=>edge.evidence.some(proof=>
        (proof.reference_url||proof.evidence_url)===edge.from.record.url
        &&directoryWitnesses.has(witnessKey(proof)));
    for(const values of incoming.values())values.sort((a,b)=>Number(directoryEntrance(b))-Number(directoryEntrance(a))
        ||anchorRank(b.from)-anchorRank(a.from)||a.id.localeCompare(b.id));
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
    return {...describe(partial,'partial'),gap:'upstream_navigation_unknown'};
}

// Retained for the existing scene; this means website reachability, not history.
export const discoveryRoute=accessRoute;

export function membershipRoute(model,item,{maxPaths=64}={}){
    const paths=[],incoming=new Map();
    // The saved School organization is the top entity of this school scope.
    // It is distinct from the root webpage and requires no invented page link.
    const root=node=>node.record.kind==='root'||node.record.entity_type==='organization'
        &&node.record.organization_kind==='school';
    for(const edge of model.links){
        if(!membershipTypes.has(edge.type))continue;
        if(!incoming.has(edge.to.id))incoming.set(edge.to.id,[]);
        incoming.get(edge.to.id).push(edge);
    }
    for(const edges of incoming.values())edges.sort((a,b)=>a.id.localeCompare(b.id));
    const describe=(route,gap)=>({...route,mode:'membership',gap,complete:!gap,
        rootConnected:!!route.nodes[0]&&root(route.nodes[0]),
        steps:route.links.map((edge,index)=>({from:route.nodes[index],to:route.nodes[index+1],edge,reversed:false}))});
    const queue=item?[{nodes:[item],links:[]}]:[];
    let truncated=false;
    while(queue.length&&paths.length<maxPaths){
        const route=queue.shift(),first=route.nodes[0],parents=incoming.get(first.id)||[];
        if(root(first)){paths.push(describe(route,null));continue;}
        if(!parents.length){paths.push(describe(route,'upstream_unknown'));continue;}
        for(const edge of parents){
            if(route.nodes.includes(edge.from)){paths.push(describe(route,'cycle'));continue;}
            queue.push({nodes:[edge.from,...route.nodes],links:[edge,...route.links]});
            if(queue.length+paths.length>maxPaths){truncated=true;break;}
        }
        if(truncated)break;
    }
    truncated ||= queue.length>0;
    if(truncated&&paths.length===0&&queue.length)paths.push(describe(queue[0],'path_limit'));
    const first=paths[0]||describe({nodes:item?[item]:[],links:[]},'no_membership_record');
    return {...first,paths,complete:paths.length>0&&paths.every(path=>path.complete)&&!truncated,
        gap:truncated?'path_limit':paths.find(path=>path.gap)?.gap||first.gap,truncated};
}

export function explorationTrace(snapshot,item,model=null){
    const nodeId=typeof item==='string'?item:item?.id;
    const empty=gap=>({mode:'exploration',events:[],nodes:[],links:[],steps:[],complete:false,rootConnected:false,gap});
    if(!nodeId)return empty('no_selection');
    // Current task summaries, DOM ancestry and path_json cannot manufacture a
    // chronology. Only the dedicated recorded-event stream is eligible.
    const events=snapshot.exploration_events||[];
    if(!events.length)return empty('exploration_events_unavailable');
    const valid=events.filter(event=>event.id&&event.node_id&&Number.isInteger(event.sequence)
        &&Number.isFinite(Date.parse(event.observed_at))&&event.event_type);
    const byId=new Map();
    for(const event of valid){
        if(byId.has(event.id)&&JSON.stringify(byId.get(event.id))!==JSON.stringify(event))return empty('event_identity_conflict');
        byId.set(event.id,event);
    }
    const candidates=valid.filter(event=>event.node_id===nodeId);
    const stream=event=>event.stream_id||'legacy_page_observations';
    if(new Set(candidates.map(stream)).size>1)return empty('event_stream_conflict');
    const latest=candidates.sort((a,b)=>b.sequence-a.sequence)[0];
    if(!latest)return empty('node_events_unavailable');
    const selected=[],seen=new Set();let cursor=latest,gap=null;
    if(latest.event_type==='source_configuration_saved'){
        const record=model?.actual.get(nodeId)?.record||snapshot.nodes?.find(node=>node.id===nodeId);
        if(!latest.current_configuration||record?.entity_type!=='source'
            ||record.source_id!==latest.source_id||record.config_hash!==latest.config_hash)gap='configuration_changed';
    }
    while(cursor){
        if(seen.has(cursor.id)){gap='event_cycle';break;}
        seen.add(cursor.id);selected.unshift(cursor);
        if(!cursor.parent_event_id){
            if(cursor.event_type!=='exploration_started')gap='upstream_event_unknown';
            break;
        }
        const parent=byId.get(cursor.parent_event_id);
        if(!parent){gap='missing_parent_event';break;}
        if((stream(parent)===stream(cursor)&&parent.sequence>=cursor.sequence)
            ||Date.parse(parent.observed_at)>Date.parse(cursor.observed_at)){
            gap='event_order_conflict';break;
        }
        if(stream(parent)!==stream(cursor)&&!(cursor.event_type==='source_configuration_saved'
            &&parent.event_type==='page_inspection_finished'&&parent.state==='fetched'
            &&[parent.url,parent.final_url].includes(cursor.url)
            &&cursor.evidence?.some(proof=>proof.reference_url===cursor.url&&proof.locator
                &&proof.content_hash===parent.content_hash))){gap='cross_stream_proof_incomplete';break;}
        cursor=parent;
    }
    const nodes=[],steps=[];let renderComplete=Boolean(model);
    for(const event of selected){
        const node=model?.actual.get(event.node_id);
        if(!node){renderComplete=false;continue;}
        const previous=nodes.at(-1);
        if(previous?.id===node.id)continue;
        if(previous){
            const configuration=event.event_type==='source_configuration_saved'&&node.record.entity_type==='source';
            const evidence=(event.evidence||[]).filter(proof=>(configuration?proof.reference_url===event.url:
                    proof.reference_url===previous.record.url)&&proof.locator&&proof.content_hash)
                .map(proof=>({...proof,observed_at:event.observed_at}));
            if((event.event_type!=='page_discovered'&&!configuration)||!evidence.length){gap||='discovery_proof_incomplete';renderComplete=false;}
            else {
                const edge={id:'recorded:'+event.id,from:previous,to:node,
                    type:configuration?'source_configuration_saved':'discovered_from',access:false,evidence};
                steps.push({from:previous,to:node,edge,event,reversed:false});
            }
        }
        nodes.push(node);
    }
    return {mode:'exploration',events:selected,nodes,links:steps.map(step=>step.edge),steps,
        complete:!gap,renderComplete:renderComplete&&!gap&&steps.length===nodes.length-1,rootConnected:false,gap};
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
