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
    let busy = false;
    async function load() {
        if (busy) return;
        busy = true;
        if (button) button.disabled = true;
        try {
            let response = await fetch(endpoint, {method: 'POST'});
            for (let attempt = 0; attempt < 30; attempt++) {
                if (!panel.isConnected) return;
                const data = await response.json();
                if (!response.ok) throw new Error(data.error || '正文暂时无法加载，请打开原文查看。');
                if (data.content_status === 'saved') {
                    const body = panel.querySelector('.body-loader-content');
                    // HTML is sanitized by the server using the same policy as stored articles.
                    if (data.content_html) body.innerHTML = data.content_html;
                    else body.textContent = data.content_text;
                    window.normalizeArticleTypography(body);
                    body.querySelectorAll('a').forEach(a => {a.target = '_blank'; a.rel = 'noopener noreferrer';});
                    if (message) message.remove();
                    if (button) button.remove();
                    return;
                }
                if (data.content_status === 'failed') throw new Error('正文暂时无法加载，可以重试或打开官网原文。');
                if (message) message.textContent = '正在读取官网正文，你也可以先打开原文阅读。';
                if (document.hidden) throw new Error('读取任务已保存，返回页面后可重试查看。');
                await new Promise(resolve => setTimeout(resolve, 2000));
                if (!panel.isConnected) return;
                response = await fetch(endpoint);
            }
            throw new Error('正文仍在等待处理，请稍后重试或打开原文。');
        } catch (error) {
            if (message) message.textContent = error.message;
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
