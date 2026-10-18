/* Collection timestamps are UTC instants; preserve the legacy default and honor an explicit display zone. */
(() => {
    'use strict';
    const uiT = (message,variables) => window.I18n?.t(message,variables) || message;
    const zone = () => window.I18n?.preferences().time_zone || (window.I18n?.locale() === 'en' ? Intl.DateTimeFormat().resolvedOptions().timeZone : 'Asia/Shanghai');
    const caption = () => document.querySelectorAll('[data-log-timezone]').forEach(node => {
        node.textContent = window.I18n?.locale() !== 'en' && zone()==='Asia/Shanghai' ? '北京时间 (Asia/Shanghai)'
            : uiT('日志时间显示：{zone}',{zone:zone()});
    });
    const formatter = () => new Intl.DateTimeFormat(window.I18n?.locale() || 'zh-CN', {
        timeZone:zone(), year:'numeric',
        month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'});
    window.formatScrapeLogTime = (value, compact = false) => {
        if (!value) return compact ? uiT('时间未知') : uiT('尚未结束');
        // Old log APIs returned naive ISO values; those stored values are also UTC.
        // Never let the browser reinterpret a runtime timestamp in its host timezone.
        const explicit = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + 'Z';
        const date = new Date(explicit);
        if (!Number.isFinite(date.getTime())) return uiT('时间未知');
        const format = formatter();
        if (!compact) return format.format(date);
        const parts = Object.fromEntries(format.formatToParts(date).map(p => [p.type, p.value]));
        return parts.month + '-' + parts.day + ' ' + parts.hour + ':' + parts.minute;
    };
    caption(); document.addEventListener('locale-changed', caption);
})();
