/* Explicit message catalogs only. Official notice titles and bodies are never rewritten. */
(() => {
    const data = document.getElementById('uiLocaleData');
    if (!data) return;
    const boot = JSON.parse(data.textContent);
    let prefs = boot.preferences;
    const patterns = Object.keys(boot.english).filter(key => /\{\w+\}/.test(key)).map(key => {
        const names = []; let source = '', offset = 0;
        for (const match of key.matchAll(/\{(\w+)\}/g)) {
            source += key.slice(offset,match.index).replace(/[.*+?^${}()|[\]\\]/g,'\\$&') + '(.+?)';
            names.push(match[1]); offset = match.index + match[0].length;
        }
        source += key.slice(offset).replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
        return {key,names,pattern:new RegExp('^'+source+'$')};
    });
    function t(message, variables = {}) {
        if (prefs.locale === 'en' && typeof message === 'string' && !Object.hasOwn(boot.english,message) && message.length < 2000) {
            for (const item of patterns) {
                const match = item.pattern.exec(message);
                if (match) return t(item.key,Object.fromEntries(item.names.map((name,index) => [name, name === 'count' ? Number(match[index+1].replaceAll(',','')) : match[index+1]])));
            }
        }
        let value = prefs.locale === 'en' ? (boot.english[message] ?? message) : message;
        if (value && typeof value === 'object') value = value[new Intl.PluralRules(prefs.locale).select(Number(variables.count))] || value.other;
        for (const [key, replacement] of Object.entries(variables)) {
            const label = prefs.locale === 'en' && key === 'audience'
                ? String(replacement).split(/[、＋,]/).map(role => t(role.trim())).join(', ') : replacement;
            value = value.split('{' + key + '}').join(String(label));
        }
        return value;
    }
    function apply(scope = document) {
        const official = node => node.closest('[data-official-content],.body-loader-content');
        const select = selector => [...(scope.matches?.(selector) ? [scope] : []), ...scope.querySelectorAll(selector)];
        for (const node of [...(scope.matches?.('[data-i18n]') ? [scope] : []), ...scope.querySelectorAll('[data-i18n]')]) {
            if (official(node)) continue;
            let vars = {}; try { vars = JSON.parse(node.dataset.i18nVars || '{}'); } catch (_) { /* literal */ }
            const value = t(node.dataset.i18n, vars); if (node.textContent !== value) node.textContent = value;
        }
        for (const attribute of ['placeholder','aria-label','title','alt']) {
            const selector = '[data-i18n-' + attribute + ']';
            for (const node of [...(scope.matches?.(selector) ? [scope] : []), ...scope.querySelectorAll(selector)]) if (!official(node)) {
                let vars={};try {vars=JSON.parse(node.getAttribute('data-i18n-'+attribute+'-vars') || '{}');} catch (_) {}
                let uiVars=[];try {uiVars=JSON.parse(node.getAttribute('data-i18n-'+attribute+'-ui-vars') || '[]');} catch (_) {}
                for (const name of uiVars) if (Object.hasOwn(vars,name)) vars[name]=t(vars[name]);
                node.setAttribute(attribute,t(node.getAttribute('data-i18n-'+attribute),vars));
            }
        }
        for (const node of select('time[data-ui-date]')) {
            if (official(node)) continue;
            const value = node.dataset.uiDate ? dateTime(node.dataset.uiDate) : t(node.dataset.uiDateEmpty || '时间未知');
            if (node.textContent !== value) node.textContent = value;
        }
        for (const node of select('[data-ui-number]')) {
            if (official(node)) continue;
            const value = number(Number(node.dataset.uiNumber)); if (node.textContent !== value) node.textContent = value;
        }
        for (const node of select('[data-ui-group-date]')) {
            if (official(node)) continue;
            const value = node.dataset.uiGroupDate;
            const matched = /^(\d{4})年(\d{2})月(\d{2})日$/.exec(value);
            const label = matched ? dateTime(matched.slice(1).join('-')) : t(value);
            if (node.textContent !== label) node.textContent = label;
        }
    }
    function dateTime(value, options = {}) {
        if (!value) return '—';
        const wallTime = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?$/.test(value);
        const date = new Date(wallTime ? value+'Z' : value); if (!Number.isFinite(date.getTime())) return '—';
        const onlyDate = typeof value === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(value);
        // Keep publication dates consistent with the server's existing Chinese format.
        if (onlyDate && prefs.locale === 'zh-CN' && Object.keys(options).length === 0) {
            return `${date.getUTCFullYear()}年${String(date.getUTCMonth()+1).padStart(2,'0')}月${String(date.getUTCDate()).padStart(2,'0')}日`;
        }
        const settings = onlyDate ? {dateStyle:'medium',timeZone:'UTC'}
            : {dateStyle:'medium',timeStyle:'medium',hourCycle:'h23',...(wallTime ? {timeZone:'UTC'} : prefs.time_zone ? {timeZone:prefs.time_zone} : {})};
        return new Intl.DateTimeFormat(prefs.locale, {...settings,...options}).format(date);
    }
    const number = (value, options = {}) => new Intl.NumberFormat(prefs.locale, options).format(value);
    const compact = value => prefs.locale === 'en' ? number(value,{notation:'compact',maximumFractionDigits:1})
        : value >= 100000000 ? number(value/100000000,{maximumFractionDigits:1})+' 亿'
        : value >= 10000 ? number(value/10000,{maximumFractionDigits:1})+' 万' : number(value);
    window.uiT = t;
    window.I18n = {t,apply,dateTime,number,compact,
        setText:(node,message,variables={}) => {
            node.dataset.i18n=message;node.dataset.i18nVars=JSON.stringify(variables);node.textContent=t(message,variables);
        },
        relative:(value,unit) => new Intl.RelativeTimeFormat(prefs.locale,{numeric:'auto'}).format(value,unit),
        month:value => new Intl.DateTimeFormat(prefs.locale,{month:'short',timeZone:'UTC'}).format(new Date(Date.UTC(2000,value-1,1))),
        preferences:() => ({...prefs}), locale:() => prefs.locale};
    const selector = document.getElementById('languageSelect');
    selector?.addEventListener('change', async () => {
        const original = prefs.locale; selector.disabled = true;
        const status = document.getElementById('languageStatus');
        status.textContent = t('正在切换语言…');
        try {
            const response = await fetch('/api/preferences/localization', {method:'POST',
                headers:{'Content-Type':'application/json','X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content},
                body:JSON.stringify({locale:selector.value}),signal:AbortSignal.timeout(12000)});
            if (!response.ok) throw new Error('save failed');
            const value = await response.json(); prefs = value.preferences;
            document.documentElement.lang = prefs.locale;
            apply(); document.dispatchEvent(new CustomEvent('locale-changed',{detail:{...prefs}}));
            status.textContent = t('语言已切换');
        } catch (_) { selector.value = original; status.textContent = t('语言保存失败，请重试'); }
        finally { selector.disabled = false; }
    });
    apply();
    // Only explicitly marked UI fragments participate. Official content is never scanned.
    const markers='[data-i18n],[data-i18n-placeholder],[data-i18n-aria-label],[data-i18n-title],[data-i18n-alt],time[data-ui-date],[data-ui-number],[data-ui-group-date]';
    new MutationObserver(records => {
        for (const record of records) for (const node of record.addedNodes) {
            if (node.nodeType === 1 && node.querySelector && (node.matches(markers) || node.querySelector(markers))) apply(node);
        }
    }).observe(document.body,{childList:true,subtree:true});
})();
