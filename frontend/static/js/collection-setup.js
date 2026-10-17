(function () {
    'use strict';
    const root = document.getElementById('collectionSetup');
    if (!root) return;
    const target = JSON.parse(document.getElementById('setupTarget').textContent);
    const field = id => document.getElementById(id);
    if (target.kind === 'add') window.bindSchoolSuggestion({nameInput:field('setupSchoolName'),
        urlInput:field('setupSchoolUrl'), hint:field('setupSchoolUrlHint')});
    let busy = false;
    root.querySelectorAll('[name="ai_mode"]').forEach(input => input.addEventListener('change', () => {
        field('setupFeedback').hidden = true;
        if (field('setupAIDetails')) field('setupAIDetails').open = input.value === 'ai';
    }));
    async function post(url, data) {
        const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json',
            'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}, body:JSON.stringify(data)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || '保存未完成，请重试');
        return result;
    }
    document.addEventListener('ai-settings-updated', event => {
        const data = event.detail;
        const profile = data.profiles.find(p => p.id === data.bindings.directory && p.enabled && p.tested);
        field('setupAIState').textContent = profile ? '已配置：' + profile.model + '，可直接复用。'
            : '尚未连接学校来源识别服务。请测试并保存功能设置，或选择暂不使用 AI。';
    });
    field('collectionSetupForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        busy = true;
        const button = field('setupStart'), original = button.textContent;
        button.disabled = true;
        button.textContent = '正在保存…';
        field('setupFeedback').hidden = true;
        try {
            const data = {ai_mode:root.querySelector('[name="ai_mode"]:checked').value};
            if (root.dataset.admin === 'true') {
                data.since_date = field('setupSinceDate').value;
                data.interval = Number(field('setupInterval').value);
            }
            const saved = await post('/api/collection/setup', data);
            let result;
            if (target.kind === 'settings') {
                field('setupFeedback').textContent = '基础配置已保存，下次更新使用这些设置。';
                field('setupFeedback').hidden = false;
                return;
            }
            button.textContent = '正在' + (target.kind === 'add' ? '添加学校…' : '订阅…');
            const mode = {ai_assist:saved.ai_assist};
            if (target.kind === 'school') result = await post('/api/subscriptions', {school_id:target.school_id, ...mode});
            else if (target.kind === 'catalog') result = await post('/api/catalog/subscribe', {name:target.name, ...mode});
            else result = await post('/api/schools', {name:field('setupSchoolName').value.trim(), url:field('setupSchoolUrl').value.trim(), ...mode});
            location.assign('/subscriptions/' + (target.school_id || result.school_id || result.id));
        } catch (error) {
            field('setupFeedback').textContent = error.message;
            field('setupFeedback').hidden = false;
            field('setupFeedback').focus();
        } finally {
            busy = false;
            button.disabled = false;
            button.textContent = original;
        }
    });
    if (window.loadAISettings) window.loadAISettings();
})();
