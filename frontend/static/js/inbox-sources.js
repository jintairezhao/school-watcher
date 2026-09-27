/* Source presentation is independent of the URL and fragment navigation. */
(function () {
    window.createInboxSources = function (workspace) {
        const find = selector => document.querySelector(selector);
        const pinViewport = matchMedia('(min-width: 1200px)');
        const preferenceKey = 'inbox-source-mode:' + workspace.dataset.userId;
        let pinned = false, pinSuspended = false, temporaryOpen = false, returnTarget = null, searchBase = null;
        let outsideElements = [];
        try { pinned = localStorage.getItem(preferenceKey) === 'pinned'; } catch (_) {}

        const mode = () => pinned && !pinSuspended && pinViewport.matches ? 'pinned' : temporaryOpen ? 'overlay' : 'closed';
        const sourceKey = () => 'inbox-sources:' + find('#sourcePanel').dataset.sourceScope;
        const scrollArea = () => find('#sourceTreeScroll');
        function rememberPin(value) {
            pinned = value;
            try { localStorage.setItem(preferenceKey, value ? 'pinned' : 'closed'); } catch (_) {}
        }
        function setExpanded(unit, open) {
            unit.querySelector('.source-unit-toggle').setAttribute('aria-expanded', String(open));
            unit.querySelector('.source-unit-columns').hidden = !open;
        }
        function setGroupExpanded(group, open) {
            group.querySelector('.source-group-toggle').setAttribute('aria-expanded', String(open));
            group.querySelector('.source-group-items').hidden = !open;
        }
        function snapshot() {
            const expanded = {}, groups = {};
            find('#sourcePanel').querySelectorAll('[data-source-unit]').forEach(unit => {
                expanded[unit.dataset.sourceUnit] = unit.querySelector('.source-unit-toggle').getAttribute('aria-expanded') === 'true';
            });
            find('#sourcePanel').querySelectorAll('[data-source-group]').forEach(group => {
                groups[group.dataset.sourceGroup] = group.querySelector('.source-group-toggle').getAttribute('aria-expanded') === 'true';
            });
            return {expanded, groups, top: scrollArea().scrollTop};
        }
        function applySnapshot(saved) {
            find('#sourcePanel').querySelectorAll('[data-source-unit]').forEach(unit => {
                if (Object.hasOwn(saved.expanded || {}, unit.dataset.sourceUnit)) setExpanded(unit, saved.expanded[unit.dataset.sourceUnit]);
            });
            find('#sourcePanel').querySelectorAll('[data-source-group]').forEach(group => {
                if (Object.hasOwn(saved.groups || {}, group.dataset.sourceGroup)) setGroupExpanded(group, saved.groups[group.dataset.sourceGroup]);
            });
            scrollArea().scrollTop = saved.top || 0;
        }
        function save() {
            const state = {...(searchBase || snapshot()), query: find('#sourceQuery')?.value || '', searchTop: scrollArea().scrollTop};
            try { sessionStorage.setItem(sourceKey(), JSON.stringify(state)); } catch (_) {}
        }
        function search() {
            const normalize = text => text.normalize('NFKC').toLocaleLowerCase().trim();
            const query = normalize(find('#sourceQuery')?.value || '');
            const terms = query.split(/\s+/).filter(Boolean);
            const panel = find('#sourcePanel');
            panel.querySelectorAll('[data-source-unit], [data-source-group], .department-option').forEach(item => { item.hidden = false; });
            if (find('#clearSourceSearch')) find('#clearSourceSearch').hidden = !query;
            if (find('#sourceSearchEmpty')) find('#sourceSearchEmpty').hidden = true;
            if (!query) {
                if (searchBase) applySnapshot(searchBase);
                searchBase = null;
                return;
            }
            if (!searchBase) searchBase = snapshot();
            const matches = text => terms.every(term => normalize(text).includes(term));
            function visit(container, path) {
                let any = false;
                [...container.children].forEach(child => {
                    if (child.matches('.department-option')) {
                        child.hidden = !matches(path + ' ' + child.dataset.sourceLabel);
                        any = !child.hidden || any;
                    } else if (child.matches('[data-source-unit]')) {
                        const found = visit(child.querySelector('.source-unit-columns'), path + ' ' + child.dataset.sourceUnitName);
                        child.hidden = !found;
                        setExpanded(child, found);
                        any = found || any;
                    }
                });
                return any;
            }
            let found = false;
            panel.querySelectorAll('[data-source-group]').forEach(group => {
                const match = visit(group.querySelector('.source-group-items'), group.dataset.sourceGroup);
                group.hidden = !match;
                setGroupExpanded(group, match);
                found = match || found;
            });
            if (find('#sourceSearchEmpty')) find('#sourceSearchEmpty').hidden = found;
        }
        function restore() {
            searchBase = null;
            try {
                const saved = JSON.parse(sessionStorage.getItem(sourceKey()) || 'null');
                if (saved) {
                    applySnapshot(saved);
                    if (find('#sourceQuery')) find('#sourceQuery').value = saved.query || '';
                    search();
                    if (searchBase) scrollArea().scrollTop = saved.searchTop || 0;
                }
            } catch (_) {}
        }
        function syncCounts() {
            find('#sourcePanel').querySelectorAll('[data-source-unit]').forEach(unit => {
                const inputs = [...new Map([...unit.querySelectorAll('[data-department]:not(:disabled)')].map(input => [input.value, input])).values()];
                const selected = inputs.filter(input => input.checked).length;
                const checkbox = unit.querySelector('[data-unit-select]');
                checkbox.checked = inputs.length > 0 && selected === inputs.length;
                checkbox.disabled = !inputs.length;
                checkbox.indeterminate = selected > 0 && selected < inputs.length;
                unit.classList.toggle('has-selection', selected > 0);
            });
            const count = new Set([...find('#sourcePanel').querySelectorAll('[data-department]:checked:not(:disabled)')].map(i => i.value)).size;
            find('#clearDepartments').hidden = !count && !find('[data-source-group-filter][aria-pressed="true"]');
            find('#sourceSelectionCount').textContent = count ? ' · 已选 ' + count : '';
            find('#sourcePanel').querySelectorAll('[data-source-group]').forEach(group => {
                const selected = new Set([...group.querySelectorAll('[data-department]:checked:not(:disabled)')].map(i => i.value)).size;
                const label = group.querySelector('[data-group-selection]');
                label.textContent = '已选 ' + selected;
                label.hidden = !selected;
            });
        }
        function returnFocus() {
            const trigger = returnTarget?.isConnected && returnTarget.getClientRects().length ? returnTarget :
                [...document.querySelectorAll('[data-open-sources]')].find(item => item.getClientRects().length);
            trigger?.focus({preventScroll: true});
            returnTarget = null;
        }
        function render() {
            const currentMode = mode(), overlay = currentMode === 'overlay', panel = find('#sourcePanel');
            // Release only the inert attributes we introduced, including detached fragments.
            outsideElements.forEach(element => { element.inert = false; });
            outsideElements = [];
            workspace.dataset.sourceMode = currentMode;
            panel.hidden = currentMode === 'closed';
            panel.inert = panel.hidden;
            panel.classList.toggle('is-open', overlay);
            find('#filterScrim').hidden = !overlay;
            document.body.classList.toggle('filters-open', overlay);
            panel.removeAttribute('role');
            panel.removeAttribute('aria-modal');
            if (overlay) {
                panel.setAttribute('role', 'dialog');
                panel.setAttribute('aria-modal', 'true');
                let node = panel;
                while (node.parentElement && node !== document.body) {
                    [...node.parentElement.children].forEach(sibling => {
                        if (sibling !== node && sibling.id !== 'filterScrim' && !sibling.matches('script,style,link') && !sibling.inert) {
                            sibling.inert = true;
                            outsideElements.push(sibling);
                        }
                    });
                    node = node.parentElement;
                }
            }
            find('#pinSources').hidden = !pinViewport.matches || pinSuspended || currentMode === 'pinned';
            find('#closeFilters').setAttribute('aria-label', currentMode === 'pinned' ? '收起来源栏' : '关闭来源筛选');
            find('[data-close-sources]').hidden = currentMode === 'pinned';
            document.querySelectorAll('[data-open-sources]').forEach(trigger => trigger.setAttribute('aria-expanded', String(currentMode !== 'closed')));
        }
        function open(trigger) {
            if (mode() === 'pinned') { find('#schoolFilter').focus({preventScroll: true}); return; }
            returnTarget = trigger || document.activeElement;
            temporaryOpen = true;
            render();
            restore();
            find('#closeFilters').focus({preventScroll: true});
        }
        function close() {
            save();
            if (mode() === 'pinned') rememberPin(false);
            temporaryOpen = false;
            render();
            returnFocus();
        }
        document.addEventListener('click', event => {
            const trigger = event.target.closest('[data-open-sources]');
            const button = event.target.closest('button');
            if (trigger) open(trigger);
            else if (event.target.id === 'filterScrim' || button?.id === 'closeFilters' || button?.hasAttribute('data-close-sources')) close();
            else if (button?.id === 'pinSources' && pinViewport.matches && !pinSuspended) {
                rememberPin(true); temporaryOpen = false; render();
                find('#closeFilters').focus({preventScroll: true});
            } else if (button?.id === 'clearSourceSearch') {
                find('#sourceQuery').value = ''; search(); save(); find('#sourceQuery').focus({preventScroll: true});
            } else if (button?.matches('.source-group-toggle')) {
                setGroupExpanded(button.closest('[data-source-group]'), button.getAttribute('aria-expanded') !== 'true'); save();
            } else if (button?.matches('.source-unit-toggle')) {
                setExpanded(button.closest('[data-source-unit]'), button.getAttribute('aria-expanded') !== 'true'); save();
            }
        });
        document.addEventListener('input', event => {
            if (event.target.id !== 'sourceQuery') return;
            search();
            if (event.target.value.trim()) scrollArea().scrollTop = 0;
            save();
        });
        document.addEventListener('keydown', event => {
            if (mode() !== 'overlay') return;
            if (event.key === 'Escape') { event.preventDefault(); close(); }
            if (event.key === 'Tab') {
                const controls = [...find('#sourcePanel').querySelectorAll('a,button,select,input')].filter(item => !item.disabled && item.getClientRects().length);
                const first = controls[0], last = controls[controls.length - 1];
                if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
                if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
            }
        });
        pinViewport.addEventListener('change', () => {
            const focusedInside = find('#sourcePanel').contains(document.activeElement);
            save(); temporaryOpen = false; render(); restore();
            if (focusedInside && mode() === 'closed') returnFocus();
        });
        window.addEventListener('pagehide', save);
        render(); restore(); syncCounts();
        function suspendPin(value) {
            if (pinSuspended !== value) {
                save(); pinSuspended = value; temporaryOpen = false; render(); restore();
            } else render();
        }
        return {save, restore, render, syncCounts, setGroupExpanded, suspendPin, isOverlay: () => mode() === 'overlay'};
    };
})();
