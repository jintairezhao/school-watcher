/* Remember successful source choices per user and mailbox, across page visits. */
(() => {
    'use strict';
    const user = document.body.dataset.userId;
    const sourceKeys = ['school', 'dept', 'group'];
    const bases = new WeakMap();
    const mailbox = url => {
        const value = url.searchParams.get('view') === 'focus' ? url.searchParams.get('mailbox') : url.searchParams.get('view');
        return ['saved', 'trash'].includes(value) ? value : 'inbox';
    };
    const key = box => 'inbox-sources:' + user + ':' + box;
    const positiveId = value => typeof value === 'string' && /^[1-9]\d*$/.test(value);
    function read(box) {
        if (!user) return null;
        try {
            const state = JSON.parse(localStorage.getItem(key(box)) || 'null');
            if (!state || typeof state !== 'object' || !Array.isArray(state.departments)) return null;
            if (state.school !== '' && !positiveId(state.school)) return null;
            if (!state.departments.every(positiveId) || typeof state.group !== 'string') return null;
            return state;
        } catch (_) { return null; }
    }
    function withSources(url, state) {
        const next = new URL(url);
        if (!state) return next;
        sourceKeys.forEach(name => next.searchParams.delete(name));
        if (state.school) {
            next.searchParams.set('school', state.school);
            state.departments.forEach(id => next.searchParams.append('dept', id));
            if (state.group) next.searchParams.set('group', state.group);
        }
        return next;
    }
    function syncLinks() {
        document.querySelectorAll('[data-inbox-entry]').forEach(link => {
            if (!bases.has(link)) bases.set(link, link.getAttribute('href'));
            const base = new URL(bases.get(link), location.origin);
            if (base.origin !== location.origin || base.pathname !== '/') return;
            const next = withSources(base, read(link.dataset.inboxEntry));
            link.href = next.pathname + next.search;
        });
    }
    function remember(url, actualSchool) {
        if (!user) return;
        const school = positiveId(actualSchool) && actualSchool === url.searchParams.get('school') ? actualSchool : '';
        const state = {school, departments: school ? url.searchParams.getAll('dept').filter(positiveId) : [],
            group: school ? url.searchParams.get('group') || '' : ''};
        try { localStorage.setItem(key(mailbox(url)), JSON.stringify(state)); } catch (_) {}
        syncLinks();
    }
    function initialURL(url) {
        // Explicit filters and Back/Forward own their URL. Only a fresh
        // mailbox entry inherits the last successful source selection.
        if ([...url.searchParams.keys()].some(name => !['view', 'mailbox'].includes(name)) ||
            performance.getEntriesByType('navigation')[0]?.type === 'back_forward') return url;
        return withSources(url, read(mailbox(url)));
    }
    window.inboxSourceMemory = {remember, initialURL};
    window.addEventListener('inbox:navigated', syncLinks);
    window.addEventListener('pageshow', syncLinks);
    window.addEventListener('storage', event => {
        if (event.key?.startsWith('inbox-sources:' + user + ':')) syncLinks();
    });
    syncLinks();
})();
