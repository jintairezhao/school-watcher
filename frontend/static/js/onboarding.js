(function () {
    'use strict';
    const root = document.getElementById('discoveryStatus');
    if (!root) return;
    const field = id => document.getElementById(id);
    const endpoint = '/api/subscriptions/' + root.dataset.schoolId + '/discovery';
    let timer, busy = false, dirty = false, choices = null, state = null;
    function updateChoices() {
        const all = document.querySelector('[name="mode"]:checked').value === 'all';
        document.querySelectorAll('[name="department"]').forEach(input => { input.disabled = all; });
        if (field('sourceClearSelection')) field('sourceClearSelection').hidden = all;
    }
    field('sourceClearSelection')?.addEventListener('click', () => {
        dirty = true;
        document.querySelectorAll('[name="department"]').forEach(input => { input.checked = false; });
    });
    document.querySelector('.sources-form').addEventListener('change', event => {
        dirty = true;
        const input = event.target;
        if (input.matches('[name="department"]')) {
            const unit = input.closest('.source-unit');
            if (unit && input.hasAttribute('data-source-unit')) {
                unit.querySelectorAll('[name="department"]').forEach(child => { child.checked = input.checked; });
            }
            if (!input.checked) {
                let parent = unit;
                while (parent) {
                    const selector = parent.querySelector(':scope > summary [data-source-unit]');
                    if (selector) selector.checked = false;
                    parent = parent.parentElement.closest('.source-unit');
                }
            }
        }
        updateChoices();
    });
    function render(data) {
        state = data.state;
        field('discoveryMessage').textContent = data.message;
        field('discoveryProgress').hidden = !data.busy;
        field('discoveryActivity').textContent = data.state === 'pausing' ? '正在暂停' : data.state === 'paused' ? '已暂停'
            : data.state === 'queued' ? '排队中' : data.state === 'retry_wait' ? '等待重试' : data.busy ? '进行中' : '';
        const pause = field('discoveryPause');
        if (pause) {
            pause.hidden = !data.can_pause && !data.can_resume && data.state !== 'pausing';
            pause.dataset.action = data.can_resume ? 'resume' : 'pause';
            pause.textContent = data.can_resume ? '继续' : data.state === 'pausing' ? '正在暂停…' : '暂停';
        }
        field('discoveryCounts').textContent = '已列出 ' + (data.department_count ?? 0) + ' 个部门 · 已接入 ' + (data.verified_source_count ?? 0) + ' 个栏目'
            + (data.processing_count ? ' · 还有 ' + data.processing_count + ' 个页面待处理' : '');
        if (field('discoveryGaps')) {
            const gaps = data.coverage?.gaps || [];
            field('discoveryGaps').hidden = gaps.length === 0;
            field('discoveryGapList').replaceChildren(...gaps.map(gap => {
                const item = document.createElement('li'); item.textContent = gap.reason || '该项尚未完成';
                if (/^https?:\/\//i.test(gap.url || '')) {
                    const link = document.createElement('a'); link.href = gap.url;
                    link.textContent = gap.name || gap.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
                    item.append(document.createTextNode(' · '), link);
                }
                return item;
            }));
        }
        field('discoveryCurrent').textContent = data.state === 'paused' ? '' : data.current_label || '';
        document.querySelector('.source-mode').hidden = data.source_count === 0;
        document.querySelector('.source-save').hidden = data.source_count === 0;
        field('discoveryRetry').hidden = !data.can_retry;
        if (field('discoveryVerify')) field('discoveryVerify').hidden = !data.needs_verification;
        // Preserve unsaved selections while new results arrive.
        if (data.choices_html !== choices && !dirty) {
            field('sourceChoices').innerHTML = data.choices_html;
            choices = data.choices_html;
            updateChoices();
        }
        // A queued task's stored timestamp is from whenever it last did work, so
        // "still working, last update N seconds ago" would be untrue of it.
        if (data.updated_at && data.busy) {
            const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(data.updated_at)) / 1000));
            if (elapsed > 30) field('discoveryActivity').textContent = elapsed + ' 秒前更新';
        }
        return data.active || data.state === 'waiting' || data.state === 'idle';
    }
    async function refresh(method, action) {
        if (busy) return;
        busy = true;
        clearTimeout(timer);
        field('discoveryError').hidden = true;
        field('discoveryRetry').disabled = true;
        if (field('discoveryPause')) field('discoveryPause').disabled = true;
        let again = true;
        try {
            const response = await fetch(endpoint, {method: method || 'GET', headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content,
                ...(action ? {'Content-Type':'application/json'} : {})}, body: action ? JSON.stringify({action}) : undefined, signal: AbortSignal.timeout(15000)});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '无法读取进度');
            again = render(data);
        } catch (error) {
            field('discoveryError').textContent = error.name === 'TimeoutError' ? '连接超时，正在重新确认状态…'
                : (action === 'pause' ? '暂停未成功：' : action === 'resume' ? '继续未成功：' : '进度连接中断：') + error.message;
            field('discoveryError').hidden = false;
            field('discoveryActivity').textContent = '连接中断';
            field('discoveryProgress').hidden = true;
        } finally {
            busy = false;
            field('discoveryRetry').disabled = false;
            if (field('discoveryPause')) field('discoveryPause').disabled = state === 'pausing';
            if (again) timer = setTimeout(() => refresh(), document.hidden ? 10000 : 2500);
        }
    }
    field('discoveryRetry').addEventListener('click', () => refresh('POST'));
    if (field('discoveryPause')) field('discoveryPause').addEventListener('click', () => refresh('POST', field('discoveryPause').dataset.action));
    field('discoveryRefresh').addEventListener('click', () => refresh());
    window.addEventListener('pagehide', () => clearTimeout(timer));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    updateChoices();
    refresh();
})();
