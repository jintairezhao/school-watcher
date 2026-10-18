(function () {
    'use strict';
    const field = id => document.getElementById(id);
    const t = message => window.I18n?.t(message) || message;
    const panel = field('aiUsagePanel');
    if (!panel) return;
    let days = 30, trendDays = 30, activityMode = 'daily', metric = 'tokens', data = null, serial = 0, controller;
    const number = value => window.I18n.number(Number(value));
    const compact = value => window.I18n.compact(Number(value));
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
        return day.date + '\n' + number(day.tokens) + ' Token · ' + number(day.calls) + uiT(' 次调用')
            + uiT('\n完整结果 ') + day.succeeded + uiT(' · 失败 ') + day.failed
            + (day.partial ? uiT(' · 部分结果 ') + day.partial : '')
            + (day.pending ? uiT(' · 进行中 ') + day.pending : '')
            + (day.uncertain ? uiT(' · 结果待确认 ') + day.uncertain : '')
            + (day.unknown_usage ? '\n' + day.unknown_usage + uiT(' 次调用未返回用量') : '');
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
        const cumulative = activityMode === 'cumulative';
        const scroll = field('aiActivityScroll');
        scroll.hidden = cumulative;
        field('aiActivityLifetime').hidden = !cumulative;
        if (cumulative) {
            const total = data.lifetime;
            text('aiLifetimeTokens', number(total.tokens));
            text('aiLifetimeCalls', number(total.calls));
            text('aiLifetimeBreakdown', uiT('输入 ') + number(total.input_tokens) + uiT(' · 输出 ') + number(total.output_tokens)
                + (total.unclassified_tokens ? uiT(' · 未细分 ') + number(total.unclassified_tokens) : ''));
            text('aiLifetimeOutcomes', total.succeeded + uiT(' 次完整结果 · ') + total.failed + uiT(' 次失败')
                + (total.partial ? ' · ' + total.partial + uiT(' 次部分结果') : '')
                + (total.uncertain ? ' · ' + total.uncertain + uiT(' 次结果待确认') : '')
                + (total.pending ? ' · ' + total.pending + uiT(' 次进行中') : ''));
            field('aiLifetimeUnknown').hidden = !total.unknown_usage;
            text('aiLifetimeUnknown', total.unknown_usage + uiT(' 次调用未返回用量，未计入 Token 总量。'));
            field('aiActivityLifetime').setAttribute('aria-label', total.start
                ? total.start + ' — ' + total.end + uiT(' · 本站记录的全部历史用量') : uiT('尚无调用记录'));
            return;
        }
        const entries = data.calendar;
        const startDay = new Date(entries[0].date + 'T00:00:00Z').getUTCDay();
        const padding = activityMode === 'weekly' ? (startDay + 6) % 7 : 0;
        const weeks = Math.ceil((padding + entries.length) / 7);
        const grid = field('aiActivityGrid'), months = field('aiActivityMonths');
        grid.replaceChildren(); months.replaceChildren();
        grid.style.setProperty('--weeks', weeks); months.style.setProperty('--weeks', weeks);
        for (let i = 0; i < padding; i++) grid.append(node('span'));
        grid.setAttribute('aria-label', activityMode === 'weekly'
            ? uiT('最近365天，每列一周，从上到下为周一至周日，方向键查看日期')
            : uiT('最近365天，每格一天，从上到下、从左到右按日期排列，方向键查看日期'));
        const max = Math.max(...entries.map(d => d.tokens), 1);
        let lastMonth = '', lastColumn = -4;
        entries.forEach((day, index) => {
            const button = inspectable(node('button', '', 'usage-day'), day);
            button.dataset.date = day.date;
            button.dataset.level = day.tokens ? Math.min(4, Math.ceil(day.tokens / max * 4)) : 0;
            // A failed/unknown call with no reported tokens still has visible activity.
            if (!day.tokens && day.calls) button.dataset.level = 1;
            grid.append(button);
            const month = day.date.slice(0, 7), column = Math.floor((padding + index) / 7) + 1;
            if (month !== lastMonth && (index > 0 || Number(day.date.slice(8)) <= 7)
                && column - lastColumn >= 3 && column < weeks - 1) {
                const label = node('span', window.I18n.month(Number(day.date.slice(5, 7))));
                label.style.gridColumn = column + ' / span 2'; months.append(label); lastColumn = column;
            }
            lastMonth = month;
        });
        grid.lastElementChild.tabIndex = 0;
        // Keep the entire year on small screens, starting at the most recent dates.
        requestAnimationFrame(() => { scroll.scrollLeft = scroll.scrollWidth; });
    }
    function renderTrend() {
        hideTooltip();
        const entries = data.calendar.slice(-trendDays);
        const values = entries.map(d => d[metric]);
        const max = Math.max(...values, 0);
        const magnitude = Math.pow(10, Math.floor(Math.log10(max || 1)));
        const ceiling = Math.max(2, Math.ceil(max / magnitude) * magnitude);
        field('aiUsageAxis').replaceChildren(...[ceiling, ceiling / 2, 0].map(value => node('span', compact(value))));
        const bars = field('aiUsageBars'); bars.replaceChildren(); bars.style.setProperty('--days', trendDays);
        entries.forEach(day => {
            const button = inspectable(node('button', '', 'usage-bar'), day);
            const bar = node('span'); bar.style.height = day[metric] / ceiling * 100 + '%';
            button.dataset.zero = String(day[metric] === 0); button.append(bar); bars.append(button);
        });
        bars.lastElementChild.tabIndex = 0;
        field('aiUsageDates').replaceChildren(...[entries[0], entries[Math.floor((trendDays - 1) / 2)], entries[trendDays - 1]]
            .map(day => node('span', day.date.slice(5).replace('-', '/'))));
        const calls = entries.reduce((sum, entry) => sum + entry.calls, 0);
        field('aiUsageEmpty').hidden = calls > 0;
        if (calls && !max) {
            field('aiUsageEmpty').hidden = false;
            text('aiUsageEmpty', uiT('这段时间有调用记录，已记录的 Token 用量为 0。'));
        } else text('aiUsageEmpty', uiT('这段时间还没有调用记录。使用 AI 识别学校来源后，会在这里显示。'));
        const rows = field('aiUsageRows'); rows.replaceChildren();
        [...entries].reverse().forEach(day => {
            const row = node('tr');
            [day.date, number(day.tokens), number(day.calls), number(day.succeeded), number(day.failed),
                number(day.partial || 0), number(day.uncertain), number(day.pending)]
                .forEach(value => row.append(node('td', value)));
            rows.append(row);
        });
    }
    function ranks(id, entries, model) {
        const box = field(id); box.replaceChildren();
        if (!entries.length) { box.append(node('p', uiT('所选时段暂无调用'), 'usage-empty')); return; }
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
            row.append(line, track, node('small', (model ? t(entry.provider_name) + ' · ' : '') + share + number(entry.calls) + uiT(' 次调用')
                + (entry.unknown_usage ? ' · ' + entry.unknown_usage + uiT(' 次用量待确认') : '')));
            box.append(row);
        });
    }
    function renderOverview() {
        if (!data) return;
        const entries = data.calendar.slice(-days);
        const summary = {};
        ['tokens', 'input_tokens', 'output_tokens', 'unclassified_tokens', 'calls', 'succeeded',
            'failed', 'partial', 'uncertain', 'pending', 'unknown_usage', 'validation_failed', 'other_failed',
            'cache_hit_tokens', 'cache_miss_tokens', 'cache_reported_calls', 'cache_unreported_calls'].forEach(key => {
            summary[key] = entries.reduce((sum, entry) => sum + (entry[key] || 0), 0);
        });
        const completed = summary.succeeded + summary.failed + summary.partial;
        summary.success_rate = completed ? Math.round(summary.succeeded / completed * 1000) / 10 : null;
        const peak = entries.reduce((best, entry) => entry.tokens > best.tokens ? entry : best);
        summary.peak_tokens = peak.tokens; summary.peak_date = peak.tokens ? peak.date : null;
        text('aiUsageRange', entries[0].date.replaceAll('-', '/') + ' — ' + data.end.replaceAll('-', '/'));
        text('aiUsageTokens', compact(summary.tokens)); field('aiUsageTokens').title = number(summary.tokens) + ' Token';
        text('aiUsageTokenDetail', uiT('输入 ') + compact(summary.input_tokens) + uiT(' · 输出 ') + compact(summary.output_tokens)
            + (summary.unclassified_tokens ? uiT(' · 未细分 ') + compact(summary.unclassified_tokens) : ''));
        text('aiUsageCalls', number(summary.calls));
        text('aiUsageCallDetail', [summary.partial && summary.partial + uiT(' 次部分结果'),
            summary.pending && summary.pending + uiT(' 次进行中'), summary.uncertain && summary.uncertain + uiT(' 次结果待确认')]
            .filter(Boolean).join(' · ') || uiT('含连接测试'));
        text('aiUsageSuccess', summary.success_rate === null ? '—' : summary.success_rate + '%');
        text('aiUsageSuccessDetail', summary.succeeded + uiT(' 次完整结果 · ') + summary.failed + uiT(' 次失败')
            + (summary.partial ? ' · ' + summary.partial + uiT(' 次部分结果') : ''));
        field('aiUsageSuccess').title = uiT('完整结果次数 ÷（完整结果＋失败＋部分结果）；不含进行中和结果待确认的调用');
        field('aiUsageFailureDetail').hidden = !summary.failed;
        text('aiUsageFailureDetail', uiT('失败分类：') + summary.validation_failed + uiT(' 次结果格式或字段不合要求 · ')
            + summary.other_failed + uiT(' 次其他失败。'));
        const cacheInput = summary.cache_hit_tokens + summary.cache_miss_tokens;
        text('aiUsageCache', cacheInput
            ? uiT('服务商输入缓存命中 ') + (Math.round(summary.cache_hit_tokens / cacheInput * 1000) / 10) + uiT('% · 命中 ')
                + number(summary.cache_hit_tokens) + uiT(' Token · 未命中 ') + number(summary.cache_miss_tokens) + ' Token。'
                + uiT('仅统计 ') + summary.cache_reported_calls + uiT(' 次有明细的调用')
                + (summary.cache_unreported_calls ? uiT('，另有 ') + summary.cache_unreported_calls + uiT(' 次未返回明细') : '') + '。'
            : summary.cache_reported_calls ? uiT('已返回的输入缓存明细中输入 Token 为 0，无法计算命中率。')
                : summary.calls ? uiT('服务商未返回输入缓存明细，命中率未知。') : uiT('暂无输入缓存记录。'));
        text('aiUsagePeak', compact(summary.peak_tokens)); field('aiUsagePeak').title = number(summary.peak_tokens) + ' Token';
        text('aiUsagePeakDate', summary.peak_date || uiT('暂无 Token 用量'));
        field('aiUsageUnknown').hidden = !summary.unknown_usage;
        text('aiUsageUnknown', summary.unknown_usage + uiT(' 次调用未返回用量，未计入 Token 统计。'));
    }
    function render() {
        renderOverview();
        renderCalendar(); renderTrend();
        ranks('aiUsagePurposes', data.purposes, false); ranks('aiUsageModels', data.models, true);
        text('aiBudgetMonth', data.budget_month + uiT(' · UTC 月度'));
        const budgets = field('aiUsageBudgets'); budgets.replaceChildren();
        data.budgets.forEach(budget => {
            const row = node('div', undefined, 'usage-budget'), label = node('p');
            label.append(node('strong', uiT(budget.label)), node('span', number(budget.used_tokens) + ' / '
                + (budget.limit ? number(budget.limit) + ' Token' : uiT('不限'))));
            row.append(label);
            if (budget.limit) {
                const track = node('div', undefined, 'usage-track'), used = node('span'), reserved = node('span', undefined, 'usage-reserved');
                const usedPercent = Math.min(100, budget.used_tokens / budget.limit * 100);
                used.style.width = usedPercent + '%';
                reserved.style.width = Math.min(100 - usedPercent, budget.reserved_tokens / budget.limit * 100) + '%';
                track.setAttribute('aria-label', uiT('已使用 ') + number(budget.used_tokens) + uiT('，预留 ') + number(budget.reserved_tokens) + uiT('，上限 ') + number(budget.limit) + ' Token');
                track.append(used, reserved); row.append(track);
            }
            if (budget.reserved_tokens) row.append(node('small', uiT('另预留 ') + number(budget.reserved_tokens) + uiT(' Token，等待结算')));
            budgets.append(row);
        });
        text('aiUsageUpdated', uiT('更新于 ') + new Date(data.generated_at).toLocaleTimeString(window.I18n.locale(), {hour:'2-digit', minute:'2-digit'}) + uiT(' · 每日统计按当前时区'));
    }
    window.loadAIUsage = async function () {
        const current = ++serial;
        if (controller) controller.abort();
        controller = new AbortController();
        const requestController = controller;
        let timedOut = false;
        const timeout = setTimeout(() => { timedOut = true; requestController.abort(); }, 20000);
        hideTooltip(); panel.setAttribute('aria-busy', 'true');
        field('aiUsageStatus').hidden = false; text('aiUsageStatus', data ? uiT('正在刷新…') : uiT('正在读取用量…'));
        try {
            const response = await fetch('/api/admin/ai/usage?days=30&offset=' + (-new Date().getTimezoneOffset()), {signal:requestController.signal});
            if (!response.ok) throw new Error(uiT('无法读取'));
            const result = await response.json();
            if (current !== serial) return;
            data = result; render();
            field('aiUsageContent').hidden = false; field('aiUsageStatus').hidden = true;
        } catch (error) {
            if (current !== serial || (error.name === 'AbortError' && !timedOut)) return;
            field('aiUsageContent').hidden = true;
            text('aiUsageStatus', uiT('用量暂时无法加载，请点击“刷新”重试。'));
        } finally {
            clearTimeout(timeout);
            if (current === serial) panel.setAttribute('aria-busy', 'false');
        }
    };
    panel.querySelectorAll('[data-usage-days]').forEach(button => button.addEventListener('click', () => {
        days = Number(button.dataset.usageDays); pressed('data-usage-days', days); renderOverview();
    }));
    panel.querySelectorAll('[data-trend-days]').forEach(button => button.addEventListener('click', () => {
        trendDays = Number(button.dataset.trendDays); pressed('data-trend-days', trendDays); if (data) renderTrend();
    }));
    panel.querySelectorAll('[data-activity-mode]').forEach(button => button.addEventListener('click', () => {
        activityMode = button.dataset.activityMode; pressed('data-activity-mode', activityMode); renderCalendar();
    }));
    panel.querySelectorAll('[data-usage-metric]').forEach(button => button.addEventListener('click', () => {
        metric = button.dataset.usageMetric; pressed('data-usage-metric', metric); if (data) renderTrend();
    }));
    field('aiUsageRefresh').addEventListener('click', () => window.loadAIUsage());
    document.addEventListener('locale-changed', () => {
        if (data) render();
    });
    keyboardChart(field('aiActivityGrid'), true); keyboardChart(field('aiUsageBars'), false);
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
