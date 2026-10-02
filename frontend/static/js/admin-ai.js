(function () {
    'use strict';
    let settings = null;
    let savedKey = null;
    let keyRevision = 0;
    let editingVersion = null;
    const field = id => document.getElementById(id);
    async function api(url, method, data) {
        const response = await fetch(url, {method: method || 'GET', headers: {'Content-Type': 'application/json'},
            body: data === undefined ? undefined : JSON.stringify(data)});
        const result = await response.json();
        if (!response.ok || result.status === 'failed') throw new Error(result.error || '操作未完成，请重试');
        return result;
    }
    function option(value, label) { return new Option(label, value); }
    const regionNames = {'default': '默认区域', 'cn-beijing': '中国内地（北京）',
        'ap-southeast-1': '新加坡', 'us-east-1': '美国（弗吉尼亚）'};
    function profileLabel(profile) {
        const provider = settings.providers.find(p => p.id === profile.provider);
        const parts = [provider ? provider.name : profile.provider, profile.model];
        if (provider && provider.regions.length > 1) parts.push(regionNames[profile.region] || profile.region);
        if (settings.profiles.some(p => p.id !== profile.id && p.provider === profile.provider &&
                p.model === profile.model && p.region === profile.region)) parts.push('连接 ' + profile.id);
        return parts.join(' · ');
    }
    function profileStatus(profile) {
        if (!profile.has_key) return '已移除密钥';
        if (profile.enabled && profile.tested) return '可用';
        if (profile.tested) return '已停用';
        return profile.last_test_code && profile.last_test_code !== 'not_tested' ? '测试未通过' : '待测试';
    }
    function regions(selected) {
        const provider = settings.providers.find(p => p.id === field('aiProvider').value);
        field('aiRegion').replaceChildren(...(provider ? provider.regions : []).map(r => option(r, regionNames[r] || r)));
        if (selected) field('aiRegion').value = selected;
        field('aiRegionGroup').hidden = !provider || provider.regions.length <= 1;
        field('aiModelHelp').textContent = provider && provider.id === 'ark'
            ? '填写方舟控制台中的模型名称；如果你创建过自定义推理接入点，也可以填它的 ep- 开头编号。这个编号代表你在方舟创建的模型调用入口。'
            : '填写' + (provider ? provider.name : '服务商') + '提供的模型名称，按控制台或 API 文档中的原样复制。';
    }
    async function action(button, operation) {
        button.disabled = true;
        try { await operation(); }
        catch (error) { field('aiStatus').textContent = error.message; }
        finally { button.disabled = false; }
    }
    function edit(profile) {
        clearKey();
        field('aiStatus').textContent = '';
        editingVersion = profile.version;
        field('aiProfileId').value = profile.id;
        field('aiFormTitle').textContent = '修改 AI 服务';
        field('aiReset').hidden = false;
        field('aiProvider').value = profile.provider;
        regions(profile.region);
        field('aiModel').value = profile.model;
        field('aiKey').required = !profile.has_key;
        field('aiKey').placeholder = profile.has_key ? '已保存密钥，点击小眼睛查看' : '';
        field('aiKeyHelp').textContent = profile.has_key
            ? '点击小眼睛查看已保存的密钥。留空继续使用原密钥，填写新密钥则替换。'
            : '这项服务的密钥已移除，请重新填写。';
        field('aiProvider').focus();
    }
    function resetForm() {
        field('aiProfileForm').reset();
        clearKey();
        editingVersion = null;
        field('aiProfileId').value = '';
        field('aiFormTitle').textContent = '添加 AI 服务';
        field('aiReset').hidden = true;
        field('aiKey').required = true;
        field('aiKeyHelp').textContent = '从所选服务商获取密钥。密钥加密保存，点击小眼睛可显示或隐藏。';
        regions();
    }
    function keyVisibility(visible) {
        field('aiKey').type = visible ? 'text' : 'password';
        const label = visible ? '隐藏 API 密钥' : '显示 API 密钥';
        field('aiKeyToggle').setAttribute('aria-label', label);
        field('aiKeyToggle').title = label;
        field('aiKeyToggle').setAttribute('aria-pressed', String(visible));
        field('aiKeyShowIcon').toggleAttribute('hidden', visible);
        field('aiKeyHideIcon').toggleAttribute('hidden', !visible);
    }
    function clearKey() {
        keyRevision += 1;
        savedKey = null;
        field('aiKey').value = '';
        field('aiKey').placeholder = '';
        keyVisibility(false);
    }
    async function toggleKey() {
        if (field('aiKey').type === 'text') { keyVisibility(false); return; }
        const id = Number(field('aiProfileId').value);
        const profile = settings && settings.profiles.find(p => p.id === id);
        if (!field('aiKey').value && profile && profile.has_key) {
            const revision = keyRevision;
            const result = await api('/api/admin/ai/profiles/' + id + '/key', 'POST', {expected_version: editingVersion});
            if (revision !== keyRevision) return;
            savedKey = result.api_key;
            field('aiKey').value = savedKey;
        }
        keyVisibility(true);
    }
    window.loadAISettings = async function () {
        try {
            settings = await api('/api/admin/ai');
            field('aiStatus').textContent = settings.encryption_available
                ? (settings.legacy ? '原有 DeepSeek 摘要服务可继续使用。要发现学校栏目，请添加并测试服务，再保存下方的功能设置。' : '')
                : '尚未配置本实例的加密主密钥，请按部署文档设置后保存 API 密钥。';
            const currentProvider = field('aiProvider').value;
            const currentRegion = field('aiRegion').value;
            field('aiProvider').replaceChildren(...settings.providers.map(p => option(p.id, p.name)));
            if (currentProvider) field('aiProvider').value = currentProvider;
            regions(currentRegion);
            const profiles = field('aiProfiles'); profiles.replaceChildren();
            if (!settings.profiles.length) profiles.textContent = '还没有连接 AI，请在下方添加。';
            settings.profiles.forEach(profile => {
                const row = document.createElement('div'); row.className = 'admin-user-row';
                const label = document.createElement('span');
                label.className = 'ai-profile-label';
                label.textContent = profileLabel(profile) + ' · ' + profileStatus(profile);
                row.append(label);
                const actions = document.createElement('span'); actions.className = 'admin-user-actions';
                [['编辑', () => edit(profile)], ['测试连接（调用 API）', async () => {
                    field('aiStatus').textContent = '正在测试 AI 连接…';
                    const result = await api('/api/admin/ai/profiles/' + profile.id + '/test', 'POST', {});
                    await window.loadAISettings();
                    field('aiStatus').textContent = result.tested === true ? '测试通过，请在下方选择使用这个服务的功能并保存。' : (result.test_code === 'usage_unavailable' ? '服务商未返回调用用量，暂时无法启用。' : (result.error || '测试未通过，请核对模型名称、密钥及其所属区域。'));
                }], [profile.enabled ? '停用' : '启用', async () => {
                    const enabled = !profile.enabled;
                    await api('/api/admin/ai/profiles/' + profile.id, 'PUT', {enabled, expected_version: profile.version});
                    if (Number(field('aiProfileId').value) === profile.id) resetForm();
                    await window.loadAISettings();
                    field('aiStatus').textContent = enabled ? '服务已启用。' : '服务已停用，密钥和功能设置已保留。';
                }], ['删除', async () => {
                    if (!window.confirm('删除“' + profileLabel(profile) + '”？密钥和功能绑定将一并移除，历史用量记录会保留。')) return;
                    await api('/api/admin/ai/profiles/' + profile.id, 'DELETE');
                    if (Number(field('aiProfileId').value) === profile.id) resetForm();
                    await window.loadAISettings();
                    field('aiStatus').textContent = '服务已删除。';
                }]].forEach(([text, handler]) => {
                    const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-xs btn-outline';
                    if (text === '删除') button.className = 'btn btn-xs btn-danger-outline';
                    if (!profile.has_key && text === '测试连接（调用 API）') button.disabled = true;
                    if (text === '启用' && (!profile.has_key || !profile.tested)) {
                        button.disabled = true;
                        button.title = profile.has_key ? '请先测试连接，通过后即可启用' : '请先编辑服务并填写密钥';
                    }
                    button.textContent = text; button.addEventListener('click', () => action(button, handler)); actions.append(button);
                });
                row.append(actions); profiles.append(row);
            });
            for (const [id, purpose] of [['aiDirectory', 'directory'], ['aiSummary', 'summary']]) {
                field(id).replaceChildren(option('', '请选择测试通过的服务'), ...settings.profiles
                    .filter(p => (p.enabled && p.tested) || p.id === settings.bindings[purpose])
                    .map(p => {
                        const available = p.enabled && p.tested;
                        const item = option(p.id, profileLabel(p) + (available ? '' : '（' + profileStatus(p) + '）'));
                        item.disabled = !available;
                        return item;
                    }));
                field(id).value = settings.bindings[purpose] || '';
            }
            for (const [id, key] of [['aiTotalLimit', 'total'], ['aiDirectoryLimit', 'directory'], ['aiSummaryLimit', 'summary'], ['aiSchoolLimit', 'school_discovery']]) field(id).value = settings.limits[key];
            field('aiConcurrent').value = settings.max_concurrent;
        } catch (error) { field('aiStatus').textContent = error.message; }
    };
    field('aiProvider').addEventListener('change', () => regions());
    field('aiKeyToggle').addEventListener('click', event => action(event.currentTarget, toggleKey));
    field('aiKey').addEventListener('input', () => { keyRevision += 1; });
    window.addEventListener('hashchange', () => { keyRevision += 1; keyVisibility(false); });
    field('aiReset').addEventListener('click', resetForm);
    field('aiProfileForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            const id = field('aiProfileId').value;
            const data = {provider: field('aiProvider').value,
                region: field('aiRegion').value, model: field('aiModel').value};
            if (field('aiKey').value && field('aiKey').value !== savedKey) data.api_key = field('aiKey').value;
            if (id) data.expected_version = editingVersion;
            const saved = await api('/api/admin/ai/profiles' + (id ? '/' + id : ''), id ? 'PUT' : 'POST', data);
            resetForm();
            await window.loadAISettings();
            field('aiStatus').textContent = saved.enabled && saved.tested
                ? '服务已保存，可在下方选择使用它的功能。'
                : (saved.tested ? '服务已保存，当前已停用。点击“启用”即可继续使用。'
                    : '服务已保存。请点击这项服务的“测试连接”，通过后再保存下方的功能设置。');
        });
    });
    field('aiBindingForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            for (const [id, purpose] of [['aiDirectory', 'directory'], ['aiSummary', 'summary']]) {
                const profileId = Number(field(id).value);
                if (profileId && profileId !== settings.bindings[purpose]) {
                    await api('/api/admin/ai/bindings/' + purpose, 'PUT', {profile_id: profileId});
                    settings.bindings[purpose] = profileId;
                }
            }
            const school = new URLSearchParams(location.search).get('onboarding');
            const directory = settings.profiles.find(p => p.id === Number(field('aiDirectory').value));
            if (school && /^\d+$/.test(school) && directory && directory.enabled && directory.tested) {
                await api('/api/subscriptions/' + school + '/discovery', 'POST', {});
                location.assign('/subscriptions/' + school);
            } else field('aiStatus').textContent = '功能设置已保存';
        });
    });
    field('aiLimitsForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            await api('/api/admin/ai/limits', 'PUT', {total: Number(field('aiTotalLimit').value), directory: Number(field('aiDirectoryLimit').value),
                summary: Number(field('aiSummaryLimit').value), school_discovery: Number(field('aiSchoolLimit').value), max_concurrent: Number(field('aiConcurrent').value)});
            field('aiStatus').textContent = '用量设置已保存。';
        });
    });
})();
