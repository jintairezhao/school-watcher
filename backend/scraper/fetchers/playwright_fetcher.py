"""Playwright Chromium headless 抓取模块 — JS 渲染页面的智能回退方案

提供浏览器单例管理、JS 渲染页面抓取、以及 JS 必要性检测。

用法:
    from backend.scraper.fetchers.playwright_fetcher import fetch_html_with_browser, is_playwright_available

    if is_playwright_available():
        html = fetch_html_with_browser(url)
"""

import os
import logging
import threading
import re

# ⛔ 强制 Playwright 浏览器安装到 D 盘（项目规则：非必要不往C盘塞东西）
_PLAYWRIGHT_BROWSERS_DIR = r'D:\Jinta\Tools\playwright-browsers'
os.environ['PLAYWRIGHT_BROWSERS_PATH'] = _PLAYWRIGHT_BROWSERS_DIR

logger = logging.getLogger(__name__)

# --- 可用性检测 ------------------------------------------------------------
_playwright_available = False
_playwright_import_error = None
_sync_playwright = None

try:
    from playwright.sync_api import sync_playwright as _sync_playwright
    _playwright_available = True
except ImportError as e:
    _playwright_import_error = str(e)

# --- 浏览器单例 ------------------------------------------------------------
_browser = None           # playwright.sync_api.Browser | None
_playwright_instance = None  # playwright.sync_api.Playwright | None
_lock = threading.Lock()
_browser_init_failed = False

_BROWSER_LAUNCH_KWARGS = {
    'headless': True,
    'args': [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--disable-extensions',
        '--disable-background-networking',
        '--disable-sync',
        '--mute-audio',
        '--hide-scrollbars',
        '--no-first-run',
        '--no-default-browser-check',
    ],
}


def is_playwright_available() -> bool:
    """检测 Playwright 和 Chromium 是否可用（懒检测，只测一次）。"""
    return _playwright_available


def get_playwright_error() -> str or None:
    """返回导入错误信息（如果不可用）。"""
    return _playwright_import_error


def _get_browser():
    """懒加载单例浏览器实例（必须在 _lock 内调用）。

    Raises:
        RuntimeError: 浏览器初始化失败
    """
    global _browser, _playwright_instance, _browser_init_failed

    if _browser_init_failed:
        raise RuntimeError("Chromium 初始化之前已失败，不再重试")

    if _browser is not None:
        # 检查浏览器进程是否还活着
        try:
            if _browser.is_connected():
                return _browser
        except Exception:
            logger.warning("浏览器连接已断开，重新初始化")
            _browser = None
            if _playwright_instance:
                try:
                    _playwright_instance.stop()
                except Exception:
                    pass
                _playwright_instance = None

    try:
        _playwright_instance = _sync_playwright().start()
        _browser = _playwright_instance.chromium.launch(**_BROWSER_LAUNCH_KWARGS)
        logger.info("Playwright Chromium headless 浏览器已启动")
        return _browser
    except Exception as e:
        _browser_init_failed = True
        logger.error(f"无法启动 Playwright Chromium: {e}")
        logger.error("请运行: python -m playwright install chromium")
        if _playwright_instance:
            try:
                _playwright_instance.stop()
            except Exception:
                pass
            _playwright_instance = None
        raise RuntimeError(f"无法启动 Chromium 浏览器: {e}")


def fetch_html_with_browser(url: str, timeout_ms: int = 30000,
                            wait_until: str = 'networkidle') -> str or None:
    """用 Chromium headless 获取页面渲染后的完整 HTML。

    Args:
        url: 目标 URL
        timeout_ms: 页面加载超时（毫秒）
        wait_until: 'load' | 'domcontentloaded' | 'networkidle'
                    默认 'networkidle' 以等待 AJAX 完成

    Returns:
        渲染后的 HTML 字符串，失败返回 None
    """
    if not _playwright_available:
        logger.warning("Playwright 未安装，无法使用浏览器抓取")
        return None

    with _lock:
        try:
            browser = _get_browser()
        except RuntimeError as e:
            logger.error(f"获取浏览器实例失败: {e}")
            return None

        try:
            context = browser.new_context(
                user_agent=(
                    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/125.0.0.0 Safari/537.36'
                ),
                viewport={'width': 1920, 'height': 1080},
                locale='zh-CN',
            )
            page = context.new_page()
        except Exception as e:
            logger.error(f"创建浏览器上下文失败: {e}")
            return None

    try:
        response = page.goto(url, timeout=timeout_ms, wait_until=wait_until)
        # 如果HTTP状态码是4xx/5xx，仍返回HTML（可能是自定义错误页）
        if response and response.status >= 400:
            logger.debug(f"Playwright 页面返回 HTTP {response.status}: {url}")
        html = page.content()
        return html
    except Exception as e:
        logger.warning(f"Playwright 页面加载失败: {url} - {e}")
        return None
    finally:
        with _lock:
            try:
                page.close()
                context.close()
            except Exception as e:
                logger.debug(f"关闭 page/context 时出错: {e}")


