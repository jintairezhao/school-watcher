/* Collection timestamps are UTC instants; both log views display Beijing time. */
(() => {
    'use strict';
    const format = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Asia/Shanghai', year: 'numeric',
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23'});
    window.formatScrapeLogTime = (value, compact = false) => {
        if (!value) return compact ? '时间未知' : '尚未结束';
        // Old log APIs returned naive ISO values; those stored values are also UTC.
        // Never let the browser reinterpret a runtime timestamp in its host timezone.
        const explicit = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : value + 'Z';
        const date = new Date(explicit);
        if (!Number.isFinite(date.getTime())) return '时间未知';
        if (!compact) return format.format(date);
        const parts = Object.fromEntries(format.formatToParts(date).map(p => [p.type, p.value]));
        return parts.month + '-' + parts.day + ' ' + parts.hour + ':' + parts.minute;
    };
})();
