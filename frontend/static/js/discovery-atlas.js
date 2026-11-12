/* Local WebGL scene. Viewing or touring never dispatches discovery work. */
import {buildAtlas, ancestors, visibleEntries, trackingTarget, accessRoute, membershipRoute, explorationTrace, atlasLayoutSignature, refreshAtlasRecords} from './discovery-atlas-model.js';
import {focusContext, displayLink, labelPolicy, labelImportance, labelSearchMatch, searchLabelLevel, placeLabelBox, labelHierarchyLevels, labelAncestors} from './discovery-atlas-view.js';
import {identity, birthAnchor, growthPlan, recordedGrowthPlan, growthAt, mix, smooth, clamp} from './discovery-atlas-motion.js';
import {pickSphere} from './discovery-atlas-picking.js';
import {AtlasLayoutController} from './discovery-atlas-layout.js';
import {appearanceColor} from './structure-appearance.js';
import {AtlasRelationView} from './discovery-atlas-relations.js';

const root = document.getElementById('discoveryMonitor');
const el = id => document.getElementById(id);
const stage = el('atlasStage'), viewport = el('atlasViewport');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let atlas;
const preferenceKey = 'school-watcher:atlas:preferences';
try {
    const saved = JSON.parse(localStorage.getItem(preferenceKey) || '{}');
    el('atlasFollow').checked = saved.trace !== false;
    el('atlasNodeLabels').checked = typeof saved.nodeLabels === 'boolean' ? saved.nodeLabels : saved.departmentNames !== false;
    if (Number.isFinite(saved.spacing)) el('atlasSpacing').value = Math.max(.7, Math.min(1.8, saved.spacing));
} catch (_) { /* Presentation remains usable when local storage is unavailable. */ }
function savePreferences() {
    try { localStorage.setItem(preferenceKey, JSON.stringify({trace:el('atlasFollow').checked, spacing:Number(el('atlasSpacing').value), nodeLabels:el('atlasNodeLabels').checked})); }
    catch (_) { /* A blocked preference store must not block the graph. */ }
}

function fallback(message) {
    if (root.classList.contains('atlas-expanded')) leaveImmersive();
    stage.dataset.renderer = 'unavailable';
    el('atlasStatus').textContent = message;
    el('atlasListPanel').open = true;
    el('atlasPlay').disabled = true;
    for (const id of ['atlasZoomIn', 'atlasZoomOut', 'atlasOverview', 'atlasLocate', 'atlasSpacing', 'atlasNodeLabels']) el(id).disabled = true;
    el('atlasFollowStatus').textContent = uiT('三维视图暂不可用，已展开路径列表。');
}

