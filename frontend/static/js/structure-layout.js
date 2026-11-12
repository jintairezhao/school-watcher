/* Geometry solver used by the version-fenced atlas Worker.
 * Pairwise stress relaxation is adapted to three coordinates; this is not a
 * port of fCoSE. Compounds here are overlapping, explicitly evidenced groups,
 * never a selected navigation tree or a new source of ownership facts.
 */
import {configuredSourceSets,sourceCountRadius} from './structure-appearance.js';

const RULES = Object.freeze({
    navigation_link: {weight: .12, length: 1.5},
    directory_listing: {weight: .55, length: 1.05},
    organization_membership: {weight: 1, length: 1.05},
    source_ownership: {weight: 1.2, length: .8},
    column_membership: {weight: 1.2, length: .8},
    source_on_page: {weight: 1.4, length: .48},
    entity_page: {weight: 1.1, length: .65},
    cross_entity_reference: {weight: .45, length: 1.5},
    source_preview: {weight: .45, length: 1.5},
});
const MEMBERSHIP_TYPES = new Set(['organization_membership', 'source_ownership', 'column_membership']);
const AXES = ['x', 'y', 'z'];
const dist = (a, b) => Math.hypot(a.x-b.x, a.y-b.y, a.z-b.z);
const finite = p => p && AXES.every(axis => Number.isFinite(p[axis]));
const copy = p => ({x:p.x, y:p.y, z:p.z});
function hash(text) {
    let value = 2166136261;
    for (const c of String(text)) value = Math.imul(value ^ c.charCodeAt(0), 16777619);
    return value >>> 0;
}
function random(seed) {
    let state = seed >>> 0;
    return () => {state += 0x6d2b79f5; let x = state; x = Math.imul(x ^ x >>> 15, x | 1);
        x ^= x + Math.imul(x ^ x >>> 7, x | 61); return ((x ^ x >>> 14) >>> 0) / 4294967296;};
}
function seedPoint(id, extent) {
    const rng=random(hash(id)), z=2*rng()-1, theta=rng()*2*Math.PI;
    const radius=extent*Math.cbrt(.15+.85*rng()), xy=Math.sqrt(1-z*z);
    return {x:radius*xy*Math.cos(theta), y:radius*xy*Math.sin(theta), z:radius*z};
}
const average = values => values.length ? values.reduce((a,b)=>a+b,0)/values.length : 0;
function quantile(values, fraction) {
    if (!values.length) return 0;
    const sorted=[...values].sort((a,b)=>a-b);
    return sorted[Math.min(sorted.length-1, Math.floor((sorted.length-1)*fraction))];
}
function centroid(points) {
    return Object.fromEntries(AXES.map(axis=>[axis,average(points.map(point=>point[axis]))]));
}
function previousMap(input) {
    return input instanceof Map ? input : new Map(Object.entries(input || {}));
}

export function directoryDisplayGroups(input) {
    const nodes=new Map((input.nodes || []).map(node=>[node.id,node]));
    return [...nodes.values()].filter(node=>node.entity_type==='directory_group').sort((a,b)=>a.id.localeCompare(b.id)).flatMap(node=>{
        const rows=(input.relations || []).filter(edge=>edge.type==='directory_listing' && edge.source===node.id
            && edge.target!==node.id && nodes.has(edge.target) && (edge.evidence || []).some(proof=>
                proof.reference_url && proof.locator && proof.content_hash));
        if(!rows.length)return [];
        return [{id:'directory:'+node.id,kind:'display',label:node.label || '',establishesOwnership:false,
            relationIds:[...new Set(rows.map(edge=>edge.id).filter(Boolean))].sort(),
            members:[...new Set([node.id,...rows.map(edge=>edge.target)])].sort()}];
    });
}

