(function () {
    'use strict';
    const root = document.getElementById('discoveryStatus');
    if (!root) return;
    const field = id => document.getElementById(id);
    const endpoint = '/api/subscriptions/' + root.dataset.schoolId + '/discovery';
    let timer, busy = false, dirty = false, choices = null, state = null, lastData = null;
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
        lastData = data;
        state = data.state;
        field('discoveryMessage').textContent = uiT(data.message);
        field('discoveryProgress').hidden = !data.busy;
        field('discoveryActivity').textContent = data.state === 'pausing' ? uiT('正在暂停') : data.state === 'paused' ? uiT('已暂停')
            : ['budget_paused', 'ai_connection', 'ai_result'].includes(data.state) ? uiT('进度已保存') : data.state === 'queued' ? uiT('排队中') : data.state === 'retry_wait' ? uiT('等待重试') : data.busy ? uiT('进行中') : '';
        const pause = field('discoveryPause');
        if (pause) {
            pause.hidden = !data.can_pause && !data.can_resume && data.state !== 'pausing';
            pause.dataset.action = data.can_resume ? 'resume' : 'pause';
            pause.textContent = data.can_resume ? uiT('继续') : data.state === 'pausing' ? uiT('正在暂停…') : uiT('暂停');
        }
        field('discoveryCounts').textContent = uiT(`已收录 ${data.article_count ?? 0} 条通知 · 已接入 ${data.verified_source_count ?? 0} 个信息来源`);
        if (field('discoveryValue')) field('discoveryValue').textContent = uiT(data.value_summary || '优先寻找升学、奖助、实习就业、竞赛和校园办事信息。');
        if (field('discoveryStages')) field('discoveryStages').textContent = uiT(data.stage_message || '');
        if (field('discoveryChanges')) field('discoveryChanges').textContent = data.changes && Object.keys(data.changes).length
            ? uiT('本轮检查：结构未变 {unchanged} 个 · 有变化 {changed} 个 · 新入口 {added} 个',
                {unchanged:data.changes.unchanged ?? 0,changed:data.changes.changed ?? 0,added:data.changes.new ?? 0}) : '';
        if (field('discoveryAI')) field('discoveryAI').textContent = uiT(data.ai_message || '');
        if (field('discoveryAIUsage')) {
            field('discoveryAIUsage').hidden = !data.ai_usage_message;
            field('discoveryAIUsage').textContent = uiT(data.ai_usage_message || '');
        }
        if (field('discoveryGaps')) {
            const gaps = data.coverage?.gaps || [];
            field('discoveryGaps').hidden = gaps.length === 0;
            field('discoveryGapList').replaceChildren(...gaps.map(gap => {
                const item = document.createElement('li'); item.textContent = uiT(gap.reason || uiT('该项尚未完成'));
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
            if (elapsed > 30) field('discoveryActivity').textContent = uiT('{seconds}秒前更新',{seconds:elapsed});
        }
        return data.active || ['waiting', 'idle', 'ai_connection', 'ai_result'].includes(data.state);
    }
    document.addEventListener('locale-changed', () => { if (lastData) render(lastData); });
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
            if (!response.ok) throw new Error(data.error || uiT('无法读取进度'));
            again = render(data);
        } catch (error) {
            field('discoveryError').textContent = error.name === 'TimeoutError' ? uiT('连接超时，正在重新确认状态…')
                : (action === 'pause' ? uiT('暂停未成功：') : action === 'resume' ? uiT('继续未成功：') : uiT('进度连接中断：')) + error.message;
            field('discoveryError').hidden = false;
            field('discoveryActivity').textContent = uiT('连接中断');
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
