(function () {
    'use strict';
    const field = id => document.getElementById(id);
    const panel = field('aiUsagePanel');
    if (!panel) return;
    let days = 30, metric = 'tokens', data = null, serial = 0, controller;
    const narrow = matchMedia('(max-width:640px)');
    const number = value => Number(value).toLocaleString('zh-CN');
    const compact = value => value >= 100000000 ? (value / 100000000).toLocaleString('zh-CN', {maximumFractionDigits:1}) + ' 亿'
        : value >= 10000 ? (value / 10000).toLocaleString('zh-CN', {maximumFractionDigits:1}) + ' 万' : number(value);
    const node = (tag, text, className) => {
        const element = document.createElement(tag);
        if (text !== undefined) element.textContent = text;
        if (className) element.className = className;
        return element;
    };
    const text = (id, value) => { field(id).textContent = value; };
    const pressed = (attribute, value) => panel.querySelectorAll('[' + attribute + ']').forEach(button => {
        button.setAttribute('aria-pressed', String(button.getAttribute(attribute) === String(value)));
    });
    const tooltip = field('aiUsageTooltip');
    const descriptions = new WeakMap();
    let tooltipTarget;
    function hideTooltip() {
        tooltip.hidden = true;
        if (tooltipTarget) tooltipTarget.removeAttribute('aria-describedby');
        tooltipTarget = null;
    }
    function showTooltip(target, description) {
        hideTooltip();
        tooltipTarget = target;
        tooltip.textContent = description; tooltip.hidden = false;
        target.setAttribute('aria-describedby', tooltip.id);
        const box = target.getBoundingClientRect();
        const tip = tooltip.getBoundingClientRect();
        tooltip.style.left = Math.max(12, Math.min(innerWidth - tip.width - 12, box.left + box.width / 2 - tip.width / 2)) + 'px';
        tooltip.style.top = Math.max(8, box.top > tip.height + 14 ? box.top - tip.height - 10 : Math.min(innerHeight - tip.height - 8, box.bottom + 10)) + 'px';
    }
    function describe(day) {
        return day.date + '\n' + number(day.tokens) + ' Token · ' + number(day.calls) + ' 次调用'
            + '\n成功 ' + day.succeeded + ' · 失败 ' + day.failed
            + (day.pending ? ' · 进行中 ' + day.pending : '')
            + (day.uncertain ? ' · 结果待确认 ' + day.uncertain : '')
            + (day.unknown_usage ? '\n' + day.unknown_usage + ' 次调用未返回用量' : '');
    }
    function inspectable(button, day) {
        const description = describe(day);
        descriptions.set(button, description);
        button.type = 'button'; button.tabIndex = -1;
        button.setAttribute('aria-label', description.replaceAll('\n', '，'));
        button.addEventListener('mouseenter', () => {
            // Focus scrolling can move other bars underneath a stationary pointer.
            if (descriptions.has(document.activeElement) && document.activeElement !== button) return;
            showTooltip(button, description);
        });
        button.addEventListener('mouseleave', () => {
            if (tooltipTarget === button && document.activeElement !== button) hideTooltip();
        });
        button.addEventListener('focus', () => showTooltip(button, description));
        button.addEventListener('blur', hideTooltip);
        button.addEventListener('click', () => showTooltip(button, description));
        return button;
    }
    function keyboardChart(container, calendar) {
        container.addEventListener('keydown', event => {
            const items = Array.from(container.querySelectorAll('button'));
            const index = items.indexOf(document.activeElement);
            if (index < 0) return;
            const moves = {ArrowLeft:calendar ? -7 : -1, ArrowRight:calendar ? 7 : 1, ArrowUp:-1, ArrowDown:1};
            let next;
            if (event.key === 'Escape') { hideTooltip(); return; }
            if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = items.length - 1;
            else if (event.key in moves) next = Math.min(items.length - 1, Math.max(0, index + moves[event.key]));
            else return;
            event.preventDefault(); items[index].tabIndex = -1; items[next].tabIndex = 0; items[next].focus();
        });
        container.addEventListener('click', event => {
            const target = event.target.closest('button');
            if (!target) return;
            container.querySelectorAll('button').forEach(button => { button.tabIndex = button === target ? 0 : -1; });
        });
    }
    function renderCalendar() {
        if (!data) return;
        hideTooltip();
        const entries = narrow.matches ? data.calendar.slice(-91) : data.calendar;
        const startDay = new Date(entries[0].date + 'T00:00:00Z').getUTCDay();
        const padding = (startDay + 6) % 7;
        const weeks = Math.ceil((padding + entries.length) / 7);
        const grid = field('aiActivityGrid'), months = field('aiActivityMonths');
        grid.replaceChildren(); months.replaceChildren();
        grid.style.setProperty('--weeks', weeks); months.style.setProperty('--weeks', weeks);
        for (let i = 0; i < padding; i++) grid.append(node('span'));
        const max = Math.max(...entries.map(d => d.tokens), 1);
        let lastMonth = '', lastColumn = -4;
        entries.forEach((day, index) => {
            const button = inspectable(node('button', '', 'usage-day'), day);
            button.dataset.level = day.tokens ? Math.min(4, Math.ceil(day.tokens / max * 4)) : 0;
            // A failed/unknown call with no reported tokens still has visible activity.
            if (!day.tokens && day.calls) button.dataset.level = 1;
            grid.append(button);
            const month = day.date.slice(0, 7), column = Math.floor((padding + index) / 7) + 1;
            if (month !== lastMonth && (index > 0 || Number(day.date.slice(8)) <= 7)
                && column - lastColumn >= (narrow.matches ? 3 : 4) && column < weeks - 1) {
                const label = node('span', Number(day.date.slice(5, 7)) + '月');
                label.style.gridColumn = column + ' / span 2'; months.append(label); lastColumn = column;
            }
            lastMonth = month;
        });
        grid.lastElementChild.tabIndex = 0;
        text('aiActivityPeriod', narrow.matches ? '近 13 周' : '近一年');
        text('aiActivitySummary', entries.filter(d => d.calls > 0).length + ' 天有调用');
    }
    function renderTrend() {
        hideTooltip();
        const values = data.daily.map(d => d[metric]);
        const max = Math.max(...values, 0);
        const magnitude = Math.pow(10, Math.floor(Math.log10(max || 1)));
        const ceiling = Math.max(2, Math.ceil(max / magnitude) * magnitude);
        field('aiUsageAxis').replaceChildren(...[ceiling, ceiling / 2, 0].map(value => node('span', compact(value))));
        const bars = field('aiUsageBars'); bars.replaceChildren(); bars.style.setProperty('--days', days);
        data.daily.forEach(day => {
            const button = inspectable(node('button', '', 'usage-bar'), day);
            const bar = node('span'); bar.style.height = day[metric] / ceiling * 100 + '%';
            button.dataset.zero = String(day[metric] === 0); button.append(bar); bars.append(button);
        });
        bars.lastElementChild.tabIndex = 0;
        field('aiUsageDates').replaceChildren(...[data.daily[0], data.daily[Math.floor((days - 1) / 2)], data.daily[days - 1]]
            .map(day => node('span', day.date.slice(5).replace('-', '/'))));
        field('aiUsageEmpty').hidden = data.summary.calls > 0;
        if (data.summary.calls && !max) {
            field('aiUsageEmpty').hidden = false;
            text('aiUsageEmpty', '这段时间有调用记录，已记录的 Token 用量为 0。');
        } else text('aiUsageEmpty', '这段时间还没有调用记录。使用 AI 发现栏目或生成摘要后，会在这里显示。');
        const rows = field('aiUsageRows'); rows.replaceChildren();
        [...data.daily].reverse().forEach(day => {
            const row = node('tr');
            [day.date, number(day.tokens), number(day.calls), number(day.succeeded), number(day.failed), number(day.uncertain)]
                .forEach(value => row.append(node('td', value)));
            rows.append(row);
        });
    }
    function ranks(id, entries, model) {
        const box = field(id); box.replaceChildren();
        if (!entries.length) { box.append(node('p', '所选时段暂无调用', 'usage-empty')); return; }
        entries.forEach(entry => {
            const row = node('div', undefined, 'usage-rank');
            const line = node('div', undefined, 'usage-rank-label');
            line.append(node('strong', model ? entry.model : entry.label), node('span', compact(entry.tokens) + ' Token'));
            line.title = number(entry.tokens) + ' Token';
            const track = node('div', undefined, 'usage-track'), fill = node('span');
            fill.style.width = (data.summary.tokens ? entry.tokens / data.summary.tokens * 100 : 0) + '%';
            track.setAttribute('aria-hidden', 'true'); track.append(fill);
            const percent = data.summary.tokens ? entry.tokens / data.summary.tokens * 100 : 0;
            const share = data.summary.tokens ? (percent > 0 && percent < .1 ? '<0.1' : percent.toFixed(1)) + '% · ' : '';
            row.append(line, track, node('small', (model ? entry.provider_name + ' · ' : '') + share + number(entry.calls) + ' 次调用'
                + (entry.unknown_usage ? ' · ' + entry.unknown_usage + ' 次用量待确认' : '')));
            box.append(row);
        });
    }
    function render() {
        const summary = data.summary;
        text('aiUsageRange', data.start.replaceAll('-', '/') + ' — ' + data.end.replaceAll('-', '/'));
        text('aiUsageTokens', compact(summary.tokens)); field('aiUsageTokens').title = number(summary.tokens) + ' Token';
        text('aiUsageTokenDetail', '输入 ' + compact(summary.input_tokens) + ' · 输出 ' + compact(summary.output_tokens)
            + (summary.unclassified_tokens ? ' · 未细分 ' + compact(summary.unclassified_tokens) : ''));
        text('aiUsageCalls', number(summary.calls));
        text('aiUsageCallDetail', summary.pending || summary.uncertain
            ? summary.pending + ' 次进行中 · ' + summary.uncertain + ' 次结果待确认' : '含连接测试');
        text('aiUsageSuccess', summary.success_rate === null ? '—' : summary.success_rate + '%');
        text('aiUsageSuccessDetail', summary.succeeded + ' 次成功 · ' + summary.failed + ' 次失败');
        field('aiUsageSuccess').title = '成功次数 ÷ 已有明确结果的调用次数；不含进行中和结果待确认的调用';
        text('aiUsagePeak', compact(summary.peak_tokens)); field('aiUsagePeak').title = number(summary.peak_tokens) + ' Token';
        text('aiUsagePeakDate', summary.peak_date || '暂无 Token 用量');
        field('aiUsageUnknown').hidden = !summary.unknown_usage;
        text('aiUsageUnknown', summary.unknown_usage + ' 次调用未返回用量，未计入 Token 统计。');
        renderCalendar(); renderTrend();
        ranks('aiUsagePurposes', data.purposes, false); ranks('aiUsageModels', data.models, true);
        text('aiBudgetMonth', data.budget_month + ' · UTC 月度');
        const budgets = field('aiUsageBudgets'); budgets.replaceChildren();
        data.budgets.forEach(budget => {
            const row = node('div', undefined, 'usage-budget'), label = node('p');
            label.append(node('strong', budget.label), node('span', number(budget.used_tokens) + ' / '
                + (budget.limit ? number(budget.limit) + ' Token' : '不限')));
            row.append(label);
            if (budget.limit) {
                const track = node('div', undefined, 'usage-track'), used = node('span'), reserved = node('span', undefined, 'usage-reserved');
                const usedPercent = Math.min(100, budget.used_tokens / budget.limit * 100);
                used.style.width = usedPercent + '%';
                reserved.style.width = Math.min(100 - usedPercent, budget.reserved_tokens / budget.limit * 100) + '%';
                track.setAttribute('aria-label', '已使用 ' + number(budget.used_tokens) + '，预留 ' + number(budget.reserved_tokens) + '，上限 ' + number(budget.limit) + ' Token');
                track.append(used, reserved); row.append(track);
            }
            if (budget.reserved_tokens) row.append(node('small', '另预留 ' + number(budget.reserved_tokens) + ' Token，等待结算'));
            budgets.append(row);
        });
        text('aiUsageUpdated', '更新于 ' + new Date(data.generated_at).toLocaleTimeString('zh-CN', {hour:'2-digit', minute:'2-digit'}) + ' · 每日统计按当前时区');
    }
    window.loadAIUsage = async function () {
        const current = ++serial;
        if (controller) controller.abort();
        controller = new AbortController();
        const requestController = controller;
        let timedOut = false;
        const timeout = setTimeout(() => { timedOut = true; requestController.abort(); }, 20000);
        hideTooltip(); panel.setAttribute('aria-busy', 'true');
        field('aiUsageStatus').hidden = false; text('aiUsageStatus', data ? '正在刷新…' : '正在读取用量…');
        try {
            const response = await fetch('/api/admin/ai/usage?days=' + days + '&offset=' + (-new Date().getTimezoneOffset()), {signal:requestController.signal});
            if (!response.ok) throw new Error('无法读取');
            const result = await response.json();
            if (current !== serial) return;
            data = result; render();
            field('aiUsageContent').hidden = false; field('aiUsageStatus').hidden = true;
        } catch (error) {
            if (current !== serial || (error.name === 'AbortError' && !timedOut)) return;
            field('aiUsageContent').hidden = true;
            text('aiUsageStatus', '用量暂时无法加载，请点击“刷新”重试。');
        } finally {
            clearTimeout(timeout);
            if (current === serial) panel.setAttribute('aria-busy', 'false');
        }
    };
    panel.querySelectorAll('[data-usage-days]').forEach(button => button.addEventListener('click', () => {
        days = Number(button.dataset.usageDays); pressed('data-usage-days', days); window.loadAIUsage();
    }));
    panel.querySelectorAll('[data-usage-metric]').forEach(button => button.addEventListener('click', () => {
        metric = button.dataset.usageMetric; pressed('data-usage-metric', metric); if (data) renderTrend();
    }));
    field('aiUsageRefresh').addEventListener('click', () => window.loadAIUsage());
    keyboardChart(field('aiActivityGrid'), true); keyboardChart(field('aiUsageBars'), false);
    narrow.addEventListener('change', renderCalendar);
    window.addEventListener('scroll', () => {
        if (tooltipTarget && (document.activeElement === tooltipTarget || tooltipTarget.matches(':hover'))) {
            const box = tooltipTarget.getBoundingClientRect();
            if (box.bottom > 0 && box.top < innerHeight) {
                showTooltip(tooltipTarget, tooltip.textContent);
                return;
            }
        }
        hideTooltip();
    }, true);
    window.addEventListener('resize', hideTooltip);
    window.addEventListener('hashchange', hideTooltip);
    window.loadAIUsage();
})();