function prepare(input, options={}) {
    const scale = options.scale ?? 60;
    const nodes=[...(input.nodes || [])].sort((a,b)=>a.id.localeCompare(b.id));
    const indices=new Map(nodes.map((node,index)=>[node.id,index]));
    if (indices.size !== nodes.length) throw new Error('Duplicate entity identity');
    const unique=new Map(), omitted=[];
    for (const relation of input.relations || []) {
        const rule=RULES[relation.type], a=indices.get(relation.source), b=indices.get(relation.target);
        const evidence=(relation.evidence || []).filter(proof=>proof.reference_url && proof.locator && proof.content_hash);
        if (!rule || a===undefined || b===undefined || a===b || !evidence.length) {
            omitted.push({id:relation.id || null, reason:!rule?'unsupported_type':!evidence.length?'missing_evidence':'invalid_endpoint'});
            continue;
        }
        const predicate=relation.observed_relation || null;
        const key=JSON.stringify([relation.source,relation.target,relation.type,predicate]);
        if (!unique.has(key)) unique.set(key,{key,a,b,type:relation.type,predicate,ids:[],length:scale*rule.length,weight:rule.weight});
        unique.get(key).ids.push(relation.id || key);
    }
    const edges=[...unique.values()].sort((a,b)=>a.key.localeCompare(b.key));
    const navDegrees=new Array(nodes.length).fill(0);
    for (const edge of edges) if (edge.type==='navigation_link') {navDegrees[edge.a]++;navDegrees[edge.b]++;}
    for (const edge of edges) {
        // Repeated locators do not multiply attraction; hubs from shared menus
        // are weak layout evidence even when every directed link is factual.
        if (edge.type==='navigation_link') edge.weight/=Math.sqrt(Math.max(1,navDegrees[edge.a],navDegrees[edge.b]));
        edge.ids=[...new Set(edge.ids)].sort();
    }
    const groups=new Map();
    for (const edge of edges) if (MEMBERSHIP_TYPES.has(edge.type)) {
        const id='evidenced:'+JSON.stringify([nodes[edge.a].id,edge.type,edge.predicate]);
        if (!groups.has(id)) groups.set(id,{id,origin:'evidenced_membership',owner:edge.a,
            relationType:edge.type,predicate:edge.predicate,relationIds:new Set(),members:new Set([edge.a])});
        groups.get(id).members.add(edge.b);
        edge.ids.forEach(value=>groups.get(id).relationIds.add(value));
    }
    for (const explicit of input.groups || []) {
        if (explicit.kind!=='display' || !explicit.id) continue;
        const members=(explicit.members || []).map(id=>indices.get(id)).filter(index=>index!==undefined);
        if (members.length) groups.set('display:'+explicit.id,{id:'display:'+explicit.id,origin:'explicit_display_group',
            owner:null,label:explicit.label || '',relationIds:new Set(explicit.relationIds || []),members:new Set(members)});
    }
    const orderedGroups=[...groups.values()].sort((a,b)=>a.id.localeCompare(b.id));
    // Directory inclusion can shape positions but never enlarge a publisher's
    // ownership count. Display groups do not participate in this traversal.
    const sourceSets=configuredSourceSets(nodes,input.relations||[]);
    const sourceCounts=nodes.map(node=>sourceSets.get(node.id).size);
    const requestedRadii=previousMap(options.nodeRadii),radiusBasis=nodes.map(node=>
        requestedRadii.has(node.id)?'explicit_display_geometry':'unique_configured_source_count');
    const radii=sourceCounts.map((count,index)=>{
        if(!requestedRadii.has(nodes[index].id))return sourceCountRadius(count);
        const radius=requestedRadii.get(nodes[index].id);
        if(!Number.isFinite(radius)||radius<=0)throw new Error('Invalid node radius');
        return radius;
    });
    const adjacency=nodes.map(()=>[]);
    for (const edge of edges) {adjacency[edge.a].push({to:edge.b,length:edge.length});adjacency[edge.b].push({to:edge.a,length:edge.length});}
    return {nodes,indices,edges,groups:orderedGroups,radii,radiusBasis,sourceCounts,adjacency,omitted,scale};
}

// Geometry uses undirected distances. These are not website visit paths.
export function structureGraphDistances(adjacency, origin) {
    if(!Number.isInteger(origin)||origin<0||origin>=adjacency.length)throw new Error('Invalid distance origin');
    const distances=new Array(adjacency.length).fill(Infinity),settled=new Uint8Array(adjacency.length),heap=[];
    const before=(a,b)=>a.distance<b.distance || (a.distance===b.distance && a.index<b.index);
    const push=item=>{
        let at=heap.length;heap.push(item);
        while(at>0){const parent=(at-1)>>1;if(!before(item,heap[parent]))break;
            heap[at]=heap[parent];at=parent;}
        heap[at]=item;
    };
    const pop=()=>{
        const first=heap[0],last=heap.pop();
        if(heap.length){let at=0;
            while(at*2+1<heap.length){let child=at*2+1;
                if(child+1<heap.length&&before(heap[child+1],heap[child]))child++;
                if(!before(heap[child],last))break;
                heap[at]=heap[child];at=child;}
            heap[at]=last;
        }
        return first;
    };
    for(const neighbors of adjacency)for(const edge of neighbors){
        if(!Number.isInteger(edge.to)||edge.to<0||edge.to>=adjacency.length||!Number.isFinite(edge.length)||edge.length<0)
            throw new Error('Invalid geometry distance edge');
    }
    distances[origin]=0;push({index:origin,distance:0});
    while(heap.length){const item=pop(),index=item.index;
        if(settled[index]||item.distance!==distances[index])continue;
        settled[index]=1;
        for(const edge of adjacency[index]){
            const distance=item.distance+edge.length;
            if(distance<distances[edge.to]){distances[edge.to]=distance;push({index:edge.to,distance});}
        }
    }
    return distances;
}

