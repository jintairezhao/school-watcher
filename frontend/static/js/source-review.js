(function () {
    'use strict';
    let page = 1, selected = null;
    let selectedDepartment = null;
    const field = id => document.getElementById(id);
    const errorLabels = {
        article_instead_of_column: '这是一篇通知，已按官网提供的链接查找所属栏目，无需逐篇确认。',
        search_instead_of_column: '这是搜索结果页，已按官网提供的链接整理所属栏目。',
        source_login_required: '这个栏目要求学校登录，当前无法自动访问。AI 无法解除登录限制。',
        publisher_requires_review: 'AI 尚未取得足够的官方发布部门依据。',
        publisher_conflict: '栏目填写的发布部门与官网显示的不一致。',
        column_identity_or_scope_unconfirmed: 'AI 尚未确认栏目名称与发布范围。',
        publisher_unconfirmed: 'AI 尚未确认发布部门，所需官网材料由程序读取。',
        publisher_mismatch: '发布部门与官网证据不一致。',
        evidence_missing: '程序尚未取得足够的网页材料。',
        article_body_missing: '尚未取得可核对的通知正文。',
        article_identity_mismatch: '正文与列表中的通知身份不一致。',
        article_sample_missing: '程序尚未取得足够的通知正文样本。',
        duplicate_list_items: '提取范围包含重复通知，需要程序调整。',
        invalid_list_item: '程序提取结果混入非通知条目。',
        invalid_publication_date: '日期提取结果需要重新核对。',
        pagination_repeats_first_page: '下一页重复返回首页内容，分页尚未核实。',
        pagination_sample_missing: '尚未取得下一页的核对样本。',
        list_items_missing: '自动识别尚未找到有效的通知列表。',
        list_empty_unconfirmed: '列表为空，但官网没有明确说明暂无通知。',
        article_samples_missing: '程序尚未取得足够的通知正文样本。',
        independent_sample_missing: '程序尚未完成独立页面复核。',
        pagination_unverified: '程序尚未完成分页核对。',
        config_changed: '栏目配置已变化，需要程序重新验证。'
    };
    async function api(url, payload) {
        const response = await fetch(url, payload ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(payload)} : {});
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || '读取失败，请重试');
        return data;
    }
    async function inspect(id, scroll = true) {
        try {
            const data = await api('/api/admin/source-proposals/' + id); selected = id;
            selectedDepartment = data.department_id;
            field('proposalTitle').textContent = data.candidate.name + ' · 官网证据';
            const flow = data.workflow || {};
            const owners = {program: '程序处理', ai: 'AI 识别与程序适配', developer: '开发者处理', website: '受官网访问条件限制', user: '由你选择'};
            field('proposalWorkflow').textContent = (flow.label || data.state) + ' · ' + (owners[flow.owner] || '');
            field('proposalNext').textContent = (flow.user_action || '') + '。' + (flow.next_step || '');
            const aiLabels = {succeeded: '调用成功', running: '正在调用', waiting: '正在排队', not_needed: '本次无需调用', not_configured: '尚未配置', uncertain: '结果未知，未重复调用', failed: '调用失败', limit_reached: '已达尝试上限', budget_exhausted: '已达用量上限'};
            field('proposalStages').textContent = 'AI：' + (aiLabels[flow.stages?.ai] || '尚未完成') + ' · 栏目验证：' +
                (flow.stages?.validation === 'passed' ? '通过' : '尚未通过') + ' · 正式接入：' +
                (flow.stages?.activation === 'activated' ? '完成' : '未完成');
            field('proposalErrors').textContent = flow.reason || (data.validation.errors || []).map(code => errorLabels[code] || code).join('；');
            if (flow.ai_reason && flow.ai_reason !== flow.reason) field('proposalErrors').textContent += '。AI 判断：' + flow.ai_reason;
            const historyLabels = {exploration_step_v2: 'AI 判断与执行', exploration_read: '读取补充材料', automatic_review_v5: '修复后自动重检', automatic_review_v6: '通用接入流程更新后重检',
                validate: '栏目验证', activate: '正式接入', reject: '用户拒绝', resolved_to_columns: '整理到所属栏目',
                fetch_failed: '官网读取未完成', program_failed: '程序故障', access_retry: '官网访问重试', select_observed_option: '用户选择'};
            field('proposalHistory').replaceChildren(...(data.history || []).map(item => {
                const li = document.createElement('li');
                li.textContent = item.created_at.replace('T', ' ').slice(0, 19) + ' · ' + (historyLabels[item.action] || '已保存处理记录') +
                    (item.detail?.reason ? ' · ' + item.detail.reason : ''); return li;
            }));
            field('proposalMissing').replaceChildren(...(flow.needed_evidence || []).map(text => {
                const li = document.createElement('li'); li.textContent = '待补材料：' + text; return li;
            }));
            field('proposalQuestion').replaceChildren();
            if (flow.requires_user && flow.question) {
                const question = document.createElement('p'); question.textContent = flow.question.text;
                field('proposalQuestion').append(question);
                flow.question.options.forEach(option => {
                    const row = document.createElement('p');
                    const button = document.createElement('button'); button.className = 'btn btn-outline';
                    button.textContent = option.label;
                    button.addEventListener('click', async () => {
                        button.disabled = true;
                        try { await api('/api/admin/source-proposals/' + id + '/review', {action: 'select_option', choice: option.id}); await inspect(id); await load(); }
                        catch (error) { field('reviewStatus').textContent = error.message; }
                        finally { button.disabled = false; }
                    });
                    row.append(button);
                    (option.evidence_urls || []).forEach(url => {
                        if (!/^https?:\/\//i.test(url)) return;
                        const link = document.createElement('a'); link.href = url; link.textContent = ' 官网依据'; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link);
                    });
                    field('proposalQuestion').append(row);
                });
            }
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
            if (!data.evidence.length) field('proposalEvidence').textContent = '程序尚未保存网页材料。';
            field('reviewActions').hidden = ['activated', 'superseded', 'rejected'].includes(data.state);
            field('proposalDetail').hidden = false;
            field('sourceVersions').hidden = !selectedDepartment;
            if (selectedDepartment) await loadVersions(selectedDepartment);
            if (scroll) field('proposalDetail').scrollIntoView({block: 'start'});
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
    async function load() {
        try {
            const data = await api('/api/admin/source-proposals?state=' + field('proposalState').value + '&page=' + page);
            field('reviewStatus').textContent = data.total ? '共 ' + data.total + ' 项 · 第 ' + page + ' 页' : '此状态下没有待处理来源。';
            field('reviewPrev').disabled = page <= 1; field('reviewNext').disabled = page >= data.pages;
            field('proposalList').replaceChildren();
            data.items.forEach(item => {
                const row = document.createElement('div'); row.className = 'admin-user-row';
                const label = document.createElement('span'); label.textContent = item.school_name + ' · ' + item.candidate.name;
                const status = document.createElement('span'); status.className = 'text-muted';
                status.textContent = (item.workflow?.label || item.state) + ' · ' + (item.workflow?.user_action || '');
                const button = document.createElement('button'); button.className = 'btn btn-sm btn-outline'; button.textContent = '查看详情';
                button.addEventListener('click', () => inspect(item.id)); row.append(label, status, button); field('proposalList').append(row);
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
            await api('/api/admin/source-proposals/' + selected + '/review', {action: button.dataset.review});
            field('proposalDetail').hidden = true; await load();
            field('reviewStatus').textContent = button.dataset.review === 'reject' ? '建议已拒绝，已有通知保留。' : '已加入检查队列，通过后自动启用。';
        } catch (error) { field('reviewStatus').textContent = error.message; }
        finally { buttons.forEach(b => { b.disabled = false; }); }
    });
    load();
    setInterval(async () => {
        if (document.hidden) return;
        await load();
        if (selected && !field('proposalDetail').hidden && !field('proposalDetail').contains(document.activeElement)) await inspect(selected, false);
    }, 15000);
})();
