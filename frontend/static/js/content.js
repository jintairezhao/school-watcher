// Normalize presentation in the rendered copy, leaving archived HTML untouched.
window.normalizeArticleTypography = function (body) {
    if (!body) return;
    body.querySelectorAll('*').forEach(element => {
        const weight = element.style.fontWeight;
        const emphasis = element.style.fontStyle;
        ['font', 'font-family', 'font-size', 'line-height', 'letter-spacing', 'text-indent']
            .forEach(name => element.style.removeProperty(name));
        if (weight) element.style.fontWeight = Number(weight) >= 600 || weight === 'bold' ? '700' : '400';
        if (emphasis) element.style.fontStyle = emphasis;
        if (element.tagName === 'FONT') ['face', 'size', 'color'].forEach(name => element.removeAttribute(name));
    });
    body.querySelectorAll('table').forEach(table => {
        if (table.parentElement.classList.contains('reading-table-scroll')) return;
        const scroll = document.createElement('div');
        scroll.className = 'reading-table-scroll';
        scroll.tabIndex = 0;
        scroll.setAttribute('role', 'region');
        scroll.setAttribute('aria-label', '通知中的表格，可横向滚动');
        table.before(scroll);
        scroll.append(table);
    });
};

window.initContentLoaders = function (root = document) {
root.querySelectorAll('[data-content-id]').forEach(panel => {
    if (panel.dataset.loaderReady) return;
    panel.dataset.loaderReady = 'true';
    window.normalizeArticleTypography(panel.querySelector('.body-loader-content'));
    const endpoint = '/api/announcements/' + panel.dataset.contentId + '/content';
    const message = panel.querySelector('.body-loader-status');
    const button = panel.querySelector('.body-loader-retry');
    const placeholder = panel.querySelector('.body-loader-placeholder');
    const body = panel.querySelector('.body-loader-content');
    function phase(value) {
        panel.dataset.loadState = value;
        if (placeholder) placeholder.hidden = value !== 'loading';
        body.setAttribute('aria-busy', String(value === 'loading'));
        if (button) button.hidden = value === 'loading' || value === 'saved';
    }
    let busy = false;
    async function load() {
        if (busy) return;
        busy = true;
        phase('loading');
        if (message) message.textContent = '正在读取官网正文，你也可以先打开原文阅读。';
        if (button) button.disabled = true;
        const deadline = Date.now() + 60000;
        const read = method => fetch(endpoint, {method, signal: AbortSignal.timeout(Math.max(1, Math.min(15000, deadline - Date.now())))});
        try {
            let response = await read('POST');
            for (let attempt = 0; attempt < 30 && Date.now() < deadline; attempt++) {
                if (!panel.isConnected) return;
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '正文暂时无法加载，请打开原文查看。');
                if (data.content_status === 'saved') {
                    // HTML is sanitized by the server using the same policy as stored articles.
                    if (data.content_html) body.innerHTML = data.content_html;
                    else body.textContent = data.content_text;
                    window.normalizeArticleTypography(body);
                    body.querySelectorAll('a').forEach(a => {a.target = '_blank'; a.rel = 'noopener noreferrer';});
                    phase('saved');
                    panel.dataset.cached = 'true';
                    if (message) message.remove();
                    if (button) button.remove();
                    return;
                }
                // The server already worked out why: a notice behind the school's
                // sign-in reports that, not a generic failure a reader would
                // retry forever (see LOGIN_REQUIRED_MESSAGE). A failed read
                // answers 200, so data.error never arrives through line 43.
                if (data.content_status === 'failed') {
                    throw new Error(data.error || '正文暂时无法加载，可以重试或打开官网原文。');
                }
                if (message) message.textContent = '正在读取官网正文，你也可以先打开原文阅读。';
                if (document.hidden) throw new Error('读取任务已保存，返回页面后可重试查看。');
                await new Promise(resolve => setTimeout(resolve, 2000));
                if (!panel.isConnected) return;
                response = await read('GET');
            }
            throw new Error('正文仍在等待处理，请稍后重试或打开原文。');
        } catch (error) {
            if (!panel.isConnected) return;
            phase('failed');
            if (message) message.textContent = ['TimeoutError', 'AbortError'].includes(error.name)
                ? '正文读取超时，可以重试或打开官网原文。'
                : error instanceof TypeError ? '连接中断，正文暂未加载。可以重试或打开官网原文。'
                : error instanceof SyntaxError ? '暂时无法读取正文返回的内容，可以重试或打开官网原文。' : error.message;
        } finally {
            busy = false;
            if (button?.isConnected) {button.disabled = false; button.textContent = '重新加载正文';}
        }
    }
    if (button) button.addEventListener('click', load);
    if (panel.dataset.cached === 'false') load();
    else fetch(endpoint).catch(() => {});
});
};
window.initContentLoaders();

async function waitForQueuedTask(data) {
    if (!data.task_id) return data;
    for (let i = 0; i < 60; i++) {
        await new Promise(resolve => setTimeout(resolve, 2000));
        const response = await fetch('/api/tasks/' + data.task_id);
        const task = await response.json();
        if (!response.ok) throw new Error(task.error || '无法读取任务状态');
        if (task.state === 'done') return task.result;
        if (task.state === 'failed') throw new Error(task.error || '任务未完成');
    }
    throw new Error('任务仍在后台等待，请稍后重试查看。');
}