function pairConstraints(model, options={}) {
    const {nodes,edges,adjacency,scale}=model;
    const constraints=edges.map(edge=>({...edge,kind:'relation'}));
    const direct=new Set(edges.map(edge=>[Math.min(edge.a,edge.b),Math.max(edge.a,edge.b)].join(':')));
    // All pairs on small graphs, deterministic landmarks on larger graphs.
    // These are geometric distances, not website access routes or new facts.
    const landmarkCount=options.landmarkCount ?? (nodes.length<=256 ? nodes.length : 32);
    if(!Number.isInteger(landmarkCount) || landmarkCount<0)throw new Error('Invalid landmark count');
    const landmarks=landmarkCount>=nodes.length ? nodes.map((_,i)=>i) : nodes.map((node,i)=>({i,seed:hash(node.id)}))
        .sort((a,b)=>a.seed-b.seed||a.i-b.i).slice(0,landmarkCount).map(item=>item.i);
    const used=new Set(direct);
    for (const origin of landmarks) {
        const distances=structureGraphDistances(adjacency,origin);
        for (let target=0;target<nodes.length;target++) {
            const key=[Math.min(origin,target),Math.max(origin,target)].join(':');
            if(origin===target || used.has(key) || !Number.isFinite(distances[target]))continue;
            used.add(key);constraints.push({a:origin,b:target,length:distances[target],
                weight:.07/(distances[target]/scale)**2,kind:'graph_distance'});
        }
    }
    return constraints;
}

function movePair(positions,a,b,length,strength,mobility) {
    let difference=AXES.map(axis=>positions[b][axis]-positions[a][axis]);
    let distance=Math.hypot(...difference);
    if(distance<1e-8){const seed=seedPoint(a+':'+b,1);difference=AXES.map(axis=>seed[axis]);distance=Math.hypot(...difference);}
    const total=mobility[a]+mobility[b];if(!total)return;
    const correction=(distance-length)*Math.min(1,strength)/distance;
    for(let i=0;i<3;i++){
        positions[a][AXES[i]]+=difference[i]*correction*mobility[a]/total;
        positions[b][AXES[i]]-=difference[i]*correction*mobility[b]/total;
    }
}
function repel(positions,radii,mobility,padding) {
    const cellSize=2*Math.max(3,...radii)+padding, cells=new Map();
    for(let i=0;i<positions.length;i++){
        const p=positions[i], cell=AXES.map(axis=>Math.floor(p[axis]/cellSize));
        for(let x=-1;x<=1;x++)for(let y=-1;y<=1;y++)for(let z=-1;z<=1;z++){
            for(const j of cells.get([cell[0]+x,cell[1]+y,cell[2]+z].join(':')) || []){
                const minimum=radii[i]+radii[j]+padding;
                if(dist(p,positions[j])<minimum)movePair(positions,i,j,minimum,1,mobility);
            }
        }
        const key=cell.join(':');if(!cells.has(key))cells.set(key,[]);cells.get(key).push(i);
    }
}