def is_js_required(html: str) -> bool:
    """启发式检测：页面是否需要 JS 渲染才能获取实际内容。

    当 curl_cffi 拿到的静态 HTML 明显不完整时返回 True。
    宁可误判（多花时间用浏览器重试）也不漏判（导致0条通知）。

    Args:
        html: curl_cffi 获取的原始 HTML 字符串

    Returns:
        True 表示页面很可能需要 JS 渲染
    """
    if not html or len(html.strip()) < 200:
        return True  # 空页或近乎空页

    html_lower = html.lower()
    stripped = html.strip()

    # --- Signal 1: 常见 SPA 框架的空挂载点 ---
    framework_markers = [
        '<div id="app"></div>',
        '<div id="root"></div>',
        '<div id="__next">',
        '<div id="__nuxt">',
        '<app-root>',
        '<div id="app">',
    ]
    for marker in framework_markers:
        if marker in html_lower:
            return True

    # --- Signal 2: body 内几乎无可见文本（纯 script 骨架） ---
    body_match = re.search(
        r'<body[^>]*>(.*?)</body>', stripped, re.DOTALL | re.IGNORECASE
    )
    if body_match:
        body = body_match.group(1)
        # 移除 script/style/noscript 标签
        clean = re.sub(
            r'<(script|style|noscript)\b[^>]*>.*?</\1>',
            '', body, flags=re.DOTALL | re.IGNORECASE
        )
        # 移除所有 HTML 标签得到纯文本
        text = re.sub(r'<[^>]+>', '', clean).strip()
        # 小于30字符视为无实质内容（正常页面至少有导航+面包屑+标题）
        if len(text) < 30:
            return True

    # --- Signal 3: AJAX 加载占位符（页面内容极少时） ---
    ajax_placeholders = ['加载中', 'loading', '请稍候', '正在加载']
    text_no_tags = re.sub(r'<[^>]+>', '', stripped).strip()
    for p in ajax_placeholders:
        if p in stripped and len(text_no_tags) < 100:
            return True

    # --- Signal 4: HTTP 拦截/质询页面 ---
    interception_markers = [
        'just a moment',           # Cloudflare Turnstile
        'access denied',
        'checking your browser',
        'enable javascript',
        '请启用javascript',
        '请开启javascript',
        '请打开javascript',
        '您的浏览器不支持',
    ]
    for m in interception_markers:
        if m in html_lower:
            return True

    # --- Signal 5: 页面内容极少但有大量 script ---
    script_count = len(re.findall(r'<script\b', html_lower))
    text_no_tags = re.sub(r'<[^>]+>', '', stripped).strip()
    # 有 5+ 个 script 标签但去除标签后文本 < 150 字符 → 很可能是SPA
    if script_count >= 5 and len(text_no_tags) < 150:
        return True

    return False


def shutdown_browser():
    """优雅关闭浏览器和 Playwright 实例。

    通过 atexit 自动注册，应用退出时调用。
    线程安全：获取 _lock 后再关闭。
    """
    global _browser, _playwright_instance, _browser_init_failed
    with _lock:
        if _browser is not None:
            try:
                _browser.close()
                logger.info("Playwright 浏览器已关闭")
            except Exception as e:
                logger.debug(f"关闭浏览器时出错: {e}")
            _browser = None
        if _playwright_instance is not None:
            try:
                _playwright_instance.stop()
                logger.info("Playwright 实例已停止")
            except Exception as e:
                logger.debug(f"停止 Playwright 时出错: {e}")
            _playwright_instance = None
        _browser_init_failed = False


# 注册 atexit 钩子确保退出时清理
import atexit
atexit.register(shutdown_browser)
