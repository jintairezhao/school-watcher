async function directoryRequest(url, method, body) {
    const response = await fetch(url, {method, headers: {'Content-Type': 'application/json'}, body: body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '操作未完成，请稍后重试');
    return data;
}
function bindDirectoryActions(root = document) {
root.querySelectorAll('[data-subscribe-name]').forEach(button => {
    button.addEventListener('click', async () => {
        button.disabled = true;
        button.textContent = '正在订阅…';
        try {
            const schoolId = button.dataset.schoolId;
            const result = await directoryRequest(schoolId ? '/api/subscriptions' : '/api/catalog/subscribe', 'POST',
                schoolId ? {school_id: Number(schoolId)} : {name: button.dataset.subscribeName});
            location.href = '/subscriptions/' + (schoolId || result.school_id);
        } catch (error) {
            showToast('error', error.message);
            button.textContent = '重试订阅';
            button.disabled = false;
        }
    });
});
root.querySelectorAll('[data-unsubscribe]').forEach(button => {
    button.addEventListener('click', async () => {
        button.disabled = true;
        try {
            await directoryRequest('/api/subscriptions/' + button.dataset.unsubscribe, 'DELETE');
            location.reload();
        } catch (error) {
            showToast('error', error.message);
            button.disabled = false;
        }
    });
});
}
bindDirectoryActions();

(() => {
    const form = document.getElementById('directoryFilters');
    if (!form) return;
    const query = document.getElementById('catalogQuery');
    const feedback = document.getElementById('directoryFeedback');
    const results = document.getElementById('directoryResults');
    let timer, controller, serial = 0, composing = false;
    function invalidate() {
        clearTimeout(timer);
        controller?.abort();
        serial += 1;
    }
    function formURL() {
        const url = new URL(form.action);
        for (const [key, value] of new FormData(form)) {
            if (value.trim() && !(key === 'level' && value === 'all')) url.searchParams.set(key, value.trim());
        }
        return url;
    }
    function syncFilters(url) {
        query.value = url.searchParams.get('q') || '';
        form.elements.province.value = url.searchParams.get('province') || '';
        form.elements.level.value = url.searchParams.get('level') || 'all';
    }
    async function search(url = formURL(), historyMode = 'replace') {
        invalidate();
        const requestId = serial;
        const current = new AbortController();
        controller = current;
        const timeout = setTimeout(() => current.abort(), 15000);
        results.setAttribute('aria-busy', 'true');
        feedback.hidden = false;
        feedback.classList.remove('sr-only');
        feedback.textContent = '正在匹配…';
        try {
            const response = await fetch(url, {headers: {'X-Directory-Fragment': '1'}, signal: current.signal});
            if (!response.ok) throw new Error('search failed');
            const documentPart = new DOMParser().parseFromString(await response.text(), 'text/html');
            const next = documentPart.getElementById('directoryResults');
            if (!next) throw new Error('missing results');
            if (requestId !== serial) return;
            results.replaceChildren(...next.childNodes);
            bindDirectoryActions(results);
            feedback.textContent = results.querySelector('[data-directory-count]').textContent;
            feedback.classList.add('sr-only');
            if (historyMode === 'push') history.pushState(null, '', url);
            else if (historyMode === 'replace') history.replaceState(null, '', url);
        } catch (_) {
            if (requestId !== serial) return;
            feedback.classList.remove('sr-only');
            feedback.textContent = '匹配未完成，当前仍是上次结果。点击“查找学校”重试。';
        } finally {
            clearTimeout(timeout);
            if (requestId === serial) results.setAttribute('aria-busy', 'false');
        }
    }
    function schedule() {
        invalidate();
        if (!composing) timer = setTimeout(() => search(), 150);
    }
    query.addEventListener('compositionstart', () => { composing = true; invalidate(); });
    query.addEventListener('compositionend', () => { composing = false; schedule(); });
    query.addEventListener('input', schedule);
    form.querySelectorAll('select').forEach(select => select.addEventListener('change', () => search()));
    form.addEventListener('submit', event => { event.preventDefault(); if (!composing) search(); });
    results.addEventListener('click', event => {
        const link = event.target.closest('.pagination-bar a, .directory-summary a, .notice-empty a');
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        const url = new URL(link.href);
        syncFilters(url);
        search(url, 'push');
    });
    window.addEventListener('popstate', () => { const url = new URL(location.href); syncFilters(url); search(url, 'none'); });
    window.addEventListener('pagehide', invalidate);
})();

document.getElementById('submitSchoolForm')?.addEventListener('submit', async event => {
    event.preventDefault();
    const button = event.target.querySelector('button');
    const feedback = document.getElementById('submitFeedback');
    button.disabled = true;
    feedback.textContent = '正在添加学校…';
    try {
        const result = await directoryRequest('/api/schools', 'POST', {
            name: document.getElementById('subName').value.trim(), url: document.getElementById('subUrl').value.trim()
        });
        location.href = '/subscriptions/' + result.id;
    } catch (error) {
        feedback.textContent = error.message;
        button.disabled = false;
    }
});
