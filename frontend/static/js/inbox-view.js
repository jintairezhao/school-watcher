/* Reading presentation never fetches content or changes the user's read state. */
(function () {
    window.createInboxView = function (workspace, sources) {
        const find = selector => workspace.querySelector(selector);
        const wide = matchMedia('(min-width: 1024px)');
        let listTop = 0, readerTop = 0, returnTarget = null;

        function mode(url) { return new URL(url, location.origin).searchParams.get('view') === 'focus' ? 'focus' : 'split'; }
        function mailbox(url) {
            const params = new URL(url, location.origin).searchParams;
            const value = params.get('view') === 'focus' ? params.get('mailbox') : params.get('view');
            return value === 'saved' ? 'saved' : 'inbox';
        }
        function withMode(url, focused) {
            const next = new URL(url, location.origin), box = mailbox(next);
            next.searchParams.delete('mailbox');
            if (focused) {
                next.searchParams.set('view', 'focus');
                if (box !== 'inbox') next.searchParams.set('mailbox', box);
            } else if (box === 'inbox') next.searchParams.delete('view');
            else next.searchParams.set('view', box);
            return next;
        }
        function dataKey(url) {
            const next = withMode(url, false);
            next.searchParams.sort();
            return next.pathname + next.search;
        }
        function save() {
            const list = find('#noticeList'), document = find('.reader-document');
            if (list?.getClientRects().length) listTop = list.scrollTop;
            if (document?.getClientRects().length) readerTop = document.scrollTop;
            return {listTop, readerTop};
        }
        function render(url, {restore = true, focus = false} = {}) {
            const focused = mode(url) === 'focus' && !!find('.reader-document');
            workspace.dataset.readingView = focused ? 'focus' : 'split';
            sources.suspendPin(focused);
            const list = find('.notice-panel');
            if (list) list.inert = focused || (!wide.matches && workspace.classList.contains('detail-requested')) || sources.isOverlay();
            const toggle = find('[data-toggle-reading-view]');
            if (toggle) {
                toggle.setAttribute('aria-pressed', String(focused));
                toggle.setAttribute('aria-label', focused ? '返回通知列表，退出专注阅读' : '专注阅读');
                toggle.querySelector('span').textContent = focused ? '返回列表' : '专注阅读';
            }
            if (restore) {
                if (find('#noticeList')) find('#noticeList').scrollTop = listTop;
                if (find('.reader-document')) find('.reader-document').scrollTop = readerTop;
            }
            if (focus) {
                const target = focused ? toggle : returnTarget?.isConnected && returnTarget.getClientRects().length ? returnTarget : toggle;
                target?.focus({preventScroll: true});
            }
        }
        function transition(url, options = {}) {
            save();
            if (mode(url) === 'focus' && workspace.dataset.readingView !== 'focus') returnTarget = document.activeElement;
            render(url, {...options, focus: true});
        }
        wide.addEventListener('change', () => { save(); render(location.href); });
        render(location.href, {restore: false});
        return {mode, mailbox, withMode, dataKey, save, render, transition};
    };
})();
