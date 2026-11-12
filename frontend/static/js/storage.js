/* Administrator-only retention, cache cleanup and additive backup transfers. */
(() => {
    const root = document.querySelector('.storage-page');
    if (!root) return;
    function revealAncestors(node) {
        for (let group = node.closest('details'); group; group = group.parentElement?.closest('details')) group.open = true;
    }
    function openStorageAnchor() {
        let id;
        try { id = decodeURIComponent(window.location.hash.slice(1)); } catch (_) { return; }
        const target = id && document.getElementById(id);
        if (!target || !root.contains(target)) return;
        revealAncestors(target);
        requestAnimationFrame(() => {
            target.scrollIntoView({block:'start'});
            const focus = target.matches('details') ? target.querySelector('summary') : target;
            if (!focus.matches('summary,a,button,input,select')) focus.tabIndex = -1;
            focus.focus({preventScroll:true});
        });
    }
    root.addEventListener('click', event => {
        const link = event.target.closest('a[href^="#"]');
        if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        event.preventDefault();
        if (window.location.hash !== link.hash) history.pushState(null, '', link.hash);
        openStorageAnchor();
    });
    window.addEventListener('hashchange', openStorageAnchor);
    window.addEventListener('pageshow', event => {
        if (event.persisted) root.querySelectorAll('.storage-group,#storageCacheRules,#storageSelectiveCleanup').forEach(group => { group.open = false; });
        openStorageAnchor();
    });
    let busy = false;
    let noticePreview = null;
    let databaseSpace = null;
    const noticeForm = document.getElementById('noticeCleanupForm');
    const noticeSchool = document.getElementById('noticeCleanupSchool');
    const noticeBefore = document.getElementById('noticeCleanupBefore');
    const deleteNotices = document.getElementById('deleteNoticeCleanup');
    const compactButton = document.getElementById('compactDatabase');
    const policyForm = document.getElementById('storagePolicyForm');
    let savedPolicy = new FormData(policyForm);
    const status = (id, message, error = false) => {
        const node = document.getElementById(id);
        node.textContent = message;
        node.dataset.error = String(error);
        if (message) revealAncestors(node);
    };
    const changed = () => [...new FormData(policyForm)].some(([key, value]) => value !== savedPolicy.get(key));
    async function responseError(response) {
        const data = await response.json().catch(() => ({}));
        return data.error || uiT('操作未完成，请稍后重试');
    }
    async function jsonRequest(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) {
            const error = new Error(await responseError(response));
            error.status = response.status;
            throw error;
        }
        return response.json();
    }
    const bytesText = bytes => {
        if (bytes < 1024) return `${Math.round(bytes)} B`;
        if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
        return `${(bytes / 1073741824).toFixed(2)} GB`;
    };
    function updateCacheSummary() {
        const duration = key => {
            const count = Number(savedPolicy.get(key));
            return count ? uiT('{count} 天',{count}) : uiT('不限制');
        };
        window.I18n.setText(document.getElementById('cacheRetentionSummary'), '已保存：正文 {body} · 网页 {web}',
            {body:duration('body_cache_days'),web:duration('discovery_cache_days')});
    }
    document.addEventListener('locale-changed', updateCacheSummary);
    policyForm.addEventListener('invalid', event => revealAncestors(event.target), true);
    updateCacheSummary();
    const phases = {preparing:uiT('正在准备'), uploading:uiT('正在传输备份'), staging:uiT('正在读取文件'), reading:uiT('正在解压读取'),
        compressing:uiT('正在生成备份'), writing:uiT('正在写入文件'), flushing:uiT('正在保存到磁盘'), finalizing:uiT('正在完成备份'),
        checking_database:uiT('正在检查旧版数据库'), checking_relations:uiT('正在校验数据关系'), waiting_database:uiT('正在等待数据库'),
        indexing:uiT('正在检查已有通知'), merging_schools:uiT('正在合并学校'), merging_departments:uiT('正在合并栏目'),
        merging_announcements:uiT('正在合并通知'), merging_memberships:uiT('正在合并通知来源'), merging_sources:uiT('正在合并来源'),
        merging_directories:uiT('正在合并目录'), merging_attachments:uiT('正在合并已保存附件'), reviewing_sources:uiT('正在核实来源配置'), committing:uiT('正在保存合并结果'),
        downloading:uiT('正在传输到浏览器'), done:uiT('已完成')};
    function progressView(id) {
        const node = document.getElementById(id);
        const bar = node.querySelector('progress');
        const started = performance.now();
        let lastUpdate = started, active = true;
        node.hidden = false;
        function update(event) {
            lastUpdate = performance.now();
            node.querySelector('[data-phase]').textContent = phases[event.phase] ||
                (event.phase.startsWith('validating_') ? uiT('正在校验备份') : uiT('正在处理'));
            const fraction = event.total > 0 ? Math.min(100, event.done / event.total * 100) : null;
            if (fraction === null) bar.removeAttribute('value');
            else bar.value = fraction;
            node.querySelector('[data-percent]').textContent = fraction === null ? '' : `${Math.floor(fraction)}%`;
            const isBytes = event.processed_bytes != null;
            const byteStage = ['uploading', 'staging', 'reading', 'writing', 'downloading'].includes(event.phase);
            node.querySelector('[data-count]').textContent = byteStage
                ? `${bytesText(event.done || 0)}${event.total ? ' / ' + bytesText(event.total) : ''}`
                : (event.total ? uiT('{done} / {total} 条',{done:event.done,total:event.total}) : '');
            node.querySelector('[data-speed]').textContent = event.speed == null ? uiT('正在计算速度…')
                : isBytes ? `${bytesText(event.speed)}/s` : uiT("{p0} 条/s", {p0:(Math.round(event.speed))});
            if (!event.total && event.processed_bytes == null)
                node.querySelector('[data-speed]').textContent = uiT('处理中');
        }
        update({phase:'preparing'});
        const timer = setInterval(() => {
            const seconds = Math.floor((performance.now() - started) / 1000);
            node.querySelector('[data-elapsed]').textContent = uiT("已用时 {p0}:{p1}", {p0:(Math.floor(seconds / 60)), p1:(String(seconds % 60).padStart(2, '0'))});
            if (active && performance.now() - lastUpdate > 3000)
                node.querySelector('[data-speed]').textContent = uiT('处理中，等待下一批数据');
        }, 500);
        return {update, finish(success) {
            active = false;
            clearInterval(timer);
            node.querySelector('[data-speed]').textContent = '';
            if (success) {
                bar.value = 100;
                node.querySelector('[data-phase]').textContent = uiT('已完成');
                node.querySelector('[data-percent]').textContent = '100%';
            } else {
                node.querySelector('[data-phase]').textContent = uiT('未完成');
                bar.value = 0;
                node.querySelector('[data-percent]').textContent = '';
            }
        }};
    }
    function transfer(url, data, view) {
        return new Promise((resolve, reject) => {
            const xhr = new XMLHttpRequest();
            let cursor = 0, result, failure;
            const samples = [{time:performance.now(), bytes:0}];
            xhr.open('POST', url);
            xhr.setRequestHeader('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]')?.content || '');
            if (data) {
                xhr.upload.onprogress = event => {
                    const now = performance.now();
                    while (samples.length > 1 && now - samples[1].time > 2000) samples.shift();
                    const elapsed = (now - samples[0].time) / 1000;
                    view.update({phase:'uploading', done:event.loaded, total:event.lengthComputable ? event.total : 0,
                        processed_bytes:event.loaded, speed:elapsed >= .05 ? (event.loaded - samples[0].bytes) / elapsed : null});
                    samples.push({time:now, bytes:event.loaded});
                };
                xhr.upload.onload = () => view.update({phase:'preparing'});
            }
            function consume() {
                const text = xhr.responseText;
                let end;
                while ((end = text.indexOf('\n', cursor)) !== -1) {
                    const line = text.slice(cursor, end); cursor = end + 1;
                    if (!line.trim()) continue;
                    const event = JSON.parse(line);
                    if (event.type === 'progress') view.update(event);
                    if (event.type === 'result') result = event.result;
                    if (event.type === 'error') failure = event.error;
                }
            }
            xhr.onprogress = () => { if (xhr.status === 200) { try { consume(); } catch (_) { failure = uiT('进度响应异常'); } } };
            xhr.onload = () => {
                if (xhr.status !== 200) {
                    let message = uiT('传输未完成，请稍后重试');
                    try { message = JSON.parse(xhr.responseText).error || message; } catch (_) { /* Non-JSON proxy errors. */ }
                    reject(new Error(message)); return;
                }
                try { consume(); } catch (_) { failure = uiT('进度响应异常'); }
                if (failure || !result) reject(new Error(failure || uiT('连接中断，未收到完成结果，请检查后重试')));
                else resolve(result);
            };
            xhr.onerror = () => reject(new Error(uiT('连接中断，未收到完成结果，请检查后重试')));
            xhr.send(data || null);
        });
    }
    async function download(result, view) {
        const response = await fetch(result.download_url);
        if (!response.ok) throw new Error(await responseError(response));
        const reader = response.body.getReader(), chunks = [];
        const total = Number(response.headers.get('Content-Length')) || result.bytes;
        const started = performance.now();
        let done = 0;
        while (true) {
            const {value, done:ended} = await reader.read();
            if (ended) break;
            chunks.push(value); done += value.byteLength;
            const seconds = (performance.now() - started) / 1000;
            view.update({phase:'downloading', done, total, processed_bytes:done, speed:seconds > .05 ? done / seconds : null});
        }
        const url = URL.createObjectURL(new Blob(chunks, {type:'application/zip'}));
        const link = document.createElement('a');
        link.href = url; link.download = result.name;
        document.body.appendChild(link); link.click(); link.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60000);
    }
    async function refreshUsage() {
        const data = await jsonRequest('/api/storage');
        document.getElementById('storageTotal').textContent = bytesText(data.total_bytes);
        document.getElementById('announcementCount').textContent = new Intl.NumberFormat(window.I18n.locale()).format(data.announcement_count);
        // The initial page and later refreshes share the server's physical file accounting.
        root.querySelectorAll('[data-storage-group]').forEach(node => {
            node.textContent = bytesText(data.usage_groups[node.dataset.storageKind] || 0);
        });
        databaseSpace = data.database_space;
        if (databaseSpace) {
            const unavailable = databaseSpace.available === false;
            document.getElementById('databaseReclaimable').textContent = unavailable ? '—' : bytesText(databaseSpace.reclaimable_bytes || 0);
            // Availability is separate from the last action result, which remains useful after a refresh.
            const availability = document.getElementById('databaseSpaceAvailability');
            availability.textContent = unavailable ? uiT(databaseSpace.message || uiT('暂时无法读取可回收空间，请稍后重试。')) : '';
            availability.hidden = !unavailable;
        }
    }
    async function run(id, working, action, refresh = false) {
        if (busy) return;
        busy = true;
        root.setAttribute('aria-busy', 'true');
        const controls = [...root.querySelectorAll('button, input, select')].map(node => [node, node.disabled]);
        controls.forEach(([node]) => { node.disabled = true; });
        status(id, working);
        try {
            const message = await action();
            status(id, message);
            if (refresh) {
                try { await refreshUsage(); }
                catch (_) { status(id, uiT("{p0} 用量刷新失败，可刷新页面查看。", {p0:(message)})); }
            }
        } catch (error) {
            status(id, error.message || uiT('网络连接中断，请稍后重试'), true);
        } finally {
            busy = false;
            root.setAttribute('aria-busy', 'false');
            controls.forEach(([node, disabled]) => { node.disabled = disabled; });
            deleteNotices.disabled = !noticePreview || noticePreview.deletable_count < 1;
            if (databaseSpace) compactButton.disabled = databaseSpace.available === false || !databaseSpace.supported || !(databaseSpace.can_compact ?? databaseSpace.reclaimable_bytes);
        }
    }
    function invalidateNoticePreview(message = '') {
        noticePreview = null;
        deleteNotices.disabled = true;
        document.getElementById('noticeCleanupPreview').hidden = true;
        if (message) status('noticeCleanupStatus', message);
    }
    const noticeScope = () => JSON.stringify([noticeSchool.value, noticeBefore.value]);
    [noticeSchool, noticeBefore].forEach(input => input.addEventListener('input', () => {
        invalidateNoticePreview(uiT('条件已更改，请重新预览。'));
    }));
    noticeForm.addEventListener('submit', event => {
        event.preventDefault();
        if (busy || !noticeForm.reportValidity()) return;
        invalidateNoticePreview();
        const scope = noticeScope();
        run('noticeCleanupStatus', uiT('正在检查清理范围…'), async () => {
            const result = await jsonRequest('/api/storage/notices/preview', {
                method:'POST', headers:{'Content-Type':'application/json'},
                body:JSON.stringify({school_id:noticeSchool.value === 'all' ? null : Number(noticeSchool.value), before:noticeBefore.value})
            });
            if (scope !== noticeScope()) return uiT('条件已更改，请重新预览。');
            noticePreview = {...result, scope};
            root.querySelectorAll('[data-notice-count]').forEach(node => {
                node.textContent = new Intl.NumberFormat(window.I18n.locale()).format(result[node.dataset.noticeCount] || 0);
            });
            const samples = document.getElementById('noticeCleanupSamples');
            samples.replaceChildren();
            for (const sample of (result.sample || []).slice(0, 5)) {
                const item = document.createElement('li');
                const title = document.createElement('span'); title.textContent = sample.title;
                const meta = document.createElement('small');
                meta.textContent = `${sample.school} · ${String(sample.published_at).slice(0,10)}`;
                item.append(title, meta); samples.append(item);
            }
            document.getElementById('noticeCleanupPreview').hidden = false;
            return result.deletable_count > 0 ? uiT('预览完成。请核对范围后再永久删除。') : uiT('此范围内没有可删除的通知。');
        });
    });
    deleteNotices.addEventListener('click', () => {
        if (busy || !noticePreview || noticePreview.scope !== noticeScope() || noticePreview.deletable_count < 1) return;
        const preview = noticePreview;
        const school = noticeSchool.selectedOptions[0].textContent;
        const confirmation = uiT('永久删除“{school}”在 {before} 之前的 {count} 条通知？这些通知的正文、附件及所有账户的阅读状态也将删除，此操作无法撤销。',
            {school, before:noticeBefore.value, count:preview.deletable_count});
        if (!window.confirm(confirmation)) return;
        run('noticeCleanupStatus', uiT('正在永久删除通知…'), async () => {
            try {
                const result = await jsonRequest('/api/storage/notices/cleanup', {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({preview_token:preview.preview_token})
                });
                invalidateNoticePreview();
                return uiT('已永久删除 {count} 条通知；受保护的通知已保留。',{count:result.deleted_count});
            } catch (error) {
                // Every failed deletion requires a new preview; never retry a destructive request automatically.
                invalidateNoticePreview();
                if (error.status === 409) error.message = uiT('通知已变化或预览已过期，请重新预览后再删除。');
                throw error;
            }
        }, true);
    });
    compactButton.addEventListener('click', () => {
        run('databaseSpaceStatus', uiT('正在压缩数据库，请稍候…'), async () => {
            try {
                const result = await jsonRequest('/api/storage/database/compact', {
                    method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'
                });
                return uiT('已回收 {size}，通知与目录均保留。',{size:bytesText(result.reclaimed_bytes || 0)});
            } catch (error) {
                if (error.status === 409) error.message = uiT('数据库正在使用中，请稍后重试。');
                throw error;
            }
        }, true);
    });
    policyForm.addEventListener('submit', event => {
        event.preventDefault();
        const submitted = new FormData(policyForm);
        const values = Object.fromEntries([...submitted].map(([key, value]) => [key, Number(value)]));
        run('policyStatus', uiT('正在保存保留规则…'), async () => {
            await jsonRequest('/api/storage/policy', {
                method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
            });
            savedPolicy = submitted;
            updateCacheSummary();
            return uiT('保留规则已保存');
        });
    });
    root.querySelectorAll('[data-cleanup]').forEach(button => {
        button.addEventListener('click', () => {
            if (changed()) {
                status('cleanupStatus', uiT('保留规则有未保存的修改，请先保存后再清理。'), true);
                const link = document.createElement('a');
                const unsaved = [...new FormData(policyForm)].find(([key,value]) => value !== savedPolicy.get(key));
                link.href = '#policy-' + unsaved[0];
                link.textContent = uiT('查看未保存的保留规则');
                document.getElementById('cleanupStatus').append(' ',link);
                return;
            }
            const messages = {cache:uiT('清理可移除的缓存？通知、收藏和接入进度保留，部分内容之后需要重新下载。'),
                body:uiT('清理未收藏的正文缓存？通知和收藏保留。'),
                discovery:uiT('清理调查网页缓存？待处理进度保留。'), fetch:uiT('清理网页抓取缓存？正在使用的文件保留。'),
                governance:uiT('清理闲置核实证据？当前栏目和待核实事项的证据保留。'),
                backups:uiT('删除自动备份？手动导出的备份和回退资料保留。'), logs:uiT('清空已结束的抓取记录？正在运行的记录保留。')};
            const message = messages[button.dataset.cleanup] || uiT('按已保存规则清理？通知目录和收藏保留。');
            if (!window.confirm(message)) return;
            run('cleanupStatus', uiT('正在清理，请稍候…'), async () => {
                const result = await jsonRequest('/api/storage/cleanup', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: button.dataset.cleanup})
                });
                const counts = [[result.evicted,uiT('条正文缓存')],[result.discovery_snapshots_deleted,uiT('份调查缓存')],
                    [result.fetch_evidence?.removed,uiT('份抓取缓存')],[result.scrape_logs_deleted,uiT('条抓取记录')],
                    [result.backups_deleted,uiT('份自动备份')],[result.governance_evidence?.removed,uiT('份闲置证据')]]
                    .filter(([count]) => count > 0).map(([count, label]) => `${count} ${label}`);
                if (button.dataset.cleanup === 'cache') return counts.length
                    ? uiT('已清理可移除的缓存，受保护的内容已保留。') : uiT('没有可清理的缓存；受保护的内容已保留。');
                return counts.length ? uiT('已清理 {items}',{items:counts.join(window.I18n.locale()==='en'?', ':'、')}) : uiT('没有可清理的内容');
            }, true);
        });
    });
    document.getElementById('exportStorage').addEventListener('click', () => {
        run('exportStatus', uiT('正在生成数据备份，请稍候…'), async () => {
            const desktop = !!window.pywebview?.api;
            const view = progressView('exportProgress');
            let success = false;
            try {
                const result = await transfer('/api/storage/export?progress=1' + (desktop ? '&save=1' : ''), null, view);
                if (!result.saved) await download(result, view);
                success = true;
                return result.saved ? uiT('已保存到备份目录：{name}',{name:result.name}) : uiT('数据备份已生成，请在下载列表中查看');
            } finally {
                view.finish(success);
            }
        });
    });
    let originalLocations;
    const saveLocations = document.getElementById('saveLocations');
    const resetLocations = document.getElementById('resetLocations');
    const locationFields = [...root.querySelectorAll('[data-location]')];
    function fillLocations(values) {
        locationFields.forEach(input => { input.value = values[input.dataset.location] || ''; });
        saveLocations.hidden = resetLocations.hidden = true;
    }
    async function loadLocations() {
        if (!saveLocations || !window.pywebview?.api) return;
        try {
            originalLocations = await window.pywebview.api.location_state();
            if (originalLocations) fillLocations(originalLocations);
        } catch (_) { status('locationStatus', uiT('无法读取文件位置，请重新打开应用。'), true); }
    }
    window.addEventListener('pywebviewready', loadLocations);
    loadLocations();
    root.querySelectorAll('[data-location-open]').forEach(button => button.addEventListener('click', () => {
        window.pywebview?.api.open_location(button.dataset.locationOpen);
    }));
    root.querySelectorAll('[data-location-change]').forEach(button => button.addEventListener('click', async () => {
        if (!window.pywebview?.api || !originalLocations) return;
        const kind = button.dataset.locationChange;
        if (kind === 'program') {
            run('locationStatus', uiT('正在下载并校验安装程序…'), async () => {
                const result = await window.pywebview.api.reinstall();
                if (result.error) throw new Error(result.error);
                return result.cancelled ? uiT('已取消') : uiT('请在安装向导中选择新位置');
            });
            return;
        }
        const chosen = await window.pywebview.api.choose_location(kind);
        if (!chosen) return;
        const field = document.getElementById('location-' + kind);
        if (kind === 'data') {
            const previous = field.value;
            for (const key of ['cache', 'backups', 'downloads']) {
                const other = document.getElementById('location-' + key);
                if (other.value === previous || other.value.startsWith(previous + '\\') || other.value.startsWith(previous + '/'))
                    other.value = chosen + other.value.slice(previous.length);
            }
        }
        field.value = chosen;
        saveLocations.hidden = resetLocations.hidden = false;
        status('locationStatus', uiT('选择空文件夹；保存后迁移并重启。'));
    }));
    resetLocations?.addEventListener('click', () => { fillLocations(originalLocations); status('locationStatus', ''); });
    saveLocations?.addEventListener('click', () => {
        const values = Object.fromEntries(locationFields.filter(input => input.dataset.location !== 'program')
            .map(input => [input.dataset.location, input.value]));
        run('locationStatus', uiT('正在准备迁移…'), async () => {
            const result = await window.pywebview.api.change_locations(values);
            if (result.error) throw new Error(result.error);
            return result.cancelled ? uiT('已取消') : uiT('正在迁移，完成后会重新打开');
        });
    });
    const importForm = document.getElementById('storageImportForm');
    const backupFile = document.getElementById('storageBackupFile');
    const chooseBackup = document.getElementById('chooseStorageBackup');
    const backupFilename = document.getElementById('storageBackupFilename');
    let missingBackup = false;
    function updateBackupFilename() {
        const file = backupFile.files[0];
        backupFilename.dataset.fileSelected = String(!!file);
        if (file) {
            backupFilename.removeAttribute('data-i18n');
            backupFilename.textContent = file.name;
            backupFilename.title = file.name;
            chooseBackup.removeAttribute('aria-invalid');
            if (missingBackup) status('importStatus', '');
            missingBackup = false;
        } else {
            window.I18n.setText(backupFilename, '未选择文件');
            backupFilename.removeAttribute('title');
            if (missingBackup) document.getElementById('importStatus').textContent = uiT('请先选择 ZIP 备份文件');
        }
    }
    chooseBackup.addEventListener('click', () => { if (!busy) backupFile.click(); });
    backupFile.addEventListener('change', updateBackupFilename);
    backupFile.addEventListener('cancel', updateBackupFilename);
    backupFile.addEventListener('invalid', event => {
        event.preventDefault();
        missingBackup = true;
        chooseBackup.setAttribute('aria-invalid', 'true');
        status('importStatus', uiT('请先选择 ZIP 备份文件'), true);
        chooseBackup.focus();
    });
    importForm.addEventListener('reset', () => queueMicrotask(() => {
        if (missingBackup) status('importStatus', '');
        missingBackup = false;
        chooseBackup.removeAttribute('aria-invalid');
        updateBackupFilename();
    }));
    document.addEventListener('locale-changed', updateBackupFilename);
    updateBackupFilename();
    importForm.addEventListener('submit', event => {
        event.preventDefault();
        const file = backupFile.files[0];
        if (!file) return;
        const expanded = Number(document.getElementById('importExpandedMB').value);
        if (!Number.isSafeInteger(expanded) || expanded < 1) {
            status('importStatus', uiT('展开大小请输入正整数（MB）'), true);
            return;
        }
        if (file.size > expanded * 1048576) {
            document.querySelector('.storage-import-options').open = true;
            status('importStatus', uiT("文件为 {p0}，请提高允许的展开大小后重试", {p0:(bytesText(file.size))}), true);
            return;
        }
        const data = new FormData(importForm);
        run('importStatus', uiT('正在检查并合并备份，请勿重复提交…'), async () => {
            const view = progressView('importProgress');
            let success = false;
            try {
                const url = `/api/storage/import?progress=1&upload_mb=${Math.ceil(file.size / 1048576) + 2}&expanded_mb=${expanded}`;
                const result = await transfer(url, data, view);
                backupFile.value = '';
                updateBackupFilename();
                success = true;
                return uiT("合并完成：新增 {p0} 条通知，去重 {p1} 条，补回 {p2} 条正文。", {p0:(result.added), p1:(result.duplicates), p2:(result.bodies_restored)});
            } finally {
                view.finish(success);
            }
        }, true);
    });
    openStorageAnchor();
    root.dataset.storageReady = 'true';
})();
