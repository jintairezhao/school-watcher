/* Administrator-only retention, cache cleanup and additive backup transfers. */
(() => {
    const root = document.querySelector('.storage-page');
    if (!root) return;
    let busy = false;
    const policyForm = document.getElementById('storagePolicyForm');
    let savedPolicy = new FormData(policyForm);
    const status = (id, message, error = false) => {
        const node = document.getElementById(id);
        node.textContent = message;
        node.dataset.error = String(error);
    };
    const changed = () => [...new FormData(policyForm)].some(([key, value]) => value !== savedPolicy.get(key));
    async function responseError(response) {
        const data = await response.json().catch(() => ({}));
        return data.error || '操作未完成，请稍后重试';
    }
    async function jsonRequest(url, options) {
        const response = await fetch(url, options);
        if (!response.ok) throw new Error(await responseError(response));
        return response.json();
    }
    const bytesText = bytes => {
        if (bytes < 1024) return `${Math.round(bytes)} B`;
        if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
        if (bytes < 1073741824) return `${(bytes / 1048576).toFixed(1)} MB`;
        return `${(bytes / 1073741824).toFixed(2)} GB`;
    };
    const phases = {preparing:'正在准备', uploading:'正在传输备份', staging:'正在读取文件', reading:'正在解压读取',
        compressing:'正在生成备份', writing:'正在写入文件', flushing:'正在保存到磁盘', finalizing:'正在完成备份',
        checking_database:'正在检查旧版数据库', checking_relations:'正在校验数据关系', waiting_database:'正在等待数据库',
        indexing:'正在检查已有通知', merging_schools:'正在合并学校', merging_departments:'正在合并栏目',
        merging_announcements:'正在合并通知', merging_memberships:'正在合并通知来源', merging_sources:'正在合并来源',
        merging_directories:'正在合并目录', reviewing_sources:'正在核实来源配置', committing:'正在保存合并结果',
        downloading:'正在传输到浏览器', done:'已完成'};
    function progressView(id) {
        const node = document.getElementById(id);
        const bar = node.querySelector('progress');
        const started = performance.now();
        let lastUpdate = started, active = true;
        node.hidden = false;
        function update(event) {
            lastUpdate = performance.now();
            node.querySelector('[data-phase]').textContent = phases[event.phase] ||
                (event.phase.startsWith('validating_') ? '正在校验备份' : '正在处理');
            const fraction = event.total > 0 ? Math.min(100, event.done / event.total * 100) : null;
            if (fraction === null) bar.removeAttribute('value');
            else bar.value = fraction;
            node.querySelector('[data-percent]').textContent = fraction === null ? '' : `${Math.floor(fraction)}%`;
            const isBytes = event.processed_bytes != null;
            const byteStage = ['uploading', 'staging', 'reading', 'writing', 'downloading'].includes(event.phase);
            node.querySelector('[data-count]').textContent = byteStage
                ? `${bytesText(event.done || 0)}${event.total ? ' / ' + bytesText(event.total) : ''}`
                : (event.total ? `${event.done} / ${event.total} 条` : '');
            node.querySelector('[data-speed]').textContent = event.speed == null ? '正在计算速度…'
                : isBytes ? `${bytesText(event.speed)}/s` : `${Math.round(event.speed)} 条/s`;
            if (!event.total && event.processed_bytes == null)
                node.querySelector('[data-speed]').textContent = '处理中';
        }
        update({phase:'preparing'});
        const timer = setInterval(() => {
            const seconds = Math.floor((performance.now() - started) / 1000);
            node.querySelector('[data-elapsed]').textContent = `已用时 ${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
            if (active && performance.now() - lastUpdate > 3000)
                node.querySelector('[data-speed]').textContent = '处理中，等待下一批数据';
        }, 500);
        return {update, finish(success) {
            active = false;
            clearInterval(timer);
            node.querySelector('[data-speed]').textContent = '';
            if (success) {
                bar.value = 100;
                node.querySelector('[data-phase]').textContent = '已完成';
                node.querySelector('[data-percent]').textContent = '100%';
            } else {
                node.querySelector('[data-phase]').textContent = '未完成';
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
            xhr.onprogress = () => { if (xhr.status === 200) { try { consume(); } catch (_) { failure = '进度响应异常'; } } };
            xhr.onload = () => {
                if (xhr.status !== 200) {
                    let message = '传输未完成，请稍后重试';
                    try { message = JSON.parse(xhr.responseText).error || message; } catch (_) { /* Non-JSON proxy errors. */ }
                    reject(new Error(message)); return;
                }
                try { consume(); } catch (_) { failure = '进度响应异常'; }
                if (failure || !result) reject(new Error(failure || '连接中断，未收到完成结果，请检查后重试'));
                else resolve(result);
            };
            xhr.onerror = () => reject(new Error('连接中断，未收到完成结果，请检查后重试'));
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
        const mb = bytes => `${(bytes / 1048576).toFixed(1)} MB`;
        document.getElementById('storageTotal').textContent = mb(data.total_bytes);
        document.getElementById('announcementCount').textContent = data.announcement_count;
        const groups = {'运行目录与通知': data.database_bytes || 0,
            '栏目核实证据': data.source_governance_evidence_bytes || 0};
        for (const file of data.files) {
            if (file.kind === 'governance_evidence') continue;
            const name = file.name.replaceAll('\\', '/');
            let group = '日志及其他文件';
            if (name.includes('backup') || name.includes('.bak') || name.startsWith('rollback/')) group = '备份与回退资料';
            else if (name.startsWith('fetch-evidence/') || name.startsWith('fetch_evidence/')) group = '网页抓取缓存';
            else if (name.startsWith('discovery_cache.')) group = '调查缓存';
            else if (name.startsWith('school_watcher.db') || name.startsWith('source_catalog.sqlite3') || name.startsWith('catalog-generations/')) group = '运行目录与通知';
            groups[group] = (groups[group] || 0) + file.bytes;
        }
        root.querySelectorAll('[data-storage-group]').forEach(node => {
            node.textContent = mb(groups[node.dataset.storageGroup] || 0);
        });
    }
    async function run(id, working, action, refresh = false) {
        if (busy) return;
        busy = true;
        const controls = [...root.querySelectorAll('button, input, select')];
        controls.forEach(node => { node.disabled = true; });
        status(id, working);
        try {
            const message = await action();
            status(id, message);
            if (refresh) {
                try { await refreshUsage(); }
                catch (_) { status(id, `${message} 用量刷新失败，可刷新页面查看。`); }
            }
        } catch (error) {
            status(id, error.message || '网络连接中断，请稍后重试', true);
        } finally {
            busy = false;
            controls.forEach(node => { node.disabled = false; });
        }
    }
    policyForm.addEventListener('submit', event => {
        event.preventDefault();
        const submitted = new FormData(policyForm);
        const values = Object.fromEntries([...submitted].map(([key, value]) => [key, Number(value)]));
        run('policyStatus', '正在保存保留规则…', async () => {
            await jsonRequest('/api/storage/policy', {
                method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(values)
            });
            savedPolicy = submitted;
            return '保留规则已保存';
        });
    });
    root.querySelectorAll('[data-cleanup]').forEach(button => {
        button.addEventListener('click', () => {
            if (changed()) {
                status('cleanupStatus', '保留规则有未保存的修改，请先保存后再清理。', true);
                return;
            }
            const all = button.dataset.cleanup === 'all_cache';
            const messages = {body:'清理未收藏的正文缓存？通知和收藏保留。',
                discovery:'清理调查网页缓存？待处理进度保留。', fetch:'清理网页抓取缓存？正在使用的文件保留。',
                governance:'清理闲置核实证据？当前栏目和待核实事项的证据保留。',
                backups:'删除自动备份？手动导出的备份和回退资料保留。', logs:'清空已结束的抓取记录？正在运行的记录保留。'};
            const message = messages[button.dataset.cleanup] || (all
                ? '清空可清理缓存？通知目录和收藏保留。'
                : '按已保存规则清理？通知目录和收藏保留。');
            if (!window.confirm(message)) return;
            run('cleanupStatus', '正在清理，请稍候…', async () => {
                const result = await jsonRequest('/api/storage/cleanup', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({mode: button.dataset.cleanup})
                });
                const counts = [[result.evicted,'条正文缓存'],[result.discovery_snapshots_deleted,'份调查缓存'],
                    [result.fetch_evidence?.removed,'份抓取缓存'],[result.scrape_logs_deleted,'条抓取记录'],
                    [result.backups_deleted,'份自动备份'],[result.governance_evidence?.removed,'份闲置证据']]
                    .filter(([count]) => count > 0).map(([count, label]) => `${count} ${label}`);
                return counts.length ? `已清理 ${counts.join('、')}` : '没有可清理的内容';
            }, true);
        });
    });
    document.getElementById('exportStorage').addEventListener('click', () => {
        run('exportStatus', '正在生成数据备份，请稍候…', async () => {
            const desktop = !!window.pywebview?.api;
            const view = progressView('exportProgress');
            let success = false;
            try {
                const result = await transfer('/api/storage/export?progress=1' + (desktop ? '&save=1' : ''), null, view);
                if (!result.saved) await download(result, view);
                success = true;
                return result.saved ? `已保存到备份目录：${result.name}` : '数据备份已生成，请在下载列表中查看';
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
        } catch (_) { status('locationStatus', '无法读取文件位置，请重新打开应用。', true); }
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
            run('locationStatus', '正在下载并校验安装程序…', async () => {
                const result = await window.pywebview.api.reinstall();
                if (result.error) throw new Error(result.error);
                return result.cancelled ? '已取消' : '请在安装向导中选择新位置';
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
        status('locationStatus', '选择空文件夹；保存后迁移并重启。');
    }));
    resetLocations?.addEventListener('click', () => { fillLocations(originalLocations); status('locationStatus', ''); });
    saveLocations?.addEventListener('click', () => {
        const values = Object.fromEntries(locationFields.filter(input => input.dataset.location !== 'program')
            .map(input => [input.dataset.location, input.value]));
        run('locationStatus', '正在准备迁移…', async () => {
            const result = await window.pywebview.api.change_locations(values);
            if (result.error) throw new Error(result.error);
            return result.cancelled ? '已取消' : '正在迁移，完成后会重新打开';
        });
    });
    const importForm = document.getElementById('storageImportForm');
    importForm.addEventListener('submit', event => {
        event.preventDefault();
        const file = document.getElementById('storageBackupFile').files[0];
        if (!file) return;
        const expanded = Number(document.getElementById('importExpandedMB').value);
        if (!Number.isSafeInteger(expanded) || expanded < 1) {
            status('importStatus', '展开大小请输入正整数（MB）', true);
            return;
        }
        if (file.size > expanded * 1048576) {
            document.querySelector('.storage-import-options').open = true;
            status('importStatus', `文件为 ${bytesText(file.size)}，请提高允许的展开大小后重试`, true);
            return;
        }
        const data = new FormData(importForm);
        run('importStatus', '正在检查并合并备份，请勿重复提交…', async () => {
            const view = progressView('importProgress');
            let success = false;
            try {
                const url = `/api/storage/import?progress=1&upload_mb=${Math.ceil(file.size / 1048576) + 2}&expanded_mb=${expanded}`;
                const result = await transfer(url, data, view);
                document.getElementById('storageBackupFile').value = '';
                success = true;
                return `合并完成：新增 ${result.added} 条通知，去重 ${result.duplicates} 条，补回 ${result.bodies_restored} 条正文。`;
            } finally {
                view.finish(success);
            }
        }, true);
    });
})();