async function initialize() {
    const [THREE, {OrbitControls}, {EffectComposer}, {RenderPass}, {UnrealBloomPass}, {OutputPass}] = await Promise.all([
        import('three'), import('../vendor/three/addons/controls/OrbitControls.js'),
        import('../vendor/three/addons/postprocessing/EffectComposer.js'),
        import('../vendor/three/addons/postprocessing/RenderPass.js'),
        import('../vendor/three/addons/postprocessing/UnrealBloomPass.js'),
        import('../vendor/three/addons/postprocessing/OutputPass.js')
    ]);
    const renderer = new THREE.WebGLRenderer({antialias:true, alpha:false, powerPreference:'low-power'});
    renderer.setPixelRatio(Math.min(devicePixelRatio, 1.75));
    // Preserve the application's exact surface colors through the output pass.
    renderer.toneMapping = THREE.NoToneMapping;
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    viewport.append(renderer.domElement);
    renderer.domElement.setAttribute('aria-hidden', 'true');
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(42, 1, .5, 6000);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = !reduced.matches;
    controls.dampingFactor = .1;
    controls.minDistance = 35; controls.maxDistance = 2600;
    controls.zoomSpeed = .8; controls.rotateSpeed = .65;
    controls.enablePan = true; controls.screenSpacePanning = true;
    controls.mouseButtons.MIDDLE = THREE.MOUSE.PAN;
    const hemisphere = new THREE.HemisphereLight(0xf2f6ff, 0x303849, 1.8);
    const keyLight = new THREE.DirectionalLight(0xffffff, 3.2); keyLight.position.set(-180, 240, 300);
    const rimLight = new THREE.DirectionalLight(0x9bbfff, 1.1); rimLight.position.set(170, -30, -180);
    scene.add(hemisphere, keyLight, rimLight);
    const graph = new THREE.Group(), highlight = new THREE.Group();
    highlight.name = 'atlasRoutes';
    scene.add(graph, highlight);
    const composer = new EffectComposer(renderer);
    const bloom = new UnrealBloomPass(new THREE.Vector2(1, 1), .65, .42, 1.1);
    composer.addPass(new RenderPass(scene, camera)); composer.addPass(bloom); composer.addPass(new OutputPass());
    const sphereGeometry = new THREE.SphereGeometry(1, 24, 18);
    const ringGeometry = new THREE.TorusGeometry(1, .035, 5, 36);
    const routeGeometry = new THREE.CylinderGeometry(1,1,1,8,1,true);
    const selectionRingGeometry = new THREE.TorusGeometry(1,.065,6,48);
    const sphereMaterial = new THREE.MeshStandardMaterial({roughness:.47, metalness:.16});
    const matrix = new THREE.Object3D(), raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2(), projected = new THREE.Vector3();
    let model = null, nodes = [], spheres, rings, ringNodes = [], labelNodes = [], current = null;
    let relationView=null,lineMesh=null,relationSummary={availableRelations:0,summarizedNodes:0};
    const expandedRelationGroups=new Set();
    const relationBadge=document.createElement('span');relationBadge.id='atlasRelationBadge';relationBadge.hidden=true;el('atlasInspect').append(relationBadge);
    let signature = '', dataSignature = '', theme, primary, manualColor, stateColors = {}, selectedPath = [], selectedRoute = {nodes:[],links:[],complete:false}, stageIndex = 0, tourTimer = null;
    let contextAvailable = true, explicitSelection = false, replayPosition = 1, replayPlan = null, replayScope = null;
    let replayRecord=null,replayToken=null;
    let frames = new Map(), edges = [], lineGeometry = null, linePositions = null, lineColors = null, layoutMoving = false;
    let layoutSpacing = Number(el('atlasSpacing').value), lastTrackedKey = '', manualView = false;
    let fadingLinks = null, fadingAt = 0;
    let frame = null, lastFrame = 0, tween = null, dirty = true, active = false, inView = true, disposed = false;
    let bounds = new THREE.Sphere(new THREE.Vector3(), 260), extent = new THREE.Vector3(450, 300, 140), hover = null, press = null, lastPick = 0;
    let overviewDistance = 600, overviewActive = false, contextIds = new Set();
    let labelLevels=new Map(),upstreamIds=new Set();
    let modelEpoch='',geometryFocus=null;
    const geometry=new AtlasLayoutController({
        onState:state=>{stage.dataset.layout=state.status;if(model)updateStatus();},
        onReady:readyModel=>{
            if(disposed||readyModel!==model)return;
            const refit=overviewActive,focus=geometryFocus;geometryFocus=null;
            paint();
            if(refit)overview(false);else if(current?.id===focus)frameSelection(current);
            requestFrame();
        },
    });
    const collapsed = new Set(),expandedProofs=new Set();
    const tooltip = document.createElement('div'); tooltip.className = 'atlas-tooltip'; tooltip.hidden = true;
    stage.append(tooltip);

    function disposeGroup(group) {
        group.traverse(object => {
            if (object.geometry && ![sphereGeometry,ringGeometry,routeGeometry,selectionRingGeometry].includes(object.geometry)) object.geometry.dispose();
            if (object.material && object.material !== sphereMaterial) object.material.dispose();
            if (object.isInstancedMesh) object.dispose();
        });
        group.clear();
    }
    function requestFrame() {
        dirty = true;
        if (!disposed && !document.hidden && inView && frame === null) frame = requestAnimationFrame(draw);
    }
    function applyTheme() {
        theme = document.documentElement.dataset.theme === 'dark' ? 'dark' : 'light';
        const css = getComputedStyle(root);
        primary = new THREE.Color(css.getPropertyValue('--color-primary').trim());
        manualColor = new THREE.Color(css.getPropertyValue('--atlas-manual').trim());
        const token = name => css.getPropertyValue(name).trim();
        stateColors = {running:primary, connected:token('--color-success'), failed:token('--color-danger'), waiting:token('--color-warning')};
        scene.background = new THREE.Color(css.getPropertyValue('--color-bg').trim());
        scene.fog = null;
        hemisphere.intensity = theme === 'dark' ? 1.6 : 2.1;
        bloom.strength = theme === 'dark' ? .75 : .28;
        if (model) paint();
        finishTexture(); requestFrame();
    }
    function finishTexture() {
        const canvas = el('atlasFinish'), width = Math.min(stage.clientWidth, 1800), height = stage.clientHeight;
        if (!width || !height) return;
        canvas.width = width; canvas.height = height;
        const ctx = canvas.getContext('2d'), pixels = ctx.createImageData(width, height);
        let seed = 71;
        for (let offset = 0; offset < pixels.data.length; offset += 4) {
            seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
            const value = seed >>> 24;
            pixels.data[offset] = value; pixels.data[offset + 1] = value; pixels.data[offset + 2] = value;
            pixels.data[offset + 3] = theme === 'dark' ? 7 : 4;
        }
        ctx.putImageData(pixels, 0, 0);
    }
    function selectedNodeRoute(item=current) {
        if(root.dataset.pathMode==='membership')return membershipRoute(model,item);
        if(root.dataset.pathMode==='exploration')return explorationTrace(root.monitorSnapshot||{},item,model);
        return accessRoute(model,item);
    }
    function replayGrowthPlan() {
        if(replayRecord)return recordedGrowthPlan(replayRecord);
        const route=replayScope&&current?selectedNodeRoute():null;
        const parents=route?new Map(route.nodes.map((item,index)=>[item.id,index?route.nodes[index-1]:null])):null;
        return growthPlan(replayScope?nodes.filter(item=>replayScope.has(item.id)):nodes,{parents});
    }

    function renderLabels() {
        // Build identities for every real page. The current viewport, rather than
        // a fixed global shortlist, decides which labels have room to be shown.
        labelNodes = [...nodes];
        const fragment = document.createDocumentFragment();
        for (const item of labelNodes) {
            const button = document.createElement('button'); button.type = 'button';
            button.className = 'atlas-node-label'; button.textContent = item.label;
            button.dataset.summaryLabel=item.relationSummaryCount?uiT('{count} 条关联',{count:item.relationSummaryCount}):'';
            button.dataset.kind=item.record.kind;
            button.dataset.atlasNode = item.id; button.dataset.selected = String(item === current);
            button.setAttribute('aria-label', item.label + ' · ' + uiT(item.record?.state_label || uiT('已记录的导航名称')));
            button.addEventListener('click', () => pick(item)); item.labelElement = button; fragment.append(button);
        }
        el('atlasLabels').replaceChildren(fragment);
        // Measure once while every new label is visible. Hidden elements have no box.
        for (const item of labelNodes) {
            const rect = item.labelElement.getBoundingClientRect();
            item.labelWidth = rect.width; item.labelHeight = rect.height;
        }
    }
    function paint() {
        const previous=new Map(nodes.map(item=>[identity(item),{position:{...(item.renderPosition||item.position)},scale:item.baseScale ?? 1}]));
        const previousEdges=edges.filter(edge=>displayLink(edge,current,hover)).map(edge=>({from:identity(edge.from),to:identity(edge.to)}));
        disposeGroup(graph); disposeGroup(highlight);
        fadingLinks=null;lineMesh=null;
        const revealed=new Set(current&&el('atlasFollow').checked?selectedNodeRoute().nodes.map(item=>item.id):[]);
        const available=visibleEntries(model,{query:el('monitorSearch').value.trim(),state:el('monitorFilter').value,collapsed,revealed});
        const projection=relationView.project(available,relationContext());relationSummary=projection;
        nodes=projection.nodes.sort((a,b)=>a.depth-b.depth);
        for(const item of nodes)item.relationSummaryCount=summaryEnabled()?relationView.summaryCount(item.id,expandedRelationGroups):0;
        const shown=new Set(nodes.map(item=>item.id)), now=performance.now();
        frames=new Map(nodes.map(item=>{
            const old=previous.get(identity(item));
            const entering=previous.size>0 && !old && !reduced.matches;
            return [item.id,{from:old?.position || (entering?birthAnchor(item,previous):item.position),
                scale:old?.scale ?? (entering?0:1),at:now,entering,
                moving:entering || Boolean(old && (old.scale!==1 || Math.hypot(old.position.x-item.position.x,old.position.y-item.position.y,old.position.z-item.position.z)>.01))}];
        }));
        replayPlan=replayGrowthPlan();
        spheres=new THREE.InstancedMesh(sphereGeometry,sphereMaterial,nodes.length);
        spheres.name='atlasNodes';
        spheres.instanceMatrix.setUsage(THREE.DynamicDrawUsage); spheres.frustumCulled=false;
        nodes.forEach((item,index)=>spheres.setColorAt(index,new THREE.Color(appearanceColor(item.presentation,theme))));
        graph.add(spheres);
        edges=projection.edges;
        const retained=new Set(nodes.map(identity)), nextEdges=new Set(edges.map(edge=>identity(edge.from)+'|'+identity(edge.to)));
        const rewired=previousEdges.filter(edge=>retained.has(edge.from)&&retained.has(edge.to)&&!nextEdges.has(edge.from+'|'+edge.to));
        if (rewired.length && !reduced.matches) {
            const positions=rewired.flatMap(edge=>[previous.get(edge.from).position,previous.get(edge.to).position].flatMap(point=>[point.x,point.y,point.z]));
            const geometry=new THREE.BufferGeometry(); geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
            fadingLinks=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({color:theme==='dark'?0x8995a8:0x647384,transparent:true,opacity:.5,depthWrite:false}));
            fadingLinks.frustumCulled=false; fadingAt=now; graph.add(fadingLinks);
        }
        installRelationLines(edges);
        // Reserve one instance per visible entity. Status changes update colors
        // and visibility without replacing the scene's geometry or positions.
        ringNodes=nodes;
        rings=new THREE.InstancedMesh(ringGeometry,new THREE.MeshBasicMaterial({transparent:true,opacity:.8}),ringNodes.length);
        rings.instanceMatrix.setUsage(THREE.DynamicDrawUsage); rings.frustumCulled=false;
        ringNodes.forEach((item,index)=>rings.setColorAt(index,new THREE.Color(stateColors[item.record.state]||0)));
        graph.add(rings);
        if (nodes.length) {
            const box=new THREE.Box3(); nodes.forEach(item=>box.expandByPoint(new THREE.Vector3().copy(item.position)));
            box.expandByScalar(24); bounds=box.getBoundingSphere(new THREE.Sphere()); extent=box.getSize(new THREE.Vector3());
            updateCameraRange();
        }
        updateHighlight(); updateStatus(); requestFrame();
        root.dataset.atlasNodeCount=String(nodes.length);
    }
    function summaryEnabled(){return !el('monitorSearch').value.trim()&&el('monitorFilter').value==='all';}
    function relationContext(){
        const route=current&&el('atlasFollow').checked?selectedNodeRoute():{nodes:[],links:[]};
        return {selected:current,hovered:hover,forced:new Set(route.links.map(edge=>edge.id)),
            revealed:new Set(route.nodes.map(item=>item.id)),expanded:expandedRelationGroups,enabled:summaryEnabled()};
    }
    function installRelationLines(next){
        if(lineMesh){graph.remove(lineMesh);lineMesh.geometry.dispose();lineMesh.material.dispose();}
        edges=next;linePositions=new Float32Array(edges.length*2*3);
        lineGeometry=new THREE.BufferGeometry();
        lineGeometry.setAttribute('position',new THREE.BufferAttribute(linePositions,3).setUsage(THREE.DynamicDrawUsage));
        lineColors=new Float32Array(linePositions.length);
        lineGeometry.setAttribute('color',new THREE.BufferAttribute(lineColors,3).setUsage(THREE.DynamicDrawUsage));
        lineMesh=new THREE.LineSegments(lineGeometry,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.38,depthWrite:false}));
        lineMesh.userData.relationIds=edges.map(edge=>edge.id);
        lineMesh.name='atlasLinks';lineMesh.frustumCulled=false;graph.add(lineMesh);
    }
    function refreshRelationLines(){
        const next=relationView.project(nodes,relationContext()).edges;
        if(next.length!==edges.length||next.some((edge,index)=>edge!==edges[index]))installRelationLines(next);
        root.dataset.atlasShownRelations=String(edges.length);
    }
    function animateGeometry(time) {
        if (!spheres || !replayPlan) return;
        layoutMoving=false;
        const pixelFactor=2*Math.tan(THREE.MathUtils.degToRad(camera.fov/2))/Math.max(1,viewport.clientHeight);
        nodes.forEach((item,index)=>{
            const entry=frames.get(item.id), t=reduced.matches||!entry.moving?1:clamp((time-entry.at)/850), ease=smooth(t);
            layoutMoving ||= t<1;
            const base=mix(entry.from,item.position,ease);
            const reveal=replayScope && !replayScope.has(item.id) ? 1 : growthAt(replayPlan,item,replayPosition);
            item.baseScale=entry.scale+(1-entry.scale)*ease;
            item.growth=item.baseScale*reveal;
            const tracedParent=replayScope&&selectedRoute.steps?.find(step=>step.to.id===item.id)?.from;
            const origin=(tracedParent||item.parent)?.renderPosition || base;
            item.renderPosition=reduced.matches?base:mix(origin,base,reveal);
            const distance=camera.position.distanceTo(new THREE.Vector3().copy(item.renderPosition));
            item.drawRadius=Math.max(item.radius,1.35*distance*pixelFactor)*item.growth;
            matrix.position.copy(item.renderPosition); matrix.scale.setScalar(item.drawRadius); matrix.quaternion.identity(); matrix.updateMatrix();
            spheres.setMatrixAt(index,matrix.matrix);
        });
        spheres.instanceMatrix.needsUpdate=true;
        let offset=0;
        for (const edge of edges) {
            const a=edge.from.renderPosition,b=edge.to.renderPosition;
            const reveal=Math.min(edge.from.growth,edge.to.growth);
            for(const fraction of [0,reveal]) {
                const point=mix(a,b,fraction);
                linePositions[offset++]=point.x;linePositions[offset++]=point.y;linePositions[offset++]=point.z;
            }
        }
        lineGeometry.attributes.position.needsUpdate=true;
        if (fadingLinks) {
            const t=clamp((time-fadingAt)/850);fadingLinks.material.opacity=.5*(1-smooth(t));layoutMoving ||= t<1;
            if (t===1) {graph.remove(fadingLinks);fadingLinks.geometry.dispose();fadingLinks.material.dispose();fadingLinks=null;}
        }
        highlight.visible=replayPosition===1 && !layoutMoving;
        root.dataset.atlasProgress=replayPosition.toFixed(4);
    }

    function updateHighlight() {
        disposeGroup(highlight);
        selectedRoute = selectedNodeRoute();
        selectedPath = el('atlasFollow').checked ? selectedRoute.nodes : current ? [current] : [];
        refreshRelationLines();
        const visible = new Set(nodes.map(item => item.id));
        const manualEdges=el('atlasFollow').checked ? selectedRoute.links.filter(edge=>visible.has(edge.from.id)&&visible.has(edge.to.id)) : [];
        const pair=edge=>[edge.from.id,edge.to.id].sort().join('|'),painted=new Set();
        const drawRoute=(items,color,kind)=>{
            for(const edge of items){
                if(painted.has(pair(edge)))continue;painted.add(pair(edge));
                // Only the chosen path crosses the bloom threshold. Directory
                // adjacency can be dense and must remain readable underneath it.
                const path=kind==='manual';
                const line=new THREE.Mesh(routeGeometry,new THREE.MeshBasicMaterial({
                    color:color.clone().multiplyScalar(path?(theme==='dark'?3.2:1.45):1),
                    transparent:!path,opacity:path?1:(theme==='dark'?.52:.65),depthWrite:path
                }));
                line.userData.route=kind;line.userData.entry=edge.to.id;line.userData.source=edge.from.id;line.userData.edge=edge;
                line.position.copy(edge.from.position).lerp(edge.to.position,.5);
                const direction=new THREE.Vector3().copy(edge.to.position).sub(edge.from.position);
                line.userData.length=direction.length();line.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),direction.normalize());
                line.scale.set(.2,line.userData.length,.2);
                highlight.add(line);
            }
        };
        drawRoute(manualEdges,manualColor,'manual');
        const related=current?edges.filter(edge=>(edge.from===current||edge.to===current)&&visible.has(edge.from.id)&&visible.has(edge.to.id)):[];
        drawRoute(related,primary,'connection');
        root.dataset.atlasHighlight=current?'manual':'none';
        root.dataset.atlasPathMode=selectedRoute.mode;
        if(current&&visible.has(current.id)) {
            const ring=new THREE.Mesh(selectionRingGeometry,new THREE.MeshBasicMaterial({color:manualColor.clone().multiplyScalar(theme==='dark'?3.2:1.45)}));
            ring.userData.selection=current;ring.userData.faceCamera=true;highlight.add(ring);
        }
        for (const item of selectedPath) {
            if (!visible.has(item.id)) continue;
            // The fluorescent route and label convey selection; retain status hues.
            {
                const stateColor = new THREE.Color(stateColors[item.record.state]||0);
                const ring = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({color:stateColor.multiplyScalar(1.8)}));
                ring.position.copy(item.position); ring.scale.setScalar(item.radius + 1.8); ring.userData.faceCamera = true;
                ring.userData.status=item;ring.visible=Boolean(stateColors[item.record.state]);
                ring.userData.running = item.record.state === 'running'; highlight.add(ring);
            }
        }
        const crumbs = document.createDocumentFragment();
        for (const item of selectedRoute.nodes) {
            const button = document.createElement('button'); button.type = 'button'; button.textContent = item.label;
            button.addEventListener('click', () => pick(item)); crumbs.append(button);
        }
        el('atlasBreadcrumb').replaceChildren(crumbs);
        el('atlasBranch').hidden = !current?.children.length;
        el('atlasBranch').textContent = uiT(collapsed.has(current?.id) ? '展开分支' : uiT('收起分支'));
        renderRouteEvidence();
        updateEmphasis(false); renderLabels(); updateNarration(); updateFollowInfo(); updateStatus(); requestFrame();
    }
    function renderRouteEvidence() {
        el('atlasPathEvidence')?.remove();
        el('atlasAppearanceEvidence')?.remove();
        if(!current)return;
        const section=document.createElement('section');section.id='atlasPathEvidence';section.className='atlas-path-evidence';
        const heading=document.createElement('h3');heading.textContent=uiT(root.dataset.pathMode==='membership'?'目录或归属路径':
            root.dataset.pathMode==='exploration'?'真实探索记录':'官网访问路径');section.append(heading);
        const note=document.createElement('p');note.className='monitor-caption';
        note.textContent=uiT(root.dataset.pathMode==='exploration'?'仅展示实际记录的探索事件，不从导航关系推测先后顺序。':
            root.dataset.pathMode==='membership'?'目录收录与组织归属分别标识，多重关系均保留。':'沿官网中记录的链接方向显示访问路线。');section.append(note);
        if(root.dataset.pathMode==='exploration'){
            const scope=document.createElement('p');scope.className='monitor-caption';
            scope.textContent=uiT('仅回放所选节点已记录的发现与配置保存过程，较早历史可能缺失。');section.append(scope);
        }
        if(!selectedRoute.complete){const gap=document.createElement('p');gap.className='monitor-caption';gap.textContent=uiT(root.dataset.pathMode==='exploration'?
            '尚无完整的真实探索记录。':'上游依据不完整，仅显示有证据的部分。');section.append(gap);}
        const labels={navigation:uiT('网页导航'),navigation_link:uiT('网页导航'),directory:uiT('目录收录'),directory_listing:uiT('目录收录'),
            organization_membership:uiT('组织归属'),source_ownership:uiT('栏目归属'),column_membership:uiT('栏目归属'),
            source_preview:uiT('栏目预览关联'),
            discovered_from:uiT('实际发现入口'),source_configuration_saved:uiT('栏目配置已保存')};
        for(const route of selectedRoute.paths || [selectedRoute]){
            const list=document.createElement('ol');
            route.nodes.forEach((node,index)=>{
                const row=document.createElement('li'),button=document.createElement('button');button.type='button';button.textContent=node.label;
                button.title=node.record.url||'';button.addEventListener('click',()=>pick(node));row.append(button);
                const edge=index?route.steps[index-1]?.edge:null;
                if(edge){
                    const evidenceItems=edge.evidence?.length?edge.evidence:[{reference_url:edge.evidence_url,locator:edge.locator}];
                    const proofs=document.createElement('details');proofs.className='atlas-route-proof';proofs.dataset.relationId=edge.id;
                    proofs.open=expandedProofs.has(edge.id);
                    proofs.addEventListener('toggle',()=>{
                        if(!proofs.isConnected)return;
                        if(proofs.open)expandedProofs.add(edge.id);else expandedProofs.delete(edge.id);
                    });
                    const summary=document.createElement('summary'),relation=edge.type==='organization_membership'?
                        uiT(edge.observed_relation==='attached_unit'?'挂靠单位':'下属单位'):labels[edge.type]||uiT('已记录关系');
                    summary.textContent=uiT('{relation} · {count} 项依据',{relation,count:evidenceItems.length});proofs.append(summary);
                    const observations=document.createElement('ol');
                    for(const evidence of evidenceItems){
                        const observation=document.createElement('li'),proof=document.createElement('a');proof.textContent=uiT('查看来源页面');
                        proof.href=evidence.reference_url||evidence.evidence_url||'#';proof.target='_blank';proof.rel='noopener noreferrer';
                        proof.title=[evidence.locator,evidence.observed_at,evidence.content_hash].filter(Boolean).join(' · ');observation.append(proof);
                        const locator=document.createElement('p');locator.textContent=evidence.locator||uiT('页面位置未记录');observation.append(locator);
                        if(evidence.observed_at){const observed=document.createElement('time');observed.dateTime=evidence.observed_at;
                            observed.textContent=window.I18n?.dateTime(evidence.observed_at)||evidence.observed_at;observation.append(observed);}
                        observations.append(observation);
                    }
                    proofs.append(observations);row.append(proofs);
                }
                list.append(row);
            });
            if(route.nodes.length)section.append(list);
        }
        if(selectedRoute.targetSource){const source=document.createElement('p');source.textContent=uiT('栏目所在页面')+' → '+selectedRoute.targetSource.label;section.append(source);}
        if(selectedRoute.targetEntity){const entity=document.createElement('p');entity.textContent=uiT('实体对应页面')+' → '+selectedRoute.targetEntity.label;section.append(entity);}
        const eventLabels={exploration_started:uiT('开始检查官网入口'),page_discovered:uiT('首次发现并保存入口'),
            page_inspection_started:uiT('开始检查页面'),page_inspection_finished:uiT('页面检查结束'),
            page_inspection_interrupted:uiT('页面检查中断'),source_configuration_saved:uiT('栏目配置已保存'),
            navigation_application_started:uiT('开始应用导航识别结果'),
            navigation_application_finished:uiT('导航识别结果已保存'),
            navigation_application_interrupted:uiT('导航识别结果保存中断'),
            navigation_model_observed:uiT('导航判断使用记录')};
        const outcomes={read_failed:uiT('读取失败，保留未知'),access_blocked:uiT('访问受阻'),
            material_observed:uiT('页面材料已读取'),interrupted:uiT('检查中断'),reference_only:uiT('文档入口，等待适配'),
            navigation_result_applied:uiT('已按实际材料更新入口')};
        for(const event of selectedRoute.events||[]){const row=document.createElement('p');
            row.textContent=[window.I18n?.dateTime(event.observed_at)||event.observed_at,
                eventLabels[event.event_type]||uiT('已记录事件'),event.label,outcomes[event.outcome]].filter(Boolean).join(' · ');section.append(row);
            const usage=event.model_usage;
            if(usage){
                const kinds={request_attempt:uiT('本次调用模型'),ledger_reuse:uiT('复用已有执行'),
                    no_dispatch:uiT('未派发模型请求'),shared_cache_reuse:uiT('复用导航缓存')};
                for(const execution of usage.executions||[]){const invoice=document.createElement('p');
                    const label=execution.usage_status==='not_dispatched'?uiT('未派发模型请求'):
                        execution.usage_status==='pending'?uiT('调用仍在处理中，用量待确认'):
                        execution.total_tokens===null?uiT('调用用量未知'):
                        execution.usage_status==='administrator_reconciled'?uiT('人工对账用量：{count} Token',{count:execution.total_tokens}):
                        uiT('已确认用量：{count} Token',{count:execution.total_tokens});
                    invoice.textContent=(kinds[execution.use_kind]||uiT('已记录事件'))+' · '+label;section.append(invoice);}
                if(usage.cache_origin_unknown){const unknown=document.createElement('p');
                    unknown.textContent=uiT('复用了 {count} 项导航缓存；原调用记录未知',{count:usage.cache_origin_unknown});section.append(unknown);}
                if(usage.unrecorded_attempts){const unknown=document.createElement('p');unknown.textContent=uiT('部分调用缺少可核对的账本引用');section.append(unknown);}
                const note=document.createElement('p');note.textContent=uiT('用量来自原调用账本，多处复用不重复计费；此处不表示本节点新增成本。');section.append(note);
            }
        }
        el('monitorDetail').querySelector('h3')?.after(section);
        const appearance=document.createElement('section');appearance.id='atlasAppearanceEvidence';appearance.className='atlas-path-evidence';
        const headingAppearance=document.createElement('h3');headingAppearance.textContent=uiT('图谱显示依据');appearance.append(headingAppearance);
        const presentation=current.presentation,count=document.createElement('p');
        count.textContent=uiT('已确认的接入信源数：{count}',{count:presentation.connectedSourceCount});appearance.append(count);
        const group=document.createElement('p'),names=presentation.groups.map(id=>model.actual.get(id)?.label).filter(Boolean);
        group.textContent=names.length?uiT('颜色分组：{names}',{names:names.join(' / ')}):uiT('颜色分组：尚无已确认组织关联');appearance.append(group);
        if(names.length){const groupBasis=document.createElement('p');
            const basisTypes={entity_page:uiT('实体对应页面'),source_ownership:uiT('栏目归属'),column_membership:uiT('栏目归属')};
            const basis=current.record.entity_type==='organization'?uiT('已记录的组织身份'):
                [...new Set(presentation.groupRelationTypes.map(type=>basisTypes[type]||uiT('已记录关系')))].join(' / ');
            groupBasis.textContent=uiT('颜色依据：{basis}',{basis});appearance.append(groupBasis);}
        if(presentation.groupStatus==='shared'){const shared=document.createElement('p');
            shared.textContent=uiT('关联多个组织，使用共享颜色；具体关系以记录的关系类型为准。');appearance.append(shared);}
        const basis=document.createElement('p');basis.className='monitor-caption';
        basis.textContent=uiT('数量按明确归属去重；0 表示暂无已确认数量，未知部分不计入。');appearance.append(basis);
        section.after(appearance);
        const grouped=el('monitorRelationGroups');
        if(grouped&&relationView.groupsFor(current.id).some(group=>relationView.dense(group)))el('monitorDetail').querySelector('h3')?.after(grouped);
    }
    function updateEmphasis(refreshLinks=true) {
        if (!spheres || !model) return;
        if(refreshLinks)refreshRelationLines();
        contextIds=focusContext(model.links,current,hover,selectedPath);
        upstreamIds=labelAncestors(model.links,current,hover);
        const onPath = new Set(selectedPath.map(item=>item.id));
        nodes.forEach((item,index)=>{
            const focused=Boolean(current||hover),near=contextIds.has(item.id);
            const fade=focused && !near ? .8 : focused && !onPath.has(item.id) ? .25 : 0;
            spheres.setColorAt(index,new THREE.Color(appearanceColor(item.presentation,theme)).lerp(scene.background,fade));
        });
        if(spheres.instanceColor)spheres.instanceColor.needsUpdate=true;
        edges.forEach((edge,index)=>{
            const dim=(current||hover) && !(contextIds.has(edge.from.id)&&contextIds.has(edge.to.id));
            const color=new THREE.Color(theme==='dark'?'#8e9aae':'#586d82').lerp(scene.background,dim ? .87 : 0);
            for(let offset=index*6;offset<(index+1)*6;offset+=3){lineColors[offset]=color.r;lineColors[offset+1]=color.g;lineColors[offset+2]=color.b;}
        });
        if(lineGeometry)lineGeometry.attributes.color.needsUpdate=true;
        root.dataset.atlasShownRelations=String(edges.length);
    }
    function updateStatus() {
        if (!contextAvailable) {
            fallback(uiT('三维视图暂不可用，已展开路径列表。'));
            return;
        }
        stage.dataset.renderer = 'ready';
        const actualCount = nodes.filter(item => item.record).length;
        el('atlasStatus').textContent = actualCount ? uiT(geometry.state.status==='pending'
            ?'{count} 个官网入口 · 正在整理图谱位置':'{count} 个官网入口 · 三维视图', {count:actualCount}) :
            uiT(root.monitorSnapshot?.nodes.length ? '没有匹配的入口，请更换名称或状态。' : uiT('还没有已记录的官网路径。启动探索后，这里会显示真实入口。'));
        if(actualCount&&geometry.state.status==='failed')el('atlasStatus').textContent=uiT('图谱位置暂未整理完成，可刷新页面重试。节点与路径仍可查看。');
        el('atlasRelationStatus').textContent=uiT('显示 {shown}/{total} 条关系；{count} 个入口已收纳',{
            shown:edges.length,total:relationSummary.availableRelations,count:relationSummary.summarizedNodes});
        const summarized=current&&summaryEnabled()?relationView.summaryCount(current.id,expandedRelationGroups):0;
        relationBadge.hidden=!summarized;relationBadge.textContent=String(summarized||'');
        const history=explorationTrace(root.monitorSnapshot||{},current,model);
        const canReplay=Boolean(recordedGrowthPlan(history));
        el('atlasPlay').disabled = !canReplay;
        el('atlasPlay').title=uiT(canReplay?'按真实记录顺序回放':'尚无可回放的完整探索记录');
        el('atlasScrubber').disabled=!replayRecord;
        root.querySelectorAll('[data-atlas-chapter]').forEach(button=>button.disabled=true);
        root.querySelector('.atlas-chapter-buttons').hidden=true;
        root.querySelector('.atlas-growth-options').hidden=!canReplay;
    }
    function sendSelection(item, focus = true) {
        const node = item.record;
        root.dispatchEvent(new CustomEvent('monitor-select-node', {detail:{id:node.id, node, focus}}));
    }
    function dismissHover() {
        hover=null;tooltip.hidden=true;renderer.domElement.style.cursor='grab';
    }
    function pick(item) {
        dismissHover();
        overviewActive = false;
        manualView = true;
        if(replayRecord||tourTimer||replayPosition<1)completeReplay();else stopTour();
        explicitSelection = true; current = item; sendSelection(item,false); updateHighlight();
    }
    function receiveSelection(event) {
        if (!model) return;
        const item = model.actual.get(event.detail.node.id) || model.entries.get(event.detail.node.id);
        if (!item) return;
        dismissHover();
        current = item;
        if (event.detail.focus) {
            explicitSelection = true;
            if(replayRecord||tourTimer||replayPosition<1)completeReplay();else stopTour();
        }
        let revealed = false;
        if(!nodes.includes(item))revealed=true;
        for (const part of ancestors(item).slice(0, -1)) if (collapsed.delete(part.id)) revealed = true;
        if(el('atlasFollow').checked&&selectedNodeRoute(item).nodes.some(node=>!nodes.includes(node)))revealed=true;
        if (revealed) paint(); else updateHighlight();
        if (event.detail.focus) {
            manualView = true; updateFollowInfo();
            frameSelection(item);
        }
    }
    function update(snapshot) {
        if(replayRecord&&snapshot.structure_token!==replayToken)completeReplay();
        active = snapshot.nodes.some(node => node.state === 'running');
        const next = atlasLayoutSignature(snapshot,layoutSpacing);
        const nextData = JSON.stringify([snapshot.nodes,snapshot.relations,snapshot.exploration_events]);
        const initial = !model;
        const epoch=String(snapshot.structure_version?.epoch||snapshot._transport?.version?.epoch||'legacy');
        if (next !== signature||epoch!==modelEpoch) {
            signature = next; const oldId = current?.record?.id || current?.id,hoverId=hover?.id;
            const retained=model&&epoch===modelEpoch?Object.fromEntries([...model.actual].map(([id,item])=>[id,item.position])):{};
            modelEpoch=epoch;
            model = buildAtlas(snapshot,{spacing:layoutSpacing,deferLayout:true,previousPositions:retained});
            relationView=new AtlasRelationView(model.links);
            current = model.actual.get(oldId) || model.entries.get(oldId) || model.actual.get(root.dataset.selectedId) || null;
            hover=model.actual.get(hoverId)||null;tooltip.hidden=true;
            paint();
        } else if(nextData!==dataSignature&&refreshAtlasRecords(model,snapshot)) {
            const route=selectedNodeRoute();
            const revealed=new Set(current&&el('atlasFollow').checked?route.nodes.map(item=>item.id):[]);
            const shown=relationView.project(visibleEntries(model,{query:el('monitorSearch').value.trim(),state:el('monitorFilter').value,collapsed,revealed}),relationContext()).nodes;
            const ids=new Set(nodes.map(item=>item.id));
            if(shown.length!==nodes.length||shown.some(item=>!ids.has(item.id)))paint();
            else refreshMetadataVisuals(route);
        }
        labelLevels=labelHierarchyLevels([...model.actual.values()],model.links);
        dataSignature=nextData;
        geometry.update(model,snapshot,{signature:next,spacing:layoutSpacing});
        if(hover)tooltip.textContent=hover.label+' · '+uiT(hover.record.kind_label||'官网入口')+'\n'+(hover.record.url||hover.record.reference_url||'');
        if (initial && nodes.length) {
            overview(false);
        }
        syncFollowing();
        updateStatus();
        requestFrame();
    }
    function refreshMetadataVisuals(route) {
        for(const item of labelNodes){
            const button=item.labelElement;
            if(button.textContent!==item.label){
                button.textContent=item.label;
                const hidden=button.hidden,visibility=button.style.visibility;
                button.hidden=false;button.style.visibility='hidden';
                const rect=button.getBoundingClientRect();item.labelWidth=rect.width;item.labelHeight=rect.height;
                button.hidden=hidden;button.style.visibility=visibility;
            }
            button.setAttribute('aria-label',item.label+' · '+uiT(item.record.state_label||'已记录的导航名称'));
        }
        ringNodes.forEach((item,index)=>rings.setColorAt(index,new THREE.Color(stateColors[item.record.state]||0)));
        if(rings?.instanceColor)rings.instanceColor.needsUpdate=true;
        highlight.children.forEach(object=>{
            const item=object.userData.status;
            if(!item)return;
            object.visible=Boolean(stateColors[item.record.state]);
            object.material.color.set(stateColors[item.record.state]||0).multiplyScalar(1.8);
            object.userData.running=item.record.state==='running';
        });
        const changed=route.nodes.map(item=>item.id).join('|')!==selectedRoute.nodes.map(item=>item.id).join('|')
            ||route.links.map(edge=>edge.id).join('|')!==selectedRoute.links.map(edge=>edge.id).join('|');
        if(changed)updateHighlight();
        else{
            selectedRoute=route;
            [...el('atlasBreadcrumb').children].forEach((button,index)=>{if(route.nodes[index])button.textContent=route.nodes[index].label;});
            renderRouteEvidence();updateEmphasis();updateNarration();updateFollowInfo();
        }
        updateStatus();requestFrame();
    }
    function updateFollowInfo() {
        if(!contextAvailable)return;
        const target=trackingTarget(root.monitorSnapshot);
        el('atlasLocate').disabled=!target;
        let message='';
        if(el('atlasFollow').checked)message=!current?uiT('选择节点，查看从官网入口到此处的完整路径'):
            root.dataset.pathMode==='exploration'?(selectedRoute.complete?uiT('真实探索记录：{count} 个事件',{count:selectedRoute.events.length}):uiT('尚无完整的真实探索记录。')):
            selectedRoute.complete?uiT('所选路径：{count} 个入口',{count:selectedRoute.nodes.length}):uiT('上游依据不完整，仅显示有证据的部分。');
        el('atlasFollowStatus').textContent=message;
        el('atlasFollowStatus').title=message;
    }
    function syncFollowing(force=false) {
        updateFollowInfo();
        if(!force||!contextAvailable||!model||tourTimer||replayPosition<1)return;
        const target=trackingTarget(root.monitorSnapshot),item=target&&model.actual.get(target.node.id);
        if(!item)return;
        if(el('monitorSearch').value||el('monitorFilter').value!=='all'){
            el('monitorSearch').value='';el('monitorFilter').value='all';
            el('monitorSearch').dispatchEvent(new Event('input',{bubbles:true}));
        }
        if(!force&&target.key===lastTrackedKey)return;
        lastTrackedKey=target.key;manualView=false;
        let revealed=false;
        for(const part of ancestors(item).slice(0,-1))if(collapsed.delete(part.id))revealed=true;
        current=item;explicitSelection=true;
        if(revealed)paint();
        sendSelection(item,true);updateHighlight();
        updateFollowInfo();
    }
    function completeReplay() {
        stopTour();replayRecord=null;replayToken=null;replayScope=null;replayPlan=growthPlan(nodes);positionReplay(1);
    }
    function fly(target, distance, animate = true, front = false) {
        overviewActive = false;
        hover=null;tooltip.hidden=true;updateEmphasis();
        const direction = front ? new THREE.Vector3(.03,.035,1).normalize() : camera.position.clone().sub(controls.target).normalize();
        if (!Number.isFinite(direction.x) || direction.lengthSq() < .1) direction.set(.06, .07, 1).normalize();
        const destination = target.clone().addScaledVector(direction, distance);
        if (!animate || reduced.matches) {
            controls.target.copy(target); camera.position.copy(destination); controls.update(); tween = null;
        } else tween = {at:performance.now(), from:camera.position.clone(), to:destination,
            fromTarget:controls.target.clone(), toTarget:target.clone()};
        requestFrame();
    }
    function overview(animate=true) {
        const vertical=THREE.MathUtils.degToRad(camera.fov/2), horizontal=Math.atan(Math.tan(vertical)*Math.max(.2,camera.aspect));
        const direction=new THREE.Vector3(.03,.035,1).normalize(),right=new THREE.Vector3().crossVectors(camera.up,direction).normalize(),up=new THREE.Vector3().crossVectors(direction,right);
        const tanY=Math.tan(vertical)*Math.max(.4,(viewport.clientHeight-180)/viewport.clientHeight);
        const tanX=Math.tan(horizontal)*Math.max(.5,(viewport.clientWidth-64)/viewport.clientWidth);
        let distance=200;
        for(const item of nodes){const offset=new THREE.Vector3().copy(item.position).sub(bounds.center),depth=offset.dot(direction);
            distance=Math.max(distance,(Math.abs(offset.dot(right))+item.radius)/tanX+depth,(Math.abs(offset.dot(up))+item.radius)/tanY+depth);}
        distance*=1.08;
        overviewDistance=distance;
        fly(bounds.center,distance,animate,true);
        overviewActive = true;
    }
    function frameSelection(item) {
        if(geometry.state.status==='pending')geometryFocus=item.id;
        const visible=new Set(nodes), neighbors=(el('atlasFollow').checked?selectedNodeRoute(item).nodes:[item]).filter(n=>visible.has(n));
        if(!neighbors.length&&visible.has(item))neighbors.push(item);
        const box=new THREE.Box3();for(const node of neighbors)box.expandByPoint(new THREE.Vector3().copy(node.position));
        const center=box.getCenter(new THREE.Vector3()),direction=camera.position.clone().sub(controls.target).normalize();
        const right=new THREE.Vector3().crossVectors(camera.up,direction).normalize(),up=new THREE.Vector3().crossVectors(direction,right);
        const tanY=Math.tan(THREE.MathUtils.degToRad(camera.fov/2))*Math.max(.35,(viewport.clientHeight-180)/viewport.clientHeight);
        const tanX=Math.tan(THREE.MathUtils.degToRad(camera.fov/2))*camera.aspect*Math.max(.35,(viewport.clientWidth-128)/viewport.clientWidth);
        let distance=200;
        for(const node of neighbors){const offset=new THREE.Vector3().copy(node.position).sub(center),depth=offset.dot(direction);
            distance=Math.max(distance,(Math.abs(offset.dot(right))+node.radius+8)/tanX+depth,(Math.abs(offset.dot(up))+node.radius+8)/tanY+depth);}
        fly(center,distance*1.08);
    }

    function updateNarration() {
        if(replayRecord){
            const index=Math.min(replayRecord.events.length-1,Math.floor(replayPosition*replayRecord.events.length));
            const event=replayRecord.events[index];
            const text=uiT('按真实记录顺序回放')+' · '+(event.label||'');
            el('atlasNarration').textContent=text;el('atlasScrubber').setAttribute('aria-valuetext',text);
            return;
        }
        const path = selectedRoute.nodes.map(item => item.label).join(' / ');
        const lines = [uiT('从已记录的官网入口出发。'), uiT('分叉表示观察到的导航路径或已保存的目录归属。'),
            current ? uiT('当前线索：{name}', {name:current.label}) : uiT('选择一个节点，沿已记录的路径深入。'),
            current?.record?.sources.length ? uiT('此入口已有接入栏目，可查看保存的来源与状态。') : uiT('查看右侧读取记录与官网入口，未验证的内容保留待确认状态。')];
        if (el('atlasNarration').textContent!==lines[stageIndex]) el('atlasNarration').textContent=lines[stageIndex];
        if (el('atlasScrubber').getAttribute('aria-valuetext')!==lines[stageIndex]) el('atlasScrubber').setAttribute('aria-valuetext',lines[stageIndex]);
        el('atlasNarration').title = path;
        el('atlasNarration').dataset.stage = stageIndex;
    }
    function positionReplay(value) {
        replayPosition=clamp(Number(value)); stageIndex=Math.min(3,Math.floor(replayPosition*3+.001));
        el('atlasScrubber').value=replayPosition; root.dataset.atlasChapter=stageIndex;
        root.querySelectorAll('[data-atlas-chapter]').forEach(button=>button.setAttribute('aria-pressed',String(Number(button.dataset.atlasChapter)===stageIndex)));
        updateNarration(); updateFollowInfo(); requestFrame();
    }
    function setChapter(index,animate=true) {
        positionReplay(Number(index)/3);
        if (!current || !explicitSelection) return;
        const path=selectedNodeRoute().nodes, target=Number(index)===1?(path[Math.min(1,path.length-1)]||current):current;
        if (Number(index)===0) overview(animate);
        else fly(new THREE.Vector3().copy(target.position),Number(index)===1?370:Number(index)===2?235:155,animate);
        el('monitorDetail').classList.toggle('atlas-evidence-focus',Number(index)===3);
    }
    function stopTour() {
        tourTimer=null; el('atlasPlay').setAttribute('aria-pressed','false');
        el('atlasPlay').setAttribute('aria-label',uiT('播放生长动画')); updateFollowInfo(); requestFrame();
    }
    function tour() {
        if (tourTimer) {stopTour();return;}
        const history=explorationTrace(root.monitorSnapshot||{},current,model),plan=recordedGrowthPlan(history);
        if(!plan)return;
        // Replay only the selected recorded lineage. Unrelated nodes and all
        // current source states stay intact; no chapter or layout order is used.
        el('monitorPathMode').value='exploration';
        el('monitorPathMode').dispatchEvent(new Event('change',{bubbles:true}));
        replayRecord=history;replayToken=root.monitorSnapshot?.structure_token;
        replayScope=new Set(history.nodes.map(item=>item.id));replayPlan=plan;
        positionReplay(0);tourTimer={at:performance.now(),from:0,duration:Math.max(2400,Math.min(12000,history.events.length*650))};
        el('atlasPlay').setAttribute('aria-pressed','true');el('atlasPlay').setAttribute('aria-label',uiT('暂停生长动画'));
        updateStatus();requestFrame();
    }
    function clearSelection() {
        dismissHover();
        explicitSelection=false; current=null; selectedPath=[];
        delete root.dataset.selectedId; root.dispatchEvent(new CustomEvent('monitor-clear-selection')); updateHighlight();
    }

    function positionTooltip(anchor,occupied,sphereObstacles,width,height) {
        tooltip.hidden=true;
        if(!hover||hover===current||!anchor)return;
        tooltip.textContent=hover.label+' · '+uiT(hover.record.kind_label||'官网入口')+'\n'+(hover.record.url||hover.record.reference_url||'');
        // Measure actual wrapping. Keep both inspected spheres clear while
        // allowing the transient tooltip over unrelated background nodes.
        tooltip.style.left='0px';tooltip.style.top='0px';tooltip.style.visibility='hidden';tooltip.hidden=false;
        const rect=tooltip.getBoundingClientRect();
        const box=placeLabelBox({...anchor,width:rect.width,height:rect.height,
            viewportWidth:width,viewportHeight:height,occupied,
            sphereObstacles:sphereObstacles.filter(sphere=>sphere.id===hover.id||sphere.id===current?.id),focused:true});
        tooltip.hidden=!box;
        if(box){tooltip.style.left=box[0]+'px';tooltip.style.top=box[1]+'px';}
        tooltip.style.visibility='';
    }
    function labelsAndRings() {
        const width = viewport.clientWidth, height = viewport.clientHeight, occupied = [[0,0,width,8],[0,height-8,width,height]];
        const query=el('monitorSearch').value.trim();
        const searchLevel=searchLabelLevel(labelNodes,labelLevels,query);
        const reserved = [el('atlasStatus'),el('atlasHelp'),stage.querySelector('.atlas-live-controls'),
            stage.querySelector('.atlas-scene-tools'),stage.querySelector('.atlas-camera-controls'),
            el('atlasOptions').open && el('atlasOptions').querySelector('.atlas-options-panel'),
            root.classList.contains('atlas-expanded') && stage.querySelector('.atlas-immersive-tools'),
            root.classList.contains('atlas-search-open') && el('atlasSearchPanel'),
            root.classList.contains('atlas-details-open') && el('atlasDetailPanel')].filter(Boolean);
        const rect=viewport.getBoundingClientRect();
        for(const panel of reserved){const box=panel.getBoundingClientRect();occupied.push([box.left-rect.left,box.top-rect.top,box.right-rect.left,box.bottom-rect.top]);}
        const candidates=[],sphereObstacles=[],chosen=new Set(selectedPath.map(item=>item.id));
        let hoverAnchor=null;
        const zoom=overviewDistance/Math.max(1,camera.position.distanceTo(controls.target));
        const policy=labelPolicy({width,height,zoom,selected:current});
        for (const item of labelNodes) {
            item.labelElement.hidden=true;
            projected.copy(item.renderPosition || item.position).project(camera);
            const x = (projected.x + 1) * width / 2, y = (1 - projected.y) * height / 2;
            const distance = camera.position.distanceTo(new THREE.Vector3().copy(item.renderPosition || item.position));
            const offset = item.drawRadius * height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * distance) + 8;
            if(item.growth<.15||projected.z < -1||projected.z>1||x<0||x>width||y<0||y>height)continue;
            sphereObstacles.push({id:item.id,x,y,radius:Math.max(2,offset-8)+2});
            if(item===hover)hoverAnchor={x,y,offset};
            const priority=labelImportance(item,{selected:current,hovered:hover,path:chosen,context:contextIds,detailed:policy.detailed,pixels:offset-8,nodeLabels:el('atlasNodeLabels').checked,
                hierarchyLevel:labelLevels.get(item.id),upstream:upstreamIds,query,searchLevel});
            if(priority>=0)candidates.push({item,x,y,offset,priority});
        }
        candidates.sort((a,b)=>b.priority-a.priority||a.item.id.localeCompare(b.item.id));
        const limit=policy.limit;
        let count=0;
        for(const {item,x,y,offset} of candidates){
            if(count>=limit)break;
            const box=placeLabelBox({x,y,offset,width:item.labelWidth,height:item.labelHeight,
                viewportWidth:width,viewportHeight:height,occupied,sphereObstacles,
                focused:item===current||item===hover||labelLevels.get(item.id)<=2||labelSearchMatch(item,query),
                searchResult:labelSearchMatch(item,query)});
            if(!box)continue;
            const button=item.labelElement;button.hidden=false;button.style.transform=`translate(${box[0]}px,${box[1]}px)`;
            button.dataset.selected=String(item===current);button.dataset.hierarchyLevel=String(labelLevels.get(item.id));
            occupied.push([box[0]-6,box[1]-5,box[2]+6,box[3]+5]);count++;
        }
        positionTooltip(hoverAnchor,occupied,sphereObstacles,width,height);
        root.dataset.atlasLabelCount=String(count);
        root.dataset.atlasDisplayMode=current?'local':policy.detailed?'detail':'overview';
        ringNodes.forEach((item, index) => {
            matrix.position.copy(item.renderPosition || item.position); matrix.scale.setScalar(item.drawRadius && stateColors[item.record.state] ? item.drawRadius*1.18 : 0); matrix.quaternion.copy(camera.quaternion);
            matrix.updateMatrix(); rings.setMatrixAt(index, matrix.matrix);
        });
        if (rings) rings.instanceMatrix.needsUpdate = true;
        const pixelFactor=2*Math.tan(THREE.MathUtils.degToRad(camera.fov/2))/Math.max(1,height);
        highlight.children.forEach(object => {
            if(object.userData.faceCamera)object.quaternion.copy(camera.quaternion);
            if(object.userData.edge){
                const pixel=camera.position.distanceTo(object.position)*pixelFactor;
                const radius=pixel*(object.userData.route==='manual'?1.3:.45);
                object.scale.set(radius,object.userData.length,radius);
            }
            if(object.userData.selection){
                const item=object.userData.selection;object.position.copy(item.renderPosition||item.position);
                const pixel=camera.position.distanceTo(object.position)*pixelFactor;
                object.scale.setScalar(Math.max((item.drawRadius||item.radius)+pixel*4,pixel*9));
            }
        });
    }
    function draw(time) {
        frame = null;
        if (disposed || document.hidden || !inView || stage.dataset.renderer === 'unavailable') return;
        if (time - lastFrame < 14) { frame = requestAnimationFrame(draw); return; }
        lastFrame = time;
        let moving = controls.update();
        if (tween) {
            const progress = Math.min(1, (time - tween.at) / 1050), eased = 1 - 2 ** (-10 * progress);
            camera.position.lerpVectors(tween.from, tween.to, progress === 1 ? 1 : eased);
            controls.target.lerpVectors(tween.fromTarget, tween.toTarget, progress === 1 ? 1 : eased);
            camera.lookAt(controls.target); moving = true;
            if (progress === 1) tween = null;
        }
        if (tourTimer) {
            positionReplay(tourTimer.from+(time-tourTimer.at)/tourTimer.duration);
            if (replayPosition>=1) stopTour();
            moving=true;
        }
        const pulsing = active && !reduced.matches && selectedPath.some(item => item.record?.state === 'running');
        if (pulsing) highlight.children.forEach(object => {
            if (object.userData.running) object.material.color.copy(primary).multiplyScalar(2.2 + Math.sin(time / 800) * .35);
        });
        if (dirty || moving || pulsing || layoutMoving) { animateGeometry(time); labelsAndRings(); composer.render(); dirty = false; }
        if (moving || tween || pulsing || layoutMoving || tourTimer) requestFrame();
    }
    function updateCameraRange() {
        const halfFov=THREE.MathUtils.degToRad(camera.fov/2);
        const angle=Math.min(halfFov,Math.atan(Math.tan(halfFov)*Math.max(.2,camera.aspect)));
        controls.maxDistance=Math.max(1600,bounds.radius/Math.sin(angle)*4.5);
        camera.far=controls.maxDistance+bounds.radius*3;
        camera.updateProjectionMatrix();
    }
    function resize() {
        const width = viewport.clientWidth, height = viewport.clientHeight;
        if (!width || !height) return;
        camera.aspect = width / height; updateCameraRange();
        renderer.setSize(width, height); composer.setSize(width, height);
        // Fullscreen and narrow windows change the frustum. Refit only an
        // overview; a user-selected or manually adjusted camera stays intact.
        if (overviewActive && nodes.length) overview(false);
        finishTexture(); requestFrame();
    }
    function hit(event) {
        if (!spheres || !nodes.length) return null;
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
        camera.updateMatrixWorld(true);
        raycaster.setFromCamera(pointer, camera);
        return pickSphere(nodes,{origin:raycaster.ray.origin,direction:raycaster.ray.direction,
            near:camera.near,far:camera.far,pixelFactor:2*Math.tan(THREE.MathUtils.degToRad(camera.fov/2))/Math.max(1,rect.height),
            minimumPixels:event.pointerType==='touch'?8:4});
    }
    renderer.domElement.addEventListener('pointerdown', event => {
        press = event.button === 0 && event.isPrimary ? {x:event.clientX,y:event.clientY,id:event.pointerId,dragged:false} : null;
        if(event.button===1)event.preventDefault();
        tween = null; stopTour();
    });
    renderer.domElement.addEventListener('auxclick', event=>{if(event.button===1)event.preventDefault();});
    stage.addEventListener('pointerdown',event=>{
        if(event.button!==1||stage.dataset.renderer!=='ready'||event.target.closest?.('.atlas-live-controls, .atlas-scene-tools, .atlas-camera-controls, .atlas-immersive-tools'))return;
        event.preventDefault();
        if(event.target===renderer.domElement)return;
        event.stopPropagation();
        renderer.domElement.dispatchEvent(new PointerEvent('pointerdown',{pointerId:event.pointerId,pointerType:event.pointerType,isPrimary:event.isPrimary,
            button:event.button,buttons:event.buttons,clientX:event.clientX,clientY:event.clientY,cancelable:true}));
    },{capture:true});
    renderer.domElement.addEventListener('pointerup', event => {
        if (press && press.id===event.pointerId && !press.dragged && Math.hypot(event.clientX - press.x, event.clientY - press.y) < 5) { const item = hit(event); if (item) pick(item); else clearSelection(); }
        press = null;
    });
    renderer.domElement.addEventListener('pointercancel', () => { press = null; });
    renderer.domElement.addEventListener('pointermove', event => {
        if (press) {if(Math.hypot(event.clientX-press.x,event.clientY-press.y)>=5)press.dragged=true;return;}
        if (performance.now() - lastPick < 65) return;
        lastPick = performance.now(); const previousHover=hover;hover = hit(event);
        if(hover!==previousHover)updateEmphasis();
        renderer.domElement.style.cursor = hover ? 'pointer' : 'grab';
        requestFrame();
    });
    renderer.domElement.addEventListener('pointerleave', () => { tooltip.hidden = true;hover=null;updateEmphasis();requestFrame(); });
    // Labels and other HTML overlays sit above the canvas. Catch their wheel events
    // too, so a zoom gesture never chains to the surrounding document.
    stage.addEventListener('wheel',event=>{
        if(stage.dataset.renderer!=='ready')return;
        if(event.target.closest?.('.atlas-options-panel')){
            const panel=event.target.closest('.atlas-options-panel');
            event.stopPropagation();
            if(panel.scrollHeight<=panel.clientHeight||(event.deltaY<0&&panel.scrollTop<=0)||(event.deltaY>0&&panel.scrollTop+panel.clientHeight>=panel.scrollHeight-1))event.preventDefault();
            return;
        }
        event.preventDefault();
        if(event.target===renderer.domElement)return;
        if(event.target.closest?.('.atlas-live-controls, .atlas-scene-tools, .atlas-camera-controls, .atlas-immersive-tools'))return;
        renderer.domElement.dispatchEvent(new WheelEvent('wheel',{deltaX:event.deltaX,deltaY:event.deltaY,deltaZ:event.deltaZ,deltaMode:event.deltaMode,
            clientX:event.clientX,clientY:event.clientY,ctrlKey:event.ctrlKey,metaKey:event.metaKey,shiftKey:event.shiftKey,cancelable:true}));
    },{capture:true,passive:false});
    renderer.domElement.addEventListener('webglcontextlost', event => {
        event.preventDefault(); contextAvailable = false; controls.enabled = false; stopTour();
        fallback(uiT('三维视图暂不可用，已展开路径列表。'));
    });
    renderer.domElement.addEventListener('webglcontextrestored', () => {
        contextAvailable = true; controls.enabled = true;
        stage.dataset.renderer = 'ready';
        for (const id of ['atlasZoomIn','atlasZoomOut','atlasOverview','atlasSpacing','atlasNodeLabels']) el(id).disabled = false;
        updateStatus(); updateFollowInfo(); resize();
    });
    controls.addEventListener('change', requestFrame);
    controls.addEventListener('start', () => { overviewActive=false;geometryFocus=null;manualView = true; tween = null; hover=null;tooltip.hidden=true;updateEmphasis();stopTour(); });
    viewport.addEventListener('keydown', event => {
        if (event.target !== viewport) return;
        if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) {
            overviewActive = false;
            event.preventDefault(); manualView = true; tween = null; stopTour();
            const offset = camera.position.clone().sub(controls.target), spherical = new THREE.Spherical().setFromVector3(offset);
            if (event.key === 'ArrowLeft') spherical.theta -= .12;
            if (event.key === 'ArrowRight') spherical.theta += .12;
            if (event.key === 'ArrowUp') spherical.phi -= .12;
            if (event.key === 'ArrowDown') spherical.phi += .12;
            spherical.makeSafe(); camera.position.copy(controls.target).add(offset.setFromSpherical(spherical));
            controls.update(); requestFrame();
        }
        if (event.key === 'Home') { event.preventDefault(); overview(); }
        if (event.key === '+' || event.key === '=') zoom(.8);
        if (event.key === '-') zoom(1.25);
    });
    function zoom(factor) {
        manualView=true; updateFollowInfo();
        const distance=tween?tween.to.distanceTo(tween.toTarget):camera.position.distanceTo(controls.target);
        fly(controls.target, THREE.MathUtils.clamp(distance * factor, controls.minDistance, controls.maxDistance));
    }
    el('atlasOverview').addEventListener('click', () => { manualView=true;completeReplay();clearSelection(); overview(); });
    el('atlasZoomIn').addEventListener('click', () => zoom(.8));
    el('atlasZoomOut').addEventListener('click', () => zoom(1.25));
    el('atlasPlay').addEventListener('click', tour);
    el('atlasScrubber').addEventListener('input', event => { stopTour(); positionReplay(event.target.value); });
    root.querySelectorAll('[data-atlas-chapter]').forEach(button => button.addEventListener('click', () => { stopTour(); setChapter(button.dataset.atlasChapter); }));
    el('atlasBranch').addEventListener('click', () => {
        if (!current) return;
        if (collapsed.has(current.id)) collapsed.delete(current.id); else collapsed.add(current.id);
        paint();updateFollowInfo();
    });
    el('atlasFollow').addEventListener('change', () => {
        savePreferences();lastTrackedKey='';
        completeReplay();if(model)paint();
    });
    el('atlasNodeLabels').addEventListener('change',()=>{savePreferences();requestFrame();});
    el('atlasLocate').addEventListener('click',()=>{completeReplay();syncFollowing(true);});
    el('atlasReplayReset').addEventListener('click',()=>{completeReplay();if(current)frameSelection(current);else overview();});
    el('atlasSpacingValue').textContent=Math.round(layoutSpacing*100)+'%';
    el('atlasSpacing').addEventListener('input',()=>{el('atlasSpacingValue').textContent=Math.round(Number(el('atlasSpacing').value)*100)+'%';});
    el('atlasSpacing').addEventListener('change',()=>{
        layoutSpacing=Number(el('atlasSpacing').value);manualView=true;savePreferences();
        if(root.monitorSnapshot){update(root.monitorSnapshot);overview();}
    });
    document.addEventListener('pointerdown',event=>{if(el('atlasOptions').open&&!el('atlasOptions').contains(event.target))el('atlasOptions').open=false;});
    el('atlasOptions').addEventListener('toggle',()=>{
        if(el('atlasOptions').open)immersivePanel('search',false);
        requestFrame();
    });
    root.addEventListener('monitor-data', event => update(event.detail));
    root.addEventListener('monitor-node-selected', receiveSelection);
    root.addEventListener('monitor-relation-group',event=>{
        if(!relationView?.groups.has(event.detail.id))return;
        if(expandedRelationGroups.has(event.detail.id)===event.detail.expanded)return;
        if(event.detail.expanded)expandedRelationGroups.add(event.detail.id);else expandedRelationGroups.delete(event.detail.id);
        const refit=overviewActive;paint();if(refit)overview(false);
    });
    root.addEventListener('monitor-path-mode', event => {
        if(['access','membership','exploration'].includes(event.detail?.mode))root.dataset.pathMode=event.detail.mode;
        if(model){if(tourTimer||replayPosition<1)completeReplay();updateHighlight();updateStatus();}
    });
    root.addEventListener('monitor-filter', () => { if (model) { paint(); overview(); updateFollowInfo(); } });
    const resizeObserver = new ResizeObserver(resize); resizeObserver.observe(viewport);
    const intersection = new IntersectionObserver(entries => {
        // Layout/fullscreen changes can queue both exit and re-entry for this
        // viewport in one callback. The latest observation controls drawing.
        const latest = entries.filter(entry => entry.target === viewport).at(-1);
        if (!latest) return;
        inView = latest.isIntersecting;
        if (inView) requestFrame(); else { stopTour(); if (frame) cancelAnimationFrame(frame); frame = null; }
    }); intersection.observe(viewport);
    const themeObserver = new MutationObserver(applyTheme); themeObserver.observe(document.documentElement, {attributes:true, attributeFilter:['data-theme']});
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { stopTour(); if (frame) cancelAnimationFrame(frame); frame = null; } else requestFrame();
    });
    document.addEventListener('locale-changed', () => { if (model) { updateHighlight(); updateStatus(); updateFollowInfo(); } });
    window.addEventListener('pagehide', event => {
        stopTour(); if (frame) cancelAnimationFrame(frame); frame = null;
        if (event.persisted) return;
        disposed = true;geometry.dispose(); resizeObserver.disconnect(); intersection.disconnect(); themeObserver.disconnect();
        controls.dispose(); disposeGroup(graph); disposeGroup(highlight); sphereGeometry.dispose(); sphereMaterial.dispose();
        ringGeometry.dispose(); bloom.dispose(); composer.dispose(); renderer.dispose();
        routeGeometry.dispose();selectionRingGeometry.dispose();
    });
    window.addEventListener('pageshow', () => { if (!disposed) requestFrame(); });
    // Small diagnostics also let browser checks verify genuine spatial interaction.
    atlas = {camera, controls, renderer, get model() { return model; }, get current() { return current; }, scene, get progress() {return replayPosition;}, get playing() {return Boolean(tourTimer);}, get nodes() {return nodes;},
        get layout(){return geometry.state;},
        select:id => { const item = model?.actual.get(id) || model?.entries.get(id); if (item) pick(item); }};
    root.discoveryAtlas = atlas;
    applyTheme(); resize(); stage.dataset.renderer = 'ready';
    updateNarration();
    if (root.monitorSnapshot) update(root.monitorSnapshot);
    else el('atlasStatus').textContent = uiT('正在载入保存的路径…');
}

