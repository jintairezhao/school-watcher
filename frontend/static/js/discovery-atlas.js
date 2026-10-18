/* Local WebGL scene. Viewing or touring never dispatches discovery work. */
import {buildAtlas, ancestors, visibleEntries, trackingTarget} from './discovery-atlas-model.js';
import {identity, birthAnchor, growthPlan, growthAt, mix, smooth, clamp} from './discovery-atlas-motion.js';

const root = document.getElementById('discoveryMonitor');
const el = id => document.getElementById(id);
const stage = el('atlasStage'), viewport = el('atlasViewport');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let atlas;
const preferenceKey = 'school-watcher:atlas:preferences';
try {
    const saved = JSON.parse(localStorage.getItem(preferenceKey) || '{}');
    el('atlasFollow').checked = saved.follow === true;
    if (Number.isFinite(saved.spacing)) el('atlasSpacing').value = Math.max(.7, Math.min(1.8, saved.spacing));
} catch (_) { /* Presentation remains usable when local storage is unavailable. */ }
function savePreferences() {
    try { localStorage.setItem(preferenceKey, JSON.stringify({follow:el('atlasFollow').checked, spacing:Number(el('atlasSpacing').value)})); }
    catch (_) { /* A blocked preference store must not block the graph. */ }
}

function fallback(message) {
    if (root.classList.contains('atlas-expanded')) leaveImmersive();
    stage.dataset.renderer = 'unavailable';
    el('atlasStatus').textContent = message;
    el('atlasListPanel').open = true;
    el('atlasPlay').disabled = true;
    for (const id of ['atlasZoomIn', 'atlasZoomOut', 'atlasOverview', 'atlasLocate', 'atlasSpacing']) el(id).disabled = true;
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
    const sphereMaterial = new THREE.MeshStandardMaterial({roughness:.47, metalness:.16});
    const matrix = new THREE.Object3D(), raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2(), projected = new THREE.Vector3();
    const palette = {light:['#4386c3','#388e98','#8072b7','#698997','#aa778c'],
        dark:['#75b7fa','#72b0b9','#a49bce','#98aeba','#be9aaa']};
    let model = null, nodes = [], spheres, rings, ringNodes = [], labelNodes = [], current = null;
    let signature = '', theme, primary, manualColor, stateColors = {}, selectedPath = [], stageIndex = 0, tourTimer = null;
    let contextAvailable = true, explicitSelection = false, replayPosition = 1, replayPlan = null, replayScope = null;
    let frames = new Map(), edges = [], lineGeometry = null, linePositions = null, lineColors = null, layoutMoving = false;
    let layoutSpacing = Number(el('atlasSpacing').value), lastTrackedKey = '', manualView = false;
    let fadingLinks = null, fadingAt = 0;
    let frame = null, lastFrame = 0, tween = null, dirty = true, active = false, inView = true, disposed = false;
    let bounds = new THREE.Sphere(new THREE.Vector3(), 260), extent = new THREE.Vector3(450, 300, 140), hover = null, press = null, lastPick = 0;
    const collapsed = new Set();
    const tooltip = document.createElement('div'); tooltip.className = 'atlas-tooltip'; tooltip.hidden = true;
    stage.append(tooltip);

    function disposeGroup(group) {
        group.traverse(object => {
            if (object.geometry && object.geometry !== sphereGeometry && object.geometry !== ringGeometry) object.geometry.dispose();
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
    function curveFor(from,to) {
        const a=new THREE.Vector3().copy(from.renderPosition || from.position), b=new THREE.Vector3().copy(to.renderPosition || to.position);
        return new THREE.CubicBezierCurve3(a,a.clone().lerp(b,.35),a.clone().lerp(b,.7),b);
    }

    function renderLabels() {
        const chosen = new Set(selectedPath.map(item => item.id));
        labelNodes = nodes.filter(item => chosen.has(item.id) || item.record?.kind === 'root' ||
            item.id === item.cluster || (current && item.parent === current)).sort((a,b) =>
            Number(b === current) - Number(a === current) || Number(chosen.has(b.id)) - Number(chosen.has(a.id)) ||
            Number(Boolean(a.orphan)) - Number(Boolean(b.orphan)) || a.depth - b.depth).slice(0, 48);
        const fragment = document.createDocumentFragment();
        for (const item of labelNodes) {
            const button = document.createElement('button'); button.type = 'button';
            button.className = 'atlas-node-label'; button.textContent = item.label;
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
        const previousEdges=edges.map(item=>({from:identity(item.parent),to:identity(item)}));
        disposeGroup(graph); disposeGroup(highlight);
        fadingLinks=null;
        nodes=visibleEntries(model,{query:el('monitorSearch').value.trim(),state:el('monitorFilter').value,collapsed}).sort((a,b)=>a.depth-b.depth);
        const shown=new Set(nodes.map(item=>item.id)), now=performance.now();
        frames=new Map(nodes.map(item=>{
            const old=previous.get(identity(item));
            const entering=previous.size>0 && !old && !reduced.matches;
            return [item.id,{from:old?.position || (entering?birthAnchor(item,previous):item.position),
                scale:old?.scale ?? (entering?0:1),at:now,entering,
                moving:entering || Boolean(old && (old.scale!==1 || Math.hypot(old.position.x-item.position.x,old.position.y-item.position.y,old.position.z-item.position.z)>.01))}];
        }));
        replayPlan=growthPlan(replayScope ? nodes.filter(item=>replayScope.has(item.id)) : nodes);
        spheres=new THREE.InstancedMesh(sphereGeometry,sphereMaterial,nodes.length);
        spheres.instanceMatrix.setUsage(THREE.DynamicDrawUsage); spheres.frustumCulled=false;
        nodes.forEach((item,index)=>spheres.setColorAt(index,new THREE.Color(palette[theme][item.branch])));
        graph.add(spheres);
        edges=nodes.filter(item=>item.parent && shown.has(item.parent.id));
        const retained=new Set(nodes.map(identity)), nextEdges=new Set(edges.map(item=>identity(item.parent)+'|'+identity(item)));
        const rewired=previousEdges.filter(edge=>retained.has(edge.from)&&retained.has(edge.to)&&!nextEdges.has(edge.from+'|'+edge.to));
        if (rewired.length && !reduced.matches) {
            const positions=rewired.flatMap(edge=>[previous.get(edge.from).position,previous.get(edge.to).position].flatMap(point=>[point.x,point.y,point.z]));
            const geometry=new THREE.BufferGeometry(); geometry.setAttribute('position',new THREE.Float32BufferAttribute(positions,3));
            fadingLinks=new THREE.LineSegments(geometry,new THREE.LineBasicMaterial({color:theme==='dark'?0x8995a8:0x647384,transparent:true,opacity:.5,depthWrite:false}));
            fadingLinks.frustumCulled=false; fadingAt=now; graph.add(fadingLinks);
        }
        linePositions=new Float32Array(edges.length*12*2*3);
        lineGeometry=new THREE.BufferGeometry();
        lineGeometry.setAttribute('position',new THREE.BufferAttribute(linePositions,3).setUsage(THREE.DynamicDrawUsage));
        lineColors=new Float32Array(linePositions.length);
        lineGeometry.setAttribute('color',new THREE.BufferAttribute(lineColors,3).setUsage(THREE.DynamicDrawUsage));
        const lines=new THREE.LineSegments(lineGeometry,new THREE.LineBasicMaterial({vertexColors:true,transparent:true,opacity:.48,depthWrite:false}));
        lines.frustumCulled=false; graph.add(lines);
        ringNodes=nodes.filter(item=>['running','connected','waiting','failed'].includes(item.record?.state));
        rings=new THREE.InstancedMesh(ringGeometry,new THREE.MeshBasicMaterial({transparent:true,opacity:.8}),ringNodes.length);
        rings.instanceMatrix.setUsage(THREE.DynamicDrawUsage); rings.frustumCulled=false;
        ringNodes.forEach((item,index)=>rings.setColorAt(index,new THREE.Color(stateColors[item.record.state])));
        graph.add(rings);
        if (nodes.length) {
            const box=new THREE.Box3(); nodes.forEach(item=>box.expandByPoint(new THREE.Vector3().copy(item.position)));
            box.expandByScalar(24); bounds=box.getBoundingSphere(new THREE.Sphere()); extent=box.getSize(new THREE.Vector3());
            updateCameraRange();
        }
        updateHighlight(); updateStatus(); requestFrame();
        root.dataset.atlasNodeCount=String(nodes.length);
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
            const origin=item.parent?.renderPosition || base;
            item.renderPosition=reduced.matches?base:mix(origin,base,reveal);
            const distance=camera.position.distanceTo(new THREE.Vector3().copy(item.renderPosition));
            item.drawRadius=Math.max(item.radius,1.35*distance*pixelFactor)*item.growth;
            matrix.position.copy(item.renderPosition); matrix.scale.setScalar(item.drawRadius); matrix.quaternion.identity(); matrix.updateMatrix();
            spheres.setMatrixAt(index,matrix.matrix);
        });
        spheres.instanceMatrix.needsUpdate=true;
        let offset=0;
        for (const item of edges) {
            const a=item.parent.renderPosition,b=item.renderPosition;
            const reveal=Math.min(item.growth,item.parent.growth);
            for(let segment=0;segment<12;segment++) for(const step of [segment/12,(segment+1)/12]) {
                const point=mix(a,b,step*reveal);
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
        selectedPath = current ? explicitSelection ? [current.parent,current,...current.children].filter(Boolean) : ancestors(current) : [];
        const visible = new Set(nodes.map(item => item.id));
        const tracked=el('atlasFollow').checked&&trackingTarget(root.monitorSnapshot),live=tracked&&model.actual.get(tracked.node.id);
        const manualEdges=explicitSelection&&current ? [current,...current.children].filter(item=>item.parent&&visible.has(item.id)&&visible.has(item.parent.id)) : [];
        const manualIds=new Set(manualEdges.map(item=>item.id));
        const liveEdges=live?ancestors(live).filter(item=>item.parent&&visible.has(item.id)&&visible.has(item.parent.id)&&!manualIds.has(item.id)):[];
        const drawRoute=(items,color,kind)=>{
            for(const item of items){
                const geometry=new THREE.TubeGeometry(curveFor(item.parent,item),36,.58,6,false);
                const line=new THREE.Mesh(geometry,new THREE.MeshBasicMaterial({color:color.clone().multiplyScalar(theme==='dark'?3.4:2.2)}));
                line.userData.route=kind;line.userData.entry=item.id;highlight.add(line);
            }
        };
        drawRoute(liveEdges,primary,'follow');drawRoute(manualEdges,manualColor,'manual');
        root.dataset.atlasHighlight=explicitSelection&&current?'manual':liveEdges.length?'follow':'none';
        for (const item of selectedPath) {
            if (!visible.has(item.id)) continue;
            // The fluorescent route and label convey selection; retain status hues.
            if (stateColors[item.record?.state]) {
                const stateColor = new THREE.Color(stateColors[item.record.state]);
                const ring = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({color:stateColor.multiplyScalar(1.8)}));
                ring.position.copy(item.position); ring.scale.setScalar(item.radius + 1.8); ring.userData.faceCamera = true;
                ring.userData.running = item.record.state === 'running'; highlight.add(ring);
            }
        }
        const crumbs = document.createDocumentFragment();
        for (const item of current ? ancestors(current) : []) {
            const button = document.createElement('button'); button.type = 'button'; button.textContent = item.label;
            button.addEventListener('click', () => pick(item)); crumbs.append(button);
        }
        el('atlasBreadcrumb').replaceChildren(crumbs);
        el('atlasBranch').hidden = !current?.children.length;
        el('atlasBranch').textContent = uiT(collapsed.has(current?.id) ? '展开分支' : uiT('收起分支'));
        updateEmphasis(); renderLabels(); updateNarration(); requestFrame();
    }
    function updateEmphasis() {
        if (!spheres) return;
        const context = current && current.depth >= 2 ? current.cluster : null;
        const onPath = new Set(selectedPath.map(item=>item.id));
        const tracked=el('atlasFollow').checked&&trackingTarget(root.monitorSnapshot),live=tracked&&model.actual.get(tracked.node.id);
        if(live)ancestors(live).forEach(item=>onPath.add(item.id));
        nodes.forEach((item,index)=>{
            const dim = explicitSelection && current ? !onPath.has(item.id) : context && item.cluster !== context && !onPath.has(item.id);
            spheres.setColorAt(index,new THREE.Color(palette[theme][item.branch]).lerp(scene.background,dim ? .7 : 0));
        });
        if(spheres.instanceColor)spheres.instanceColor.needsUpdate=true;
        edges.forEach((item,index)=>{
            const dim=explicitSelection && current ? !(onPath.has(item.id)&&onPath.has(item.parent.id)) : context && item.cluster!==context && !onPath.has(item.id);
            const color=new THREE.Color(theme==='dark'?'#8995a8':'#647384').lerp(scene.background,dim ? .8 : item.depth>2 ? .24 : 0);
            for(let offset=index*72;offset<(index+1)*72;offset+=3){lineColors[offset]=color.r;lineColors[offset+1]=color.g;lineColors[offset+2]=color.b;}
        });
        if(lineGeometry)lineGeometry.attributes.color.needsUpdate=true;
    }
    function updateStatus() {
        if (!contextAvailable) {
            fallback(uiT('三维视图暂不可用，已展开路径列表。'));
            return;
        }
        stage.dataset.renderer = 'ready';
        const actualCount = nodes.filter(item => item.record).length;
        el('atlasStatus').textContent = actualCount ? uiT('{count} 个官网入口 · 三维视图', {count:actualCount}) :
            uiT(root.monitorSnapshot?.nodes.length ? '没有匹配的入口，请更换名称或状态。' : uiT('还没有已记录的官网路径。启动探索后，这里会显示真实入口。'));
        el('atlasPlay').disabled = !nodes.length;
    }
    function sendSelection(item, focus = true) {
        const node = item.record || {id:item.id, label:item.label, kind_label:uiT('已记录的导航名称'),
            state:'observed', state_label:uiT('路径分组'), reason:uiT('此名称来自已记录导航，尚无单独的页面读取记录。'),
            path:ancestors(item).map(part => part.label), url:'', checked_at:null, status_code:null, sources:[], tasks:[]};
        root.dispatchEvent(new CustomEvent('monitor-select-node', {detail:{id:node.id, node, focus}}));
    }
    function pick(item) {
        manualView = true;
        if(tourTimer||replayPosition<1)completeReplay();else stopTour();
        explicitSelection = true; current = item; sendSelection(item); updateHighlight();
    }
    function receiveSelection(event) {
        if (!model) return;
        const item = model.actual.get(event.detail.node.id) || model.entries.get(event.detail.node.id);
        if (!item) return;
        current = item;
        if (event.detail.focus) {
            explicitSelection = true;
            if(tourTimer||replayPosition<1)completeReplay();else stopTour();
        }
        let revealed = false;
        for (const part of ancestors(item).slice(0, -1)) if (collapsed.delete(part.id)) revealed = true;
        if (revealed) paint(); else updateHighlight();
        if (event.detail.focus) {
            manualView = true; updateFollowInfo();
            frameSelection(item);
        }
    }
    function update(snapshot) {
        active = snapshot.nodes.some(node => node.state === 'running');
        const next = JSON.stringify([layoutSpacing,snapshot.nodes.map(node => [node.id, node.label, node.path, node.kind, node.state])]);
        const initial = !model;
        if (next !== signature) {
            signature = next; const oldId = current?.record?.id || current?.id;
            model = buildAtlas(snapshot,{spacing:layoutSpacing});
            current = model.actual.get(oldId) || model.entries.get(oldId) || model.actual.get(root.dataset.selectedId) || null;
            paint();
        }
        if (initial && nodes.length) {
            overview(false);
        }
        syncFollowing();
        requestFrame();
    }
    function updateFollowInfo() {
        if(!contextAvailable)return;
        const target=trackingTarget(root.monitorSnapshot);
        el('atlasLocate').disabled=!target;
        let message='';
        if(el('atlasFollow').checked){
            if(tourTimer)message=uiT('回放结束后继续跟随');
            else if(replayPosition<1)message=uiT('回放已暂停，可点击定位');
            else if(!target)message=uiT('等待新的探索进展');
            else if(!nodes.some(item=>item.record?.id===target.node.id))message=el('monitorSearch').value||el('monitorFilter').value!=='all' ? uiT('当前入口被筛选隐藏，可点击定位') : uiT('当前入口位于收起的分支，可点击定位');
            else if(manualView)message=uiT('自由浏览中，新进展时继续跟随');
            else message=uiT(target.state==='running'?'跟随中：{name}':'最近进展：{name}',{name:target.node.label});
        }
        el('atlasFollowStatus').textContent=message;
        el('atlasFollowStatus').title=message;
    }
    function syncFollowing(force=false) {
        updateFollowInfo();
        if(!contextAvailable||!model||(!force&&!el('atlasFollow').checked)||tourTimer||replayPosition<1)return;
        const target=trackingTarget(root.monitorSnapshot),item=target&&model.actual.get(target.node.id);
        if(!item)return;
        if(!nodes.includes(item)){
            if(!force&&(el('monitorSearch').value||el('monitorFilter').value!=='all'))return;
            if(force&&(el('monitorSearch').value||el('monitorFilter').value!=='all')){
                el('monitorSearch').value='';el('monitorFilter').value='all';
                el('monitorSearch').dispatchEvent(new Event('input',{bubbles:true}));
            }
        }
        if(!force&&target.key===lastTrackedKey)return;
        lastTrackedKey=target.key;manualView=false;
        let revealed=false;
        for(const part of ancestors(item).slice(0,-1))if(collapsed.delete(part.id))revealed=true;
        current=item;explicitSelection=false;
        if(revealed)paint();
        sendSelection(item,false);updateHighlight();
        fly(new THREE.Vector3().copy(item.position),Math.max(235,item.radius*20),true);
        updateFollowInfo();
    }
    function completeReplay() {
        stopTour();replayScope=null;replayPlan=growthPlan(nodes);positionReplay(1);
    }
    function fly(target, distance, animate = true, front = false) {
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
        fly(bounds.center,distance,animate,true);
    }
    function frameSelection(item) {
        const visible=new Set(nodes), neighbors=[item.parent,item,...item.children].filter(n=>n&&visible.has(n));
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
        const path = current ? ancestors(current).map(item => item.label).join(' / ') : '';
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
        const path=ancestors(current), target=Number(index)===1?path[Math.min(1,path.length-1)]:current;
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
        if (!nodes.length) return;
        replayScope=explicitSelection && current ? new Set(ancestors(current).map(item=>item.id)) : null;
        replayPlan=growthPlan(replayScope?nodes.filter(item=>replayScope.has(item.id)):nodes);
        if (replayPosition>=1) positionReplay(0);
        tourTimer={at:performance.now(),from:replayPosition,duration:reduced.matches?1200:7200};
        el('atlasPlay').setAttribute('aria-pressed','true'); el('atlasPlay').setAttribute('aria-label',uiT('暂停生长动画'));
        updateFollowInfo();
        if (!explicitSelection) overview(false);
        requestFrame();
    }
    function clearSelection() {
        explicitSelection=false; current=null; selectedPath=[];
        delete root.dataset.selectedId; root.dispatchEvent(new CustomEvent('monitor-clear-selection')); updateHighlight();
    }

    function labelsAndRings() {
        const width = viewport.clientWidth, height = viewport.clientHeight, occupied = [[0,0,width,78],[0,height-104,width,height]];
        const reserved = [el('atlasOptions').open && el('atlasOptions').querySelector('.atlas-options-panel'),
            root.classList.contains('atlas-expanded') && stage.querySelector('.atlas-immersive-tools'),
            root.classList.contains('atlas-search-open') && el('atlasSearchPanel'),
            root.classList.contains('atlas-details-open') && el('atlasDetailPanel')].filter(Boolean);
        const rect=viewport.getBoundingClientRect();
        for(const panel of reserved){const box=panel.getBoundingClientRect();occupied.push([box.left-rect.left,box.top-rect.top,box.right-rect.left,box.bottom-rect.top]);}
        for (const item of labelNodes) {
            const button = item.labelElement;
            projected.copy(item.renderPosition || item.position).project(camera);
            const x = (projected.x + 1) * width / 2, y = (1 - projected.y) * height / 2;
            const distance = camera.position.distanceTo(new THREE.Vector3().copy(item.renderPosition || item.position));
            const offset = item.drawRadius * height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * distance) + 8;
            const left = x + offset, top = y - item.labelHeight / 2;
            const box = [left, top, left + item.labelWidth, top + item.labelHeight];
            const collides = occupied.some(other => box[0] < other[2] && box[2] > other[0] && box[1] < other[3] && box[3] > other[1]);
            const hide = item.growth < .15 || projected.z < -1 || projected.z > 1 || left < 0 || top < 0 || box[2] > width || top > height - 24 ||
                (collides && item !== current);
            button.hidden = hide;
            if (!hide) { button.style.transform = `translate(${left}px,${top}px)`; occupied.push(box); }
        }
        ringNodes.forEach((item, index) => {
            matrix.position.copy(item.renderPosition || item.position); matrix.scale.setScalar(item.drawRadius ? item.drawRadius*1.18 : 0); matrix.quaternion.copy(camera.quaternion);
            matrix.updateMatrix(); rings.setMatrixAt(index, matrix.matrix);
        });
        if (rings) rings.instanceMatrix.needsUpdate = true;
        highlight.children.forEach(object => { if (object.userData.faceCamera) object.quaternion.copy(camera.quaternion); });
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
            if (replayPosition>=1) {stopTour();if(el('atlasFollow').checked)syncFollowing(true);}
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
        renderer.setSize(width, height); composer.setSize(width, height); finishTexture(); requestFrame();
    }
    function hit(event) {
        if (!spheres || !nodes.length) return null;
        const rect = renderer.domElement.getBoundingClientRect();
        pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
        raycaster.setFromCamera(pointer, camera);
        const found = raycaster.intersectObject(spheres, false)[0];
        return found ? nodes[found.instanceId] : null;
    }
    renderer.domElement.addEventListener('pointerdown', event => {
        press = event.button === 0 ? {x:event.clientX,y:event.clientY} : null;
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
        if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) < 5) { const item = hit(event); if (item) pick(item); else clearSelection(); }
        press = null;
    });
    renderer.domElement.addEventListener('pointercancel', () => { press = null; });
    renderer.domElement.addEventListener('pointermove', event => {
        if (press || performance.now() - lastPick < 65) return;
        lastPick = performance.now(); hover = hit(event); tooltip.hidden = !hover;
        renderer.domElement.style.cursor = hover ? 'pointer' : 'grab';
        if (hover) {
            tooltip.textContent = hover.label + ' · ' + uiT(hover.record?.state_label || uiT('已记录的导航名称'));
            const rect = stage.getBoundingClientRect();
            tooltip.style.left = Math.min(event.clientX - rect.left + 14, stage.clientWidth - 225) + 'px';
            tooltip.style.top = Math.max(8, event.clientY - rect.top - 40) + 'px';
        }
    });
    renderer.domElement.addEventListener('pointerleave', () => { tooltip.hidden = true; });
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
        for (const id of ['atlasZoomIn','atlasZoomOut','atlasOverview','atlasSpacing']) el(id).disabled = false;
        updateStatus(); updateFollowInfo(); resize();
    });
    controls.addEventListener('change', requestFrame);
    controls.addEventListener('start', () => { manualView = true; tween = null; stopTour(); });
    viewport.addEventListener('keydown', event => {
        if (event.target !== viewport) return;
        if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) {
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
    el('atlasOverview').addEventListener('click', () => { manualView=true;stopTour(); replayScope=null; replayPlan=growthPlan(nodes); positionReplay(1); clearSelection(); overview(); });
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
        if(el('atlasFollow').checked){completeReplay();syncFollowing(true);}else{updateFollowInfo();if(model)updateHighlight();}
    });
    el('atlasLocate').addEventListener('click',()=>{completeReplay();syncFollowing(true);});
    el('atlasReplayReset').addEventListener('click',()=>{completeReplay();if(el('atlasFollow').checked)syncFollowing(true);else{clearSelection();overview();}});
    el('atlasSpacingValue').textContent=Math.round(layoutSpacing*100)+'%';
    el('atlasSpacing').addEventListener('input',()=>{el('atlasSpacingValue').textContent=Math.round(Number(el('atlasSpacing').value)*100)+'%';});
    el('atlasSpacing').addEventListener('change',()=>{
        layoutSpacing=Number(el('atlasSpacing').value);manualView=true;savePreferences();
        if(root.monitorSnapshot){update(root.monitorSnapshot);overview();}
    });
    document.addEventListener('pointerdown',event=>{if(el('atlasOptions').open&&!el('atlasOptions').contains(event.target))el('atlasOptions').open=false;});
    el('atlasOptions').addEventListener('toggle',requestFrame);
    root.addEventListener('monitor-data', event => update(event.detail));
    root.addEventListener('monitor-node-selected', receiveSelection);
    root.addEventListener('monitor-filter', () => { if (model) { paint(); overview(); updateFollowInfo(); } });
    const resizeObserver = new ResizeObserver(resize); resizeObserver.observe(viewport);
    const intersection = new IntersectionObserver(entries => {
        inView = entries[0].isIntersecting;
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
        disposed = true; resizeObserver.disconnect(); intersection.disconnect(); themeObserver.disconnect();
        controls.dispose(); disposeGroup(graph); disposeGroup(highlight); sphereGeometry.dispose(); sphereMaterial.dispose();
        ringGeometry.dispose(); bloom.dispose(); composer.dispose(); renderer.dispose();
    });
    window.addEventListener('pageshow', () => { if (!disposed) requestFrame(); });
    // Small diagnostics also let browser checks verify genuine spatial interaction.
    atlas = {camera, controls, renderer, get model() { return model; }, get current() { return current; }, scene, get progress() {return replayPosition;}, get playing() {return Boolean(tourTimer);}, get nodes() {return nodes;},
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
