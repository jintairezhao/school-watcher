"""Reuse a usable browser; install an isolated full Chromium only when needed."""
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from filelock import FileLock, Timeout
from playwright._repo_version import version as DRIVER_VERSION

log = logging.getLogger(__name__)


def read_status(data):
    try:
        state = json.loads((Path(data) / 'desktop-browser.json').read_text(encoding='utf-8'))
        if isinstance(state, dict) and state.get('driver') == DRIVER_VERSION:
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {'phase': 'checking', 'message': '正在检查采集组件…'}


def write_status(data, phase, message, **fields):
    data = Path(data)
    state = dict(phase=phase, message=message, driver=DRIVER_VERSION, **fields)
    descriptor, name = tempfile.mkstemp(prefix='browser-state-', suffix='.tmp', dir=data)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
            json.dump(state, output, ensure_ascii=False)
        os.replace(name, data / 'desktop-browser.json')
    finally:
        Path(name).unlink(missing_ok=True)
    return state


def probe(channel):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=channel, chromium_sandbox=True, timeout=20000)
        try:
            page = browser.new_page()
            page.set_content('<title>School Watcher</title><script>document.title += " ready"</script>')
            if page.title() != 'School Watcher ready':
                raise RuntimeError('Browser JavaScript check failed')
        finally:
            browser.close()


def install_chromium(data):
    # The official driver pins the revision matching this version of Playwright.
    # --no-shell retains headed verification and new headless in a single browser.
    from playwright._impl._driver import compute_driver_executable, get_driver_env
    node, cli = compute_driver_executable()
    environment = get_driver_env()
    environment.setdefault('PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT', '60000')
    environment['CI'] = '1'
    with (Path(data) / 'desktop-browser-download.log').open('ab') as output:
        subprocess.run([node, cli, 'install', 'chromium', '--no-shell'], env=environment,
                       stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0,
                       check=True, timeout=900)


def prepare(data, managed=False):
    data = Path(data)
    try:
        with FileLock(str(data / 'desktop-browser.lock'), timeout=0):
            write_status(data, 'checking', '正在检查采集组件…')
            preferred = os.environ.get('WATCHER_BROWSER_CHANNEL')
            channels = ([preferred] if preferred in ('chrome', 'msedge', 'chromium') else
                        (['msedge', 'chrome', 'chromium'] if sys.platform == 'win32' else ['chrome', 'msedge', 'chromium']))
            if managed:
                channels = ['chromium']
            for channel in channels:
                try:
                    probe(channel)
                except Exception:
                    log.info('Browser %s unavailable; checking next option', channel)
                    continue
                return write_status(data, 'ready', '采集组件已就绪', channel=channel,
                                    browser={'msedge': 'Microsoft Edge', 'chrome': 'Google Chrome', 'chromium': '独立采集组件'}[channel])
            write_status(data, 'downloading', '正在下载独立采集组件，首次准备可能需要几分钟。你可以继续查看通知与管理设置。')
            try:
                install_chromium(data)
                write_status(data, 'checking', '下载完成，正在验证采集组件…')
                probe('chromium')
            except Exception:
                log.exception('Could not prepare isolated Chromium')
                return write_status(data, 'error', '采集组件准备失败。请检查网络后点击重新检测；已保存的通知与设置仍可使用。')
            return write_status(data, 'ready', '采集组件已就绪', channel='chromium', browser='独立采集组件')
    except Timeout:
        return read_status(data)


def launch_channel(data):
    state = read_status(data)
    channel = state.get('channel')
    return channel if state.get('phase') == 'ready' and channel in ('msedge', 'chrome', 'chromium') else None


def request_managed_browser(data):
    write_status(data, 'checking', '当前浏览器暂不可用，正在准备独立采集组件…')
    (Path(data) / 'desktop-browser-repair.request').touch()
