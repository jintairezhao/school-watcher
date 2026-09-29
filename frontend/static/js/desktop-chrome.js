(function () {
    'use strict';
    const bar = document.getElementById('desktopChrome');
    if (!bar) return;
    async function action(name) {
        if (window.pywebview && window.pywebview.api) {
            try {
                const result = await window.pywebview.api.window_action(name);
                if (result && result.error) window.alert(result.error);
            } catch (_) {
                if (name === 'uninstall') window.alert('未能打开卸载向导，请重新打开应用后重试。');
            }
        }
    }
    bar.querySelectorAll('[data-window-action]').forEach(button => {
        button.addEventListener('click', () => {
            action(button.dataset.windowAction);
            bar.querySelector('details').open = false;
        });
    });
    document.querySelectorAll('.desktop-drag').forEach(drag => drag.addEventListener('dblclick', () => action('maximize')));
    const grip = document.getElementById('desktopResize');
    let resizeStart = null, resizing = false;
    grip.addEventListener('pointerdown', event => {
        resizeStart = {x:event.screenX, y:event.screenY, width:innerWidth, height:innerHeight};
        grip.setPointerCapture(event.pointerId);
    });
    grip.addEventListener('pointermove', async event => {
        if (!resizeStart || resizing || !window.pywebview) return;
        resizing = true;
        try { await window.pywebview.api.resize_window(resizeStart.width + event.screenX - resizeStart.x,
            resizeStart.height + event.screenY - resizeStart.y); }
        finally { resizing = false; }
    });
    grip.addEventListener('pointerup', () => { resizeStart = null; });
    grip.addEventListener('pointercancel', () => { resizeStart = null; });
    document.addEventListener('keydown', event => { if (event.key === 'Escape') bar.querySelector('details').open = false; });
    document.addEventListener('pointerdown', event => { if (!bar.contains(event.target)) bar.querySelector('details').open = false; });
})();
