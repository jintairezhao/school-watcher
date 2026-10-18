/* Observe local saving progress; GETs never queue downloads or model work. */
(() => {
    'use strict';
    const sessions = new WeakMap();
    function start(panel, retry = false) {
        const previous = sessions.get(panel);
        if (previous) clearTimeout(previous.timer);
        const session = {timer: null, until: Date.now() + 60000};
        sessions.set(panel, session);
        const id = panel.dataset.savedMaterials;
        const current = () => panel.isConnected && sessions.get(panel) === session;
        async function read(method = 'GET') {
            if (!current()) return;
            const button = panel.querySelector('[data-retry-saved]');
            if (button) button.disabled = true;
            try {
                const response = await fetch('/api/announcements/' + id + '/saved-materials',
                    {method, cache: 'no-store', signal: AbortSignal.timeout(15000)});
                const data = await response.json();
                if (!current()) return;
                if (!response.ok) throw new Error(data.error || uiT('保存状态暂时无法读取，请稍后重试'));
                panel.hidden = data.state === 'none';
                if (panel.hidden) return;
                const message = panel.querySelector('[data-saved-message]');
                const expiry = data.trash_until ? uiT('回收站保留至 ') + new Date(data.trash_until).toLocaleDateString('zh-CN') + '。' : '';
                message.textContent = expiry + (data.state === 'ready' ? uiT('正文与附件已保存在本地。') :
                    data.state === 'failed' ? uiT('正文或部分附件保存未完成，可重试；已保存的内容仍保留。') :
                    uiT('正在后台保存正文与附件（') + data.saved + '/' + data.total + uiT(' 个资源已保存）。'));
                const files = panel.querySelector('[data-saved-files]');
                files.replaceChildren();
                (data.resources || []).filter(file => file.kind === 'attachment' || file.state === 'failed').forEach(file => {
                    const item = document.createElement('li');
                    if (file.state === 'ready') {
                        const link = document.createElement('a');
                        link.href = file.url; link.textContent = file.filename || uiT('已保存附件'); item.append(link);
                    } else item.textContent = (file.filename || uiT('官网资源')) + '：' + (file.error || uiT('等待保存'));
                    files.append(item);
                });
                button.hidden = data.state !== 'failed';
                if (data.state === 'saving' && Date.now() < session.until) session.timer = setTimeout(() => read(), 2000);
            } catch (error) {
                if (!current()) return;
                panel.hidden = false;
                panel.querySelector('[data-saved-message]').textContent = error.message;
                button.hidden = false;
            } finally {
                if (current() && button) button.disabled = false;
            }
        }
        read(retry ? 'POST' : 'GET');
    }
    const init = () => document.querySelectorAll('[data-saved-materials][data-active="true"]').forEach(panel => {
        if (!sessions.has(panel)) start(panel);
    });
    document.addEventListener('click', event => {
        const button = event.target.closest('[data-retry-saved]');
        if (button && !button.disabled) start(button.closest('[data-saved-materials]'), true);
    });
    window.addEventListener('notice:favorite-changed', event => {
        const panel = document.querySelector('[data-saved-materials="' + event.detail.id + '"]');
        if (panel) { panel.dataset.active = 'true'; start(panel); }
    });
    window.addEventListener('inbox:navigated', init);
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})();
