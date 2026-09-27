const list = document.getElementById('verification-list');
const message = document.getElementById('verification-message');
const workspace = document.getElementById('verification-workspace');
const screen = document.getElementById('verification-screen');
const complete = document.getElementById('verification-complete');
const cancel = document.getElementById('verification-cancel');
let active = null;
let remote = null;
let timer = null;
const labels = {required: '需要验证', opening: '正在打开', active: '等待验证', verified: '已完成', cancelled: '已结束', expired: '已过期', failed: '未完成'};

async function api(path, method = 'GET') {
    const response = await fetch('/api/admin/browser-sessions' + path, {method,
        headers: {'X-CSRF-Token': document.querySelector('meta[name="csrf-token"]').content}});
    const value = await response.json();
    if (!response.ok) throw new Error(value.error || '操作未完成，请重试');
    return value;
}

function disconnect() {
    if (timer) clearTimeout(timer);
    timer = null;
    if (remote) remote.disconnect();
    remote = null;
    active = null;
    screen.replaceChildren();
    workspace.hidden = true;
}

async function refresh() {
    const data = await api('');
    list.replaceChildren();
    const rows = data.sessions.filter(row => row.status !== 'verified' && row.status !== 'cancelled');
    message.textContent = rows.length ? '选择一个来源处理验证。同一时间可以处理一个来源。' : '当前没有需要处理的访问验证。';
    for (const row of rows) {
        const item = document.createElement('li');
        const source = document.createElement('div');
        source.className = 'verification-source';
        const name = document.createElement('strong');
        name.textContent = row.url;
        const status = document.createElement('p');
        status.textContent = row.error || labels[row.status] || '等待处理';
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'btn btn-outline';
        button.textContent = row.status === 'active' ? '继续验证' : '处理访问验证';
        button.disabled = row.status === 'opening';
        button.addEventListener('click', () => open(row, button));
        source.append(name, status); item.append(source, button); list.append(item);
    }
}

async function open(row, button) {
    button.disabled = true;
    message.textContent = '正在打开官网验证窗口…';
    try {
        const value = await api('/' + row.id + '/open', 'POST');
        disconnect(); active = row.id; workspace.hidden = false;
        document.getElementById('verification-current').textContent = row.url;
        document.getElementById('verification-help').textContent = value.remote
            ? '请在下方官网页面完成验证，再点击“验证已完成”。窗口在 10 分钟后关闭。'
            : '请在本机新打开的浏览器窗口中完成官网验证，再返回这里点击“验证已完成”。';
        if (value.remote) {
            const {default: RFB} = await import('/browser-client/core/rfb.js');
            const ticket = await api('/' + row.id + '/ticket', 'POST');
            const url = new URL(ticket.websocket_path, location.origin);
            url.protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
            remote = new RFB(screen, url.href, {shared: true});
            remote.scaleViewport = true; remote.resizeSession = false;
            remote.addEventListener('disconnect', () => { if (active) message.textContent = '验证连接已关闭。如未完成，请重新打开验证。'; });
        }
        message.textContent = '验证窗口已打开。';
        workspace.scrollIntoView({block: 'start', behavior: 'auto'});
        timer = setTimeout(poll, 5000);
    } catch (error) { message.textContent = error.message; }
    finally { button.disabled = false; }
}

async function poll() {
    if (!active) return;
    try {
        const value = await api('/' + active);
        if (value.status !== 'active') { disconnect(); await refresh(); return; }
    } catch (error) { message.textContent = error.message; }
    timer = setTimeout(poll, 5000);
}

async function act(action) {
    if (!active) return;
    complete.disabled = cancel.disabled = true;
    try {
        await api('/' + active + '/' + action, 'POST');
        disconnect(); await refresh();
        message.textContent = action === 'verify' ? '验证已通过，系统将继续更新该来源。' : '本次验证已结束，已有通知已保留。';
    } catch (error) { message.textContent = error.message; }
    finally { complete.disabled = cancel.disabled = false; }
}
complete.addEventListener('click', () => act('verify'));
cancel.addEventListener('click', () => act('cancel'));
window.addEventListener('pagehide', disconnect);
refresh().catch(error => { message.textContent = error.message; });
