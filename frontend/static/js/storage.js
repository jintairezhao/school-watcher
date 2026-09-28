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
            const response = await fetch('/api/storage/export' + (desktop ? '?save=1' : ''), {method: 'POST'});
            if (!response.ok) throw new Error(await responseError(response));
            if (desktop) {
                const result = await response.json();
                return `已保存到备份目录：${result.name}`;
            }
            const blob = await response.blob();
            const url = URL.createObjectURL(blob);
            const link = document.createElement('a');
            link.href = url;
            link.download = (response.headers.get('Content-Disposition') || '').match(/filename="?([^";]+)/)?.[1] || 'school-watcher-data.zip';
            document.body.appendChild(link);
            link.click();
            link.remove();
            setTimeout(() => URL.revokeObjectURL(url), 60000);
            return '数据备份已生成，请在浏览器下载列表中确认并妥善保存。';
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
        if (file.size >= 100 * 1024 * 1024) {
            status('importStatus', '备份文件应小于 100 MB，请选择较小的完整备份。', true);
            return;
        }
        const data = new FormData(importForm);
        run('importStatus', '正在检查并合并备份，请勿重复提交…', async () => {
            const result = await jsonRequest('/api/storage/import', {method: 'POST', body: data});
            importForm.reset();
            return `合并完成：新增 ${result.added} 条通知，去重 ${result.duplicates} 条，补回 ${result.bodies_restored} 条正文。当前及历史独有通知均已保留。`;
        }, true);
    });
})();
