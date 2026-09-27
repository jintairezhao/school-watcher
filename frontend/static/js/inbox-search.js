/* Search suggestions are a preview. Only an explicit choice changes the inbox. */
(function () {
    window.createInboxSearch = function (workspace, {getURL, search, openNotice}) {
        const find = selector => document.querySelector(selector);
        const historyKey = 'inbox-search-history:' + workspace.dataset.userId;
        const normalize = value => value.trim().replace(/\s+/g, ' ').slice(0, 200);
        let recent = [], active = -1, opened = false, composing = false, restoringFocus = false;
        let timer, controller, generation = 0;
        try {
            const saved = JSON.parse(localStorage.getItem(historyKey) || '[]');
            if (Array.isArray(saved)) recent = [...new Set(saved.filter(item => typeof item === 'string').map(normalize).filter(Boolean))].slice(0, 10);
        } catch (_) {}
        const inside = element => element?.closest?.('.inbox-search, #inboxSearchPanel');
        const options = () => [...find('#inboxSearchOptions').querySelectorAll('[role="option"]')];
        function focusField() {
            restoringFocus = true;
            find('#inboxQuery').focus({preventScroll: true});
            restoringFocus = false;
        }

        function saveHistory(term) {
            term = normalize(term);
            if (!term) return;
            recent = [term, ...recent.filter(item => item.toLocaleLowerCase() !== term.toLocaleLowerCase())].slice(0, 10);
            writeHistory();
        }
        function writeHistory() {
            try { localStorage.setItem(historyKey, JSON.stringify(recent)); } catch (_) {}
        }
        function cancel() {
            clearTimeout(timer);
            controller?.abort();
            controller = null;
            generation += 1;
        }
        function setActive(index) {
            const rows = options();
            active = index;
            rows.forEach((row, i) => row.setAttribute('aria-selected', String(i === index)));
            const field = find('#inboxQuery');
            field.removeAttribute('aria-activedescendant');
            if (rows[index]) {
                field.setAttribute('aria-activedescendant', rows[index].id);
                rows[index].scrollIntoView({block: 'nearest'});
            }
        }
        function position() {
            if (!opened) return;
            const panel = find('#inboxSearchPanel');
            const anchor = find('.notice-tools').getBoundingClientRect();
            const viewport = window.visualViewport;
            const top = viewport?.offsetTop || 0, bottom = top + (viewport?.height || innerHeight);
            const below = bottom - anchor.bottom - 16, above = anchor.top - top - 16;
            const useAbove = below < 160 && above > below;
            panel.classList.toggle('search-panel-above', useAbove);
            panel.style.maxHeight = Math.max(0, Math.min(420, useAbove ? above : below)) + 'px';
        }
        function close() {
            cancel(); opened = false; active = -1;
            find('#inboxSearchPanel').hidden = true;
            find('#inboxQuery').setAttribute('aria-expanded', 'false');
            find('#inboxQuery').removeAttribute('aria-activedescendant');
        }
        function begin(title, message, isHistory = false) {
            opened = true;
            find('#inboxSearchPanel').hidden = false;
            find('#inboxSearchHeading').textContent = title;
            find('#inboxSearchStatus').textContent = message;
            find('#clearInboxSearchHistory').hidden = !isHistory || !recent.length;
            find('#submitInboxSearch').hidden = isHistory;
            find('#inboxSearchOptions').replaceChildren();
            find('#inboxQuery').setAttribute('aria-expanded', 'true');
            setActive(-1); position();
        }
        function row(element, index) {
            element.id = 'inbox-search-option-' + index;
            element.className = 'search-suggestion';
            element.setAttribute('role', 'option');
            element.setAttribute('aria-selected', 'false');
            element.tabIndex = -1;
            return element;
        }
        function history() {
            begin('近期搜索', recent.length ? '' : '暂无搜索记录。输入关键词查找通知。', true);
            recent.forEach((term, index) => {
                const button = row(document.createElement('button'), index);
                button.type = 'button'; button.dataset.searchTerm = term;
                button.textContent = term;
                find('#inboxSearchOptions').append(button);
            });
        }
        function highlight(element, text, query) {
            const terms = query.split(/\s+/).filter(Boolean).slice(0, 10).map(term => term.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'));
            const pattern = new RegExp(terms.join('|'), 'gi');
            let end = 0;
            for (const match of text.matchAll(pattern)) {
                element.append(document.createTextNode(text.slice(end, match.index)));
                const mark = document.createElement('mark'); mark.textContent = match[0]; element.append(mark);
                end = match.index + match[0].length;
            }
            element.append(document.createTextNode(text.slice(end)));
        }
        function matches(data, query) {
            begin('匹配通知', data.items.length ? '当前筛选范围内，按最新发布排序' : '当前范围没有匹配通知，可调整来源或时间条件。');
            data.items.forEach((item, index) => {
                const link = row(document.createElement('a'), index);
                link.href = item.url; link.dataset.searchResult = '';
                const title = document.createElement('strong'); highlight(title, item.title, query);
                const meta = document.createElement('small'); meta.textContent = [item.source, item.date].filter(Boolean).join(' · ');
                link.append(title, meta);
                if (item.snippet) {
                    const excerpt = document.createElement('span'); excerpt.className = 'search-suggestion-excerpt';
                    highlight(excerpt, item.snippet, query); link.append(excerpt);
                }
                find('#inboxSearchOptions').append(link);
            });
            find('#submitInboxSearch').textContent = data.has_more ? '查看全部匹配通知' : '在列表中查看搜索结果';
            position();
        }
        async function fetchMatches(query, number) {
            const requestController = new AbortController(); controller = requestController;
            const timeout = setTimeout(() => requestController.abort(), 8000);
            try {
                const url = getURL();
                url.pathname = '/api/inbox/search-suggestions';
                ['selected', 'page'].forEach(key => url.searchParams.delete(key));
                url.searchParams.set('q', query);
                const response = await fetch(url.pathname + url.search, {signal: requestController.signal, headers: {Accept: 'application/json'}});
                if (!response.ok) throw new Error('suggestions unavailable');
                const data = await response.json();
                if (number !== generation || !opened || normalize(find('#inboxQuery').value) !== query) return;
                if (!Array.isArray(data.items)) throw new Error('invalid suggestions');
                matches(data, query);
            } catch (_) {
                if (number === generation && opened) begin('匹配通知', '暂时无法加载匹配结果，可以按回车搜索。');
            } finally { clearTimeout(timeout); }
        }
        function update(immediate = false) {
            cancel();
            const query = normalize(find('#inboxQuery').value);
            if (!query) { history(); return; }
            begin('匹配通知', '正在查找…');
            const number = generation;
            if (immediate) fetchMatches(query, number);
            else timer = setTimeout(() => fetchMatches(query, number), 250);
        }
        function submit(term = find('#inboxQuery').value) {
            term = normalize(term); saveHistory(term); close();
            find('#inboxQuery').value = term;
            search(term);
        }
        function choose(option, event) {
            if (option.hasAttribute('data-search-term')) { submit(option.dataset.searchTerm); return; }
            saveHistory(find('#inboxQuery').value);
            close();
            if (event && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey)) return;
            event?.preventDefault();
            openNotice(new URL(option.href, location.origin));
        }
        document.addEventListener('focusin', event => {
            if (event.target.id === 'inboxQuery') { if (!composing && !restoringFocus) update(true); }
            else if (!inside(event.target)) close();
        });
        document.addEventListener('click', event => {
            if (event.target.id === 'inboxQuery' && !opened && !composing) update(true);
            const option = event.target.closest('#inboxSearchOptions [role="option"]');
            if (option) choose(option, event);
            else if (event.target.closest('#clearInboxSearchHistory')) { recent = []; writeHistory(); history(); focusField(); }
            else if (event.target.closest('#submitInboxSearch')) submit();
            else if (!inside(event.target)) close();
        });
        document.addEventListener('input', event => {
            if (event.target.id === 'inboxQuery' && !composing && !event.isComposing) update();
        });
        document.addEventListener('compositionstart', event => {
            if (event.target.id === 'inboxQuery') { composing = true; close(); }
        });
        document.addEventListener('compositionend', event => {
            if (event.target.id === 'inboxQuery') { composing = false; update(); }
        });
        document.addEventListener('submit', event => {
            if (!event.target.matches('.inbox-search')) return;
            event.preventDefault();
            if (!composing) submit();
        });
        document.addEventListener('keydown', event => {
            if (!inside(event.target) || composing || event.isComposing) return;
            if (event.key === 'Escape' && opened) {
                event.preventDefault(); close(); focusField();
                return;
            }
            if (event.target.id !== 'inboxQuery') return;
            if (['ArrowDown', 'ArrowUp'].includes(event.key)) {
                event.preventDefault();
                if (!opened) update(true);
                const count = options().length;
                if (count) setActive(event.key === 'ArrowDown' ? (active + 1) % count : (active < 0 ? count - 1 : (active - 1 + count) % count));
            } else if (event.key === 'Enter' && opened && active >= 0) {
                event.preventDefault(); choose(options()[active], event);
            }
        });
        window.addEventListener('resize', position);
        window.visualViewport?.addEventListener('resize', position);
        window.visualViewport?.addEventListener('scroll', position);

        return {
            isComposing: () => composing,
            snapshot() {
                const field = find('#inboxQuery');
                return {focused: document.activeElement === field, value: field.value, start: field.selectionStart, end: field.selectionEnd, opened};
            },
            restore(state) {
                cancel(); opened = false;
                if (!state.focused) return;
                const field = find('#inboxQuery'); field.value = state.value;
                focusField();
                if (state.start != null) field.setSelectionRange(state.start, state.end);
                if (state.opened && !composing) update(); else close();
            }
        };
    };
})();
