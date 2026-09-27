/**
 * 学校通知扒取工具 — 通用前端交互
 */

// ---- CSRF：所有写方法 fetch 自动带头（配合后端 _csrf_protect） ----
(function () {
    var _fetch = window.fetch;
    window.fetch = function (input, init) {
        init = init || {};
        var method = (init.method || 'GET').toUpperCase();
        if (['POST', 'PUT', 'DELETE', 'PATCH'].indexOf(method) >= 0) {
            init.headers = new Headers(init.headers || {});
            if (!init.headers.has('X-CSRF-Token')) {
                var m = document.querySelector('meta[name="csrf-token"]');
                if (m) init.headers.set('X-CSRF-Token', m.content);
            }
        }
        return _fetch(input, init);
    };
})();

// ---- 主题切换（浅色 / 暗色） ----
(function () {
    function effectiveDark() {
        var attr = document.documentElement.getAttribute('data-theme');
        if (attr === 'dark') return true;
        if (attr === 'light') return false;
        return window.matchMedia('(prefers-color-scheme: dark)').matches;
    }
    function syncThemeColor() {
        var meta = document.getElementById('themeColor');
        if (!meta) return;
        var bg = getComputedStyle(document.body).backgroundColor;
        meta.setAttribute('content', bg || '#f5f5f7');
        var toggle = document.getElementById('themeToggle');
        if (toggle) {
            toggle.setAttribute('aria-label', effectiveDark() ? '切换为浅色外观' : '切换为深色外观');
            toggle.title = toggle.getAttribute('aria-label');
        }
    }
    function apply(next) {
        document.documentElement.setAttribute('data-theme', next);
        try { localStorage.setItem('theme', next); } catch (e) {}
        syncThemeColor();
    }
    document.addEventListener('DOMContentLoaded', function () {
        syncThemeColor();
        var btn = document.getElementById('themeToggle');
        if (!btn) return;
        btn.addEventListener('click', function () {
            apply(effectiveDark() ? 'light' : 'dark');
        });
    });
})();

// ---- 导航栏汉堡菜单 ----
document.addEventListener('DOMContentLoaded', function() {
    const toggle = document.getElementById('navToggle');
    const links = document.getElementById('navLinks');

    if (toggle && links) {
        const setOpen = open => {
            links.classList.toggle('open', open);
            toggle.setAttribute('aria-expanded', String(open));
            toggle.setAttribute('aria-label', open ? '关闭菜单' : '打开菜单');
        };
        toggle.addEventListener('click', function() {
            setOpen(!links.classList.contains('open'));
        });

        // 点击页面其他地方关闭菜单
        document.addEventListener('click', function(e) {
            if (!toggle.contains(e.target) && !links.contains(e.target)) {
                setOpen(false);
            }
        });
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape' && links.classList.contains('open')) {
                setOpen(false);
                toggle.focus();
            }
        });
    }
});

// ---- 顶栏设置下拉菜单 ----
document.addEventListener('DOMContentLoaded', function() {
    const item = document.getElementById('settingsNavItem');
    if (!item) return;
    const btn = item.querySelector('.nav-tab--menu');

    btn.addEventListener('click', function(e) {
        e.stopPropagation();
        const willOpen = !item.classList.contains('is-open');
        item.classList.toggle('is-open', willOpen);
        btn.setAttribute('aria-expanded', willOpen ? 'true' : 'false');
    });

    document.addEventListener('click', function(e) {
        if (!item.contains(e.target)) {
            item.classList.remove('is-open');
            btn.setAttribute('aria-expanded', 'false');
        }
    });

    document.addEventListener('keydown', function(e) {
        if (e.key === 'Escape') {
            item.classList.remove('is-open');
            btn.setAttribute('aria-expanded', 'false');
        }
    });
});

// ---- Toast 通知 ----
function showToast(type, message) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = 'toast toast-' + type;
    toast.textContent = message;
    container.appendChild(toast);

    setTimeout(function() {
        toast.style.opacity = '0';
        toast.style.transition = 'opacity 0.3s ease';
        setTimeout(function() { toast.remove(); }, 300);
    }, 3000);
}

// ---- 确认对话框 ----
function confirmAction(message, callback) {
    if (confirm(message)) {
        callback();
    }
}

// ---- 相对时间格式化 ----
function timeAgo(dateStr) {
    if (!dateStr) return '';
    const date = new Date(dateStr);
    const now = new Date();
    const diff = now - date;
    const minutes = Math.floor(diff / 60000);
    const hours = Math.floor(diff / 3600000);
    const days = Math.floor(diff / 86400000);

    if (minutes < 1) return '刚刚';
    if (minutes < 60) return minutes + '分钟前';
    if (hours < 24) return hours + '小时前';
    if (days < 7) return days + '天前';
    return date.toLocaleDateString('zh-CN');
}

// ---- 键盘快捷键 ----
document.addEventListener('keydown', function(e) {
    // Ctrl+K 或 / 聚焦搜索（如果有的话）
    // Esc 关闭模态框
    if (e.key === 'Escape') {
        const modals = document.querySelectorAll('.modal-overlay');
        modals.forEach(function(m) { m.style.display = 'none'; });
    }
});
