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
from desktop.updater import UpdateError, check_update, download_update


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


def request_exit(data=None):
    """Authenticated local request used by this installation's uninstaller."""
    from desktop.runtime import user_data_dir
    try:
        state = json.loads(((Path(data) if data else user_data_dir()) / 'desktop-instance.json').read_text(encoding='utf-8'))
        port, token = int(state['port']), state['token']
        if not 1024 <= port <= 65535 or not isinstance(token, str) or len(token) != 64:
            return False
        request = urllib.request.Request(f'http://127.0.0.1:{port}/quit', data=b'',
            headers={'X-Watcher-Token': token})
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=1) as response:
            return response.status == 204
    except (OSError, ValueError, KeyError, TypeError):
        return False


class UpdateAPI:
    def __init__(self, data, quit_app, *, install_handoff=None, change_locations=False):
        self._data, self._quit = data, quit_app
        self._install_handoff = install_handoff
        self._change_locations = change_locations
        self._window = None
        self._update = None
        self._path = None
        self._lock = threading.Lock()
        self._startup_lock = threading.Lock()
        self._startup_started = False
        self._dismissed_version = None
        self._state = {'phase': 'idle', 'message': f'当前版本 {VERSION}', 'current': VERSION, 'progress': 0}

    def state(self):
        return dict(self._state)

    def startup_check(self):
        """One background check per application launch; never open a modal or download."""
        with self._startup_lock:
            if self._startup_started:
                return None
            self._startup_started = True
        thread = threading.Thread(target=lambda: self.check(only_if_idle=True),
                                  daemon=True, name='startup-update-check')
        thread.start()
        return thread

    def notification(self):
        state = self.state()
        version = state.get('version')
        return {'phase': state['phase'], 'version': version,
                'available': bool(version and version != self._dismissed_version and
                                  state['phase'] in ('available', 'downloading', 'ready', 'download-error'))}

    def dismiss_notification(self):
        self._dismissed_version = self._state.get('version')
        return self.notification()

    def check(self, only_if_idle=False):
        if not self._lock.acquire(blocking=False):
            return self.state()
        try:
            if only_if_idle and self._state['phase'] != 'idle':
                return self.state()
            self._update = None
            self._path = None
            for key in ('version', 'notes', 'size'):
                self._state.pop(key, None)
            self._state.update(phase='checking', message='正在检查 GitHub Release…')
            self._update = check_update()
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
            from desktop.locations import effective_locations
            self._path = download_update(self._update, self._data, progress,
                                         download_dir=effective_locations(self._data)['downloads'])
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
        if self._state['phase'] == 'installing' or not self._path or not self._update or not self._lock.acquire(blocking=False):
            return self.state()
        try:
            from desktop.install_update import prepare_install
            request = prepare_install(self._path, self._update, change_locations=self._change_locations)
            message = ('将退出应用，在原位置更新并自动重新打开。文件位置和个人数据保持不变。'
                       if request.in_place else '将退出应用并打开安装向导，可选择文件位置。已有数据会保留。')
            if sys.platform == 'darwin':
                message = '将退出学校通知并打开安装镜像。请把新版拖入 Applications 并替换旧版，已有数据会保留。'
            if not self._window.create_confirmation_dialog('更新学校通知' if request.in_place else '安装更新', message):
                return self.state()
            self._state.update(phase='installing', message='正在关闭服务并准备更新…')
            if not self._install_handoff or not self._install_handoff(request):
                raise UpdateError('应用暂时无法退出，请完成当前文件操作后重试。')
        except UpdateError as exc:
            self._state.update(phase='download-error', message=str(exc))
        except Exception:
            logging.exception('Could not open installer')
            self._state.update(phase='error', message='未能打开安装程序，请在文件位置中打开更新包目录后重试。')
        finally:
            self._lock.release()
        return self.state()


