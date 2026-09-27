"""Native application window and a narrowly scoped update dialog."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import urllib.request

from desktop import APP_NAME, VERSION
from desktop.runtime import resource_root
from desktop.updater import UpdateError, check_update, download_update, file_hash


def activate_existing(data):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = json.loads((Path(data) / 'desktop-instance.json').read_text(encoding='utf-8'))
            port = int(state['port'])
            if not 1024 <= port <= 65535 or len(state['token']) != 64:
                return False
            request = urllib.request.Request(f'http://127.0.0.1:{port}/activate', data=b'',
                headers={'X-Watcher-Token': state['token']})
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
                return response.status == 204
        except (OSError, ValueError, KeyError):
            time.sleep(.2)
    return False


class UpdateAPI:
    def __init__(self, data, quit_app):
        self._data, self._quit = data, quit_app
        self._window = None
        self._update = None
        self._path = None
        self._lock = threading.Lock()
        self._state = {'phase': 'idle', 'message': f'当前版本 {VERSION}', 'current': VERSION, 'progress': 0}

    def state(self):
        return dict(self._state)

    def check(self):
        if not self._lock.acquire(blocking=False):
            return self.state()
        try:
            self._state.update(phase='checking', message='正在检查 GitHub Release…')
            self._update = check_update()
            self._path = None
            if self._update:
                self._state.update(phase='available', message=f'发现新版本 {self._update.version}',
                    version=self._update.version, notes=self._update.notes, size=self._update.size)
            else:
                self._state.update(phase='latest', message='已经是最新版本')
        except UpdateError as exc:
            self._state.update(phase='error', message=str(exc))
        except Exception:
            logging.exception('Update check failed')
            self._state.update(phase='error', message='暂时无法检查更新，请稍后重试。')
        finally:
            self._lock.release()
        return self.state()

    def download(self):
        if not self._update or not self._lock.acquire(blocking=False):
            return self.state()
        try:
            self._state.update(phase='downloading', message='正在下载安装包…', progress=0)
            def progress(done, total):
                self._state['progress'] = round(done * 100 / total)
            self._path = download_update(self._update, self._data, progress)
            self._state.update(phase='ready', message='下载完成，完整性校验通过', progress=100)
        except UpdateError as exc:
            self._state.update(phase='download-error', message=str(exc))
        except Exception:
            logging.exception('Update download failed')
            self._state.update(phase='download-error', message='下载中断，请重试。现有版本不受影响。')
        finally:
            self._lock.release()
        return self.state()

    def install(self):
        if not self._path or not self._update or not self._lock.acquire(blocking=False):
            return self.state()
        try:
            if not self._path.is_file() or file_hash(self._path) != self._update.sha256:
                raise UpdateError('安装包已发生变化，请重新下载。')
            message = '将退出学校通知并打开安装程序。订阅、收藏和阅读记录会保留。'
            if sys.platform == 'darwin':
                message = '将退出学校通知并打开安装镜像。请把新版拖入 Applications 并替换旧版，已有数据会保留。'
            if not self._window.create_confirmation_dialog('安装更新', message):
                return self.state()
            if sys.platform == 'win32':
                environment = os.environ.copy()
                environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
                frozen = getattr(sys, 'frozen', False)
                if frozen:
                    import ctypes
                    ctypes.windll.kernel32.SetDllDirectoryW(None)
                try:
                    subprocess.Popen([str(self._path)], cwd=self._path.parent, env=environment)
                finally:
                    if frozen:
                        ctypes.windll.kernel32.SetDllDirectoryW(str(resource_root()))
            elif sys.platform == 'darwin':
                subprocess.run(['open', str(self._path)], check=True)
            self._quit()
        except UpdateError as exc:
            self._state.update(phase='download-error', message=str(exc))
        except Exception:
            logging.exception('Could not open installer')
            self._state.update(phase='error', message='未能打开安装程序，请在数据文件夹的 updates 目录中手动打开。')
        finally:
            self._lock.release()
        return self.state()


def run_window(runtime, smoke_test=False):
    import webview
    from webview.menu import Menu, MenuAction

    def quit_app():
        for item in list(webview.windows):
            item.destroy()

    def open_data():
        if os.name == 'nt':
            os.startfile(runtime.data_dir)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(runtime.data_dir)])

    update_window = None
    component_window = None
    def components():
        nonlocal component_window
        if component_window in webview.windows:
            component_window.restore()
            component_window.show()
            return
        class ComponentsAPI:
            def state(self):
                from desktop.browser import read_status
                return read_status(runtime.data_dir)
            def retry(self):
                runtime.prepare_browser()
                return {'phase': 'checking', 'message': '正在重新检测采集组件…'}
        component_window = webview.create_window('采集组件 · School Watcher',
            html=(resource_root() / 'desktop' / 'ui' / 'components.html').read_text(encoding='utf-8'),
            js_api=ComponentsAPI(), width=560, height=390, min_size=(480, 350), background_color='#f5f5f7')
    def updates():
        nonlocal update_window
        if update_window in webview.windows:
            update_window.restore()
            update_window.show()
            return
        api = UpdateAPI(runtime.data_dir, quit_app)
        update_window = webview.create_window('检查更新 · School Watcher',
            html=(resource_root() / 'desktop' / 'ui' / 'updates.html').read_text(encoding='utf-8'),
            js_api=api, width=560, height=500, min_size=(480, 420), background_color='#f5f5f7')
        api._window = update_window

    webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False,
                            OPEN_EXTERNAL_LINKS_IN_BROWSER=True, OPEN_DEVTOOLS_IN_DEBUG=False)
    window = webview.create_window(APP_NAME,
        html=(resource_root() / 'desktop' / 'ui' / 'loading.html').read_text(encoding='utf-8'),
        width=1380, height=900, min_size=(980, 680), text_select=True, zoomable=True,
        background_color='#f5f5f7', hidden=smoke_test)
    menu = [Menu('School Watcher', [MenuAction('检查更新…', updates), MenuAction('采集组件…', components),
        MenuAction('打开数据文件夹', open_data), MenuAction('退出', quit_app)])]

    token = secrets.token_hex(32)
    class ActivationHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            if self.path != '/activate' or not secrets.compare_digest(self.headers.get('X-Watcher-Token', ''), token):
                self.send_error(403)
                return
            window.restore()
            window.show()
            self.send_response(204)
            self.end_headers()
    control = ThreadingHTTPServer(('127.0.0.1', 0), ActivationHandler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    instance = runtime.data_dir / 'desktop-instance.json'
    descriptor = os.open(instance, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump({'port': control.server_port, 'token': token}, output)

    smoke_error = []
    def prepare():
        try:
            address = runtime.start()
            window.load_url(runtime.open_url())
            if smoke_test:
                print('Native check: waiting for page load', flush=True)
                if not window.events.loaded.wait(30):
                    raise RuntimeError('Native page load timed out.')
                print('Native check: page loaded; inspecting desktop navigation', flush=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if window.run_js('location.pathname === "/" && !!document.querySelector("a[href=\\"/admin\\"]") && !document.querySelector("a[href=\\"/login\\"],input[type=password]")'):
                        print('Native check: account-free desktop rendered', flush=True)
                        return
                    time.sleep(.2)
                raise RuntimeError('Native window did not render the desktop navigation.')
            while not runtime.stopped.wait(1):
                if runtime.failure:
                    raise RuntimeError(runtime.failure)
        except Exception as exc:
            logging.exception('Desktop startup or service failure')
            if smoke_test:
                smoke_error.append(exc)
                return
            try:
                import html
                window.load_html('<html lang="zh-CN"><meta charset="utf-8"><body style="font:16px system-ui;padding:60px">'
                    '<h2>学校通知暂时无法启动</h2><p>' + html.escape(str(exc)) + '</p><p>可从应用菜单打开数据文件夹查看日志。</p></body></html>')
            except Exception:
                pass
        finally:
            if smoke_test:
                print('Native check: closing window', flush=True)
                quit_app()
    def main_closed():
        runtime.stopped.set()
        for item in list(webview.windows):
            if item is not window:
                item.destroy()
    window.events.closed += main_closed
    try:
        webview.start(prepare, gui='edgechromium' if os.name == 'nt' else None,
            private_mode=False, storage_path=str(runtime.data_dir / 'webview'), menu=menu, debug=False)
        if smoke_error:
            raise smoke_error[0]
    finally:
        runtime.stopped.set()
        control.shutdown()
        control.server_close()
        instance.unlink(missing_ok=True)