export function layoutStructure(input, options={}) {
    const model=prepare(input,options), {nodes,edges,groups,radii,scale}=model;
    const previous=previousMap(input.previousPositions), extent=scale*Math.max(1,Math.cbrt(nodes.length))*.65;
    const positions=nodes.map(node=>finite(previous.get(node.id)) ? copy(previous.get(node.id)) : seedPoint(node.id,extent));
    const anchored=nodes.map(node=>finite(previous.get(node.id)));
    const mobility=anchored.map(value=>value?.12:1);
    // Hierarchical initialization respects every explicit membership. A shared
    // source starts between its groups rather than being assigned one parent.
    const initialCenters=new Map(groups.map(group=>[group.id,seedPoint(group.id,extent)]));
    for(let i=0;i<nodes.length;i++)if(!anchored[i]){
        const homes=groups.filter(group=>group.members.has(i)).map(group=>initialCenters.get(group.id));
        if(homes.length){const center=centroid(homes),offset=seedPoint(nodes[i].id,scale*.6);
            positions[i]=Object.fromEntries(AXES.map(axis=>[axis,center[axis]+offset[axis]]));}
    }
    // New nodes join already visible neighbors near their recorded positions.
    for(let i=0;i<nodes.length;i++)if(!anchored[i]){
        const neighbors=model.adjacency[i].filter(edge=>anchored[edge.to]).map(edge=>positions[edge.to]);
        if(neighbors.length){const center=centroid(neighbors),offset=seedPoint(nodes[i].id,scale*.55);
            positions[i]=Object.fromEntries(AXES.map(axis=>[axis,center[axis]+offset[axis]]));}
    }
    const constraints=pairConstraints(model,options), rng=random(options.seed ?? 271828), order=constraints.map((_,i)=>i);
    const iterations=options.iterations ?? 90, maxDisplacement=options.maxDisplacement ?? scale*.35;
    const constrainAnchors=()=>{
        for(let i=0;i<nodes.length;i++)if(anchored[i]){
            const origin=previous.get(nodes[i].id), length=dist(origin,positions[i]);
            for(const axis of AXES){positions[i][axis]+=(origin[axis]-positions[i][axis])*.14;
                if(length>maxDisplacement)positions[i][axis]=origin[axis]+(positions[i][axis]-origin[axis])*maxDisplacement/length;}
        }
    };
    for(let pass=0;pass<iterations;pass++){
        const step=1.6*Math.pow(.018/1.6,pass/Math.max(1,iterations-1));
        for(let i=order.length-1;i>0;i--){const other=Math.floor(rng()*(i+1));[order[i],order[other]]=[order[other],order[i]];}
        for(const index of order){const constraint=constraints[index];
            movePair(positions,constraint.a,constraint.b,constraint.length,step*constraint.weight,mobility);}
        // Soft compound cohesion cannot override strong evidence or erase a
        // second membership. No group is interpreted as an access-path edge.
        for(const group of groups){const center=centroid([...group.members].map(index=>positions[index]));
            for(const index of group.members)for(const axis of AXES)
                positions[index][axis]+=(center[axis]-positions[index][axis])*.002*mobility[index];}
        const center=centroid(positions);
        for(let i=0;i<nodes.length;i++){
            const radius=dist(positions[i],center);
            if(radius>extent*1.6)for(const axis of AXES)
                positions[i][axis]+=(center[axis]-positions[i][axis])*.002*mobility[i];
        }
        repel(positions,radii,mobility,options.padding ?? 3);
        constrainAnchors();
    }
    const output=Object.fromEntries(nodes.map((node,index)=>[node.id,copy(positions[index])]));
    return {positions:output,metrics:measureStructureLayout(input,output,{...options,model}),
        algorithm:'prototype-3d-pair-stress-v2',seed:options.seed ?? 271828,
        constraints:{relations:edges.length,graphDistance:constraints.length-edges.length,
            groups:groups.map(group=>({id:group.id,origin:group.origin,owner:group.owner===null?null:nodes[group.owner].id,
                label:group.label || '',relationType:group.relationType || null,predicate:group.predicate || null,
                establishesOwnership:group.relationType==='source_ownership',
                relationIds:[...group.relationIds].sort(),members:[...group.members].map(i=>nodes[i].id)})),
            omitted:model.omitted,participatingRelationIds:edges.flatMap(edge=>edge.ids)},
        sizes:Object.fromEntries(nodes.map((node,index)=>[node.id,{radius:radii[index],
            radiusBasis:model.radiusBasis[index],
            connectedSourceCount:model.sourceCounts[index],basis:'unique_configured_sources_in_explicit_membership'}]))};
}

