/* Recent history loads completely in small pages; archived rows load only on demand. */
(function () {
    const root = document.querySelector('.scrape-log-page');
    if (!root) return;
    const find = selector => root.querySelector(selector);
    let loaded = false, generation = 0, anchor = '', retentionLoaded = false;
    let states = {};
    const format = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit',
        day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
    function element(tag, text, className) {
        const node = document.createElement(tag);
        if (text != null) node.textContent = text;
        if (className) node.className = className;
        return node;
    }
    function time(value) { return value ? format.format(new Date(value)) : '尚未结束'; }
    function duration(seconds) {
        if (seconds == null) return '—';
        if (seconds < 60) return seconds + ' 秒';
        if (seconds < 3600) return Math.floor(seconds / 60) + ' 分 ' + Math.round(seconds % 60) + ' 秒';
        return Math.floor(seconds / 3600) + ' 小时 ' + Math.floor(seconds % 3600 / 60) + ' 分';
    }
    async function request(url, options = {}) {
        const response = await fetch(url, {...options, signal: AbortSignal.timeout(15000)});
        let data;
        try { data = await response.json(); } catch (_) { throw new Error('记录未能加载，请稍后重试。'); }
        if (!response.ok) throw new Error(data.error || '操作未完成，请稍后重试。');
        return data;
    }
    function newState(period) {
        return {period, cursor: null, loaded: false, busy: false, count: 0, groups: new Map(), ids: new Set()};
    }
    function appendRow(state, record) {
        if (state.ids.has(record.id)) return;
        state.ids.add(record.id); state.count++;
        const key = record.school_id ?? 'unknown';
        let group = state.groups.get(key);
        if (!group) {
            const section = element('section', null, 'scrape-log-school');
            section.dataset.schoolId = key;
            const heading = element('h4', record.school_name), count = element('span', '');
            heading.append(count); section.append(heading);
            const table = element('table', null, 'scrape-log-table');
            table.append(element('caption', record.school_name + '的抓取记录', 'sr-only'));
            const head = element('thead'), tr = element('tr');
            ['开始 / 结束时间', '学院或栏目', '结果', '新增 / 检查', '耗时'].forEach(text => {
                const th = element('th', text); th.scope = 'col'; tr.append(th);
            });
            head.append(tr); table.append(head);
            const body = element('tbody'); table.append(body); section.append(table);
            find(state.period === 'recent' ? '#recentLogSchools' : '#archiveLogSchools').append(section);
            group = {body, count, size: 0}; state.groups.set(key, group);
        }
        group.count.textContent = ' · ' + (++group.size) + ' 条';
        const row = element('tr'); row.dataset.logId = record.id;
        const times = element('td', null, 'scrape-log-times');
        const started = element('time', time(record.started_at)); started.dateTime = record.started_at || '';
        times.append(started, element('small', '结束：' + time(record.finished_at)));
        const source = element('td', null, 'scrape-log-source');
        source.append(element('strong', record.source_name), element('small', '记录 #' + record.id));
        if (record.error_message) source.append(element('p', record.error_message, 'scrape-log-message'));
        const status = element('td', null, 'scrape-log-result');
        const badge = element('span', record.status_label, 'scrape-log-badge');
        badge.dataset.status = record.status; status.append(badge);
        const counts = element('td', null, 'scrape-log-counts');
        counts.append(element('span', '新增 ' + (record.new_count || 0)), element('span', '检查 ' + (record.total_count || 0)));
        const elapsed = element('td', '耗时 ' + duration(record.duration_seconds), 'scrape-log-duration');
        row.append(times, source, status, counts, elapsed); group.body.append(row);
    }
    async function loadPages(period, all = false) {
        const state = states[period];
        if (!state || state.busy || (state.loaded && !state.cursor)) return;
        if (period === 'archive' && !anchor) {
            find('#archiveLogStatus').textContent = '正在确认记录的时间范围…';
            return;
        }
        state.busy = true;
        const number = generation;
        const status = find(period === 'recent' ? '#recentLogStatus' : '#archiveLogStatus');
        const more = find(period === 'recent' ? '#retryRecentLogs' : '#loadMoreArchiveLogs');
        more.hidden = true;
        try {
            do {
                status.textContent = state.count ? `已显示 ${state.count} 条，正在继续加载…` : '正在加载记录…';
                const params = new URLSearchParams({period});
                if (anchor) params.set('as_of', anchor);
                if (find('#logSchoolFilter').value) params.set('school_id', find('#logSchoolFilter').value);
                if (state.cursor) Object.entries(state.cursor).forEach(([key, value]) => params.set(key, value));
                const data = await request('/api/admin/scrape-logs?' + params);
                if (number !== generation) return;
                anchor = data.as_of;
                data.rows.forEach(row => appendRow(state, row));
                state.cursor = data.next_cursor; state.loaded = true;
                find(period === 'recent' ? '#recentLogCount' : '#archiveLogCount').textContent = `· ${data.total} 条`;
                if (period === 'recent') {
                    find('#archiveLogCount').textContent = `· ${data.older_total} 条`;
                    if (find('#archiveLogSection').open && !states.archive.loaded) loadPages('archive');
                }
                status.textContent = data.total ? (state.cursor ? `已显示 ${state.count} / ${data.total} 条记录。` : `已完整显示 ${state.count} 条记录。`) : '这个时间范围暂无记录。';
                if (!all || !state.cursor) break;
            } while (number === generation);
            more.hidden = !state.cursor;
            more.textContent = period === 'recent' ? '继续加载记录' : '加载更早记录';
        } catch (error) {
            if (number !== generation) return;
            status.textContent = (state.count ? `已保留 ${state.count} 条已加载记录。` : '') + error.message;
            more.textContent = '重试加载'; more.hidden = false;
        } finally {
            state.busy = false;
        }
    }
    async function loadRetention() {
        try {
            const data = await request('/api/admin/scrape-logs/retention');
            find('#logRetentionDays').value = String(data.days); retentionLoaded = true;
            find('#logRetentionStatus').textContent = '';
        } catch (error) { find('#logRetentionStatus').textContent = error.message; }
    }
    function reload() {
        ++generation; anchor = ''; states = {recent: newState('recent'), archive: newState('archive')};
        find('#recentLogSchools').replaceChildren(); find('#archiveLogSchools').replaceChildren();
        find('#recentLogCount').textContent = ''; find('#archiveLogCount').textContent = '';
        find('#retryRecentLogs').hidden = true; find('#loadMoreArchiveLogs').hidden = true;
        find('#archiveLogSection').open = false;
        find('#archiveLogStatus').textContent = '展开后加载较早记录。';
        loadPages('recent', true);
    }
    window.loadScrapeLogHistory = () => {
        if (!loaded) { loaded = true; reload(); }
        if (!retentionLoaded) loadRetention();
    };
    find('#reloadScrapeLogs').addEventListener('click', reload);
    find('#logSchoolFilter').addEventListener('change', reload);
    find('#retryRecentLogs').addEventListener('click', () => loadPages('recent', true));
    find('#loadMoreArchiveLogs').addEventListener('click', () => loadPages('archive'));
    find('#archiveLogSection').addEventListener('toggle', event => {
        if (event.target.open) loadPages('archive');
    });
    find('#logRetentionForm').addEventListener('submit', async event => {
        event.preventDefault(); const button = find('#saveLogRetention'); button.disabled = true;
        const days = Number(find('#logRetentionDays').value);
        find('#logRetentionStatus').textContent = '正在保存…';
        try {
            const data = await request('/api/admin/scrape-logs/retention', {method: 'PUT',
                headers: {'Content-Type': 'application/json'}, body: JSON.stringify({days})});
            retentionLoaded = true;
            find('#logRetentionDays').value = String(data.days);
            find('#logRetentionStatus').textContent = data.days === 0 ? '已设置为永久保留记录。' : data.message;
        } catch (error) { find('#logRetentionStatus').textContent = '未保存。' + error.message; }
        finally { button.disabled = false; }
    });
})();
