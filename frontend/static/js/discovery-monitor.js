/* Read-only telemetry; refresh and task controls are intentionally separate. */
import {relationGroups} from './discovery-atlas-relations.js';
(() => {
    const root = document.getElementById('discoveryMonitor');
    if (!root) return;
    const el = id => document.getElementById(id);
    const number = value => Number.isFinite(value) ? (window.I18n?.number(value) || value.toLocaleString()) : '—';
    const date = value => value && Number.isFinite(Date.parse(value))
        ? (window.I18n?.dateTime(value) || new Date(value).toLocaleString()) : uiT('尚无记录');
    const create = (tag, text, className) => {
        const item = document.createElement(tag);
        if (text !== undefined) item.textContent = text;
        if (className) item.className = className;
        return item;
    };
    const link = (title, url) => {
        const item = create('a', title);
        try { if (['https:', 'http:'].includes(new URL(url).protocol)) item.href = url; } catch (_) { /* unlinked */ }
        item.target = '_blank'; item.rel = 'noopener noreferrer'; return item;
    };
    let data = null, selected = null, timer = null, loading = false, stopped = false, failures = 0, graphSignature = '';
    let abort = null, controlBusy = false, refreshQueued = false, connectionState = 'loading';
    const expandedRelations=new Set();
    function connection() {
        const message = connectionState === 'error' ? (data ? uiT('连接中断，保留上次数据') : uiT('暂时无法读取进度')) :
            !el('monitorAuto').checked ? uiT('自动刷新已关闭，探索任务不受影响') :
            connectionState === 'ready' ? uiT('每 3 秒同步实际进度') : uiT('正在读取实际进度…');
        field('monitorConnection', message);
        field('monitorReadAt', data?.observed_at ? uiT('数据读取于{date}', {date:date(data.observed_at)}) : '');
    }
    const relationLabel = edge => edge.type === 'organization_membership' ? uiT(edge.observed_relation === 'attached_unit' ? '挂靠单位' : '下属单位') : uiT(({navigation:'网页导航', navigation_link:'网页导航', directory:'目录收录',
        directory_listing:'目录收录', organization_membership:'组织归属', source_ownership:'栏目归属',
        source_on_page:'栏目所在页面', entity_page:'实体对应页面', cross_entity_reference:'跨部门关联',source_preview:'栏目预览关联'})[edge.type] || '已记录关系');
    function field(id, text) { el(id).textContent = text; }
    function detail(node) {
        if (!node) return;
        const box = el('monitorDetail');
        const dl = create('dl');
        const source = node.entity_type === 'source';
        for (const [name, value] of [[uiT('状态'), uiT(node.state_label) + ' · ' + uiT(node.reason)],
            [uiT('入口类型'), uiT(node.kind_label)],
            [uiT(source ? '访问记录时间' : '最近读取'), date(node.checked_at)], [uiT('网页响应'), node.status_code ? 'HTTP ' + node.status_code : uiT('尚无响应记录')]]) {
            dl.append(create('dt', name), create('dd', value));
        }
        if (source) {
            const labels = {responded:'已记录成功响应',restricted:'访问受阻',not_found:'本次观察未找到入口',
                failed:'本次读取未完成',conflicting:'访问记录存在分歧',unknown:'尚无响应记录'};
            const value = create('dd', uiT(labels[node.access] || '尚无响应记录'));
            value.dataset.sourceAccess = node.access || 'unknown';
            dl.append(create('dt', uiT('入口访问记录')), value);
            if (node.access_observation?.reference_url) {
                const entry = create('dd');
                entry.append(link(node.access_observation.reference_url, node.access_observation.reference_url));
                dl.append(create('dt', uiT('记录入口')), entry);
            }
        }
        const badge = create('p', uiT(node.state_label), 'atlas-node-state'); badge.dataset.state = node.state;
        box.replaceChildren(badge, create('h3', node.label));
        if(node.label_source==='url')box.append(create('p',uiT('尚未记录页面名称，暂以网页地址标识。'),'monitor-caption'));
        if (node.url) { const action = link(uiT('打开官网入口'), node.url); action.className = 'atlas-source-action'; box.append(action); }
        box.append(dl);
        if(source)box.append(create('p',uiT('访问记录仅代表该次观察，不表示官网当前可用。'),'monitor-caption'));
        if(['configured_multiple_regions','configured_module_preview'].includes(node.scope_kind)){
            const scope=create('details',undefined,'atlas-path-evidence');
            scope.append(create('summary',uiT(node.scope_kind==='configured_module_preview'?'主页预览范围':'组合采集范围')),
                create('p',uiT('保留原订阅；页面预览与完整栏目分别记录。'),'monitor-caption'));
            const list=create('ul');
            for(const component of node.source_scope?.components||[]){const row=create('li');
                row.append(create('span',component.name||uiT('栏目名称待核实')),document.createTextNode(' · '),
                    create('span',uiT(component.status_label||'关联依据待补充')));
                if(component.column_url)row.append(document.createTextNode(' · '),link(uiT('官网栏目'),component.column_url));
                list.append(row);
            }
            scope.append(list);box.append(scope);
        }
        const relations=(data?.relations||[]).filter(edge=>edge.source===node.id||edge.target===node.id);
        if(relations.length){
            const section=create('section',undefined,'monitor-relation-groups');section.id='monitorRelationGroups';
            section.append(create('h3',uiT('已记录关系')),
                create('p',uiT('展开查看入口与依据；密集连线随分组展开。'),'monitor-caption'));
            const byId=new Map(data.nodes.map(item=>[item.id,item]));
            for(const group of relationGroups(relations,node.id)){
                const details=create('details');details.dataset.relationGroup=group.id;details.open=expandedRelations.has(group.id);
                details.dataset.relationCount=String(group.links.length);
                const direction=uiT(group.direction==='outgoing'?'从此节点出发':'指向此节点');
                details.append(create('summary',uiT('{direction} · {relation} · {count} 条关系',{
                    direction,relation:relationLabel({...group.links[0],type:group.type}),count:group.links.length})));
                let built=false;
                function populate(){
                    if(built||!details.open)return;built=true;
                    const list=create('ul');
                    for(const edge of group.links){
                        const source=byId.get(edge.source),target=byId.get(edge.target);if(!source||!target)continue;
                        const row=create('li'),button=create('button',source.label+' → '+target.label,'monitor-related-node');button.type='button';
                        button.dataset.relatedNode=source.id===node.id?target.id:source.id;
                        button.addEventListener('click',()=>choose(byId.get(button.dataset.relatedNode),true));row.append(button);
                        for(const assertion of edge.assertions||[])row.append(create('p',assertion.text||assertion.identity_text||'','monitor-caption'));
                        const proofs=edge.evidence||[],material=create('details');material.className='monitor-relation-material';
                        material.append(create('summary',uiT('查看 {count} 项依据',{count:proofs.length})));
                        material.addEventListener('toggle',()=>{
                            if(!material.open||material.dataset.built)return;material.dataset.built='true';
                            if(edge.evidence_url)material.append(link(relationLabel(edge),edge.evidence_url));
                            for(const proof of proofs){
                                const evidence=create('p',[proof.locator,proof.observed_at?date(proof.observed_at):uiT('观察时间未记录'),
                                    proof.content_hash?uiT('材料版本')+' '+proof.content_hash.slice(0,12):''].filter(Boolean).join(' · '),'monitor-caption');
                                if(proof.reference_url)evidence.append(document.createTextNode(' · '),link(uiT('来源页面'),proof.reference_url));
                                if(proof.kind==='web_observation'&&proof.material_available!==true)evidence.append(document.createTextNode(' · '+
                                    uiT(proof.material_available===false?'原始材料未保留':'原始材料可用性未确认')));
                                material.append(evidence);
                            }
                        });
                        row.append(material);list.append(row);
                    }
                    details.append(list);
                }
                details.addEventListener('toggle',()=>{
                    if(!details.isConnected)return;
                    if(details.open)expandedRelations.add(group.id);else expandedRelations.delete(group.id);
                    populate();root.dispatchEvent(new CustomEvent('monitor-relation-group',{detail:{id:group.id,expanded:details.open}}));
                });
                populate();section.append(details);
            }
            box.append(section);
        }
        box.append(create('p', node.url, 'monitor-caption'));
        box.append(create('p', node.cost ? uiT('此入口已确认{used} Token；另预留{reserved} Token。',
            {used:number(node.cost.confirmed_tokens),reserved:number(node.cost.reserved_tokens)}) : uiT('此入口尚无可准确归属的 AI 用量记录。'), 'monitor-caption'));
        if (node.sources.length) {
            box.append(create('h3', uiT(node.state === 'connected' ? '已接入栏目' : '已保存的栏目配置')));
            const list = create('ul'); node.sources.forEach(source => list.append(create('li', [source.group, source.name].filter(Boolean).join(' / ')))); box.append(list);
        }
        if (node.tasks.length) {
            box.append(create('h3', uiT('相关任务')));
            const list = create('ul'); node.tasks.forEach(task => list.append(create('li', uiT(task.reason) + ' · ' + date(task.updated_at)))); box.append(list);
        }
    }
    function choose(node, focus = false) {
        selected = node.id;
        root.dataset.selectedId = selected;
        el('monitorTree').querySelectorAll('[data-node-id]').forEach(item => {
            if (item.tagName === 'BUTTON') item.setAttribute('aria-pressed', String(item.dataset.nodeId === selected));
            else if (item.dataset.nodeId === selected) item.setAttribute('aria-current', 'true');
            else item.removeAttribute('aria-current');
        });
        detail(node);
        root.dispatchEvent(new CustomEvent('monitor-node-selected', {detail:{node, focus}}));
    }
    function clearSelection() {
        selected = null;
        delete root.dataset.selectedId;
        el('monitorTree').querySelectorAll('[aria-pressed]').forEach(button => button.setAttribute('aria-pressed', 'false'));
        el('monitorTree').querySelectorAll('[aria-current]').forEach(item => item.removeAttribute('aria-current'));
        el('monitorDetail').replaceChildren(create('p', uiT('选择左侧入口，查看路径、状态和接入结果。'), 'monitor-empty'));
    }
    function nodeButton(node) {
        const button = create('button', undefined, 'monitor-node'); button.type = 'button';
        button.dataset.nodeId = node.id; button.dataset.state = node.state;
        button.setAttribute('aria-pressed', String(selected === node.id));
        button.append(create('span', node.label, 'monitor-node-name'), create('span', window.I18n?.t(node.state_label) || node.state_label, 'monitor-node-status'));
        button.addEventListener('click', () => {
            const current = data?.nodes.find(item => item.id === node.id);
            if (current) choose(current, true);
        }); return button;
    }
    function tree(force = false) {
        if (!data) return;
        if(!el('atlasListPanel').open){
            graphSignature='';
            el('monitorTree').replaceChildren(create('p',uiT('展开列表后查看已记录的入口。'),'monitor-empty'));
            const node=data.nodes.find(item=>item.id===selected);if(node)choose(node);return;
        }
        const query = el('monitorSearch').value.trim().toLocaleLowerCase();
        const state = el('monitorFilter').value;
        const signature = JSON.stringify([data.nodes, data.relations, query, state]);
        if (!force && graphSignature === signature) return;
        // Incoming progress must not remove the control a keyboard user is on.
        if (!force && el('monitorTree').contains(document.activeElement)) {
            detail(data.nodes.find(item => item.id === selected)); return;
        }
        graphSignature = signature;
        const box = el('monitorTree'); const scroll = box.scrollTop;
        const nodes = data.nodes.filter(node => (state === 'all' || node.state === state) &&
            (!query || [node.label, ...node.path].join(' ').toLocaleLowerCase().includes(query)));
        if (!nodes.length) {
            box.replaceChildren(create('p', query || state !== 'all' ? uiT('没有匹配的入口，请更换名称或状态。') : uiT('还没有已记录的官网路径。启动探索后，这里会显示真实入口。'), 'monitor-empty')); return;
        }
        const list = create('ul');
        // A name-only path is context, not an evidenced parent-child relationship.
        // The accessible fallback retains actual page identities just like WebGL.
        for (const node of nodes) {
            const item = create('li');item.append(nodeButton(node));
            item.append(create('p',node.url,'monitor-caption'));list.append(item);
        }
        box.replaceChildren(list); box.scrollTop = scroll;
        const node = data.nodes.find(item => item.id === selected);
        if (node) choose(node);
    }
    function render(value) {
        data = value; root.monitorSnapshot = value; const p = data.progress;
        // Selection belongs to this committed view, even when its list is
        // collapsed or keeping keyboard focus. Never retain a removed record.
        const selectionRemoved = selected && !data.nodes.some(node => node.id === selected);
        if (selectionRemoved) clearSelection();
        connection();
        field('monitorMessage', uiT(p.message));
        field('monitorCurrent', p.current_label ? uiT('正在处理：{name}',{name:p.current_label}) : uiT(p.stage_message));
        field('monitorWorkAt', uiT('任务最近进展：{date} · {state}',{date:date(p.updated_at),state:p.state === 'paused' ? uiT('探索已暂停') :
            p.state === 'pausing' ? uiT('正在等待当前任务结束') : data.tasks.some(task => task.state === 'running') ? uiT('任务进行中') : uiT('当前没有正在执行的探索')}));
        field('monitorChecked', p.updated_at ? number(p.checked_pages) : '—');
        field('monitorPending', p.updated_at ? number(p.pending_pages) : '—');
        field('monitorCandidates', number(p.column_tasks)); field('monitorConnected', number(p.verified_source_count));
        field('monitorCatalogNote', data.catalog.unavailable ? uiT('暂时读不到已保存的路径，其他任务数据仍可查看。') :
            uiT('结构快照：{date}；正在处理的入口来自实时任务。',{date:date(data.catalog.published_at)}) + (data.catalog.truncated ? uiT('当前显示前 1,200 个入口，完整结构请打开“官网结构”。') : ''));
        const scope=data.catalog.scope;
        if (data.structure_scope) {
            field('monitorCatalogNote', uiT(data.structure_scope.mode === 'connected' ?
                '显示已接入信源及其有据入口；候选内容可在探索视图查看。' : '探索视图包含已记录的候选入口，不代表均已接入。'));
            if (data.structure_gaps?.total) el('monitorCatalogNote').textContent += ' ' + uiT('部分历史关系缺少依据，未作为已核实连线展示。');
        }
        if(!data.structure_scope && scope?.mode==='school_structure')el('monitorCatalogNote').textContent+=' '+uiT('已识别 {included} 个本校结构入口，当前显示 {shown} 个；已排除 {omitted} 个外链、文章或非结构页面。',{included:number(scope.included_nodes),shown:number(data.nodes.length),omitted:number(scope.omitted_nodes)});
        field('monitorProfiles', data.profile_context?.groups?.length
            ? uiT('本校探索画像：') + data.profile_context.groups.map(group => {
                const roles=group.label.split('＋').map(part => window.I18n?.t(part) || part).join(' + ');
                const region=group.education_system === 'global' ? uiT('国际／其他教育体系') : uiT('中国内地高校');
                return roles + ' (' + (window.I18n?.t(region) || region) + ')';
            }).join('、')
            : uiT('尚未设置个人画像，暂沿用已有探索策略；可在基础配置中选择身份。'));
        const max = Math.max(1, ...data.directions.map(item => item.observed));
        el('monitorDirections').replaceChildren(...data.directions.map(item => {
            const row = create('div', undefined, 'monitor-direction');
            const track = create('div', undefined, 'monitor-direction-track'); track.setAttribute('aria-hidden', 'true');
            const fill = create('span'); fill.style.width = (item.observed / max * 100) + '%'; track.append(fill);
            row.append(create('span', window.I18n?.t(item.label) || item.label), track, create('small', uiT('{observed}线索 · {connected}接入',{observed:number(item.observed),connected:number(item.connected)}))); return row;
        }));
        const c = data.cost.totals; const amount = value => data.cost.scope === 'unavailable' ? '—' : number(value);
        el('monitorCost').replaceChildren(...[[uiT('服务商已确认'), amount(c.confirmed_tokens) + ' Token'],
            [uiT('另预留，尚未结算'), amount(c.reserved_tokens) + ' Token'], [uiT('实际发送请求'), amount(c.actual_requests) + uiT(' 次')],
            [uiT('结果或用量待确认'), amount(c.unknown_requests) + uiT(' 次')], [uiT('复用已识别入口'), number(p.ai_cached_routes) + uiT(' 个')]].map(([name, text]) => {
                const row = create('div'); row.append(create('dt', name), create('dd', text)); return row;
            }));
        field('monitorAI', uiT(p.ai_message));
        field('monitorBillingNote', (data.cost.scope === 'unavailable' ? uiT('暂未找到可准确归属的本校用量记录。') : '') +
            uiT('预留额度不计入已确认用量。入口复用与服务商的输入缓存是两项不同统计。') +
            (data.cost.ledger_gap?.counter_mismatch ? uiT(' 历史额度计数与逐次账单有差额，暂不据此计算成本效率。') : ''));
        el('monitorRecent').replaceChildren(...(data.recent.length ? data.recent.map(item => {
            const row = create('li'); const time = create('time', date(item.at)); if (item.at) time.dateTime = item.at;
            const label=item.label_source==='application'?uiT(item.label):item.label;
            row.append(time, item.url ? link(label, item.url) : create('span', label), create('span', uiT(item.detail), 'monitor-recent-detail')); return row;
        }) : [create('li', uiT('暂无任务更新记录'), 'monitor-empty')]));
        const control = el('monitorControl');
        if (control) {
            control.hidden = !p.can_pause && !p.can_resume && p.state !== 'pausing';
            control.dataset.action = p.can_resume ? 'resume' : 'pause';
            control.textContent = p.can_resume ? uiT('继续探索') : p.state === 'pausing' ? uiT('正在暂停…') : uiT('暂停探索');
            control.disabled = controlBusy || p.state === 'pausing';
        }
        tree(Boolean(selectionRemoved)); el('monitorTree').setAttribute('aria-busy', 'false');
        root.dispatchEvent(new CustomEvent('monitor-data', {detail:data}));
    }
    function schedule() {
        clearTimeout(timer);
        if (!stopped && !document.hidden && el('monitorAuto').checked) timer = setTimeout(refresh, Math.min(3000 * 2 ** failures, 30000));
    }
    async function refresh() {
        if (stopped) return;
        if (loading) { refreshQueued = true; return; }
        loading = true; clearTimeout(timer); el('monitorRefresh').disabled = true;
        abort = new AbortController(); const timeout = setTimeout(() => abort.abort(), 12000);
        try {
            const endpoint = new URL(root.dataset.endpoint, location.href);
            endpoint.searchParams.set('scope', el('monitorScope')?.value || 'connected');
            if (data?.structure_token) endpoint.searchParams.set('since',data.structure_token);
            const response = await fetch(endpoint, {cache:'no-store', signal:abort.signal});
            if (!response.ok) {
                if ([401,403,404].includes(response.status)) {
                    data=null; graphSignature=''; root.monitorSnapshot=null;
                    clearSelection(); el('monitorTree').replaceChildren();
                    root.dispatchEvent(new CustomEvent('monitor-data',{detail:{nodes:[],relations:[]}}));
                }
                throw new Error([401,403,404].includes(response.status) ? uiT('登录或订阅状态已变化，请重新打开学校目录。') : uiT('进度服务暂时不可用'));
            }
            let result = await response.json();
            if(result.update_mode==='delta'){
                if(!data || result.base_token!==data.structure_token || result.structure_version.epoch!==data.structure_version.epoch){
                    data=null; refreshQueued=true; return;
                }
                for(const key of ['nodes','relations']){
                    const rows=new Map(data[key].map(item=>[item.id,item]));
                    result.delta[key].remove.forEach(id=>rows.delete(id));
                    result.delta[key].upsert.forEach(item=>rows.set(item.id,item));
                    result[key]=[...rows.values()];
                }
            }
            connectionState = 'ready'; render(result); failures = 0;
            el('monitorError').hidden = true; el('monitorDot').dataset.state = el('monitorAuto').checked ? 'live' : '';
        } catch (error) {
            if (stopped) return;
            failures += 1; el('monitorDot').dataset.state = 'error';
            connectionState = 'error'; connection();
            field('monitorError', (error.name === 'AbortError' ? uiT('读取进度超时。') : uiT(error.message) + ' · ') + uiT('可以点击“立即刷新”重试。')); el('monitorError').hidden = false;
        } finally {
            clearTimeout(timeout); loading = false; abort = null; el('monitorRefresh').disabled = false;
            if (refreshQueued && !stopped) { refreshQueued = false; refresh(); } else schedule();
        }
    }
    function filterChanged() {
        tree(true);
        if (el('monitorSearch').value || el('monitorFilter').value !== 'all') el('atlasListPanel').open = true;
        root.dispatchEvent(new CustomEvent('monitor-filter'));
    }
    el('monitorSearch').addEventListener('input', filterChanged);
    el('monitorFilter').addEventListener('change', filterChanged);
    el('monitorScope')?.addEventListener('change', () => {data=null; graphSignature=''; refresh();});
    el('monitorPathMode')?.addEventListener('change', event => {
        root.dataset.pathMode=event.target.value;
        root.dispatchEvent(new CustomEvent('monitor-path-mode',{detail:{mode:event.target.value}}));
    });
    root.addEventListener('monitor-select-node', event => {
        const node = data?.nodes.find(item => item.id === event.detail.id);
        if (node) choose(node, event.detail.focus !== false);
    });
    root.addEventListener('monitor-clear-selection', clearSelection);
    el('monitorRefresh').addEventListener('click', refresh);
    el('atlasListPanel').addEventListener('toggle',()=>tree(true));
    el('monitorSchool').addEventListener('change', event => { location.href = event.target.value; });
    el('monitorAuto').addEventListener('change', () => {
        clearTimeout(timer); el('monitorDot').dataset.state = '';
        connectionState = 'loading'; connection();
        if (el('monitorAuto').checked) refresh();
    });
    el('monitorControl')?.addEventListener('click', async () => {
        if (controlBusy) return;
        controlBusy = true; el('monitorControl').disabled = true;
        try {
            const response = await fetch(root.dataset.control, {method:'POST', headers:{'Content-Type':'application/json',
                'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
                body:JSON.stringify({action:el('monitorControl').dataset.action}), signal:AbortSignal.timeout(15000)});
            const value = await response.json(); if (!response.ok) throw new Error(value.error || uiT('任务操作未完成'));
            await refresh();
        } catch (error) { field('monitorError', uiT(error.message)); el('monitorError').hidden = false; }
        finally { controlBusy = false; if (el('monitorControl')) el('monitorControl').disabled = data?.progress.state === 'pausing'; }
    });
    document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden && el('monitorAuto').checked) refresh(); });
    window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); abort?.abort(); });
    window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; refresh(); } });
    document.addEventListener('locale-changed', () => { graphSignature=''; if (data) render(data); else connection(); });
    refresh();
})();