function compoundModel(model) {
    const {nodes,edges,groups,scale}=model, children=nodes.map(()=>new Set()), factual=new Map();
    for(const edge of edges)if(MEMBERSHIP_TYPES.has(edge.type)){
        children[edge.a].add(edge.b);
        if(!factual.has(edge.a))factual.set(edge.a,{id:'compound:'+nodes[edge.a].id,
            owner:edge.a,origin:'evidenced_membership',direct:new Set([edge.a]),relationIds:new Set(),types:new Set()});
        const group=factual.get(edge.a);group.direct.add(edge.b);group.types.add(edge.type);
        edge.ids.forEach(id=>group.relationIds.add(id));
    }
    const closure=origins=>{
        const seen=new Set(),queue=[...origins];
        while(queue.length){const next=queue.pop();if(seen.has(next))continue;
            seen.add(next);queue.push(...children[next]);}
        return seen;
    };
    const compounds=[...factual.values(),...groups.filter(group=>group.origin==='explicit_display_group').map(group=>({
        id:group.id,owner:null,origin:group.origin,label:group.label,direct:new Set(group.members),
        relationIds:new Set(group.relationIds),types:new Set()}))]
        .sort((a,b)=>a.id.localeCompare(b.id));
    for(const group of compounds){
        // Expanding a display envelope through evidenced membership is geometry
        // only. Neither its members nor bounds are written back as facts.
        group.members=closure(group.direct);group.parents=[];group.cyclePeers=[];
        group.radius=scale*(.45+.38*Math.cbrt(group.members.size));
    }
    for(const group of compounds)if(group.owner!==null)for(const parent of compounds){
        if(group===parent || !parent.direct.has(group.owner))continue;
        if(parent.owner!==null && group.members.has(parent.owner) && parent.members.has(group.owner)){
            group.cyclePeers.push(parent.id);continue;
        }
        group.parents.push(parent.id);
    }
    const byId=new Map(compounds.map(group=>[group.id,group]));
    const level=(group,seen=new Set())=>seen.has(group.id)?0:Math.max(0,...group.parents.map(id=>
        1+level(byId.get(id),new Set([...seen,group.id]))));
    for(const group of compounds)group.level=level(group);
    for(const child of [...compounds].sort((a,b)=>b.level-a.level))for(const id of child.parents)
        byId.get(id).radius=Math.max(byId.get(id).radius,child.radius+scale*.35);
    const homes=nodes.map((_,index)=>compounds.filter(group=>group.members.has(index)));
    const separated=[];
    for(let a=0;a<compounds.length;a++)for(let b=a+1;b<compounds.length;b++){
        if([...compounds[a].members].some(index=>compounds[b].members.has(index)))continue;
        separated.push([a,b]);
    }
    return {compounds,homes,separated};
}

export function structureCompoundGroups(input, options={}) {
    const model=prepare(input,options), {compounds}=compoundModel(model);
    return compounds.map(group=>({id:group.id,origin:group.origin,
        owner:group.owner===null?null:model.nodes[group.owner].id,label:group.label || '',
        level:group.level,parents:[...group.parents],cyclePeers:[...group.cyclePeers],
        directMembers:[...group.direct].map(index=>model.nodes[index].id).sort(),
        members:[...group.members].map(index=>model.nodes[index].id).sort(),
        relationIds:[...group.relationIds].sort(),relationTypes:[...group.types].sort(),
        establishesOwnership:false,geometryOnly:true}));
}

function volumeAxes(positions) {
    const center=centroid(positions);
    const matrix=AXES.map(a=>AXES.map(b=>average(positions.map(p=>(p[a]-center[a])*(p[b]-center[b])))));
    const vectors=[[1,0,0],[0,1,0],[0,0,1]];
    // Jacobi diagonalization of the full symmetric covariance, including the
    // off-diagonal terms of a distribution tilted relative to the view axes.
    for(let sweep=0;sweep<18;sweep++){
        let p=0,q=1;
        for(const [a,b] of [[0,2],[1,2]])if(Math.abs(matrix[a][b])>Math.abs(matrix[p][q])){p=a;q=b;}
        if(Math.abs(matrix[p][q])<1e-12)break;
        const angle=.5*Math.atan2(2*matrix[p][q],matrix[q][q]-matrix[p][p]);
        const c=Math.cos(angle),s=Math.sin(angle),app=matrix[p][p],aqq=matrix[q][q],apq=matrix[p][q];
        for(let k=0;k<3;k++)if(k!==p&&k!==q){
            const a=matrix[k][p],b=matrix[k][q];
            matrix[k][p]=matrix[p][k]=c*a-s*b;matrix[k][q]=matrix[q][k]=s*a+c*b;
        }
        matrix[p][p]=c*c*app-2*s*c*apq+s*s*aqq;
        matrix[q][q]=s*s*app+2*s*c*apq+c*c*aqq;matrix[p][q]=matrix[q][p]=0;
        for(let k=0;k<3;k++){
            const a=vectors[k][p],b=vectors[k][q];vectors[k][p]=c*a-s*b;vectors[k][q]=s*a+c*b;
        }
    }
    return {center,axes:[0,1,2].map(k=>({variance:Math.max(0,matrix[k][k]),
        direction:vectors.map(row=>row[k])})).sort((a,b)=>a.variance-b.variance)};
}

export function relaxSphericalVolume(positions, mobility) {
    const {center,axes}=volumeAxes(positions),mean=average(axes.map(axis=>axis.variance));
    for(let i=0;i<positions.length;i++){
        const difference=AXES.map(axis=>positions[i][axis]-center[axis]),delta=[0,0,0];
        for(const eigen of axes){
            const projection=difference.reduce((sum,value,k)=>sum+value*eigen.direction[k],0);
            const correction=Math.max(-.006,Math.min(.006,(Math.sqrt(mean/Math.max(eigen.variance,1e-8))-1)*.02));
            eigen.direction.forEach((value,k)=>delta[k]+=value*projection*correction);
        }
        AXES.forEach((axis,k)=>positions[i][axis]+=delta[k]*mobility[i]);
    }
}

