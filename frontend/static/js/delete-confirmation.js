(function () {
    'use strict';
    const dialog = document.getElementById('deleteDialog');
    const field = suffix => document.getElementById('deleteDialog' + suffix);
    const cancel = field('Cancel'), confirm = field('Confirm'), error = field('Error');
    let current = null, busy = false, finish = null, opener = null;

    function close(deleted) {
        if (busy || !current) return;
        dialog.close();
        current = null;
        opener?.focus();
        const resolve = finish;
        finish = null;
        resolve(deleted);
    }

    window.confirmDeletion = options => {
        if (current) return Promise.resolve(false);
        current = options;
        opener = document.activeElement;
        field('Title').textContent = '删除' + options.kind + '？';
        field('Target').textContent = options.name;
        field('Description').textContent = options.kind === '学校'
            ? '将删除这所学校的部门、栏目、订阅和独有通知，以及这些通知的收藏与阅读记录。其他学校共用的通知会保留。'
            : '将删除这个部门或栏目的独有通知，以及这些通知的收藏与阅读记录。其他栏目共用的通知会保留。';
        confirm.textContent = '删除' + options.kind;
        cancel.disabled = confirm.disabled = false;
        error.hidden = true;
        error.textContent = '';
        field('Status').textContent = '';
        const result = new Promise(resolve => { finish = resolve; });
        dialog.showModal();
        cancel.focus();
        return result;
    };

    cancel.addEventListener('click', () => close(false));
    dialog.addEventListener('cancel', event => { event.preventDefault(); close(false); });
    dialog.addEventListener('keydown', event => {
        // Keep Escape from also closing the department panel underneath.
        if (event.key === 'Escape') event.stopPropagation();
        if (event.key === 'Tab') {
            if (busy) { event.preventDefault(); return; }
            if (event.shiftKey && document.activeElement === cancel) {
                event.preventDefault(); confirm.focus();
            } else if (!event.shiftKey && document.activeElement === confirm) {
                event.preventDefault(); cancel.focus();
            }
        }
    });
    dialog.addEventListener('click', event => {
        const rect = dialog.getBoundingClientRect();
        if (event.target === dialog && (event.clientX < rect.left || event.clientX > rect.right ||
            event.clientY < rect.top || event.clientY > rect.bottom)) close(false);
    });
    confirm.addEventListener('click', async () => {
        if (busy || !current) return;
        busy = true;
        cancel.disabled = confirm.disabled = true;
        dialog.setAttribute('aria-busy', 'true');
        confirm.textContent = '正在删除…';
        field('Status').textContent = '正在删除，请稍候。';
        error.hidden = true;
        let deleted = false;
        try {
            const response = await fetch(current.url, {method: 'DELETE', signal: AbortSignal.timeout(30000)});
            const body = await response.json().catch(() => ({}));
            // A lost response followed by a retry may find the item already gone.
            if (!response.ok && response.status !== 404) {
                throw new Error(body.error || '删除未完成，请稍后重试。');
            }
            deleted = true;
        } catch (failure) {
            error.textContent = failure.name === 'TimeoutError' || failure instanceof TypeError
                ? '暂未收到删除结果，请检查连接后重试。' : failure.message;
            error.hidden = false;
        } finally {
            busy = false;
            dialog.removeAttribute('aria-busy');
            cancel.disabled = confirm.disabled = false;
            field('Status').textContent = '';
            confirm.textContent = deleted ? '删除' + current.kind : '重试删除';
        }
        if (deleted) close(true);
        else error.focus();
    });
})();
