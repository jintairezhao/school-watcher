/* Copy the full notice title and its observed official publication URL. */
(() => {
    'use strict';
    function legacyCopy(text) {
        const active = document.activeElement;
        const selection = window.getSelection();
        const ranges = selection ? Array.from({length: selection.rangeCount}, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
        const field = document.createElement('textarea');
        field.value = text;
        field.readOnly = true;
        field.setAttribute('aria-label', '待复制的通知');
        field.style.cssText = 'position:fixed;top:0;left:-9999px;font-size:16px;';
        document.body.append(field);
        try {
            field.focus({preventScroll: true});
            field.select();
            return document.execCommand('copy');
        } finally {
            field.remove();
            if (active?.isConnected) active.focus({preventScroll: true});
            if (selection) {
                selection.removeAllRanges();
                ranges.forEach(range => selection.addRange(range));
            }
        }
    }
    document.addEventListener('click', async event => {
        const button = event.target.closest('[data-share-notice]');
        if (!button || button.disabled) return;
        const title = (button.dataset.shareTitle || '').replace(/\s+/g, ' ').trim();
        const url = button.dataset.shareUrl || '';
        if (!title || !/^https?:\/\//i.test(url)) {
            showToast('error', uiT('暂无可分享的官网原文链接'));
            return;
        }
        const text = title + '\n' + url;
        // Keep keyboard focus on the initiating control during a pending write.
        // aria-disabled fences repeated clicks without moving focus elsewhere.
        if (button.getAttribute('aria-disabled') === 'true') return;
        button.setAttribute('aria-disabled', 'true');
        button.setAttribute('aria-busy', 'true');
        try {
            let copied = false;
            try {
                if (navigator.clipboard?.writeText) {
                    await navigator.clipboard.writeText(text);
                    copied = true;
                }
            } catch (_) { /* Older or restricted webviews may allow a copy command. */ }
            if (!copied) copied = legacyCopy(text);
            if (!copied) throw new Error('Clipboard unavailable');
            showToast('success', uiT('标题和官网链接已复制'));
        } catch (_) {
            showToast('error', uiT('复制未成功，请重试或手动复制标题和原文链接'));
        } finally {
            button.removeAttribute('aria-disabled');
            button.removeAttribute('aria-busy');
        }
    });
})();
