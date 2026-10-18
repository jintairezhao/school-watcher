/* Local WebGL scene. Viewing or touring never dispatches discovery work. */
import {buildAtlas, ancestors, visibleEntries} from './discovery-atlas-model.js';

const root = document.getElementById('discoveryMonitor');
const el = id => document.getElementById(id);
const stage = el('atlasStage'), viewport = el('atlasViewport');
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
let atlas;

function fallback(message) {
    stage.dataset.renderer = 'unavailable';
    el('atlasStatus').textContent = message;
    el('atlasListPanel').open = true;
    el('atlasPlay').disabled = true;
    for (const id of ['atlasZoomIn', 'atlasZoomOut', 'atlasOverview']) el(id).disabled = true;
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
    const hemisphere = new THREE.HemisphereLight(0xf2f6ff, 0x303849, 1.8);
    const keyLight = new THREE.DirectionalLight(0xffffff, 3.2); keyLight.position.set(-180, 240, 300);
    const rimLight = new THREE.DirectionalLight(0x9bbfff, 1.1); rimLight.position.set(170, -30, -180);
    scene.add(hemisphere, keyLight, rimLight);
    const graph = new THREE.Group(), highlight = new THREE.Group();
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
    let signature = '', theme, primary, stateColors = {}, selectedPath = [], stageIndex = 0, tourTimer = null;
    let contextAvailable = true;
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
        const token = name => css.getPropertyValue(name).trim();
        stateColors = {running:primary, connected:token('--color-success'), failed:token('--color-danger'), waiting:token('--color-warning')};
        scene.background = new THREE.Color(css.getPropertyValue('--color-bg').trim());
        scene.fog = new THREE.Fog(scene.background, 850, 2100);
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
    function curveFor(from, to) {
        const a = new THREE.Vector3().copy(from.position), b = new THREE.Vector3().copy(to.position);
        const dx = b.x - a.x;
        return new THREE.CubicBezierCurve3(a, new THREE.Vector3(a.x + dx * .45, a.y, a.z),
            new THREE.Vector3(b.x - dx * .35, b.y, b.z), b);
    }
    function renderLabels() {
        const chosen = new Set(selectedPath.map(item => item.id));
        labelNodes = nodes.filter(item => chosen.has(item.id) || item.record?.kind === 'root' ||
            ((item.record?.kind === 'unit' || item.children.length > 2) && item.depth <= 2)).sort((a,b) =>
            Number(b === current) - Number(a === current) || Number(chosen.has(b.id)) - Number(chosen.has(a.id)) || a.depth - b.depth).slice(0, 24);
        const fragment = document.createDocumentFragment();
        for (const item of labelNodes) {
            const button = document.createElement('button'); button.type = 'button';
            button.className = 'atlas-node-label'; button.textContent = item.label;
            button.dataset.atlasNode = item.id; button.dataset.selected = String(item === current);
            button.setAttribute('aria-label', item.label + ' · ' + uiT(item.record?.state_label || '已记录的导航名称'));
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
        disposeGroup(graph); disposeGroup(highlight);
        nodes = visibleEntries(model, {query:el('monitorSearch').value.trim(), state:el('monitorFilter').value, collapsed});
        const shown = new Set(nodes.map(item => item.id));
        spheres = new THREE.InstancedMesh(sphereGeometry, sphereMaterial, nodes.length);
        spheres.instanceMatrix.setUsage(THREE.StaticDrawUsage);
        const positions = [];
        nodes.forEach((item, index) => {
            matrix.position.copy(item.position); matrix.scale.setScalar(item.radius); matrix.quaternion.identity(); matrix.updateMatrix();
            spheres.setMatrixAt(index, matrix.matrix); spheres.setColorAt(index, new THREE.Color(palette[theme][item.branch]));
            if (item.parent && shown.has(item.parent.id)) {
                const points = curveFor(item.parent, item).getPoints(24);
                for (let point = 1; point < points.length; point++) positions.push(...points[point - 1].toArray(), ...points[point].toArray());
            }
        });
        graph.add(spheres);
        const lines = new THREE.BufferGeometry(); lines.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
        graph.add(new THREE.LineSegments(lines, new THREE.LineBasicMaterial({color:theme === 'dark' ? 0x8995a8 : 0x7c899e,
            transparent:true, opacity:theme === 'dark' ? .3 : .32, depthWrite:false})));
        ringNodes = nodes.filter(item => ['running','connected','waiting','failed'].includes(item.record?.state));
        rings = new THREE.InstancedMesh(ringGeometry, new THREE.MeshBasicMaterial({transparent:true, opacity:.7}), ringNodes.length);
        ringNodes.forEach((item, index) => rings.setColorAt(index, new THREE.Color(stateColors[item.record.state])));
        graph.add(rings);
        if (nodes.length) {
            const box = new THREE.Box3(); nodes.forEach(item => box.expandByPoint(new THREE.Vector3().copy(item.position)));
            box.expandByScalar(20); bounds = box.getBoundingSphere(new THREE.Sphere()); extent = box.getSize(new THREE.Vector3());
        }
        updateHighlight(); renderLabels(); updateStatus(); requestFrame();
        root.dataset.atlasNodeCount = String(nodes.length);
    }
    function updateHighlight() {
        disposeGroup(highlight);
        selectedPath = current ? ancestors(current) : [];
        const visible = new Set(nodes.map(item => item.id));
        for (const item of selectedPath) {
            if (!visible.has(item.id)) continue;
            // Selection is conveyed by the blue route and label; retain the status hue.
            if (stateColors[item.record?.state]) {
                const stateColor = new THREE.Color(stateColors[item.record.state]);
                const ring = new THREE.Mesh(ringGeometry, new THREE.MeshBasicMaterial({color:stateColor.multiplyScalar(1.8)}));
                ring.position.copy(item.position); ring.scale.setScalar(item.radius + 1.8); ring.userData.faceCamera = true;
                ring.userData.running = item.record.state === 'running'; highlight.add(ring);
            }
            if (item.parent && visible.has(item.parent.id)) {
                const geometry = new THREE.TubeGeometry(curveFor(item.parent, item), 36, .48, 6, false);
                highlight.add(new THREE.Mesh(geometry, new THREE.MeshBasicMaterial({color:primary.clone().multiplyScalar(theme === 'dark' ? 4 : 12)})));
            }
        }
        const crumbs = document.createDocumentFragment();
        for (const item of selectedPath) {
            const button = document.createElement('button'); button.type = 'button'; button.textContent = item.label;
            button.addEventListener('click', () => pick(item)); crumbs.append(button);
        }
        el('atlasBreadcrumb').replaceChildren(crumbs);
        el('atlasBranch').hidden = !current?.children.length;
        el('atlasBranch').textContent = uiT(collapsed.has(current?.id) ? '展开分支' : '收起分支');
        renderLabels(); updateNarration(); requestFrame();
    }
    function updateStatus() {
        if (!contextAvailable) {
            fallback(uiT('三维视图暂不可用，已展开路径列表。'));
            return;
        }
        stage.dataset.renderer = 'ready';
        const actualCount = nodes.filter(item => item.record).length;
        el('atlasStatus').textContent = actualCount ? uiT('{count} 个官网入口 · 三维视图', {count:actualCount}) :
            uiT(root.monitorSnapshot?.nodes.length ? '没有匹配的入口，请更换名称或状态。' : '还没有已记录的官网路径。启动探索后，这里会显示真实入口。');
        el('atlasPlay').disabled = !nodes.length;
    }
    function sendSelection(item, focus = true) {
        const node = item.record || {id:item.id, label:item.label, kind_label:uiT('已记录的导航名称'),
            state:'observed', state_label:uiT('路径分组'), reason:uiT('此名称来自已记录导航，尚无单独的页面读取记录。'),
            path:ancestors(item).map(part => part.label), url:'', checked_at:null, status_code:null, sources:[], tasks:[]};
        root.dispatchEvent(new CustomEvent('monitor-select-node', {detail:{id:node.id, node, focus}}));
    }
    function pick(item) {
        stopTour(); el('atlasFollow').checked = false;
        current = item; sendSelection(item); updateHighlight();
    }
    function receiveSelection(event) {
        if (!model) return;
        const item = model.actual.get(event.detail.node.id) || model.entries.get(event.detail.node.id);
        if (!item) return;
        current = item;
        let revealed = false;
        for (const part of ancestors(item).slice(0, -1)) if (collapsed.delete(part.id)) revealed = true;
        if (revealed) paint(); else updateHighlight();
        if (event.detail.focus) {
            el('atlasFollow').checked = false;
            setChapter(2, true);
        }
    }
    function update(snapshot) {
        active = snapshot.nodes.some(node => node.state === 'running');
        const next = JSON.stringify(snapshot.nodes.map(node => [node.id, node.label, node.path, node.kind, node.state]));
        const initial = !model;
        if (next !== signature) {
            signature = next; const oldId = current?.record?.id || current?.id;
            model = buildAtlas(snapshot);
            current = model.actual.get(oldId) || model.entries.get(oldId) || model.actual.get(root.dataset.selectedId) || null;
            paint();
        }
        const running = el('atlasFollow').checked && !tourTimer ? snapshot.nodes.find(node => node.state === 'running') : null;
        if (running && current?.record?.id !== running.id) {
            current = model.actual.get(running.id); sendSelection(current, false); updateHighlight();
            if (!initial) setChapter(2, true);
        }
        if (initial && nodes.length) {
            current ||= nodes.find(item => item.record) || nodes[0]; sendSelection(current, false); updateHighlight(); overview(false);
        }
        requestFrame();
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
    function overview(animate = true) {
        const aspect = Math.max(.45, camera.aspect);
        const vertical = THREE.MathUtils.degToRad(camera.fov / 2);
        const distance = Math.max(200, Math.max(extent.y, extent.x / aspect) / (2 * Math.tan(vertical)) * 1.14 + extent.z / 2);
        if (!animate) { camera.position.set(bounds.center.x + 12, bounds.center.y + 18, bounds.center.z + distance); controls.target.copy(bounds.center); }
        fly(bounds.center, distance, animate, true);
    }
    function updateNarration() {
        const path = current ? ancestors(current).map(item => item.label).join(' / ') : '';
        const lines = [uiT('从已记录的官网入口出发。'), uiT('分叉表示观察到的导航路径，不代表行政隶属关系。'),
            current ? uiT('当前线索：{name}', {name:current.label}) : uiT('选择一个节点，沿已记录的路径深入。'),
            current?.record?.sources.length ? uiT('此入口已有接入栏目，可查看保存的来源与状态。') : uiT('查看右侧读取记录与官网入口，未验证的内容保留待确认状态。')];
        el('atlasNarration').textContent = lines[stageIndex];
        el('atlasScrubber').setAttribute('aria-valuetext', lines[stageIndex]);
        el('atlasNarration').title = path;
        el('atlasNarration').dataset.stage = stageIndex;
    }
    function setChapter(index, animate = true) {
        stageIndex = Number(index); el('atlasScrubber').value = stageIndex;
        root.dataset.atlasChapter = stageIndex;
        for (const button of root.querySelectorAll('[data-atlas-chapter]')) {
            button.setAttribute('aria-pressed', String(Number(button.dataset.atlasChapter) === stageIndex));
        }
        updateNarration();
        if (!current) return;
        const path = ancestors(current);
        if (stageIndex === 0) overview(animate);
        else {
            const target = stageIndex === 1 ? path[Math.min(1, path.length - 1)] : current;
            fly(new THREE.Vector3().copy(target.position), stageIndex === 1 ? 370 : stageIndex === 2 ? 235 : 155, animate);
        }
        el('monitorDetail').classList.toggle('atlas-evidence-focus', stageIndex === 3);
    }
    function stopTour() {
        clearInterval(tourTimer); tourTimer = null;
        el('atlasPlay').setAttribute('aria-pressed', 'false');
        el('atlasPlay').setAttribute('aria-label', uiT('播放路径导览'));
    }
    function tour() {
        if (tourTimer) { stopTour(); return; }
        if (!current) return;
        el('atlasFollow').checked = false;
        setChapter(0); el('atlasPlay').setAttribute('aria-pressed','true');
        el('atlasPlay').setAttribute('aria-label', uiT('暂停路径导览'));
        tourTimer = setInterval(() => {
            if (stageIndex === 3) { stopTour(); return; }
            setChapter(stageIndex + 1);
        }, 3000);
    }
    function labelsAndRings() {
        const width = viewport.clientWidth, height = viewport.clientHeight, occupied = [];
        for (const item of labelNodes) {
            const button = item.labelElement;
            projected.copy(item.position).project(camera);
            const x = (projected.x + 1) * width / 2, y = (1 - projected.y) * height / 2;
            const distance = camera.position.distanceTo(new THREE.Vector3().copy(item.position));
            const offset = item.radius * height / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)) * distance) + 8;
            const left = x + offset, top = y - item.labelHeight / 2;
            const box = [left, top, left + item.labelWidth, top + item.labelHeight];
            const collides = occupied.some(other => box[0] < other[2] && box[2] > other[0] && box[1] < other[3] && box[3] > other[1]);
            const hide = projected.z < -1 || projected.z > 1 || left < 0 || top < 0 || box[2] > width || top > height - 24 ||
                (collides && item !== current) || (left < Math.min(340, width * .5) && top < (width < 600 ? 140 : 190));
            button.hidden = hide;
            if (!hide) { button.style.transform = `translate(${left}px,${top}px)`; occupied.push(box); }
        }
        ringNodes.forEach((item, index) => {
            matrix.position.copy(item.position); matrix.scale.setScalar(item.radius + 1.7); matrix.quaternion.copy(camera.quaternion);
            matrix.updateMatrix(); rings.setMatrixAt(index, matrix.matrix);
        });
        if (rings) rings.instanceMatrix.needsUpdate = true;
        highlight.children.forEach(object => { if (object.userData.faceCamera) object.quaternion.copy(camera.quaternion); });
    }
    function draw(time) {
        frame = null;
        if (disposed || document.hidden || !inView || stage.dataset.renderer === 'unavailable') return;
        if (time - lastFrame < 28) { frame = requestAnimationFrame(draw); return; }
        lastFrame = time;
        let moving = controls.update();
        if (tween) {
            const progress = Math.min(1, (time - tween.at) / 1050), eased = 1 - 2 ** (-10 * progress);
            camera.position.lerpVectors(tween.from, tween.to, progress === 1 ? 1 : eased);
            controls.target.lerpVectors(tween.fromTarget, tween.toTarget, progress === 1 ? 1 : eased);
            camera.lookAt(controls.target); moving = true;
            if (progress === 1) tween = null;
        }
        const pulsing = active && !reduced.matches && selectedPath.some(item => item.record?.state === 'running');
        if (pulsing) highlight.children.forEach(object => {
            if (object.userData.running) object.material.color.copy(primary).multiplyScalar(2.2 + Math.sin(time / 800) * .35);
        });
        if (dirty || moving || pulsing) { labelsAndRings(); composer.render(); dirty = false; }
        if (moving || tween || pulsing) requestFrame();
    }
    function resize() {
        const width = viewport.clientWidth, height = viewport.clientHeight;
        if (!width || !height) return;
        camera.aspect = width / height; camera.updateProjectionMatrix();
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
    renderer.domElement.addEventListener('pointerdown', event => { press = {x:event.clientX, y:event.clientY}; tween = null; stopTour(); });
    renderer.domElement.addEventListener('pointerup', event => {
        if (press && Math.hypot(event.clientX - press.x, event.clientY - press.y) < 5) { const item = hit(event); if (item) pick(item); }
        press = null;
    });
    renderer.domElement.addEventListener('pointercancel', () => { press = null; });
    renderer.domElement.addEventListener('pointermove', event => {
        if (press || performance.now() - lastPick < 65) return;
        lastPick = performance.now(); hover = hit(event); tooltip.hidden = !hover;
        renderer.domElement.style.cursor = hover ? 'pointer' : 'grab';
        if (hover) {
            tooltip.textContent = hover.label + ' · ' + uiT(hover.record?.state_label || '已记录的导航名称');
            const rect = stage.getBoundingClientRect();
            tooltip.style.left = Math.min(event.clientX - rect.left + 14, stage.clientWidth - 225) + 'px';
            tooltip.style.top = Math.max(8, event.clientY - rect.top - 40) + 'px';
        }
    });
    renderer.domElement.addEventListener('pointerleave', () => { tooltip.hidden = true; });
    renderer.domElement.addEventListener('webglcontextlost', event => {
        event.preventDefault(); contextAvailable = false; controls.enabled = false; stopTour();
        fallback(uiT('三维视图暂不可用，已展开路径列表。'));
    });
    renderer.domElement.addEventListener('webglcontextrestored', () => {
        contextAvailable = true; controls.enabled = true;
        stage.dataset.renderer = 'ready';
        for (const id of ['atlasZoomIn','atlasZoomOut','atlasOverview']) el(id).disabled = false;
        updateStatus(); resize();
    });
    controls.addEventListener('change', requestFrame);
    controls.addEventListener('start', () => { tween = null; stopTour(); el('atlasFollow').checked = false; });
    viewport.addEventListener('keydown', event => {
        if (event.target !== viewport) return;
        if (['ArrowLeft','ArrowRight','ArrowUp','ArrowDown'].includes(event.key)) {
            event.preventDefault(); tween = null; stopTour();
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
        fly(controls.target, THREE.MathUtils.clamp(camera.position.distanceTo(controls.target) * factor, controls.minDistance, controls.maxDistance));
    }
    el('atlasOverview').addEventListener('click', () => { stopTour(); setChapter(0); });
    el('atlasZoomIn').addEventListener('click', () => zoom(.8));
    el('atlasZoomOut').addEventListener('click', () => zoom(1.25));
    el('atlasPlay').addEventListener('click', tour);
    el('atlasScrubber').addEventListener('input', event => { stopTour(); setChapter(event.target.value); });
    root.querySelectorAll('[data-atlas-chapter]').forEach(button => button.addEventListener('click', () => { stopTour(); setChapter(button.dataset.atlasChapter); }));
    el('atlasBranch').addEventListener('click', () => {
        if (!current) return;
        if (collapsed.has(current.id)) collapsed.delete(current.id); else collapsed.add(current.id);
        paint();
    });
    el('atlasFollow').addEventListener('change', () => { if (el('atlasFollow').checked && root.monitorSnapshot) { stopTour(); update(root.monitorSnapshot); } });
    root.addEventListener('monitor-data', event => update(event.detail));
    root.addEventListener('monitor-node-selected', receiveSelection);
    root.addEventListener('monitor-filter', () => { if (model) { paint(); overview(); } });
    const resizeObserver = new ResizeObserver(resize); resizeObserver.observe(viewport);
    const intersection = new IntersectionObserver(entries => {
        inView = entries[0].isIntersecting;
        if (inView) requestFrame(); else { stopTour(); if (frame) cancelAnimationFrame(frame); frame = null; }
    }); intersection.observe(viewport);
    const themeObserver = new MutationObserver(applyTheme); themeObserver.observe(document.documentElement, {attributes:true, attributeFilter:['data-theme']});
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) { stopTour(); if (frame) cancelAnimationFrame(frame); frame = null; } else requestFrame();
    });
    document.addEventListener('locale-changed', () => { if (model) { updateHighlight(); updateStatus(); } });
    window.addEventListener('pagehide', event => {
        stopTour(); if (frame) cancelAnimationFrame(frame); frame = null;
        if (event.persisted) return;
        disposed = true; resizeObserver.disconnect(); intersection.disconnect(); themeObserver.disconnect();
        controls.dispose(); disposeGroup(graph); disposeGroup(highlight); sphereGeometry.dispose(); sphereMaterial.dispose();
        ringGeometry.dispose(); bloom.dispose(); composer.dispose(); renderer.dispose();
    });
    window.addEventListener('pageshow', () => { if (!disposed) requestFrame(); });
    // Small diagnostics also let browser checks verify genuine spatial interaction.
    atlas = {camera, controls, renderer, get model() { return model; }, get current() { return current; },
        select:id => { const item = model?.actual.get(id) || model?.entries.get(id); if (item) pick(item); }};
    root.discoveryAtlas = atlas;
    applyTheme(); resize(); stage.dataset.renderer = 'ready';
    updateNarration();
    if (root.monitorSnapshot) update(root.monitorSnapshot);
    else el('atlasStatus').textContent = uiT('正在载入保存的路径…');
}

el('atlasFullscreen').addEventListener('click', () => {
    const expanded = root.classList.toggle('atlas-expanded');
    document.body.classList.toggle('atlas-is-fullscreen', expanded);
    el('atlasFullscreen').setAttribute('aria-pressed', String(expanded));
    el('atlasFullscreen').textContent = uiT(expanded ? '退出全屏' : '全屏探索');
});
document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && root.classList.contains('atlas-expanded')) el('atlasFullscreen').click();
});
initialize().catch(() => fallback(uiT('当前设备无法显示三维网络，已展开可操作的路径列表。')));
