(function () {
    'use strict';
    var current, pendingSuggestion, aiRequestId;
    var byId = function (id) { return document.getElementById(id); };
    var t = function (value) { return window.I18n ? window.I18n.t(value) : value; };
    function setMessage(message, error) {
        byId('tuningStatus').textContent = t(message);
        byId('tuningStatus').dataset.error = error ? 'true' : 'false';
    }
    function busy(value) {
        document.querySelectorAll('.tuning-page button, .tuning-page input, .tuning-page select, .tuning-page textarea').forEach(function (el) { el.disabled = value; });
        if (!value && current) {
            byId('suggestParameters').disabled = !current.feedback.length;
            byId('applySuggestion').disabled = !pendingSuggestion || pendingSuggestion.base_version !== current.version;
        }
    }
    async function request(action, payload) {
        var response = await fetch('/api/discovery-tuning' + (action ? '/' + action : ''), {
            method: action ? 'POST' : 'GET',
            headers: {'Content-Type': 'application/json', 'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content},
            body: action ? JSON.stringify(Object.assign({expected_revision: current.revision}, payload)) : undefined
        });
        var result = await response.json();
        if (!response.ok) throw new Error(result.error || t('调试记录暂时无法读取，请刷新后重试。'));
        return result;
    }
    function text(tag, value) { var node = document.createElement(tag); node.textContent = value; return node; }
    function showSuggestion(suggestion) {
        pendingSuggestion = suggestion;
        byId('suggestionPanel').hidden = !suggestion;
        if (!suggestion) return;
        byId('suggestionStatus').textContent = suggestion.applied ? t('已应用到参数版本') + ' ' + suggestion.applied_version
            : suggestion.base_version !== current.version ? t('建议对应的参数已变化，请重新评估') : '';
        byId('suggestionValues').hidden = !!suggestion.applied;
        byId('applySuggestion').hidden = !!suggestion.applied;
        byId('suggestionReason').textContent = suggestion.rationale;
        byId('suggestionValues').replaceChildren();
        current.fields.forEach(function (field) {
            byId('suggestionValues').append(text('dt', t(field.label)), text('dd', (suggestion.base_parameters || current.effective)[field.key] + ' → ' + suggestion.parameters[field.key]));
        });
        byId('applySuggestion').disabled = suggestion.base_version !== current.version;
    }
    function render(data) {
        current = data;
        byId('tuningEnabled').checked = data.enabled;
        byId('tuningWorkspace').hidden = !data.enabled;
        byId('tuningSummary').textContent = t('参数版本') + ' ' + data.version + ' · ' + t('连续无新增判断的起始暂返线') + ': ' + data.initial_no_progress_decisions;
        byId('parameterFields').replaceChildren();
        data.fields.forEach(function (field) {
            var row = document.createElement('div'), label = text('label', t(field.label)), input = document.createElement('input');
            input.type = 'number'; input.id = 'parameter-' + field.key; input.name = field.key;
            input.min = field.minimum; input.max = field.maximum; input.step = 1; input.required = true;
            input.value = data.effective[field.key]; label.htmlFor = input.id; row.append(label, input); byId('parameterFields').append(row);
        });
        var placeholder = text('option', t('请选择反馈类型'));
        placeholder.value = ''; placeholder.disabled = true; placeholder.selected = true;
        byId('feedbackKind').replaceChildren(placeholder);
        Object.keys(data.feedback_kinds).forEach(function (key) { var option = text('option', t(data.feedback_kinds[key])); option.value = key; byId('feedbackKind').append(option); });
        byId('feedbackList').replaceChildren();
        data.feedback.slice(-5).reverse().forEach(function (row) {
            var li = document.createElement('li'); li.append(text('strong', t(data.feedback_kinds[row.kind]) + ' · ' + t('参数版本') + ' ' + row.version), text('p', row.note));
            if (row.reference) li.append(text('small', row.reference)); byId('feedbackList').append(li);
        });
        byId('versionList').replaceChildren();
        data.history.slice(-10).reverse().forEach(function (row) {
            var li = document.createElement('li'); li.append(text('strong', t('参数版本') + ' ' + row.version),
                text('p', row.restored_version ? t('回退到参数版本') + ' ' + row.restored_version : t(row.reason)));
            if (data.enabled && row.version !== data.version) {
                var button = text('button', t('回退到这个版本')); button.type = 'button'; button.className = 'btn btn-outline';
                button.addEventListener('click', function () { perform('rollback', {version: row.version}); }); li.append(button);
            }
            byId('versionList').append(li);
        });
        showSuggestion(data.suggestions.length ? data.suggestions[data.suggestions.length - 1] : null);
        busy(false);
    }
    async function perform(action, payload) {
        busy(true); setMessage('正在保存…');
        try { render(await request(action, payload)); setMessage('调试记录已保存'); }
        catch (error) {
            if (action === 'enable' || action === 'disable') byId('tuningEnabled').checked = current.enabled;
            setMessage(error.message, true);
        }
        finally { busy(false); }
    }
    byId('tuningEnabled').addEventListener('change', function () { perform(this.checked ? 'enable' : 'disable', {}); });
    byId('parametersForm').addEventListener('submit', function (event) {
        event.preventDefault(); var values = {};
        current.fields.forEach(function (field) { values[field.key] = Number(byId('parameter-' + field.key).value); });
        perform('save', {parameters: values, reason: byId('parameterReason').value,
            feedback_ids: current.feedback.filter(function (row) { return row.version === current.version; }).map(function (row) { return row.id; })});
    });
    byId('feedbackForm').addEventListener('submit', async function (event) {
        event.preventDefault();
        await perform('feedback', {version: current.version, kind: byId('feedbackKind').value, note: byId('feedbackNote').value, reference: byId('feedbackReference').value});
    });
    byId('suggestParameters').addEventListener('click', async function () {
        if (!aiRequestId) aiRequestId = crypto.randomUUID().replace(/-/g, '');
        busy(true); setMessage('AI 正在分析反馈…');
        try {
            var result = await request('suggest', {request_id: aiRequestId, consent: true});
            render(result.state); showSuggestion(result.suggestion); aiRequestId = null;
            setMessage('AI 建议已生成，确认后才会应用');
        } catch (error) { setMessage(error.message, true); }
        finally { busy(false); if (pendingSuggestion) byId('applySuggestion').disabled = pendingSuggestion.base_version !== current.version; }
    });
    byId('applySuggestion').addEventListener('click', function () { perform('apply_suggestion', {suggestion_id: pendingSuggestion.id}); });
    byId('exportTuning').addEventListener('click', function () {
        var url = URL.createObjectURL(new Blob([JSON.stringify(current, null, 2)], {type: 'application/json'}));
        var a = document.createElement('a'); a.href = url; a.download = 'discovery-tuning-feedback.json'; a.click(); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
    request().then(render).catch(function (error) { setMessage(error.message, true); });
}());