const workspace = root.querySelector('.monitor-workspace');
let immersiveFocus = null, immersiveScroll = 0, fullscreenPending = false, isolatedElements = [];
function immersiveState(mode) {
    const expanded = Boolean(mode);
    root.classList.toggle('atlas-expanded', expanded);
    document.body.classList.toggle('atlas-is-fullscreen', expanded);
    root.dataset.atlasFullscreen = mode || 'off';
    if (expanded && !isolatedElements.length) {
        for(let branch=workspace;branch.parentElement;branch=branch.parentElement) {
            for(const sibling of branch.parentElement.children) if(sibling!==branch) {
                isolatedElements.push([sibling,sibling.inert]); sibling.inert=true;
            }
            if(branch.parentElement===document.body)break;
        }
    }
    fullscreenCopy(mode);
    if (!expanded) {
        for(const [element,inert] of isolatedElements)element.inert=inert;
        isolatedElements=[];
        immersivePanel('search', false); immersivePanel('details', false);
        el('atlasOptions').open = false;
        scrollTo(0, immersiveScroll);
        immersiveFocus?.focus({preventScroll:true}); immersiveFocus = null;
    }
}
function fullscreenCopy(mode) {
    const button = el('atlasFullscreen'), expanded = mode && mode !== 'off';
    delete button.dataset.i18n;
    button.textContent = uiT(expanded ? mode === 'native' ? '退出全屏' : '退出沉浸' : '全屏探索');
    button.setAttribute('aria-pressed', String(Boolean(expanded)));
    if (mode === 'window') button.title = uiT('当前环境使用铺满窗口模式');
    else button.removeAttribute('title');
}
document.addEventListener('locale-changed', () => fullscreenCopy(root.dataset.atlasFullscreen));
function immersivePanel(name, open) {
    root.classList.toggle('atlas-' + name + '-open', open);
    el(name === 'search' ? 'atlasSearchToggle' : 'atlasInspect').setAttribute('aria-expanded', String(open));
    atlas?.controls.dispatchEvent({type:'change'});
}
async function leaveImmersive() {
    if (document.fullscreenElement === workspace) {
        try { await document.exitFullscreen(); } catch (_) { /* Keep the exit control available if the browser refuses. */ }
        if (document.fullscreenElement === workspace) return;
    }
    if (root.classList.contains('atlas-expanded')) immersiveState(null);
}
el('atlasFullscreen').addEventListener('click', async () => {
    if (fullscreenPending) return;
    if (root.classList.contains('atlas-expanded')) { await leaveImmersive(); return; }
    immersiveFocus = document.activeElement; immersiveScroll = scrollY;
    el('atlasOptions').open = false;
    fullscreenPending = true;
    try {
        if (!document.fullscreenEnabled || !workspace.requestFullscreen) throw new Error('Fullscreen unavailable');
        await workspace.requestFullscreen({navigationUI:'hide'});
        immersiveState('native');
    } catch (_) {
        // Embedded hosts may not expose browser fullscreen. Keep a truthful,
        // usable edge-to-edge canvas in their existing window instead.
        immersiveState('window');
    } finally { fullscreenPending = false; }
    viewport.focus({preventScroll:true});
});
document.addEventListener('fullscreenchange', () => {
    if (document.fullscreenElement === workspace) immersiveState('native');
    else if (root.dataset.atlasFullscreen === 'native') immersiveState(null);
});
el('atlasSearchToggle').addEventListener('click', () => {
    const open = !root.classList.contains('atlas-search-open');
    if(open)el('atlasOptions').open=false;
    immersivePanel('details', false); immersivePanel('search', open);
    if (open) el('monitorSearch').focus();
});
el('atlasInspect').addEventListener('click', () => {
    immersivePanel('search', false);
    immersivePanel('details', !root.classList.contains('atlas-details-open'));
});
el('atlasDetailClose').addEventListener('click', () => { immersivePanel('details', false); el('atlasInspect').focus(); });
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && el('atlasOptions').open) {
        el('atlasOptions').open = false; el('atlasOptions').querySelector('summary').focus(); return;
    }
    if (event.key === 'Escape' && root.classList.contains('atlas-expanded')) leaveImmersive();
});
initialize().catch(() => fallback(uiT('当前设备无法显示三维网络，已展开可操作的路径列表。')));
