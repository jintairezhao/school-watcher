(function () {
    'use strict';
    let settings = null;
    const field = id => document.getElementById(id);
    async function api(url, method, data) {
        const response = await fetch(url, {method: method || 'GET', headers: {'Content-Type': 'application/json'},
            body: data === undefined ? undefined : JSON.stringify(data)});
        const result = await response.json();
        if (!response.ok || result.status === 'failed') throw new Error(result.error || '操作未完成，请重试');
        return result;
    }
    function option(value, label) { return new Option(label, value); }
    function regions(selected) {
        const provider = settings.providers.find(p => p.id === field('aiProvider').value);
        field('aiRegion').replaceChildren(...(provider ? provider.regions : []).map(r => option(r, r === 'default' ? '默认区域' : r)));
        if (selected) field('aiRegion').value = selected;
    }
    async function action(button, operation) {
        button.disabled = true;
        try { await operation(); }
        catch (error) { field('aiStatus').textContent = error.message; }
        finally { button.disabled = false; }
    }
    function edit(profile) {
        field('aiProfileId').value = profile.id;
        field('aiName').value = profile.name;
        field('aiProvider').value = profile.provider;
        regions(profile.region);
        field('aiModel').value = profile.model;
        field('aiKey').value = '';
        field('aiName').focus();
    }
    window.loadAISettings = async function () {
        try {
            settings = await api('/api/admin/ai');
            field('aiStatus').textContent = settings.encryption_available
                ? (settings.legacy ? '原有 DeepSeek 摘要配置可继续使用；目录识别需单独选择并保存用途。' : '保存后需手动测试，再分配用途。')
                : '尚未配置本实例的加密主密钥，请按部署文档设置后保存 API 密钥。';
            const currentProvider = field('aiProvider').value;
            field('aiProvider').replaceChildren(...settings.providers.map(p => option(p.id, p.name)));
            if (currentProvider) field('aiProvider').value = currentProvider;
            regions();
            const profiles = field('aiProfiles'); profiles.replaceChildren();
            settings.profiles.forEach(profile => {
                const row = document.createElement('div'); row.className = 'admin-user-row';
                const label = document.createElement('span');
                label.textContent = profile.name + ' · ' + profile.model + ' · ' + (profile.enabled && profile.tested ? '可用' : '未启用');
                row.append(label);
                const actions = document.createElement('span'); actions.className = 'admin-user-actions';
                [['编辑', () => edit(profile)], ['测试连接（调用 API）', async () => {
                    field('aiStatus').textContent = '正在测试连接与结构化输出…';
                    const result = await api('/api/admin/ai/profiles/' + profile.id + '/test', 'POST', {});
                    await window.loadAISettings();
                    field('aiStatus').textContent = result.tested === true ? '测试通过，请选择用途。' : (result.test_code === 'usage_unavailable' ? '返回格式有效，但服务商未提供可核对用量，配置尚未启用。' : (result.error || '测试未通过，请核对模型、区域和密钥。'));
                }], ['停用并移除密钥', async () => {
                    await api('/api/admin/ai/profiles/' + profile.id, 'DELETE'); await window.loadAISettings();
                }]].forEach(([text, handler]) => {
                    const button = document.createElement('button'); button.type = 'button'; button.className = 'btn btn-xs btn-outline';
                    button.textContent = text; button.addEventListener('click', () => action(button, handler)); actions.append(button);
                });
                row.append(actions); profiles.append(row);
            });
            for (const [id, purpose] of [['aiDirectory', 'directory'], ['aiSummary', 'summary']]) {
                field(id).replaceChildren(option('', '请选择测试通过的服务'), ...settings.profiles.filter(p => p.enabled && p.tested).map(p => option(p.id, p.name)));
                field(id).value = settings.bindings[purpose] || '';
            }
            for (const [id, key] of [['aiTotalLimit', 'total'], ['aiDirectoryLimit', 'directory'], ['aiSummaryLimit', 'summary']]) field(id).value = settings.limits[key];
            field('aiConcurrent').value = settings.max_concurrent;
            field('aiUsage').textContent = settings.usage.map(u => u.key + '：已使用 ' + u.used_tokens + '，预留 ' + u.reserved_tokens).join('；') || '暂无调用记录';
        } catch (error) { field('aiStatus').textContent = error.message; }
    };
    field('aiProvider').addEventListener('change', () => regions());
    field('aiReset').addEventListener('click', () => { field('aiProfileForm').reset(); field('aiProfileId').value = ''; regions(); });
    field('aiProfileForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            const id = field('aiProfileId').value;
            const data = {name: field('aiName').value, provider: field('aiProvider').value,
                region: field('aiRegion').value, model: field('aiModel').value};
            if (field('aiKey').value) data.api_key = field('aiKey').value;
            if (id) data.expected_version = settings.profiles.find(p => p.id === Number(id)).version;
            await api('/api/admin/ai/profiles' + (id ? '/' + id : ''), id ? 'PUT' : 'POST', data);
            field('aiKey').value = ''; field('aiProfileId').value = ''; field('aiProfileForm').reset();
            await window.loadAISettings();
        });
    });
    field('aiBindingForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            for (const [id, purpose] of [['aiDirectory', 'directory'], ['aiSummary', 'summary']]) {
                if (field(id).value) await api('/api/admin/ai/bindings/' + purpose, 'PUT', {profile_id: Number(field(id).value)});
            }
            const school = new URLSearchParams(location.search).get('onboarding');
            if (school && /^\d+$/.test(school) && field('aiDirectory').value) {
                await api('/api/subscriptions/' + school + '/discovery', 'POST', {});
                location.assign('/subscriptions/' + school);
            } else field('aiStatus').textContent = '用途已保存';
        });
    });
    field('aiLimitsForm').addEventListener('submit', event => {
        event.preventDefault(); action(event.submitter, async () => {
            await api('/api/admin/ai/limits', 'PUT', {total: Number(field('aiTotalLimit').value), directory: Number(field('aiDirectoryLimit').value),
                summary: Number(field('aiSummaryLimit').value), max_concurrent: Number(field('aiConcurrent').value)});
            field('aiStatus').textContent = '用量设置已保存。';
        });
    });
})();
