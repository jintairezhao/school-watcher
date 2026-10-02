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
        const total = state?.total || 0, completed = (state?.done || 0) + (state?.failed || 0);
        const scopeLabel = state?.tracking?.scope_label || (scope === 'all' ? '全部订阅' : '本次更新');
        let text = message;
        if (reconnecting) text = '连接中断，正在重连';
        else if (recovering) text = '正在恢复更新进度…';
        else if (sending) text = '正在提交更新…';
        else if (submissionUncertain) text = '本次抓取请求未确认，可重新尝试抓取';
        else if (tracked && total) {
            if (busy) text = `${scopeLabel} · 更新中 ${completed}/${total}`;
            else if (state.failed) text = `${scopeLabel} · ${state.failed} 个来源暂未成功`;
            else if (state.done === total) text = `更新完成 · ${total} 个来源`;
            else text = `${scopeLabel} · 部分来源尚未更新`;
        }
        status.textContent = text || '暂无更新任务';
        status.dataset.state = reconnecting ? 'reconnecting' : busy || sending || recovering ? 'running' : state?.failed ? 'error' : 'idle';
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
            progress.setAttribute('aria-valuetext', !total ? '正在查询更新进度' : reconnecting ? `连接中断，最近确认 ${completed}/${total} 个来源完成` : `${completed}/${total} 个来源完成`);
        }
        const refreshButton = find('#refreshCurrentSources');
        if (refreshButton) {
            refreshButton.disabled = refreshingView;
            refreshButton.setAttribute('aria-busy', String(refreshingView));
        }
        const current = find('#collectCurrentSources'), all = find('#collectAllSources');
        if (current) current.disabled = checking || sending || (!submissionUncertain && busy && scope === 'current');
        if (all) all.disabled = checking || sending || (!submissionUncertain && busy && scope === 'all');
        const sourcesById = new Map((state?.sources || []).map(source => [String(source.id), source]));
        document.querySelectorAll('[data-department]').forEach(input => {
            const source = sourcesById.get(input.value);
            if (!source) return;
            const row = input.closest('.department-option'), label = row?.querySelector('[data-source-status]');
            if (!row) return;
            if (label) {
                label.textContent = source.status_label || '';
                label.hidden = !source.status_label;
                row.title = source.name + (source.status_label ? ' · ' + source.status_label : '') + (source.message ? '：' + source.message : '');
            }
            const count = row.querySelector('.department-count');
            if (count) {
                const saved = Number(count.dataset.savedCount || 0);
                const known = saved > 0 || source.state === 'done' || source.state === 'saved';
                count.textContent = known ? String(saved) : '—';
                count.setAttribute('aria-label', known ? `${saved} 条已保存通知` : '尚未取得通知，数量未知');
            }
        });
        const details = find('#inboxRefreshDetails'), list = find('#inboxRefreshSources');
        if (!details || !list) return;
        details.hidden = false;
        const items = (state?.sources || []).map(source => {
            const item = document.createElement('li');
            const label = {pending: '等待更新', running: '正在更新', done: '已更新', saved: '已保存', failed: '更新失败', unloaded: '尚未采集'}[source.state] || '尚未更新';
            item.textContent = source.name + '：' + label + (source.state === 'done' ? ` · 最近一次新增 ${source.new_count || 0} 条` : '') + (source.message ? ' · ' + source.message : '');
            return item;
        });
        if (reconnecting || (!items.length && message)) {
            const item = document.createElement('li');
            item.textContent = reconnecting ? '暂时无法连接网站，正在自动重新查询。已提交的更新不会因离开页面而取消。' : message;
            items.unshift(item);
        }
        if (submissionUncertain) {
            const item = document.createElement('li');
            item.textContent = '本次抓取请求未确认，可重新尝试抓取。' + submissionReason + ' 下方只显示后台已确认的来源状态。';
            items.unshift(item);
        }
        list.replaceChildren(...items);
    }
    async function request(url, options = {}) {
        let response;
        try { response = await fetch(url, {...options, cache: 'no-store', signal: AbortSignal.timeout(15000)}); }
        catch (_) { throw Object.assign(new Error('暂时无法连接网站。'), {retryable: true}); }
        if (response.status === 401 || response.status === 403) {
            throw Object.assign(new Error('本机会话已失效，请重新打开应用后查看更新。'), {retryable: false});
        }
        let data;
        try { data = await response.json(); }
        catch (_) { throw Object.assign(new Error('暂时无法读取更新进度。'), {retryable: true}); }
        if (!response.ok) throw Object.assign(new Error(data.error || '更新请求未完成，请稍后重试。'),
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
        const signature = data.sources.filter(s => s.new_count > 0 && (s.state === 'done' || s.pages_checked > 0))
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
            if (!data.total && number === generation) { message = '当前范围没有可更新的订阅来源。'; render(); }
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
    document.addEventListener('visibilitychange', () => {
        if (document.hidden) clearTimeout(timer);
        else if (!suspended) recover();
    });
    if (isInbox()) checkDueOnOpen();
    else recover();
})();