export function layoutCompoundStructure(input, options={}) {
    const model=prepare(input,options), {nodes,edges,radii,scale}=model;
    const {compounds,homes,separated}=compoundModel(model);
    const previous=previousMap(input.previousPositions),extent=scale*Math.max(1,Math.cbrt(nodes.length))*.65;
    const anchored=nodes.map(node=>finite(previous.get(node.id))),mobility=anchored.map(value=>value?.12:1);
    const centers=compounds.map(group=>{
        const retained=[...group.members].filter(index=>anchored[index]).map(index=>previous.get(nodes[index].id));
        return retained.length?centroid(retained):seedPoint(group.id,extent);
    });
    const groupIndex=new Map(compounds.map((group,index)=>[group.id,index]));
    // Coarse links aggregate geometry weights, never structural identities.
    // Fractional endpoint membership preserves multiple parents without picking
    // a primary owner. Every original relation still runs in the fine pass.
    const aggregate=new Map();
    for(const edge of edges)for(const a of homes[edge.a])for(const b of homes[edge.b]){
        if(a===b)continue;
        const ai=groupIndex.get(a.id),bi=groupIndex.get(b.id),key=[Math.min(ai,bi),Math.max(ai,bi)].join(':');
        if(!aggregate.has(key))aggregate.set(key,{a:Math.min(ai,bi),b:Math.max(ai,bi),weight:0,ids:new Set()});
        const pair=aggregate.get(key);pair.weight+=edge.weight/(homes[edge.a].length*homes[edge.b].length);
        edge.ids.forEach(id=>pair.ids.add(id));
    }
    const coarse=[...aggregate.values()].sort((a,b)=>a.a-b.a||a.b-b.b),groupMobility=compounds.map(()=>1);
    for(let pass=0;pass<24;pass++){
        for(const edge of coarse)movePair(centers,edge.a,edge.b,
            (compounds[edge.a].radius+compounds[edge.b].radius)*.8,.08*Math.min(1,edge.weight),groupMobility);
        for(const [a,b] of separated){const minimum=compounds[a].radius+compounds[b].radius+scale*.15;
            if(dist(centers[a],centers[b])<minimum)movePair(centers,a,b,minimum,.25,groupMobility);}
        for(let child=0;child<compounds.length;child++)for(const id of compounds[child].parents){
            const parent=groupIndex.get(id),limit=compounds[parent].radius-compounds[child].radius;
            if(dist(centers[parent],centers[child])>limit)movePair(centers,parent,child,limit,.12,groupMobility);
        }
    }
    const positions=nodes.map((node,index)=>{
        if(anchored[index])return copy(previous.get(node.id));
        const retained=model.adjacency[index].filter(edge=>anchored[edge.to]).map(edge=>previous.get(nodes[edge.to].id));
        const own=homes[index];
        const center=retained.length?centroid(retained):own.length?centroid(own.map(group=>centers[groupIndex.get(group.id)])):null;
        const offset=seedPoint(node.id,center?scale*.65:extent);
        return center?Object.fromEntries(AXES.map(axis=>[axis,center[axis]+offset[axis]])):offset;
    });
    const constraints=pairConstraints(model,{landmarkCount:options.landmarkCount ?? 32});
    const order=constraints.map((_,index)=>index),rng=random(options.seed ?? 271828);
    const iterations=options.iterations ?? 90,maxDisplacement=options.maxDisplacement ?? scale*.35;
    const constrainAnchors=()=>{
        for(let i=0;i<nodes.length;i++)if(anchored[i]){
            const origin=previous.get(nodes[i].id);
            for(const axis of AXES)positions[i][axis]+=(origin[axis]-positions[i][axis])*.14;
            const length=dist(origin,positions[i]);
            if(length>maxDisplacement)for(const axis of AXES)
                positions[i][axis]=origin[axis]+(positions[i][axis]-origin[axis])*maxDisplacement/length;
        }
    };
    const bounds=()=>compounds.map(group=>({center:centroid([...group.members].map(index=>positions[index])),radius:group.radius}));
    for(let pass=0;pass<iterations;pass++){
        const step=1.6*Math.pow(.018/1.6,pass/Math.max(1,iterations-1));
        for(let i=order.length-1;i>0;i--){const other=Math.floor(rng()*(i+1));[order[i],order[other]]=[order[other],order[i]];}
        for(const index of order){const edge=constraints[index];movePair(positions,edge.a,edge.b,edge.length,step*edge.weight,mobility);}
        const envelopes=bounds();
        for(let g=0;g<compounds.length;g++){
            const group=compounds[g],{center,radius}=envelopes[g];
            for(const index of group.members){
                const length=dist(center,positions[index]);
                const amount=(length > radius ? .07*(1-radius/length) : .0015)*mobility[index]/Math.max(1,homes[index].length);
                for(const axis of AXES)positions[index][axis]+=(center[axis]-positions[index][axis])*amount;
            }
        }
        for(let child=0;child<compounds.length;child++)for(const id of compounds[child].parents){
            const parent=groupIndex.get(id),length=dist(envelopes[parent].center,envelopes[child].center);
            const limit=envelopes[parent].radius-envelopes[child].radius;
            if(length<=limit || length<1e-8)continue;
            const amount=.05*(1-limit/length);
            for(const index of compounds[child].members)for(const axis of AXES)
                positions[index][axis]+=(envelopes[parent].center[axis]-envelopes[child].center[axis])*amount
                    *mobility[index]/Math.max(1,homes[index].length);
        }
        // Disjoint envelopes have separation pressure; intersecting membership
        // sets are allowed to overlap. Shared sources are never copied/removed.
        for(const [a,b] of separated){
            const delta=AXES.map(axis=>envelopes[b].center[axis]-envelopes[a].center[axis]);
            let distance=Math.hypot(...delta);
            if(distance<1e-8){const offset=seedPoint(compounds[a].id+compounds[b].id,1);
                AXES.forEach((axis,index)=>delta[index]=offset[axis]);distance=Math.hypot(...delta);}
            const minimum=(envelopes[a].radius+envelopes[b].radius)*.85;
            if(distance>=minimum)continue;
            const shift=(minimum-distance)*.025/distance;
            for(const [g,sign] of [[a,-1],[b,1]])for(const index of compounds[g].members)
                AXES.forEach((axis,k)=>positions[index][axis]+=sign*delta[k]*shift*mobility[index]/Math.max(1,homes[index].length));
        }
        // A weak volume penalty discourages a flat arrangement, while all
        // relationship and compound constraints remain the primary objective.
        relaxSphericalVolume(positions,mobility);
        repel(positions,radii,mobility,options.padding ?? 3);constrainAnchors();
    }
    for(let pass=0;pass<8;pass++){repel(positions,radii,mobility,options.padding ?? 3);constrainAnchors();}
    const output=Object.fromEntries(nodes.map((node,index)=>[node.id,copy(positions[index])]));
    const targets=bounds(),envelopes=targets.map((target,index)=>({...target,radius:Math.max(0,
        ...[...compounds[index].members].map(i=>dist(target.center,positions[i])+radii[i]))}));
    // Actual envelopes resize bottom-up. Their containment is a construction
    // invariant, not a claim that the smaller soft packing targets were met.
    for(const child of [...compounds].sort((a,b)=>b.level-a.level))for(const id of child.parents){
        const c=groupIndex.get(child.id),p=groupIndex.get(id);
        envelopes[p].radius=Math.max(envelopes[p].radius,dist(envelopes[p].center,envelopes[c].center)+envelopes[c].radius);
    }
    const groupReports=structureCompoundGroups(input,options).map((group,index)=>{
        const envelope=envelopes[index],members=[...compounds[index].members];
        const excess=members.map(i=>Math.max(0,dist(envelope.center,positions[i])+radii[i]-targets[index].radius));
        return {...group,targetRadius:targets[index].radius,center:envelope.center,actualRadius:envelope.radius,
            outsideTarget:excess.filter(value=>value>.01).length,maxExcess:Math.max(0,...excess)};
    });
    const nested=compounds.flatMap((group,child)=>group.parents.map(id=>{
        const parent=groupIndex.get(id);
        return {parent:id,child:group.id,excess:Math.max(0,
            dist(envelopes[parent].center,envelopes[child].center)+envelopes[child].radius-envelopes[parent].radius),
            targetExcess:Math.max(0,dist(targets[parent].center,targets[child].center)+targets[child].radius-targets[parent].radius)};
    }));
    return {positions:output,algorithm:'prototype-3d-compound-stress-v3',seed:options.seed ?? 271828,
        metrics:{...measureStructureLayout(input,output,{...options,model}),
            compoundTargetExcess:{groups:groupReports.length,outsideTarget:groupReports.reduce((sum,g)=>sum+g.outsideTarget,0),
                maxExcess:Math.max(0,...groupReports.map(g=>g.maxExcess))},
            compoundContainment:{groups:groupReports.length,outsideActual:envelopes.reduce((sum,envelope,g)=>sum+
                [...compounds[g].members].filter(i=>dist(envelope.center,positions[i])+radii[i]>envelope.radius+.01).length,0),
                basis:'Enclosing bounds resized from final positions; construction invariant only'},
            nestedContainment:{constraints:nested.length,violated:nested.filter(row=>row.excess>.01).length,
                maxExcess:Math.max(0,...nested.map(row=>row.excess)),rows:nested},
            nestedTargetExcess:{constraints:nested.length,violated:nested.filter(row=>row.targetExcess>.01).length,
                maxExcess:Math.max(0,...nested.map(row=>row.targetExcess))},
            disjointEnvelopeOverlaps:separated.filter(([a,b])=>dist(envelopes[a].center,envelopes[b].center)<envelopes[a].radius+envelopes[b].radius).length},
        constraints:{relations:edges.length,graphDistance:constraints.length-edges.length,
            participatingRelationIds:edges.flatMap(edge=>edge.ids),omitted:model.omitted,groups:groupReports,
            coarseGeometryOnly:true,coarseLinks:coarse.map(edge=>({a:compounds[edge.a].id,b:compounds[edge.b].id,
                weight:edge.weight,relationIds:[...edge.ids].sort()})),disjointPairs:separated.map(([a,b])=>[compounds[a].id,compounds[b].id])},
        sizes:Object.fromEntries(nodes.map((node,index)=>[node.id,{radius:radii[index],
            radiusBasis:model.radiusBasis[index],
            connectedSourceCount:model.sourceCounts[index],basis:'unique_configured_sources_in_explicit_membership'}]))};
}

