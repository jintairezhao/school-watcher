/* Read-only telemetry; refresh and task controls are intentionally separate. */
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
    let abort = null, controlBusy = false, refreshQueued = false, treeOpen = null, showingTree = false;
    function field(id, text) { el(id).textContent = text; }
    function detail(node) {
        if (!node) return;
        const box = el('monitorDetail');
        const dl = create('dl');
        for (const [name, value] of [[uiT('状态'), uiT(node.state_label) + ' · ' + uiT(node.reason)],
            [uiT('入口类型'), uiT(node.kind_label)], [uiT('导航路径'), node.path.length ? node.path.join(' / ') : uiT('路径尚未记录，单独展示此入口')],
            [uiT('最近读取'), date(node.checked_at)], [uiT('网页响应'), node.status_code ? 'HTTP ' + node.status_code : uiT('尚无响应记录')]]) {
            dl.append(create('dt', name), create('dd', value));
        }
        const badge = create('p', uiT(node.state_label), 'atlas-node-state'); badge.dataset.state = node.state;
        box.replaceChildren(badge, create('h3', node.label));
        if (node.url) { const action = link(uiT('打开官网入口'), node.url); action.className = 'atlas-source-action'; box.append(action); }
        box.append(dl);
        box.append(create('p', node.url, 'monitor-caption'));
        box.append(create('p', node.cost ? uiT('此入口已确认{used} Token；另预留{reserved} Token。',
            {used:number(node.cost.confirmed_tokens),reserved:number(node.cost.reserved_tokens)}) : uiT('此入口尚无可准确归属的 AI 用量记录。'), 'monitor-caption'));
        if (node.sources.length) {
            box.append(create('h3', uiT('已接入栏目')));
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
    function nodeButton(node) {
        const button = create('button', undefined, 'monitor-node'); button.type = 'button';
        button.dataset.nodeId = node.id; button.dataset.state = node.state;
        button.setAttribute('aria-pressed', String(selected === node.id));
        button.append(create('span', node.label, 'monitor-node-name'), create('span', window.I18n?.t(node.state_label) || node.state_label, 'monitor-node-status'));
        button.addEventListener('click', () => choose(data.nodes.find(item => item.id === node.id) || node, true)); return button;
    }
    function tree(force = false) {
        if (!data) return;
        const query = el('monitorSearch').value.trim().toLocaleLowerCase();
        const state = el('monitorFilter').value;
        const signature = JSON.stringify([data.nodes, query, state]);
        if (!force && graphSignature === signature) return;
        // Incoming progress must not remove the control a keyboard user is on.
        if (!force && el('monitorTree').contains(document.activeElement)) {
            detail(data.nodes.find(item => item.id === selected)); return;
        }
        graphSignature = signature;
        const box = el('monitorTree'); const scroll = box.scrollTop;
        if (showingTree) treeOpen = new Set([...box.querySelectorAll('details[open]')].map(item => item.dataset.path));
        const opened = treeOpen;
        const nodes = data.nodes.filter(node => (state === 'all' || node.state === state) &&
            (!query || [node.label, ...node.path].join(' ').toLocaleLowerCase().includes(query)));
        if (!nodes.length) {
            box.replaceChildren(create('p', query || state !== 'all' ? uiT('没有匹配的入口，请更换名称或状态。') : uiT('还没有已记录的官网路径。启动探索后，这里会显示真实入口。'), 'monitor-empty')); return;
        }
        const list = create('ul');
        showingTree = !query && state === 'all';
        if (query || state !== 'all') {
            for (const node of nodes) {
                const item = create('li'); item.append(nodeButton(node));
                if (node.path.length) item.append(create('p', node.path.join(' / '), 'monitor-caption'));
                list.append(item);
            }
        } else {
            const rootGroup = {children:new Map(), nodes:[]};
            for (const node of nodes) {
                let target = rootGroup;
                const segments = [...node.path];
                if (segments.at(-1) === node.label) segments.pop();
                if (!segments.length && node.kind !== 'root') segments.push(uiT('导航路径未记录'));
                segments.push(node.label);
                for (const part of segments) {
                    if (!target.children.has(part)) target.children.set(part, {children:new Map(), nodes:[]});
                    target = target.children.get(part);
                }
                target.nodes.push(node);
            }
            function appendGroups(parent, groups, path = []) {
                for (const [name, value] of groups) {
                    const item = create('li');
                    if (value.children.size || value.nodes.length > 1) {
                        const key = JSON.stringify([...path, name]);
                        const group = create('details'); group.dataset.path = key;
                        group.open = opened ? opened.has(key) : path.length < 3;
                        const summary = create('summary', name); const children = create('ul');
                        if (value.nodes.length === 1) {
                            const node = value.nodes[0]; summary.dataset.nodeId = node.id;
                            summary.append(create('small', uiT(node.state_label)));
                            summary.addEventListener('click', () => choose(data.nodes.find(item => item.id === node.id) || node, true));
                        } else for (const node of value.nodes) { const leaf = create('li'); leaf.append(nodeButton(node)); children.append(leaf); }
                        appendGroups(children, value.children, [...path, name]);
                        group.append(summary, children); item.append(group);
                    } else for (const node of value.nodes) item.append(nodeButton(node));
                    parent.append(item);
                }
            }
            appendGroups(list, rootGroup.children);
        }
        box.replaceChildren(list); box.scrollTop = scroll;
        const node = data.nodes.find(item => item.id === selected);
        if (node) choose(node);
    }
    function render(value) {
        data = value; root.monitorSnapshot = value; const p = data.progress;
        field('monitorMessage', uiT(p.message));
        field('monitorCurrent', p.current_label ? uiT('正在处理：{name}',{name:p.current_label}) : uiT(p.stage_message));
        field('monitorWorkAt', uiT('任务最近进展：{date} · {state}',{date:date(p.updated_at),state:p.state === 'paused' ? uiT('探索已暂停') :
            p.state === 'pausing' ? uiT('正在等待当前任务结束') : data.tasks.some(task => task.state === 'running') ? uiT('任务进行中') : uiT('当前没有正在执行的探索')}));
        field('monitorChecked', p.updated_at ? number(p.checked_pages) : '—');
        field('monitorPending', p.updated_at ? number(p.pending_pages) : '—');
        field('monitorCandidates', number(p.column_tasks)); field('monitorConnected', number(p.verified_source_count));
        field('monitorCatalogNote', data.catalog.unavailable ? uiT('暂时读不到已保存的路径，其他任务数据仍可查看。') :
            uiT('结构快照：{date}；正在处理的入口来自实时任务。',{date:date(data.catalog.published_at)}) + (data.catalog.truncated ? uiT('当前显示前 1,200 个入口，完整结构请打开“官网结构”。') : ''));
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
            row.append(time, item.url ? link(item.label, item.url) : create('span', item.label), create('span', uiT(item.detail), 'monitor-recent-detail')); return row;
        }) : [create('li', uiT('暂无任务更新记录'), 'monitor-empty')]));
        const control = el('monitorControl');
        if (control) {
            control.hidden = !p.can_pause && !p.can_resume && p.state !== 'pausing';
            control.dataset.action = p.can_resume ? 'resume' : 'pause';
            control.textContent = p.can_resume ? uiT('继续探索') : p.state === 'pausing' ? uiT('正在暂停…') : uiT('暂停探索');
            control.disabled = controlBusy || p.state === 'pausing';
        }
        tree(); el('monitorTree').setAttribute('aria-busy', 'false');
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
            const response = await fetch(root.dataset.endpoint, {cache:'no-store', signal:abort.signal});
            if (!response.ok) throw new Error(response.status === 403 || response.status === 401 ? uiT('登录或订阅状态已变化，请重新打开学校目录。') : uiT('进度服务暂时不可用'));
            const result = await response.json(); render(result); failures = 0;
            el('monitorError').hidden = true; el('monitorDot').dataset.state = el('monitorAuto').checked ? 'live' : '';
            field('monitorConnection', el('monitorAuto').checked ? uiT('每 3 秒同步实际进度') : uiT('自动刷新已关闭，探索任务不受影响'));
            field('monitorReadAt', uiT('数据读取于{date}',{date:date(result.observed_at)}));
        } catch (error) {
            if (stopped) return;
            failures += 1; el('monitorDot').dataset.state = 'error';
            field('monitorConnection', data ? uiT('连接中断，保留上次数据') : uiT('暂时无法读取进度'));
            field('monitorError', (error.name === 'AbortError' ? uiT('读取进度超时。') : error.message + '。') + uiT('可以点击“立即刷新”重试。')); el('monitorError').hidden = false;
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
    root.addEventListener('monitor-select-node', event => {
        const node = data?.nodes.find(item => item.id === event.detail.id) || event.detail.node;
        if (node) choose(node, event.detail.focus !== false);
    });
    root.addEventListener('monitor-clear-selection', () => {
        selected=null;
        el('monitorTree').querySelectorAll('[aria-pressed]').forEach(button=>button.setAttribute('aria-pressed','false'));
        el('monitorDetail').replaceChildren(create('p',uiT('选择左侧入口，查看路径、状态和接入结果。'),'monitor-empty'));
    });
    el('monitorRefresh').addEventListener('click', refresh);
    el('monitorSchool').addEventListener('change', event => { location.href = event.target.value; });
    el('monitorAuto').addEventListener('change', () => {
        clearTimeout(timer); el('monitorDot').dataset.state = '';
        field('monitorConnection', el('monitorAuto').checked ? uiT('正在恢复自动刷新…') : uiT('自动刷新已关闭，探索任务不受影响'));
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
        } catch (error) { field('monitorError', error.message); el('monitorError').hidden = false; }
        finally { controlBusy = false; if (el('monitorControl')) el('monitorControl').disabled = data?.progress.state === 'pausing'; }
    });
    document.addEventListener('visibilitychange', () => { clearTimeout(timer); if (!document.hidden && el('monitorAuto').checked) refresh(); });
    window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); abort?.abort(); });
    window.addEventListener('pageshow', event => { if (event.persisted) { stopped = false; refresh(); } });
    document.addEventListener('locale-changed', () => { graphSignature=''; if (data) render(data); });
    refresh();
})();
