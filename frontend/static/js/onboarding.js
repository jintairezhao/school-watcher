(function () {
    'use strict';
    const root = document.getElementById('discoveryStatus');
    if (!root) return;
    const field = id => document.getElementById(id);
    const endpoint = '/api/subscriptions/' + root.dataset.schoolId + '/discovery';
    let timer, busy = false, dirty = false, choices = null;
    function updateChoices() {
        const all = document.querySelector('[name="mode"]:checked').value === 'all';
        document.querySelectorAll('[name="department"]').forEach(input => { input.disabled = all; });
    }
    document.querySelector('.sources-form').addEventListener('change', () => { dirty = true; updateChoices(); });
    function render(data) {
        field('discoveryMessage').textContent = data.message;
        field('discoveryProgress').hidden = !data.active;
        field('discoveryActivity').textContent = data.active ? '进行中' : '';
        field('discoveryCounts').textContent = '已检查 ' + data.checked_pages + ' 页 · ' + data.source_count + ' 个部门与栏目' + (data.pending_pages ? ' · 待检查 ' + data.pending_pages + ' 页' : '');
        field('discoveryCurrent').textContent = data.current_label || '';
        document.querySelector('.source-mode').hidden = data.source_count === 0;
        document.querySelector('.source-save').hidden = data.source_count === 0;
        const aiText = {running: 'AI 正在分析官网', succeeded: 'AI 辅助已完成本批识别', failed: 'AI 暂不可用，继续自动识别', uncertain: 'AI 调用结果待核实，继续自动识别'};
        field('discoveryAI').textContent = aiText[data.ai_state] || (data.ai_available ? 'AI 辅助已开启' : '开启 AI 辅助，识别学校部门与栏目');
        if (field('discoverySetup')) field('discoverySetup').hidden = data.ai_available;
        field('discoveryRetry').hidden = !data.ai_available || !data.can_retry;
        if (field('discoveryVerify')) field('discoveryVerify').hidden = !data.needs_verification;
        // Preserve unsaved selections while new results arrive.
        if (data.choices_html !== choices && !dirty) {
            field('sourceChoices').innerHTML = data.choices_html;
            choices = data.choices_html;
            updateChoices();
        }
        if (data.updated_at && data.active) {
            const elapsed = Math.max(0, Math.floor((Date.now() - Date.parse(data.updated_at)) / 1000));
            if (elapsed > 30) field('discoveryActivity').textContent = '仍在处理 · ' + elapsed + ' 秒前更新';
        }
        return data.active || data.state === 'waiting' || data.state === 'idle';
    }
    async function refresh(method) {
        if (busy) return;
        busy = true;
        clearTimeout(timer);
        field('discoveryError').hidden = true;
        field('discoveryRetry').disabled = true;
        let again = true;
        try {
            const response = await fetch(endpoint, {method: method || 'GET', headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}, signal: AbortSignal.timeout(15000)});
            const data = await response.json();
            if (!response.ok) throw new Error(data.error || '无法读取进度');
            again = render(data);
        } catch (error) {
            field('discoveryError').textContent = error.name === 'TimeoutError' ? '进度连接超时，正在重新连接…' : '进度连接中断：' + error.message;
            field('discoveryError').hidden = false;
            field('discoveryActivity').textContent = '连接中断';
            field('discoveryProgress').hidden = true;
        } finally {
            busy = false;
            field('discoveryRetry').disabled = false;
            if (again) timer = setTimeout(() => refresh(), document.hidden ? 10000 : 2500);
        }
    }
    field('discoveryRetry').addEventListener('click', () => refresh('POST'));
    field('discoveryRefresh').addEventListener('click', () => refresh());
    window.addEventListener('pagehide', () => clearTimeout(timer));
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
    updateChoices();
    refresh();
})();
