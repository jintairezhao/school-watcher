(function () {
    'use strict';
    let page = 1, selected = null;
    let selectedDepartment = null, pickerToken = '', pickerHash = '', picks = {}, lastNodes = [];
    const field = id => document.getElementById(id);
    const errorLabels = {
        article_instead_of_column: '这是一篇通知，已按官网提供的链接查找所属栏目，无需逐篇确认。',
        search_instead_of_column: '这是搜索结果页，已按官网提供的链接整理所属栏目。',
        source_login_required: '这个栏目要求学校登录，当前无法自动访问。AI 无法解除登录限制。',
        publisher_requires_review: '尚未确认发布部门，可先自动检查；仍无法确认时再填写归属说明。',
        publisher_conflict: '栏目填写的发布部门与官网显示的不一致。',
        column_identity_or_scope_unconfirmed: '尚未确认栏目名称或发布范围，请核对官网身份。',
        publisher_unconfirmed: '尚未确认发布部门，请补充官网机构依据。',
        publisher_mismatch: '发布部门与官网证据不一致。',
        evidence_missing: '缺少网页证据，请重新检查。',
        article_body_missing: '尚未取得可核对的通知正文。',
        article_identity_mismatch: '正文与列表中的通知身份不一致。',
        article_sample_missing: '缺少通知正文样本，请重新检查。',
        duplicate_list_items: '列表包含重复通知，请核对所选列表范围。',
        invalid_list_item: '部分列表项不是有效通知，请核对通知行。',
        invalid_publication_date: '日期提取结果需要重新核对。',
        pagination_repeats_first_page: '下一页重复返回首页内容，分页尚未核实。',
        pagination_sample_missing: '尚未取得下一页的核对样本。',
        list_items_missing: '未找到可核对的通知列表，请点选正确的通知区域。',
        list_empty_unconfirmed: '列表为空，但官网没有明确说明暂无通知。',
        article_samples_missing: '缺少通知正文样本，请重新检查。',
        independent_sample_missing: '缺少独立复核页面，请重新检查。',
        pagination_unverified: '分页尚未通过核对，请重新检查。',
        config_changed: '当前栏目配置已变化，请重新检查此建议。'
    };
    async function api(url, payload) {
        const response = await fetch(url, payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '读取失败，请重试');
        return data;
    }
    async function inspect(id) {
        try {
            const data = await api('/api/admin/source-proposals/' + id); selected = id;
            selectedDepartment = data.department_id;
            pickerToken = ''; picks = {}; field('sourcePicker').hidden = true;
            field('manualSourceReview').open = false;
            field('pickerFrame').removeAttribute('srcdoc');
            field('proposalTitle').textContent = data.candidate.name + ' · 官网证据';
            field('proposalErrors').textContent = (data.validation.errors || []).map(code => errorLabels[code] ||
                (/[\u4e00-\u9fff]/.test(code) ? code : '网页证据尚未通过核对，请查看原始页面或重新检查。')).join('；') || '请核对原始网页与栏目归属。';
            field('proposalEvidence').replaceChildren();
            field('proposalRelated').replaceChildren();
            (data.validation.related_columns || []).forEach(column => {
                const button = document.createElement('button');
                button.className = 'btn btn-sm btn-outline';
                button.textContent = '查看所属栏目：' + column.name;
                button.addEventListener('click', () => inspect(column.id));
                field('proposalRelated').append(button);
            });
            data.evidence.forEach(item => {
                const details = document.createElement('details');
                const heading = document.createElement('summary'); heading.textContent = item.url || '网页样本';
                const text = document.createElement('p'); text.textContent = item.text || item.error;
                text.style.overflowWrap = 'anywhere'; details.append(heading, text); field('proposalEvidence').append(details);
            });
            if (!data.evidence.length) field('proposalEvidence').textContent = '尚无网页证据，可重新检查。';
            field('reviewNote').value = ''; field('reviewActions').hidden = ['activated', 'superseded', 'rejected'].includes(data.state);
            field('proposalDetail').hidden = false;
            field('sourceVersions').hidden = !selectedDepartment;
            if (selectedDepartment) await loadVersions(selectedDepartment);
            field('proposalDetail').scrollIntoView({block: 'start'});
        } catch (error) { field('reviewStatus').textContent = error.message; }
    }
    async function loadVersions(departmentId) {
        const versions = await api('/api/admin/departments/' + departmentId + '/versions');
        const list = field('sourceVersionList'); list.replaceChildren();
        field('sourceVersionStatus').textContent = versions.length ? '' : '尚无已发布的配置版本。';
        versions.forEach(version => {
            const row = document.createElement('p');
            const label = document.createElement('span'); label.textContent = '版本 ' + version.version + ' · ' + version.config.name + ' ';
            const button = document.createElement('button'); button.className = 'btn btn-sm btn-outline'; button.textContent = '检查并恢复此版本';
            button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    await api('/api/admin/departments/' + departmentId + '/rollback', {version: version.version});
                    field('sourceVersionStatus').textContent = '已安排复核，通过后恢复该版本。';
                } catch (error) { field('sourceVersionStatus').textContent = error.message; }
                finally { button.disabled = false; }
            });
            row.append(label, button); list.append(row);
        });
    }
    function showPicks() {
        const list = field('pickerSelections'); list.replaceChildren();
        for (const [name, label] of [['row', '通知行'], ['title', '标题'], ['link', '链接'], ['date', '日期']]) {
            const term = document.createElement('dt'); term.textContent = label;
            const value = document.createElement('dd'); value.textContent = picks[name] ? picks[name].text : '未选择';
            list.append(term, value);
        }
    }
    function chooseNode(index) {
        const node = lastNodes[index]; if (!node) return;
        picks[field('pickerField').value] = node;
        field('pickerStatus').textContent = '已选择：' + node.text;
        field('pickerFrame').contentWindow.postMessage({type: 'highlight', token: pickerToken, id: node.id}, '*');
        showPicks();
    }
    field('openSourcePicker').addEventListener('click', async () => {
        const button = field('openSourcePicker'); button.disabled = true;
        try {
            const data = await api('/api/admin/source-proposals/' + selected + '/picker');
            pickerHash = data.evidence_hash; pickerToken = crypto.randomUUID(); picks = {}; lastNodes = [];
            field('pickerField').value = 'row';
            field('pickerElement').replaceChildren(new Option('请先点击下方网页', '')); field('pickerElement').disabled = true;
            field('pickerStatus').textContent = '请选择完整通知行。'; showPicks();
            const nonce = crypto.randomUUID();
            // The only script below is application-authored. The server strips all
            // website scripts, URLs and attributes before returning this markup.
            const script = `(() => {const token=${JSON.stringify(pickerToken)};let highlighted;
                function mark(id){if(highlighted)highlighted.style.outline='';highlighted=document.querySelector('[data-pick-id="'+id+'"]');if(highlighted)highlighted.style.outline='2px solid #0071e3';}
                addEventListener('click',e=>{e.preventDefault();e.stopPropagation();let n=e.target.closest('[data-pick-id]'),nodes=[];while(n&&n.tagName!=='BODY'&&n.tagName!=='HTML'){nodes.push({id:n.dataset.pickId,tag:n.tagName.toLowerCase(),text:(n.innerText||n.textContent||'').trim().slice(0,100)});n=n.parentElement?.closest('[data-pick-id]');}parent.postMessage({type:'source-pick',token,nodes},'*');},true);
                addEventListener('message',e=>{if(e.source===parent&&e.data?.token===token&&e.data.type==='highlight'&&/^\\d+$/.test(e.data.id))mark(e.data.id);});})();`;
            field('pickerFrame').srcdoc = '<!doctype html><html lang="zh-CN"><head><meta charset="UTF-8">' +
                '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; script-src \'nonce-' + nonce + '\'; form-action \'none\'; base-uri \'none\'">' +
                '<style>body{font:15px/1.8 system-ui,sans-serif;color:#1d1d1f;padding:16px;overflow-wrap:anywhere}a{color:#0071e3}li,tr{padding:6px}*[data-pick-id]:hover{background:#f0f5fc;cursor:crosshair}</style></head>' +
                data.html + '<script nonce="' + nonce + '">' + script + '</script></html>';
            field('sourcePicker').hidden = false;
        } catch (error) { field('reviewStatus').textContent = error.message; }
        finally { button.disabled = false; }
    });
    window.addEventListener('message', event => {
        if (event.source !== field('pickerFrame').contentWindow || !pickerToken ||
            event.data?.token !== pickerToken || event.data.type !== 'source-pick' || !Array.isArray(event.data.nodes)) return;
        lastNodes = event.data.nodes.filter(n => typeof n.id === 'string' && /^\d+$/.test(n.id) && typeof n.text === 'string').slice(0,30);
        if (!lastNodes.length) return;
        const selector = field('pickerElement');
        selector.replaceChildren(...lastNodes.map((node, index) => new Option((index ? '外层区域 · ' : '点击位置 · ') + node.text.slice(0,60), index)));
        selector.disabled = false;
        const rowIndex = field('pickerField').value === 'row' ? lastNodes.findIndex(n => ['li','tr','article'].includes(n.tag)) : 0;
        selector.value = String(Math.max(0, rowIndex)); chooseNode(Number(selector.value));
    });
    field('pickerElement').addEventListener('change', () => chooseNode(Number(field('pickerElement').value)));
    field('clearPickerField').addEventListener('click', () => { delete picks[field('pickerField').value]; showPicks(); });
    field('submitSourcePicks').addEventListener('click', async () => {
        const button = field('submitSourcePicks'); button.disabled = true;
        try {
            if (!picks.row || !picks.title) throw new Error('请先选择完整通知行和标题。');
            await api('/api/admin/source-proposals/' + selected + '/picker', {evidence_hash: pickerHash,
                picks: Object.fromEntries(Object.entries(picks).map(([key,node]) => [key,node.id]))});
            field('pickerStatus').textContent = '点选已提交，通过官网内容检查后生效。';
        } catch (error) { field('pickerStatus').textContent = error.message; }
        finally { button.disabled = false; }
    });
    async function load() {
        try {
            const data = await api('/api/admin/source-proposals?state=' + field('proposalState').value + '&page=' + page);
            field('reviewStatus').textContent = data.total ? '共 ' + data.total + ' 项 · 第 ' + page + ' 页' : '此状态下没有待处理来源。';
            field('reviewPrev').disabled = page <= 1; field('reviewNext').disabled = page >= data.pages;
            field('proposalList').replaceChildren();
            data.items.forEach(item => {
                const row = document.createElement('div'); row.className = 'admin-user-row';
                const label = document.createElement('span'); label.textContent = item.school_name + ' · ' + item.candidate.name;
                const button = document.createElement('button'); button.className = 'btn btn-sm btn-outline'; button.textContent = '查看依据';
                button.addEventListener('click', () => inspect(item.id)); row.append(label, button); field('proposalList').append(row);
            });
        } catch (error) { field('reviewStatus').textContent = error.message; }
    }
    field('proposalState').addEventListener('change', () => { page = 1; load(); });
    field('reviewPrev').addEventListener('click', () => { page--; load(); });
    field('reviewNext').addEventListener('click', () => { page++; load(); });
    field('reviewActions').addEventListener('click', async event => {
        const button = event.target.closest('[data-review]'); if (!button || !selected) return;
        const buttons = field('reviewActions').querySelectorAll('button'); buttons.forEach(b => { b.disabled = true; });
        try {
            await api('/api/admin/source-proposals/' + selected + '/review', {action: button.dataset.review, note: field('reviewNote').value});
            field('proposalDetail').hidden = true; await load();
            field('reviewStatus').textContent = button.dataset.review === 'reject' ? '建议已拒绝，已有通知保留。' : '已加入检查队列，通过后自动启用。';
        } catch (error) { field('reviewStatus').textContent = error.message; }
        finally { buttons.forEach(b => { b.disabled = false; }); }
    });
    load();
})();