function projectionAreas(points) {
    const normalize=vector=>{const length=Math.hypot(...vector);return vector.map(value=>value/length);};
    const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1],a[2]*b[0]-a[0]*b[2],a[0]*b[1]-a[1]*b[0]];
    const dot=(p,v)=>p.x*v[0]+p.y*v[1]+p.z*v[2];
    return [[0,0,1],[1,0,0],[0,1,0],[1,1,1],[1,-1,1],[1,0,1]].map(view=>{
        const normal=normalize(view),right=normalize(cross(normal,Math.abs(normal[1])>.9?[1,0,0]:[0,1,0])),up=cross(right,normal);
        const projected=points.map(p=>[dot(p,right),dot(p,up)]).sort((a,b)=>a[0]-b[0]||a[1]-b[1]);
        const turn=(a,b,c)=>(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0]);
        const half=values=>{const result=[];for(const point of values){while(result.length>1&&turn(result.at(-2),result.at(-1),point)<=0)result.pop();result.push(point);}return result;};
        const hull=[...half(projected).slice(0,-1),...half([...projected].reverse()).slice(0,-1)];
        return Math.abs(hull.reduce((area,p,i)=>{const q=hull[(i+1)%hull.length];return area+p[0]*q[1]-p[1]*q[0];},0))/2;
    });
}

