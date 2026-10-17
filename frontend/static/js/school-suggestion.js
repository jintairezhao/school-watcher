/* Shared by school setup and the existing school editor. */
window.bindSchoolSuggestion = function ({nameInput, urlInput, hint, active = () => true, editing = () => false}) {
    let version = 0, timer, controller;
    function reset() {
        version += 1;
        clearTimeout(timer);
        controller?.abort();
        controller = null;
        hint.textContent = '';
    }
    nameInput.addEventListener('input', () => {
        reset();
        if (urlInput.dataset.autoFilled === 'true') {
            urlInput.value = '';
            urlInput.dataset.autoFilled = 'false';
        }
        const name = nameInput.value.trim(), expected = version;
        if (editing() || name.length < 2) return;
        timer = setTimeout(async () => {
            const current = new AbortController();
            controller = current;
            const timeout = setTimeout(() => current.abort(), 15000);
            try {
                const response = await fetch('/api/schools/suggest-url?name=' + encodeURIComponent(name), {signal:current.signal});
                if (!response.ok) throw new Error('地址查询暂未完成');
                const data = await response.json();
                if (expected !== version || name !== nameInput.value.trim() || !active() || editing()) return;
                if (data.found && data.url && (!urlInput.value.trim() || urlInput.dataset.autoFilled === 'true')) {
                    urlInput.value = data.url;
                    urlInput.dataset.autoFilled = 'true';
                    hint.textContent = data.message || '已匹配学校官网地址';
                } else if (!urlInput.value.trim()) hint.textContent = data.message || '未找到确切匹配，可填写官网地址';
            } catch (_) {
                if (expected === version && active() && !urlInput.value.trim()) hint.textContent = '地址查询暂未完成，可继续填写官网地址';
            } finally {
                clearTimeout(timeout);
                if (controller === current) controller = null;
            }
        }, 600);
    });
    urlInput.addEventListener('input', () => { reset(); urlInput.dataset.autoFilled = 'false'; });
    window.addEventListener('pagehide', reset);
    return {reset};
};
