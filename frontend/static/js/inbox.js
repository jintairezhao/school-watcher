/* Keep source selection in place; only refresh the changing reading panels. */
(function () {
    const workspace = document.querySelector('.inbox-workspace');
    if (!workspace) return;
    const find = selector => document.querySelector(selector);
    let desiredURL = new URL(location.href);
    let appliedURL = new URL(location.href);
    let requestNumber = 0;
    let controller;
    let failedURL;
    let openNoticePopover = '';
    let updatesAvailable = false;
    function pendingUpdates(value) {
        updatesAvailable = value;
        const button = find('#inboxNewNotices');
        if (button) button.hidden = !value;
    }
    function listKey(url) {
        const next = new URL(url);
        next.searchParams.delete('selected');
        return readingView.dataKey(next);
    }
    const noticePopoverTriggers = {noticeFilterPanel: 'toggleNoticeFilters', noticeActionsMenu: 'toggleNoticeActions'};

    function setNoticePopover(id = '', {focus = false, returnFocus = false} = {}) {
        const previous = openNoticePopover;
        openNoticePopover = id;
        Object.entries(noticePopoverTriggers).forEach(([panelId, triggerId]) => {
            const panel = document.getElementById(panelId), trigger = document.getElementById(triggerId);
            if (panel) panel.hidden = panelId !== id;
            trigger?.setAttribute('aria-expanded', String(panelId === id));
        });
        if (id && find('#inboxRefreshDetails')) find('#inboxRefreshDetails').open = false;
        if (focus && id) {
            const panel = document.getElementById(id);
            [...panel.querySelectorAll('button:not([data-close-notice-popover]),select,a,input')]
                .find(control => !control.disabled && control.getClientRects().length)?.focus({preventScroll: true});
        } else if (returnFocus && previous) {
            document.getElementById(noticePopoverTriggers[previous])?.focus({preventScroll: true});
        }
    }

    const sources = window.createInboxSources(workspace);
    const readingView = window.createInboxView(workspace, sources);
    const conditionKeys = ['period', 'year', 'month', 'read'];
    const conditionPreferenceKey = 'inbox-conditions:' + workspace.dataset.userId;
    function noticeConditions(url) {
        const params = url.searchParams;
        let period = params.get('period') || (readingView.mailbox(url) === 'inbox' ? 'week' : 'all');
        const year = Number(params.get('year')), month = Number(params.get('month'));
        if (!['week', 'all', 'archive'].includes(period) ||
            (period === 'archive' && (!Number.isInteger(year) || year < 1 || year > 9999))) period = 'week';
        const conditions = {period, read: ['read', 'unread'].includes(params.get('read')) ? params.get('read') : 'all'};
        if (period === 'archive') {
            conditions.year = String(year);
            if (Number.isInteger(month) && month >= 1 && month <= 12) conditions.month = String(month);
        }
        return conditions;
    }
    function withConditions(url, conditions) {
        const next = new URL(url);
        conditionKeys.forEach(key => {
            next.searchParams.delete(key);
            if (conditions[key]) next.searchParams.set(key, conditions[key]);
        });
        return next;
    }
    function rememberConditions(url) {
        try { localStorage.setItem(conditionPreferenceKey, JSON.stringify(noticeConditions(url))); } catch (_) {}
    }
    function initialConditionsURL() {
        // Explicit links and history retain their own conditions. Restore defaults only
        // on a fresh entry without filters, never while handling Back/Forward.
        if (conditionKeys.some(key => appliedURL.searchParams.has(key))) return appliedURL;
        try {
            const saved = JSON.parse(localStorage.getItem(conditionPreferenceKey) || 'null');
            if (saved && ['week', 'all', 'archive'].includes(saved.period) && ['all', 'read', 'unread'].includes(saved.read)) {
                const next = withConditions(appliedURL, saved);
                return withConditions(next, noticeConditions(next));
            }
        } catch (_) {}
        return appliedURL;
    }
    const inboxSearch = window.createInboxSearch(workspace, {
        getURL: () => new URL(appliedURL),
        search: q => navigateWith({q}),
        openNotice: url => navigate(url)
    });
    const saveSources = sources.save, syncUnits = sources.syncCounts;
    function switchReadingView(url, {history: historyMode = 'push'} = {}) {
        const next = new URL(url, location.origin);
        if (historyMode === 'none' && readingView.dataKey(desiredURL) !== readingView.dataKey(next)) {
            controller?.abort(); requestNumber++;
            desiredURL = new URL(next);
            syncControls(next); setFeedback();
            find('.notice-panel')?.removeAttribute('aria-busy');
        }
        readingView.transition(next);
        if (historyMode !== 'none' && next.href !== appliedURL.href) history.pushState(null, '', next.pathname + next.search);
        appliedURL = next;
        desiredURL = readingView.withMode(desiredURL, readingView.mode(next) === 'focus');
    }
    let feedbackTimer;
    function setFeedback(message = '', state = '') {
        clearTimeout(feedbackTimer);
        find('#sourceFilterStatus').textContent = message;
        find('#inboxFilterFeedback').hidden = !message;
        find('#inboxFilterFeedback').dataset.state = state;
        find('#retryInboxFilter').hidden = state !== 'error';
        find('#sourcePanelFeedback').hidden = state !== 'error';
        find('#sourcePanelStatus').textContent = state === 'error' ? message : '';
        find('[data-retry-source]').hidden = state !== 'error';
        if (state === 'success') feedbackTimer = setTimeout(() => setFeedback(), 1800);
    }
    function sourceStructure(panel) {
        return [...panel.querySelectorAll('[data-source-group], [data-source-unit], [data-department]')]
            .map(element => element.hasAttribute('data-source-group') ? ['group', element.dataset.sourceGroup] :
                element.hasAttribute('data-source-unit') ? ['unit', element.dataset.sourceUnit,
                    element.querySelector('.source-unit-name').firstChild.textContent] :
                ['column', element.value, element.disabled,
                    element.closest('.department-option').querySelector('.department-name').firstChild.textContent])
            .map(item => JSON.stringify(item)).join('|');
    }
    function syncControls(url) {
        const params = url.searchParams;
        const selected = params.getAll('dept');
        document.querySelectorAll('[data-department]').forEach(input => { input.checked = !input.disabled && selected.includes(input.value); });
        if (find('#schoolFilter')) find('#schoolFilter').value = params.get('school') || '';
        document.querySelectorAll('[data-source-group-filter]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.sourceGroupFilter === params.get('group'))));
        syncUnits();
    }
    async function navigate(url, options = {}) {
        url = new URL(url, location.origin);
        const keepList = options.preserveList && !options.refreshList && listKey(url) === listKey(appliedURL);
        if (url.searchParams.get('school') !== appliedURL.searchParams.get('school') || readingView.mailbox(url) !== readingView.mailbox(appliedURL)) setNoticePopover();
        desiredURL = url;
        saveSources();
        if (controller) controller.abort();
        controller = new AbortController();
        const activeController = controller;
        const number = ++requestNumber;
        const timeout = setTimeout(() => activeController.abort(), 15000);
        if (!keepList) setFeedback('正在更新通知…', 'loading');
        find('.notice-panel').setAttribute('aria-busy', 'true');
        try {
            const response = await fetch(url.pathname + url.search, {
                headers: {'X-Inbox-Fragment': '1'}, signal: activeController.signal
            });
            if (!response.ok) throw new Error('筛选未更新，请重试。');
            const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
            if (number !== requestNumber) return;
            const next = doc.querySelector('.inbox-workspace');
            if (!next) { location.assign(response.url || url.href); return; }
            const oldPanel = find('#sourcePanel'), newPanel = next.querySelector('#sourcePanel');
            const focusedControl = document.activeElement;
            const searchState = inboxSearch.snapshot();
            const preserveSearchDraft = options.history === 'none' && readingView.dataKey(url) === readingView.dataKey(appliedURL);
            const focusId = focusedControl?.id;
            const focusChoice = focusedControl?.hasAttribute('data-period') ? ['data-period', focusedControl.dataset.period] :
                focusedControl?.hasAttribute('data-read') ? ['data-read', focusedControl.dataset.read] : null;
            const refreshDetailsOpen = !!find('#inboxRefreshDetails')?.open;
            const listTop = find('#noticeList').scrollTop;
            readingView.save();
            const sameArticle = find('#undoRead')?.dataset.announcementId === next.querySelector('#undoRead')?.dataset.announcementId;
            const sameSources = oldPanel.dataset.sourceScope === newPanel.dataset.sourceScope &&
                sourceStructure(oldPanel) === sourceStructure(newPanel);
            if (sameSources) {
                const rows = new Map([...newPanel.querySelectorAll('[data-department]')].map(input =>
                    [input.value, input.closest('.department-option')]));
                oldPanel.querySelectorAll('[data-department]').forEach(input => {
                    const row = input.closest('.department-option'), nextRow = rows.get(input.value);
                    const current = row.querySelector('.department-count');
                    const nextCount = nextRow?.querySelector('.department-count');
                    if (current && nextCount) {
                        current.textContent = nextCount.textContent;
                        current.dataset.savedCount = nextCount.dataset.savedCount;
                        current.setAttribute('aria-label', nextCount.getAttribute('aria-label'));
                    }
                    const status = row.querySelector('[data-source-status]'), nextStatus = nextRow?.querySelector('[data-source-status]');
                    if (status && nextStatus) {
                        status.textContent = nextStatus.textContent;
                        status.hidden = nextStatus.hidden;
                        row.title = nextRow.title;
                    }
                });
            } else {
                oldPanel.replaceWith(newPanel);
            }
            if (keepList) {
                // Selecting an article changes the reader, not the DOM under
                // the pointer. New arrivals stay behind the explicit update.
                const selected = url.searchParams.get('selected');
                const nextRows = new Map([...next.querySelectorAll('[data-notice-link]')].map(row => [row.dataset.announcementId, row]));
                const currentRows = [...find('#noticeList').querySelectorAll('[data-notice-link]')];
                if ([...nextRows.keys()].some(id => !currentRows.some(row => row.dataset.announcementId === id))) updatesAvailable = true;
                currentRows.forEach(row => {
                    row.classList.toggle('is-selected', row.dataset.announcementId === selected);
                    const fresh = nextRows.get(row.dataset.announcementId);
                    if (fresh || row.dataset.announcementId === selected) {
                        row.classList.toggle('is-unread', !!fresh?.classList.contains('is-unread'));
                        if (fresh?.hasAttribute('aria-label')) row.setAttribute('aria-label', fresh.getAttribute('aria-label'));
                        else row.removeAttribute('aria-label');
                    }
                });
            } else {
                find('.notice-panel').replaceWith(next.querySelector('.notice-panel'));
                updatesAvailable = false;
            }
            find('.reader-panel').replaceWith(next.querySelector('.reader-panel'));
            pendingUpdates(updatesAvailable);
            setNoticePopover(openNoticePopover);
            if (refreshDetailsOpen && find('#inboxRefreshDetails') && !openNoticePopover) find('#inboxRefreshDetails').open = true;
            workspace.className = next.className;
            workspace.dataset.backUrl = next.dataset.backUrl;
            workspace.dataset.view = next.dataset.view;
            if (options.preserveList) find('#noticeList').scrollTop = listTop;
            // A view toggle is immediate, including while a data request is still in flight.
            url = readingView.withMode(url, readingView.mode(desiredURL) === 'focus');
            if (options.history !== 'none' && url.href !== appliedURL.href) {
                history[options.history === 'replace' ? 'replaceState' : 'pushState'](null, '', url.pathname + url.search);
            }
            appliedURL = url;
            desiredURL = new URL(url);
            // Only a successful, current response can replace the saved preference.
            rememberConditions(url);
            syncControls(url);
            sources.render();
            if (!sameSources) sources.restore();
            readingView.render(url, {restore: options.preserveList && sameArticle});
            if (!keepList) setFeedback('已更新', 'success');
            const restoreFocus = focusId ? document.getElementById(focusId) : focusChoice ?
                document.querySelector(`[${focusChoice[0]}="${focusChoice[1]}"]`) : null;
            if (restoreFocus?.id !== 'inboxQuery' && restoreFocus?.getClientRects().length && !restoreFocus.closest('[inert]')) restoreFocus.focus({preventScroll: true});
            else if (sources.isOverlay() && !find('#sourcePanel').contains(document.activeElement)) find('#closeFilters').focus({preventScroll: true});
            inboxSearch.restore(searchState);
            if (preserveSearchDraft && !searchState.focused) find('#inboxQuery').value = searchState.value;
            if (window.initContentLoaders) window.initContentLoaders(workspace);
            window.dispatchEvent(new CustomEvent('inbox:navigated', {detail: {history: options.history || 'push'}}));
        } catch (error) {
            if (number !== requestNumber) return;
            failedURL = url;
            desiredURL = new URL(appliedURL);
            syncControls(appliedURL);
            setFeedback('筛选未更新，仍显示上次结果。', 'error');
            // A failed Back/Forward request must not leave the URL claiming different results.
            if (options.history === 'none') history.replaceState(null, '', appliedURL.pathname + appliedURL.search);
        } finally {
            clearTimeout(timeout);
            if (number === requestNumber) find('.notice-panel')?.removeAttribute('aria-busy');
        }
    }
    function navigateWith(update = {}, remove = []) {
        const url = new URL(desiredURL);
        remove.concat(['selected', 'page']).forEach(key => url.searchParams.delete(key));
        Object.entries(update).forEach(([key, value]) => {
            if (value === '' || value == null) url.searchParams.delete(key);
            else url.searchParams.set(key, value);
        });
        navigate(url);
    }
    function selectColumns() {
        const url = new URL(desiredURL);
        ['dept', 'selected', 'page', 'group'].forEach(key => url.searchParams.delete(key));
        new Set([...document.querySelectorAll('[data-department]:checked:not(:disabled)')].map(i => i.value))
            .forEach(value => url.searchParams.append('dept', value));
        document.querySelectorAll('[data-source-group-filter]').forEach(button => button.setAttribute('aria-pressed', 'false'));
        syncUnits();
        navigate(url);
    }

    document.addEventListener('change', event => {
        const input = event.target;
        if (input.matches('[data-department]')) {
            document.querySelectorAll('[data-department]').forEach(child => {
                if (child.value === input.value && !child.disabled) child.checked = input.checked;
            });
            selectColumns();
        }
        else if (input.matches('[data-unit-select]')) {
            const ids = new Set([...input.closest('[data-source-unit]').querySelectorAll('[data-department]:not(:disabled)')].map(i => i.value));
            document.querySelectorAll('[data-department]:not(:disabled)').forEach(child => {
                if (ids.has(child.value)) child.checked = input.checked;
            });
            selectColumns();
        } else if (input.id === 'schoolFilter') navigateWith({school: input.value}, ['dept', 'group']);
        else if (input.id === 'yearFilter') navigateWith({period: input.value ? 'archive' : 'week', year: input.value}, ['month']);
        else if (input.id === 'monthFilter') navigateWith({period: 'archive', month: input.value, year: find('#yearFilter').value});
    });
    document.addEventListener('click', async event => {
        const button = event.target.closest('button');
        const link = event.target.closest('a');
        if (button?.hasAttribute('data-toggle-reading-view')) {
            setNoticePopover();
            switchReadingView(readingView.withMode(appliedURL, readingView.mode(appliedURL) !== 'focus'));
            return;
        }
        const targetPanel = Object.keys(noticePopoverTriggers).find(id => button?.id === noticePopoverTriggers[id]);
        if (targetPanel) {
            setNoticePopover(openNoticePopover === targetPanel ? '' : targetPanel, {focus: true});
            return;
        }
        if (button?.hasAttribute('data-close-notice-popover')) {
            setNoticePopover('', {returnFocus: true});
            return;
        }
        if (openNoticePopover && !event.target.closest('#' + openNoticePopover)) setNoticePopover();
        if (event.target.closest('#noticeActionsMenu') && (button || link)) setNoticePopover('', {returnFocus: true});
        if (!event.target.closest('#inboxRefreshDetails') && find('#inboxRefreshDetails')) find('#inboxRefreshDetails').open = false;
        if (link && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey && event.button === 0 &&
            link.matches('[data-notice-link], .reader-back, .pagination-bar a, .mailbox-views a, .clear-filters, .notice-empty a')) {
            let url = new URL(link.href);
            if (link.matches('.mailbox-views a')) url = withConditions(url, noticeConditions(desiredURL));
            if (readingView.mode(appliedURL) === 'focus' && !link.matches('.reader-back')) url = readingView.withMode(url, true);
            if (link.matches('.reader-back')) url = readingView.withMode(url, false);
            if (url.origin === location.origin && url.pathname === '/' && !link.target) {
                event.preventDefault();
                navigate(url, {preserveList: link.matches('[data-notice-link], .reader-back')});
                return;
            }
        }
        if (!button) return;
        if (button.hasAttribute('data-source-group-filter')) {
            navigateWith({group: button.dataset.sourceGroupFilter}, ['dept']);
        } else if (button.id === 'clearDepartments') {
            document.querySelectorAll('[data-department]').forEach(input => { input.checked = false; });
            selectColumns();
        } else if (button.id === 'retryInboxFilter' || button.hasAttribute('data-retry-source')) navigate(failedURL || desiredURL);
        else if (button.id === 'resetNoticeConditions') navigateWith({}, ['period', 'year', 'month', 'read']);
        else if (button.hasAttribute('data-period')) navigateWith({period: button.dataset.period}, ['year', 'month']);
        else if (button.hasAttribute('data-read')) navigateWith({read: button.dataset.read === 'all' ? '' : button.dataset.read});
        else if (button.id === 'undoRead' || button.id === 'markScopeRead' || button.hasAttribute('data-state-field')) {
            button.disabled = true;
            try {
                let endpoint, init;
                if (button.id === 'undoRead') {
                    endpoint = '/api/announcements/' + button.dataset.announcementId + '/read'; init = {method: 'DELETE'};
                } else if (button.id === 'markScopeRead') {
                    endpoint = '/api/inbox/read' + appliedURL.search; init = {method: 'POST'};
                } else {
                    endpoint = '/api/announcements/' + button.dataset.id + '/state';
                    init = {method: 'PUT', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({[button.dataset.stateField]: button.dataset.stateValue === 'true'})};
                }
                const response = await fetch(endpoint, init), data = await response.json();
                if (!response.ok) throw new Error(data.error || '操作失败');
                if (button.id === 'undoRead') {
                    find('[data-notice-link][data-announcement-id="' + button.dataset.announcementId + '"]')?.classList.add('is-unread');
                    button.textContent = '已设为未读'; showToast('success', '这条通知已恢复为未读');
                } else if (button.dataset.stateField === 'starred') {
                    const saved = button.dataset.stateValue === 'true';
                    document.querySelectorAll('[data-state-field="starred"][data-id="' + button.dataset.id + '"]').forEach(control => {
                        control.dataset.stateValue = String(!saved);
                        const label = saved ? '取消收藏' : '收藏';
                        if (control.hasAttribute('data-row-state')) { control.setAttribute('aria-label', label); control.title = label; }
                        else control.textContent = label;
                        control.setAttribute('aria-pressed', String(saved)); control.disabled = false;
                    });
                    showToast('success', saved ? '已加入我的收藏' : '已取消收藏');
                    if (!saved && readingView.mailbox(appliedURL) === 'saved') {
                        const keepSelected = button.dataset.id !== appliedURL.searchParams.get('selected');
                        await navigate(readingView.withMode(keepSelected ? appliedURL : workspace.dataset.backUrl,
                            readingView.mode(appliedURL) === 'focus'), {preserveList: true, refreshList: true});
                    }
                } else {
                    await navigate(readingView.withMode(workspace.dataset.backUrl,
                        readingView.mode(appliedURL) === 'focus'), {preserveList: true, refreshList: true});
                }
            } catch (error) { button.disabled = false; showToast('error', error.message); }
        }
    });
    document.addEventListener('keydown', event => {
        if (event.defaultPrevented) return;
        if (event.key === 'Escape' && openNoticePopover) {
            event.preventDefault();
            setNoticePopover('', {returnFocus: true});
            return;
        }
        if (event.key === 'Escape' && find('#inboxRefreshDetails')?.open) {
            event.preventDefault();
            find('#inboxRefreshDetails').open = false;
            find('#inboxRefreshSummary')?.focus({preventScroll: true});
            return;
        }
        if (event.key === 'Escape' && document.querySelector('dialog[open], [role="dialog"][aria-modal="true"]:not([hidden])')) return;
        if (event.key === 'Escape' && readingView.mode(appliedURL) === 'focus') {
            event.preventDefault();
            switchReadingView(readingView.withMode(appliedURL, false));
            return;
        }
        if (event.key === 'Escape' && innerWidth < 1024 && workspace.classList.contains('detail-requested')) {
            event.preventDefault();
            navigate(readingView.withMode(workspace.dataset.backUrl, false), {preserveList: true});
        }
        if (event.target.matches('[data-notice-link]') && ['ArrowUp', 'ArrowDown'].includes(event.key)) {
            const links = [...document.querySelectorAll('[data-notice-link]')];
            const next = links[links.indexOf(event.target) + (event.key === 'ArrowDown' ? 1 : -1)];
            if (next) { event.preventDefault(); next.focus(); }
        }
    });
    document.addEventListener('focusin', event => {
        if (!openNoticePopover) return;
        if (event.target.id !== noticePopoverTriggers[openNoticePopover] && !event.target.closest('#' + openNoticePopover)) setNoticePopover();
    });
    document.addEventListener('toggle', event => {
        if (event.target.id === 'inboxRefreshDetails' && event.target.open) setNoticePopover();
    }, true);
    window.addEventListener('popstate', () => {
        const next = new URL(location.href);
        if (readingView.dataKey(next) === readingView.dataKey(appliedURL)) switchReadingView(next, {history: 'none'});
        else navigate(next, {history: 'none'});
    });
    window.notifyInboxUpdates = () => pendingUpdates(true);
    window.refreshInboxView = () => appliedURL.href === desiredURL.href && !inboxSearch.isComposing()
        ? navigate(appliedURL, {history: 'none', preserveList: true, refreshList: true}) : Promise.resolve();
    syncUnits();
    const initialURL = initialConditionsURL();
    if (initialURL.href !== appliedURL.href) navigate(initialURL, {history: 'replace'});
    else if (conditionKeys.some(key => appliedURL.searchParams.has(key))) rememberConditions(appliedURL);
})();
