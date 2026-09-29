(() => {
    const notice = document.getElementById('desktopUpdateNotice');
    if (!notice) return;
    const message = document.getElementById('desktopUpdateMessage');
    const open = document.getElementById('desktopUpdateOpen');
    const dismiss = document.getElementById('desktopUpdateDismiss');
    let timer, busy = false, started = false, stopped = false;
    function paint(state) {
        notice.hidden = !state?.available;
        if (state?.available) message.textContent = `发现新版本 ${state.version}`;
    }
    async function refresh() {
        if (busy || stopped) return;
        busy = true;
        let state;
        try {
            state = await window.pywebview.api.update_notification();
            paint(state);
        } catch (_) { /* Automatic checks must not interrupt reading when unavailable. */ }
        finally {
            busy = false;
            if (!stopped && state && ['idle', 'checking'].includes(state.phase)) timer = setTimeout(refresh, 1000);
        }
    }
    function start() {
        if (started || !window.pywebview?.api?.update_notification) return;
        started = true;
        refresh();
    }
    open.addEventListener('click', async () => {
        open.disabled = true;
        try {
            const opened = await window.pywebview.api.window_action('updates');
            if (opened === true) paint(await window.pywebview.api.dismiss_update());
        } catch (_) { message.textContent = '暂时无法打开更新，请重试'; }
        finally { open.disabled = false; }
    });
    dismiss.addEventListener('click', async () => {
        dismiss.disabled = true;
        try { paint(await window.pywebview.api.dismiss_update()); }
        catch (_) { message.textContent = '暂时无法关闭提醒，请重试'; }
        finally { dismiss.disabled = false; }
    });
    window.addEventListener('pywebviewready', start);
    window.addEventListener('pagehide', () => { stopped = true; clearTimeout(timer); });
    start();
})();
