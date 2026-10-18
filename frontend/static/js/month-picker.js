/* Editable YYYY-MM field with a keyboard-accessible, themed month popover. */
(() => {
    'use strict';
    document.querySelectorAll('[data-month-picker]').forEach(root => {
        const input = root.querySelector('.form-input');
        const toggle = root.querySelector('[popovertarget]');
        const panel = root.querySelector('[popover]');
        const year = panel.querySelector('input[type="number"]');
        const months = [...panel.querySelectorAll('[data-month]')];
        const error = document.getElementById('sinceMonthError');
        const steps = [...panel.querySelectorAll('[data-year-step]')];
        const supportsPopover = typeof panel.showPopover === 'function';
        let opened = false;

        function bounds() {
            const now = new Date();
            return {min: input.min || '1900-01',
                max: input.max || `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`};
        }
        function valueFor(y, m) { return `${y}-${String(m).padStart(2, '0')}`; }
        function validate(showError = false) {
            const {min, max} = bounds();
            const validFormat = /^[0-9]{4}-(0[1-9]|1[0-2])$/.test(input.value);
            const message = !validFormat ? uiT('请选择起始年月，或按 2023-01 的格式输入。') :
                input.value < min || input.value > max ? uiT('起始年月需在 1900 年至本月之间。') : '';
            input.setCustomValidity(message);
            input.setAttribute('aria-invalid', String(Boolean(showError && message)));
            error.hidden = !showError || !message;
            error.textContent = showError ? message : '';
            return !message;
        }
        function normalize() {
            if (/^[0-9]{6}$/.test(input.value)) input.value = input.value.slice(0, 4) + '-' + input.value.slice(4);
        }
        function render() {
            const {min, max} = bounds();
            year.min = min.slice(0, 4);
            year.max = max.slice(0, 4);
            const y = year.valueAsNumber;
            const validYear = year.validity.valid && Number.isInteger(y);
            steps.forEach(button => {
                const next = y + Number(button.dataset.yearStep);
                button.disabled = !validYear || next < Number(year.min) || next > Number(year.max);
            });
            const enabled = [];
            months.forEach(button => {
                const value = valueFor(y, button.dataset.month);
                button.disabled = !validYear || value < min || value > max;
                button.setAttribute('aria-pressed', String(value === input.value));
                button.setAttribute('aria-label', window.I18n?.locale() === 'en' ? window.I18n.month(Number(button.dataset.month))+' '+y : uiT("{p0}年{p1}月", {p0:(y), p1:(button.dataset.month)}));
                const label = button.querySelector('[data-month-label]');
                if (label && window.I18n) label.textContent = window.I18n.month(Number(button.dataset.month));
                if (value === max) button.setAttribute('aria-current', 'date');
                else button.removeAttribute('aria-current');
                button.tabIndex = -1;
                if (!button.disabled) enabled.push(button);
            });
            const active = enabled.find(button => button.getAttribute('aria-pressed') === 'true') || enabled[0];
            if (active) active.tabIndex = 0;
        }
        function position() {
            if (!opened) return;
            const anchor = input.getBoundingClientRect();
            const box = panel.getBoundingClientRect();
            const left = Math.max(12, Math.min(anchor.left, window.innerWidth - box.width - 12));
            let top = anchor.bottom + 8;
            if (top + box.height > window.innerHeight - 12) top = anchor.top - box.height - 8;
            top = Math.max(12, Math.min(top, window.innerHeight - box.height - 12));
            panel.style.left = `${left}px`;
            panel.style.top = `${top}px`;
        }
        function prepare() {
            normalize();
            const {max} = bounds();
            year.value = validate() ? input.value.slice(0, 4) : max.slice(0, 4);
            render();
        }
        function afterOpen() {
            opened = true;
            toggle.setAttribute('aria-expanded', 'true');
            position();
            panel.querySelector('[data-month][tabindex="0"]')?.focus({preventScroll: true});
        }
        function close(restoreFocus = false) {
            if (!opened) return;
            if (supportsPopover) panel.hidePopover();
            else panel.hidden = true;
            opened = false;
            toggle.setAttribute('aria-expanded', 'false');
            if (restoreFocus) toggle.focus({preventScroll: true});
        }
        function choose(value) {
            input.value = value;
            input.dispatchEvent(new Event('change', {bubbles: true}));
            close();
            input.focus({preventScroll: true});
        }
        if (supportsPopover) {
            panel.addEventListener('beforetoggle', event => { if (event.newState === 'open') prepare(); });
            panel.addEventListener('toggle', event => {
                if (event.newState === 'open') afterOpen();
                else { opened = false; toggle.setAttribute('aria-expanded', 'false'); }
            });
        } else {
            panel.hidden = true;
            toggle.addEventListener('click', () => {
                if (opened) close();
                else { prepare(); panel.hidden = false; afterOpen(); }
            });
            document.addEventListener('click', event => { if (opened && !root.contains(event.target)) close(); });
        }
        input.addEventListener('input', () => validate());
        input.addEventListener('change', () => {
            normalize();
            toggle.disabled = input.disabled;
            validate(Boolean(input.value));
            if (opened) render();
        });
        input.addEventListener('invalid', event => {
            event.preventDefault();
            normalize();
            validate(true);
            input.focus();
        });
        input.addEventListener('keydown', event => {
            if (event.key === 'ArrowDown' && event.altKey && !toggle.disabled) {
                event.preventDefault();
                if (supportsPopover) panel.showPopover();
                else toggle.click();
            }
        });
        year.addEventListener('input', render);
        year.addEventListener('keydown', event => {
            if (event.key === 'Enter') {
                event.preventDefault();
                if (year.reportValidity()) panel.querySelector('[data-month][tabindex="0"]')?.focus();
            }
        });
        steps.forEach(button => button.addEventListener('click', () => {
            year.value = Number(year.value) + Number(button.dataset.yearStep);
            render();
        }));
        months.forEach(button => button.addEventListener('click', () => choose(valueFor(year.valueAsNumber, button.dataset.month))));
        panel.querySelectorAll('[data-month-shortcut]').forEach(button => button.addEventListener('click', () => {
            const {max} = bounds();
            const currentYear = Number(max.slice(0, 4));
            choose(button.dataset.monthShortcut === 'current' ? max :
                valueFor(currentYear - (button.dataset.monthShortcut === 'last-year' ? 1 : 0), 1));
        }));
        panel.addEventListener('keydown', event => {
            if (event.key === 'Escape') { event.preventDefault(); close(true); return; }
            const button = event.target.closest('[data-month]');
            if (!button) return;
            const moves = {ArrowLeft: -1, ArrowRight: 1, ArrowUp: -4, ArrowDown: 4};
            let next = months.indexOf(button);
            if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = months.findLastIndex(item => !item.disabled);
            else if (event.key === 'PageUp' || event.key === 'PageDown') {
                event.preventDefault();
                const step = steps.find(item => Number(item.dataset.yearStep) === (event.key === 'PageUp' ? -1 : 1));
                if (!step.disabled) step.click();
                next = Math.min(next, months.findLastIndex(item => !item.disabled));
            } else if (Object.hasOwn(moves, event.key)) next += moves[event.key];
            else return;
            event.preventDefault();
            next = Math.max(0, Math.min(next, months.findLastIndex(item => !item.disabled)));
            if (months[next] && !months[next].disabled) {
                months.forEach(item => { item.tabIndex = -1; });
                months[next].tabIndex = 0;
                months[next].focus();
            }
        });
        window.addEventListener('resize', position);
        window.addEventListener('scroll', position, true);
        window.addEventListener('hashchange', () => close());
        document.addEventListener('locale-changed', render);
        validate();
    });
})();