def run_window(runtime, smoke_test=False):
    import webview
    from webview.menu import Menu, MenuAction

    migration = None
    migration_window = None
    def quit_app():
        if migration and not migration.done.is_set():
            return False
        for item in list(webview.windows):
            item.destroy()
        return True

    def open_data():
        if os.name == 'nt':
            os.startfile(runtime.data_dir)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', str(runtime.data_dir)])

    update_window = None
    component_window = None
    def install_handoff(request):
        runtime.requested_update = request
        try:
            if quit_app():
                return True
        except Exception:
            runtime.requested_update = None
            raise
        runtime.requested_update = None
        return False
    update_api = UpdateAPI(runtime.data_dir, quit_app, install_handoff=install_handoff)
    location_busy = threading.Lock()
    def start_migration(values, release):
        nonlocal migration, migration_window
        from desktop.migration import Migration
        operation = Migration(runtime, values)
        migration = operation
        finished = threading.Event()

        class MigrationAPI:
            def state(self):
                return operation.state()
            def cancel(self):
                return operation.cancel()
            def finish(self):
                if not operation.done.is_set() or finished.is_set():
                    return False
                finished.set()
                if operation.restart:
                    runtime.restart_requested = True
                    quit_app()
                else:
                    dialog.destroy()
                    window.show()
                    window.restore()
                    release()
                return True

        api = MigrationAPI()
        dialog = webview.create_window('更改文件位置',
            html=(resource_root() / 'desktop' / 'ui' / 'migration.html').read_text(encoding='utf-8'),
            js_api=api, width=560, height=480, min_size=(460, 440), background_color='#f5f5f7')
        migration_window = dialog
        def closing():
            if not operation.done.is_set():
                operation.cancel()
                return False
            if not finished.is_set():
                threading.Thread(target=api.finish, daemon=True).start()
                return False
            return True
        dialog.events.closing += closing
        window.hide()
        threading.Thread(target=operation.run, daemon=True, name='location-migration').start()
    def locations():
        window.load_url(runtime.address + '/admin/storage#locations')
    def uninstall():
        from desktop.uninstall import uninstaller_path
        if not location_busy.acquire(blocking=False):
            return {'error': '文件操作正在进行，请稍候。'}
        update_locked = False
        try:
            if update_api:
                update_locked = update_api._lock.acquire(blocking=False)
                if not update_locked:
                    return {'error': '更新正在进行，请完成后再卸载。'}
            path = uninstaller_path()
            if path is None:
                return {'error': '当前是便携版，请退出应用后删除程序文件夹；个人数据会保留。'}
            if not window.create_confirmation_dialog('卸载学校通知', '将退出应用并打开卸载向导，可选择保留个人数据。'):
                return {'cancelled': True}
            runtime.requested_uninstaller = path
            quit_app()
            return {'quitting': True}
        finally:
            if not getattr(runtime, 'requested_uninstaller', None):
                if update_locked: update_api._lock.release()
                location_busy.release()
    def components():
        nonlocal component_window
        if location_busy.locked():
            return
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
        if location_busy.locked():
            return False
        if update_window in webview.windows:
            update_window.restore()
            update_window.show()
            return True
        update_window = webview.create_window('检查更新 · School Watcher',
            html=(resource_root() / 'desktop' / 'ui' / 'updates.html').read_text(encoding='utf-8'),
            js_api=update_api, width=560, height=500, min_size=(480, 420), background_color='#f5f5f7')
        update_api._window = update_window
        return True

    webview.settings.update(ALLOW_DOWNLOADS=True, ALLOW_FILE_URLS=False,
                            OPEN_EXTERNAL_LINKS_IN_BROWSER=True, OPEN_DEVTOOLS_IN_DEBUG=False)
    class WindowAPI:
        def _allowed(self):
            from urllib.parse import urlsplit
            if window.events.closed.is_set():
                return False
            actual = urlsplit(window.get_current_url() or '')
            expected = urlsplit(runtime.address or '')
            return actual.scheme == 'http' and actual.netloc == expected.netloc and actual.hostname == '127.0.0.1'

        def window_action(self, action):
            if not self._allowed():
                return False
            if migration and not migration.done.is_set():
                return False
            if action == 'minimize':
                window.minimize()
            elif action == 'maximize':
                window.restore() if state['maximized'] else window.maximize()
            elif action == 'close':
                quit_app()
            elif action == 'updates':
                return updates()
            elif action == 'components':
                components()
            elif action == 'data':
                open_data()
            elif action == 'locations':
                locations()
            elif action == 'uninstall':
                return uninstall()
            else:
                return False
            return True

        def location_state(self):
            if not self._allowed(): return None
            from desktop.locations import location_state
            return location_state(runtime.data_dir)

        def update_notification(self):
            if not self._allowed(): return None
            return update_api.notification()

        def dismiss_update(self):
            if not self._allowed(): return None
            return update_api.dismiss_notification()

        def choose_location(self, kind):
            if not self._allowed() or kind not in ('data', 'cache', 'backups', 'downloads'):
                return None
            from desktop.locations import effective_locations
            choices = window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(effective_locations(runtime.data_dir)[kind]))
            return str(choices[0]) if choices else None

        def open_location(self, kind):
            if not self._allowed() or kind not in ('data', 'cache', 'backups', 'downloads', 'program'):
                return False
            from desktop.locations import location_state
            path = Path(location_state(runtime.data_dir)[kind])
            path.mkdir(parents=True, exist_ok=True)
            if os.name == 'nt': os.startfile(path)
            elif sys.platform == 'darwin': subprocess.Popen(['open', str(path)])
            return True

        def change_locations(self, values):
            if not self._allowed(): return {'error': '无法更改文件位置'}
            if not location_busy.acquire(blocking=False): return {'error': '文件操作正在进行，请稍候。'}
            from desktop.locations import validate_locations
            update_locked = False
            started = False
            try:
                validate_locations(values, runtime.data_dir)
                if update_api:
                    update_locked = update_api._lock.acquire(blocking=False)
                    if not update_locked:
                        return {'error': '安装包正在下载，请完成后再更改位置。'}
                if not window.create_confirmation_dialog('更改文件位置', '将暂停抓取、迁移数据并重新打开。原目录保留副本。'):
                    return {'cancelled': True}
                def release():
                    if update_locked: update_api._lock.release()
                    location_busy.release()
                start_migration(values, release)
                started = True
                return {'migrating': True}
            except (ValueError, OSError) as exc:
                return {'error': str(exc)}
            finally:
                if not started:
                    if update_locked: update_api._lock.release()
                    location_busy.release()

        def reinstall(self):
            if not self._allowed(): return {'error': '无法打开安装程序'}
            if not location_busy.acquire(blocking=False): return {'error': '文件操作正在进行，请稍候。'}
            update_locked = False
            try:
                if update_api:
                    update_locked = update_api._lock.acquire(blocking=False)
                    if not update_locked: return {'error': '安装包正在下载，请稍候。'}
                from desktop.locations import effective_locations
                update = check_update(allow_current=True)
                if not update: raise UpdateError('暂时没有可用的安装程序。')
                package = download_update(update, runtime.data_dir, download_dir=effective_locations(runtime.data_dir)['downloads'])
                api = UpdateAPI(runtime.data_dir, quit_app, install_handoff=install_handoff, change_locations=True)
                api._path, api._update, api._window = package, update, window
                result = api.install()
                return {'error': result['message']} if result['phase'] in ('error', 'download-error') else result
            except (UpdateError, OSError) as exc:
                return {'error': str(exc)}
            finally:
                if update_locked: update_api._lock.release()
                location_busy.release()

        def resize_window(self, width, height):
            if not self._allowed() or state['maximized'] or type(width) not in (int, float) or type(height) not in (int, float):
                return False
            window.resize(max(760, min(10000, int(width))), max(520, min(10000, int(height))))
            return True

    state = {'maximized': False}
    main_api = WindowAPI()
    window = webview.create_window(APP_NAME,
        html=(resource_root() / 'desktop' / 'ui' / 'loading.html').read_text(encoding='utf-8'),
        width=1280, height=820, min_size=(760, 520), text_select=True, zoomable=True,
        background_color='#f5f5f7', hidden=smoke_test, js_api=main_api,
        frameless=os.name == 'nt', easy_drag=False)
    restore_bridge = None
    if sys.platform == 'darwin':
        from desktop.native_bridge import install_csp_bridge
        restore_bridge = install_csp_bridge(window, main_api)
    def maximized():
        state['maximized'] = True
    def restored():
        state['maximized'] = False
    window.events.maximized += maximized
    window.events.restored += restored
    def main_closing():
        return not migration or migration.done.is_set()
    window.events.closing += main_closing
    menu = [Menu('School Watcher', [MenuAction('检查更新…', updates), MenuAction('采集组件…', components),
        MenuAction('文件位置…', locations), MenuAction('打开数据文件夹', open_data), MenuAction('退出', quit_app)])]

    token = secrets.token_hex(32)
    class ActivationHandler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            if self.path not in ('/activate', '/quit') or not secrets.compare_digest(self.headers.get('X-Watcher-Token', ''), token):
                self.send_error(403)
                return
            if self.path == '/activate':
                target = migration_window if migration_window in webview.windows else window
                target.restore()
                target.show()
            if self.path == '/quit' and migration and not migration.done.is_set():
                self.send_error(409, 'Location migration in progress')
                return
            self.send_response(204)
            self.end_headers()
            if self.path == '/quit':
                threading.Thread(target=quit_app, daemon=True).start()
    control = ThreadingHTTPServer(('127.0.0.1', 0), ActivationHandler)
    threading.Thread(target=control.serve_forever, daemon=True).start()
    instance = runtime.data_dir / 'desktop-instance.json'
    descriptor = os.open(instance, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        json.dump({'port': control.server_port, 'token': token}, output)

    smoke_error = []
    def prepare():
        try:
            if os.name == 'nt':
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and (window.native is None or not window.native.IsHandleCreated):
                    time.sleep(.05)
                if window.native is None or not window.native.IsHandleCreated:
                    raise RuntimeError('窗口初始化超时')
                from desktop.window_bounds import install_work_area
                install_work_area(window)
            address = runtime.start()
            window.load_url(runtime.open_url())
            if not smoke_test:
                update_api.startup_check()
            if smoke_test:
                print('Native check: waiting for page load', flush=True)
                if not window.events.loaded.wait(30):
                    raise RuntimeError('Native page load timed out.')
                print('Native check: page loaded; inspecting desktop navigation', flush=True)
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if window.run_js('location.pathname === "/" && !!document.querySelector("a[href=\\"/admin\\"]") && !document.querySelector("a[href=\\"/login\\"],input[type=password]")'):
                        print('Native check: account-free desktop rendered', flush=True)
                        if sys.platform == 'darwin':
                            # Wait for real page-to-native replies before the test
                            # immediately closes the newly rendered Cocoa window.
                            until = time.monotonic() + 10
                            while time.monotonic() < until:
                                if window.run_js('Object.values(window.pywebview._returnValuesCallbacks || {}).every(calls => Object.keys(calls).length === 0)'):
                                    break
                                time.sleep(.1)
                            else:
                                raise RuntimeError('Native page callbacks did not finish.')
                        if os.name == 'nt':
                            if not window.run_js('!!document.querySelector("[data-window-action=close]")'):
                                raise RuntimeError('Desktop window controls did not render.')
                            window.run_js('window.pywebview.api.resize_window(1000,700).then(ok => {window.__resizeChecked=ok;})')
                            until = time.monotonic() + 10
                            while time.monotonic() < until and not window.run_js('window.__resizeChecked === true'):
                                time.sleep(.1)
                            if not window.run_js('window.__resizeChecked === true'):
                                raise RuntimeError('Native resize bridge did not respond.')
                            window.run_js('window.pywebview.api.window_action("invalid-action").then(ok => {window.__actionRejected=ok===false;})')
                            until = time.monotonic() + 10
                            while time.monotonic() < until and not window.run_js('window.__actionRejected === true'):
                                time.sleep(.1)
                            if not window.run_js('window.__actionRejected === true'):
                                raise RuntimeError('Native action allowlist did not reject an unknown action.')
                            from System import Action
                            from System.Windows.Forms import Screen
                            window.native.Invoke(Action(lambda: setattr(window.native, 'Opacity', 0)))
                            window.show()
                            window.maximize()
                            time.sleep(.3)
                            work = Screen.FromHandle(window.native.Handle).WorkingArea
                            bounds = window.native.Bounds
                            if not work.Contains(bounds):
                                raise RuntimeError('Maximized window covered the taskbar.')
                            window.restore()
                            import ctypes
                            from ctypes import wintypes
                            user32 = ctypes.WinDLL('user32', use_last_error=True)
                            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
                            user32.GetWindowLongW.restype = wintypes.LONG
                            user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
                            user32.IsIconic.argtypes = [wintypes.HWND]
                            handle = window.native.Handle.ToInt64()
                            style = user32.GetWindowLongW(handle, -16)
                            if not style & 0x20000 or style & 0xC00000:
                                raise RuntimeError('Frameless shell minimize capability was lost.')
                            # Exercise native system commands, not just the custom JS buttons.
                            for command, minimized in ((0xF020, True), (0xF120, False)):
                                user32.PostMessageW(handle, 0x112, command, 0)
                                until = time.monotonic() + 5
                                while time.monotonic() < until and bool(user32.IsIconic(handle)) != minimized:
                                    time.sleep(.05)
                                if bool(user32.IsIconic(handle)) != minimized:
                                    raise RuntimeError('Native minimize/restore command did not take effect.')
                            window.run_js('window.pywebview.api.location_state().then(v => {window.__locationsChecked=!!v.data && !!v.cache && !!v.backups && !!v.downloads;})')
                            until = time.monotonic() + 10
                            while time.monotonic() < until and not window.run_js('window.__locationsChecked === true'):
                                time.sleep(.1)
                            if not window.run_js('window.__locationsChecked === true'):
                                raise RuntimeError('Native file locations bridge did not respond.')
                            print('Native check: frameless controls and resize bridge verified', flush=True)
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
            private_mode=False, storage_path=str(runtime.data_dir / 'webview'),
            menu=menu if sys.platform == 'darwin' else [], debug=False)
        if smoke_error:
            raise smoke_error[0]
    finally:
        runtime.stopped.set()
        if restore_bridge:
            restore_bridge()
        control.shutdown()
        control.server_close()
        instance.unlink(missing_ok=True)
