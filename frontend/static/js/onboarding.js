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
    }
    document.querySelector('.sources-form').addEventListener('change', () => { dirty = true; updateChoices(); });
    function render(data) {
        state = data.state;
        field('discoveryMessage').textContent = data.message;
        field('discoveryProgress').hidden = !data.active;
        field('discoveryActivity').textContent = data.state === 'pausing' ? '正在暂停' : data.state === 'paused' ? '已暂停' : data.active ? '进行中' : '';
        const pause = field('discoveryPause');
        if (pause) {
            pause.hidden = !data.can_pause && !data.can_resume && data.state !== 'pausing';
            pause.dataset.action = data.can_resume ? 'resume' : 'pause';
            pause.textContent = data.can_resume ? '继续' : data.state === 'pausing' ? '正在暂停…' : '暂停';
        }
        field('discoveryCounts').textContent = '已检查 ' + data.checked_pages + ' 页 · 已接入 ' + data.source_count + ' 项'
            + (data.review_count ? ' · 待核实 ' + data.review_count + ' 项' : '')
            + (data.pending_pages ? ' · 待检查 ' + data.pending_pages + ' 页' : '');
        if (field('discoveryReview')) field('discoveryReview').hidden = !data.review_count;
        field('discoveryCurrent').textContent = data.state === 'paused' ? '' : data.current_label || '';
        document.querySelector('.source-mode').hidden = data.source_count === 0;
        document.querySelector('.source-save').hidden = data.source_count === 0;
        field('discoveryAI').textContent = data.state === 'paused' ? '' : data.ai_message || (data.ai_available ? 'AI 辅助已开启' : '开启 AI 辅助，识别学校部门与栏目');
        if (field('discoverySetup')) field('discoverySetup').hidden = data.ai_available;
        field('discoveryRetry').hidden = !data.ai_available || !data.can_retry;
        if (field('discoveryVerify')) field('discoveryVerify').hidden = !data.needs_verification;
        // Preserve unsaved selections while new results arrive.
        if (data.choices_html !== choices && !dirty) {
            field('sourceChoices').innerHTML = data.choices_html;
            choices = data.choices_html;
            updateChoices();
        }
        if (data.updated_at && data.active && data.state !== 'pausing') {
            const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(data.updated_at)) / 1000));
            if (elapsed > 30) field('discoveryActivity').textContent = '仍在处理 · ' + elapsed + ' 秒前更新';
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