export function measureStructureLayout(input, positions, options={}) {
    const model=options.model || prepare(input,options), {nodes,edges,radii}=model;
    const map=previousMap(positions), previous=previousMap(input.previousPositions);
    if(nodes.some(node=>!finite(map.get(node.id))))throw new Error('Missing or non-finite position');
    const points=nodes.map(node=>map.get(node.id)), byType={};let stress=0,weight=0,overlaps=0;
    for(const edge of edges){const length=dist(points[edge.a],points[edge.b]);
        (byType[edge.type]??=[]).push(length);stress+=edge.weight*((length-edge.length)/edge.length)**2;weight+=edge.weight;}
    for(let a=0;a<points.length;a++)for(let b=a+1;b<points.length;b++)if(dist(points[a],points[b])<radii[a]+radii[b])overlaps++;
    const shape=volumeAxes(points),variances=shape.axes.map(axis=>axis.variance);
    const areas=projectionAreas(points), movement=nodes.filter(node=>finite(previous.get(node.id)))
        .map(node=>dist(map.get(node.id),previous.get(node.id)));
    return {nodes:nodes.length,relations:edges.length,weightedRelativeStress:weight?stress/weight:0,
        distanceByType:Object.fromEntries(Object.entries(byType).map(([type,values])=>[type,
            {count:values.length,mean:average(values),median:quantile(values,.5),p95:quantile(values,.95)}])),
        sphereOverlaps:overlaps,projectionAreas:areas,projectionAreaRatio:Math.max(...areas)>0?Math.min(...areas)/Math.max(...areas):null,
        volumeIsotropy:{covarianceEigenvalues:variances,ratio:variances[2]>0?variances[0]/variances[2]:null},
        updateMovement:{count:movement.length,mean:average(movement),p95:quantile(movement,.95),max:Math.max(0,...movement)},
        labelOcclusion:'not_measured',taskCompletion:'not_measured'};
}
