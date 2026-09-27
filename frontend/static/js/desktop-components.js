(() => {
    const box = document.getElementById('desktopComponents');
    const message = document.getElementById('desktopComponentMessage');
    const retry = document.getElementById('desktopComponentRetry');
    let busy = false;
    async function refresh(method = 'GET') {
        if (busy) return;
        busy = true;
        try {
            const response = await fetch('/_desktop/components', {method, headers: {
                'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content
            }});
            if (!response.ok) return;
            const state = await response.json();
            box.hidden = state.phase === 'ready';
            message.textContent = state.message;
            retry.hidden = state.phase !== 'error';
        } catch (_) { /* The next poll recovers when the local service is ready. */ }
        finally { busy = false; }
    }
    retry.addEventListener('click', () => refresh('POST'));
    refresh();
    setInterval(() => { if (!document.hidden) refresh(); }, 3000);
})();
