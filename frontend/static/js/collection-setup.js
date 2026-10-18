(function () {
    'use strict';
    const root = document.getElementById('collectionSetup');
    if (!root) return;
    const target = JSON.parse(document.getElementById('setupTarget').textContent);
    const field = id => document.getElementById(id);
    const t = (text,vars) => window.I18n?.t(text,vars) || text;
    const previews = JSON.parse(field('setupProfilePreviews').textContent);
    const selectedProfiles = () => [...root.querySelectorAll('[name="profiles"]:checked')].map(input => input.value);
    function renderProfile() {
        const preview = previews.find(item => item.key === selectedProfiles().join('+'));
        const separator = window.I18n?.locale() === 'en' ? ', ' : '、';
        const label = preview?.label.split('＋').map(part => t(part)).join(window.I18n?.locale() === 'en' ? ' + ' : '＋');
        field('setupProfileLabel').textContent = preview ? t('当前画像：{label}',{label}) : t('请选择至少一种身份');
        field('setupProfileSummary').textContent = preview ? t('关注方向：{categories}。',{categories:preview.categories.map(text => t(text)).join(separator)}) : t('选择后可预览你的探索重点。');
        field('setupProfileOverlap').textContent = preview?.shared.length
            ? t('共同需求会适度优先：{categories}；同时保留各身份独有的需求。',{categories:preview.shared.map(text => t(text)).join(separator)}) : '';
    }
    root.querySelectorAll('[name="profiles"]').forEach(input => input.addEventListener('change', renderProfile));
    renderProfile();
    document.addEventListener('locale-changed',renderProfile);
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
        if (!response.ok) throw new Error(t(result.error || uiT('保存未完成，请重试')));
        return result;
    }
    document.addEventListener('ai-settings-updated', event => {
        const data = event.detail;
        const profile = data.profiles.find(p => p.id === data.bindings.directory && p.enabled && p.tested);
        const state = field('setupAIState');
        state.dataset.i18n = profile ? uiT('已配置：{label}，可直接复用。')
            : uiT('尚未连接学校来源识别服务。可以在下方配置，也可以暂不使用 AI。');
        state.dataset.i18nVars = JSON.stringify({label:profile?.model || ''});
        state.textContent = t(state.dataset.i18n, JSON.parse(state.dataset.i18nVars));
    });
    field('collectionSetupForm').addEventListener('submit', async event => {
        event.preventDefault();
        if (busy) return;
        busy = true;
        const button = field('setupStart'), original = button.textContent;
        button.disabled = true;
        button.textContent = t('正在保存…');
        field('setupFeedback').hidden = true;
        try {
            const data = {ai_mode:root.querySelector('[name="ai_mode"]:checked').value};
            data.profiles = selectedProfiles();
            if (!data.profiles.length) throw new Error(t('请至少选择一种身份，可同时选择多项'));
            data.localization = {education_system:field('setupEducation').value,time_zone:field('setupTimeZone').value.trim()};
            if (root.dataset.admin === 'true') {
                data.since_date = field('setupSinceDate').value;
                data.interval = Number(field('setupInterval').value);
            }
            const saved = await post('/api/collection/setup', data);
            let result;
            if (target.kind === 'settings') {
                field('setupFeedback').textContent = t('基础配置已保存，下次更新使用这些设置。');
                field('setupFeedback').hidden = false;
                return;
            }
            button.textContent = t(target.kind === 'add' ? uiT('正在添加学校…') : uiT('正在订阅…'));
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
            button.textContent = t(button.dataset.i18n || original);
        }
    });
    if (window.loadAISettings) window.loadAISettings();
})();
