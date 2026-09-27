/* Read existing jobs on navigation; paid work starts only from an explicit click. */
(() => {
    const active = new Set(['pending', 'running', 'waiting_content']);
    const timers = new WeakMap();

    function show(panel, result) {
        panel.dataset.summaryStatus = result.status || 'none';
        panel.dataset.summaryCompact = String(!result.summary && !result.error &&
            (!result.status || result.status === 'none') && !panel.querySelector('.shared-summary-history'));
        const busy = active.has(result.status);
        const text = panel.querySelector('[data-summary-text]');
        text.textContent = result.summary || '';
        text.hidden = !result.summary;
        const button = panel.querySelector('[data-summary-generate]');
        if (button) {
            button.hidden = !!result.summary && !busy;
            button.disabled = busy || result.status === 'uncertain';
            button.textContent = busy ? '正在生成…' : result.status === 'failed' ? '重试生成' : '生成摘要';
        }
        const force = panel.querySelector('[data-summary-force]');
        if (force) force.hidden = busy || (!result.summary && result.status !== 'uncertain');
        const message = panel.querySelector('[data-summary-message]');
        message.textContent = result.status === 'waiting_content' ? '正在读取官网正文…' : busy ?
            '正在生成，完成后会显示在这里。' : result.error || (result.summary ?
            '根据正文生成，附件未纳入摘要。' : '点击后生成，所有读者共享这份摘要。');
        panel.setAttribute('aria-busy', String(busy));
        if (busy) schedule(panel);
    }

    function schedule(panel) {
        clearTimeout(timers.get(panel));
        if (!panel.isConnected) return;
        timers.set(panel, setTimeout(() => refresh(panel), 3000));
    }

    async function refresh(panel) {
        if (!panel.isConnected) return;
        if (document.hidden) { schedule(panel); return; }
        try {
            const response = await fetch('/api/announcements/' + panel.dataset.summaryPanel + '/summary');
            const result = await response.json();
            if ([401, 403, 404].includes(response.status)) {
                panel.querySelector('[data-summary-message]').textContent = result.error || '暂时无法读取摘要状态';
                return;
            }
            if (!response.ok) throw new Error(result.error || '暂时无法读取摘要状态');
            if (panel.isConnected) show(panel, result);
        } catch (error) {
            if (!panel.isConnected) return;
            panel.querySelector('[data-summary-message]').textContent = error.message + '，稍后继续检查。';
            schedule(panel);
        }
    }

    document.addEventListener('click', async event => {
        const button = event.target.closest('[data-summary-generate], [data-summary-force]');
        if (!button) return;
        const panel = button.closest('[data-summary-panel]');
        if (!panel || button.disabled) return;
        button.disabled = true;
        panel.setAttribute('aria-busy', 'true');
        panel.dataset.summaryCompact = 'false';
        panel.querySelector('[data-summary-message]').textContent = '正在提交摘要请求…';
        try {
            const response = await fetch('/api/announcements/' + panel.dataset.summaryPanel + '/summary', {
                method: 'POST', headers: {'Content-Type': 'application/json',
                    'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]')?.content || ''},
                body: JSON.stringify({force: button.hasAttribute('data-summary-force')})
            });
            const result = await response.json();
            if (!response.ok) throw new Error(result.error || '摘要请求未成功，请重试');
            if (panel.isConnected) show(panel, result);
        } catch (error) {
            if (panel.isConnected) panel.querySelector('[data-summary-message]').textContent = error.message;
        } finally {
            if (panel.isConnected) {
                button.disabled = active.has(panel.dataset.summaryStatus) ||
                    (button.hasAttribute('data-summary-generate') && panel.dataset.summaryStatus === 'uncertain');
                panel.setAttribute('aria-busy', String(active.has(panel.dataset.summaryStatus)));
            }
        }
    });

    function initialize() {
        document.querySelectorAll('[data-summary-panel]').forEach(panel => {
            if (active.has(panel.dataset.summaryStatus)) schedule(panel);
        });
    }
    window.addEventListener('inbox:navigated', initialize);
    initialize();
})();
