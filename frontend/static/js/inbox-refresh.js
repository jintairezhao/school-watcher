/* Progress is restored from durable, user-scoped server tasks on every return. */
(function () {
    if (!document.querySelector('.inbox-workspace')) return;
    const find = selector => document.querySelector(selector);
    const isInbox = () => find('.inbox-workspace')?.dataset.view === 'inbox';
    const canCollect = () => find('.inbox-workspace')?.dataset.canCollect === 'true';
    const resumeURL = '/api/inbox/refresh?resume=1';
    let generation = 0, timer, state = null, busy = false, sending = false;
    let tracked = false, reconnecting = false, recovering = false, suspended = false;
    let retryCount = 0, scope = 'current', message = '', lastScope = scopeKey(), completedSignature = '';
    let checking = false, refreshingView = false;
    let submissionUncertain = false, submissionReason = '';

    function scopeKey() {
        const p = new URL(location.href).searchParams;
        return JSON.stringify([p.get('view') || 'inbox', p.get('school'), p.get('group'), [...new Set(p.getAll('dept'))].sort()]);
    }
    function payload(mode) {
        const p = new URL(location.href).searchParams;
        return mode === 'all' ? {scope: 'all'} : {scope: 'current',
            school_id: p.get('school') ? Number(p.get('school')) : null,
            department_ids: [...new Set(p.getAll('dept'))].map(Number), group: p.get('group') || ''};
    }
    function statusURL(data) {
        const p = new URLSearchParams({scope: data.scope});
        if (data.school_id) p.set('school', data.school_id);
        if (data.group) p.set('group', data.group);
        (data.department_ids || []).forEach(id => p.append('dept', id));
        return '/api/inbox/refresh?' + p;
    }
    function render() {
        const status = find('#inboxRefreshStatus');
        if (!status) return;
        const total = state?.total || 0;
        const outside = state?.outside_batch || [];
        const accounting = state?.scope_summary;
        const completed = state?.processed ?? ((state?.done || 0) + (state?.failed || 0) + (state?.incomplete || 0));
        const scopeLabel = uiT(state?.tracking?.scope_label || (scope === 'all' ? uiT('全部订阅') : uiT('本次更新')));
        let text = message;
        if (reconnecting) text = uiT('连接中断，正在重连');
        else if (recovering) text = uiT('正在恢复更新进度…');
        else if (sending) text = uiT('正在提交更新…');
        else if (submissionUncertain) text = uiT('本次抓取请求未确认，可重新尝试抓取');
        else if (tracked && total) {
            if (busy) text = uiT('{scope} · 本轮更新 {completed}/{total}', {scope:scopeLabel, completed, total});
            else if (state.incomplete) text = uiT('{scope} · {done}个已更新，{count}个仍待处理',
                {scope:scopeLabel,done:state.done || 0,count:state.incomplete + (state.failed || 0)});
            else if (state.failed) text = uiT("{p0} · {p1} 个来源暂未成功", {p0:(scopeLabel), p1:(state.failed)});
            else if (state.done === total) text = outside.length
                ? uiT('本轮结束 · {count} 个来源已更新', {count:total})
                : uiT("更新完成 · {p0} 个来源", {p0:(total)});
            else text = uiT("{p0} · 部分来源尚未更新", {p0:(scopeLabel)});
        }
        else if (tracked && accounting?.total) text = uiT('本轮未安排新的抓取任务');
        if (tracked && outside.length && !reconnecting && !recovering && !sending && !submissionUncertain) {
            text += uiT('；{count} 个未参加本轮', {count:outside.length});
        }
        status.textContent = uiT(text) || uiT('暂无更新任务');
        status.dataset.state = reconnecting ? 'reconnecting' : busy || sending || recovering ? 'running' : state?.failed || state?.incomplete ? 'error' : 'idle';
        const activity = find('#inboxRefreshActivity');
        if (activity) activity.hidden = !(tracked || sending || recovering || reconnecting || submissionUncertain || message);
        const progress = find('#inboxRefreshProgress');
        if (progress) {
            progress.hidden = !(busy || sending || recovering || reconnecting);
            progress.classList.toggle('is-indeterminate', !total);
            progress.style.setProperty('--progress', total ? Math.min(100, completed / total * 100) : 0);
            progress.setAttribute('aria-valuemin', '0');
            progress.setAttribute('aria-valuemax', String(Math.max(1, total)));
            if (total) progress.setAttribute('aria-valuenow', String(Math.min(total, completed)));
            else progress.removeAttribute('aria-valuenow');
            progress.setAttribute('aria-valuetext', !total ? uiT('正在查询更新进度') : reconnecting
                ? uiT('连接中断，最近确认已检查{completed}/{total}个来源', {completed,total})
                : uiT('本轮已检查{completed}/{total}个来源', {completed,total}));
        }
        const refreshButton = find('#refreshCurrentSources');
        if (refreshButton) {
            refreshButton.disabled = refreshingView;
            refreshButton.setAttribute('aria-busy', String(refreshingView));
        }
        const current = find('#collectCurrentSources'), all = find('#collectAllSources');
        if (current) current.disabled = checking || sending || (!submissionUncertain && busy && scope === 'current');
        if (all) all.disabled = checking || sending || (!submissionUncertain && busy && scope === 'all');
        const sourcesById = new Map([...outside, ...(state?.sources || [])].map(source => [String(source.id), source]));
        document.querySelectorAll('[data-department]').forEach(input => {
            const source = sourcesById.get(input.value);
            if (!source) return;
            const row = input.closest('.department-option'), label = row?.querySelector('[data-source-status]');
            if (!row) return;
            if (label) {
                const statusKey=row.dataset.sourceFixedStatus||source.status_label||'';
                label.dataset.i18n=statusKey;label.textContent = uiT(statusKey);
                label.hidden = !statusKey;
                row.title = (row.dataset.sourceName||source.name) + (statusKey ? ' · ' + uiT(statusKey) : '') + (source.message ? ': ' + uiT(source.message) : '');
            }
            const count = row.querySelector('.department-count');
            if (count) {
                const saved = Number(count.dataset.savedCount || 0);
                const known = saved > 0 || source.state === 'done' || source.state === 'saved';
                count.textContent = known ? String(saved) : '—';
                count.setAttribute('aria-label', known ? uiT('{saved} 条已保存通知',{saved}) : uiT('尚未取得通知，数量未知'));
                count.dataset.i18nAriaLabel=known?'{saved} 条已保存通知':'尚未取得通知，数量未知';
                count.setAttribute('data-i18n-aria-label-vars',JSON.stringify(known?{saved}:{}));
            }
        });
        const details = find('#inboxRefreshDetails'), list = find('#inboxRefreshSources');
        if (!details || !list) return;
        details.hidden = false;
        const scopeInfo = find('#inboxRefreshScope');
        if (scopeInfo) {
            scopeInfo.hidden = !tracked || !accounting;
            scopeInfo.replaceChildren(...(accounting?.schools || []).flatMap(school => {
                const line = document.createElement('p');
                line.textContent = uiT('{school}：已启用 {configured} 个，本轮处理其中 {batch} 个',
                    {school:school.name,configured:school.configured,batch:school.batch_configured});
                const pending = school.total - school.configured;
                if (!pending) return [line];
                const extra = document.createElement('p');
                extra.textContent = uiT('另有 {count} 个待识别入口，本轮处理其中 {batch} 个',
                    {count:pending,batch:school.batch_total-school.batch_configured});
                return [line, extra];
            }));
        }
        const outsideDetails = find('#inboxRefreshOutside'), outsideList = find('#inboxRefreshOutsideSources');
        if (outsideDetails && outsideList) {
            outsideDetails.hidden = !tracked || !outside.length;
            find('#inboxRefreshOutsideSummary').textContent = uiT('查看 {count} 个未参加本轮的来源', {count:outside.length});
            const reasons = {recent_success:uiT('近期已更新，暂不重复抓取'), recent_attempt:uiT('近期尝试过，等待下次检查'),
                retry_delay:uiT('暂未到重试时间'), unrecorded:uiT('旧批次未记录未参加原因')};
            outsideList.replaceChildren(...outside.map(source => {
                const row = document.createElement('li');
                const reason = reasons[source.admission_reason] || reasons.unrecorded;
                const latest = source.status_label ? uiT(source.status_label)
                    : uiT({done:'已更新',saved:'已保存',unloaded:'尚未采集'}[source.state] || '尚未更新');
                row.textContent = uiT('{school} / {source}：{reason}。最近状态：{status}',
                    {school:source.school_name,source:source.name,reason,status:latest});
                return row;
            }));
        }
        const items = (state?.sources || []).map(source => {
            const item = document.createElement('li');
            const label = source.status_label ? uiT(source.status_label)
                : {pending: uiT('等待更新'), running: uiT('正在更新'), done: uiT('已更新'), saved: uiT('已保存'), failed: uiT('更新失败'), unloaded: uiT('尚未采集')}[source.state] || uiT('尚未更新');
            item.textContent = source.name + ': ' + label + (source.state === 'done' ? ' · ' + uiT('最近一次新增 {count} 条', {count:source.new_count || 0}) : '') + (source.message ? ' · ' + uiT(source.message) : '');
            return item;
        });
        if (tracked) {
            const note = document.createElement('li');
            note.textContent = uiT('以下为本轮更新任务，不代表学校全部来源。');
            items.unshift(note);
        }
        if (reconnecting || (!items.length && message)) {
            const item = document.createElement('li');
            item.textContent = reconnecting ? uiT('暂时无法连接网站，正在自动重新查询。已提交的更新不会因离开页面而取消。') : message;
            items.unshift(item);
        }
        if (submissionUncertain) {
            const item = document.createElement('li');
            item.textContent = uiT('本次抓取请求未确认，可重新尝试抓取。') + submissionReason + uiT(' 下方只显示后台已确认的来源状态。');
            items.unshift(item);
        }
        list.replaceChildren(...items);
    }
    async function request(url, options = {}) {
        let response;
        try { response = await fetch(url, {...options, cache: 'no-store', signal: AbortSignal.timeout(15000)}); }
        catch (_) { throw Object.assign(new Error(uiT('暂时无法连接网站。')), {retryable: true}); }
        if (response.status === 401 || response.status === 403) {
            throw Object.assign(new Error(uiT('本机会话已失效，请重新打开应用后查看更新。')), {retryable: false});
        }
        let data;
        try { data = await response.json(); }
        catch (_) { throw Object.assign(new Error(uiT('暂时无法读取更新进度。')), {retryable: true}); }
        if (!response.ok) throw Object.assign(new Error(data.error || uiT('更新请求未完成，请稍后重试。')),
            {retryable: response.status === 408 || response.status === 429 || response.status >= 500});
        return data;
    }
    function schedule(number, delay) {
        clearTimeout(timer);
        if (!suspended && !document.hidden) timer = setTimeout(() => poll(number), delay);
    }
    async function accept(data, number, updateList, display = true) {
        if (number !== generation) return;
        state = data; busy = data.active > 0; tracked = display && !!data.tracked;
        scope = data.tracking?.scope || scope;
        checking = sending = recovering = reconnecting = false; retryCount = 0; message = '';
        render();
        const signature = data.sources.filter(s => s.new_count > 0 && (s.state === 'done' || s.pages_checked > 0 || s.partial === true))
            .map(s => s.state === 'done' ? s.id + ':' + s.updated_at : s.id + ':partial:' + s.new_count).join('|');
        const changed = signature !== completedSignature;
        completedSignature = signature;
        if (updateList && signature && changed) {
            window.notifyInboxUpdates?.();
        }
        if (busy) schedule(number, 2000);
    }
    function failed(error, number) {
        if (number !== generation) return;
        checking = sending = recovering = false;
        if (error.retryable !== false) {
            reconnecting = true;
            render();
            schedule(number, Math.min(15000, 2000 * 2 ** Math.min(retryCount++, 3)));
        } else {
            state = null; busy = tracked = reconnecting = false;
            submissionUncertain = false; submissionReason = '';
            message = error.message; render();
        }
    }
    async function poll(number) {
        try { await accept(await request(resumeURL), number, true); }
        catch (error) { failed(error, number); }
    }
    async function recover() {
        // A return to a visible tab must not invalidate its in-flight POST and
        // replace that response with an earlier, still-empty status snapshot.
        if (sending || checking) return;
        clearTimeout(timer); const number = ++generation;
        recovering = true; suspended = false; render();
        try {
            const data = await request(resumeURL);
            if (number !== generation) return;
            if (!data.tracked && isInbox()) {
                await accept(await request(statusURL(payload('current'))), number, false, false);
            } else await accept(data, number, true);
        } catch (error) { failed(error, number); }
    }
    async function checkDueOnOpen() {
        // Only a document open checks freshness. The server enforces the shared
        // interval; filters and the refresh button only read saved information.
        clearTimeout(timer); const number = ++generation;
        checking = true; render();
        try {
            const data = await request('/api/inbox/sync', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({scope: 'all'})});
            await accept(data, number, !!data.scheduled);
        } catch (error) { failed(error, number); }
    }
    async function refreshView() {
        if (!isInbox() || refreshingView) return;
        refreshingView = true; render();
        try {
            await window.refreshInboxView?.();
            await recover();
        } finally { refreshingView = false; render(); }
    }
    async function collect(mode = 'current') {
        if (!isInbox() || !canCollect() || sending || checking) return;
        clearTimeout(timer); const number = ++generation;
        sending = true; recovering = reconnecting = false; message = ''; render();
        try {
            const data = await request('/api/inbox/refresh', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload(mode))});
            if (number === generation) { submissionUncertain = false; submissionReason = ''; }
            await accept(data, number, true);
            if (!data.total && number === generation) { message = uiT('当前范围没有可更新的订阅来源。'); render(); }
        } catch (error) {
            if (number === generation && error.retryable !== false) {
                submissionUncertain = true; submissionReason = error.message;
            }
            failed(error, number);
        }
    }
    document.addEventListener('click', event => {
        if (event.target.closest('#refreshCurrentSources, #inboxNewNotices')) refreshView();
        else if (event.target.closest('#collectCurrentSources')) collect();
        else if (event.target.closest('#collectAllSources')) collect('all');
    });
    window.addEventListener('inbox:navigated', () => {
        const next = scopeKey();
        if (next !== lastScope) {
            lastScope = next;
            recover();
        } else render();
    });
    window.addEventListener('pagehide', () => { clearTimeout(timer); ++generation; suspended = true; checking = sending = refreshingView = false; });
    window.addEventListener('pageshow', event => { if (event.persisted) recover(); });
    window.addEventListener('online', () => recover());
    document.addEventListener('locale-changed',render);
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) clearTimeout(timer);
        else if (!suspended) recover();
    });
    if (isInbox()) checkDueOnOpen();
    else recover();
})();
