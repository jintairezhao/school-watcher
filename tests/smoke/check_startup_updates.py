"""Real hidden native window, isolated empty profile, and a simulated newer release."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def check():
    import webview
    from desktop.updater import Update
    from desktop.runtime import DesktopRuntime
    from desktop.window import run_window

    def wait(predicate, timeout=45):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.1)
        raise AssertionError('Native update notification timed out')

    with tempfile.TemporaryDirectory(prefix='watcher-startup-update-') as folder:
        root = Path(folder)
        original_create = webview.create_window
        windows, apis, failures = [], [], []
        def create(*args, **kwargs):
            kwargs['hidden'] = True
            window = original_create(*args, **kwargs)
            windows.append(window)
            apis.append(kwargs['js_api'])
            return window
        release = Update('9.0.0', 'fixture.exe', 'https://example.invalid', 100, 'a' * 64, '隔离验收版本')
        with patch.dict(os.environ, {'WATCHER_LOCATION_SETTINGS': str(root / 'locations.json')}), \
                patch('webview.create_window', side_effect=create), \
                patch('desktop.window.check_update', return_value=release) as checker, \
                patch('desktop.window.download_update') as downloader:
            runtime = DesktopRuntime(root / 'profile')
            def drive():
                try:
                    # Native URL/JS calls have their own short initialization timeout.
                    # Wait for the actual app navigation and loaded event first, including
                    # a clean CI profile's runtime/bootstrap work on slower machines.
                    wait(lambda: checker.called and windows and windows[0].events.loaded.is_set(), timeout=120)
                    wait(lambda: windows and apis[0]._allowed())
                    main = windows[0]
                    print(json.dumps({'update': apis[0].update_notification(), 'bridge': main.run_js('''JSON.stringify((() => {
                        let dynamicCode; try { dynamicCode = new Function('return 1')() === 1; }
                        catch (error) { dynamicCode = String(error); }
                        return {notice: !!document.getElementById('desktopUpdateNotice'),
                            api: Object.keys(window.pywebview?.api || {}), dynamicCode};
                    })())''')}), flush=True)
                    wait(lambda: main.run_js('!!document.querySelector("#desktopUpdateNotice:not([hidden])")'))
                    assert apis[0].update_notification()['version'] == '9.0.0'
                    main.run_js('document.getElementById("desktopUpdateOpen").click()')
                    wait(lambda: len(windows) == 2)
                    wait(lambda: windows[1].run_js('document.getElementById("action")?.textContent === "下载更新"'))
                    assert apis[1].state()['phase'] == 'available'
                    checker.assert_called_once()
                    downloader.assert_not_called()
                    assert not apis[0].update_notification()['available']
                    windows[1].destroy()
                    main.load_url(runtime.address + '/explore')
                    wait(lambda: main.run_js('!!document.getElementById("catalogQuery")'))
                    assert main.run_js('document.getElementById("desktopUpdateNotice").hidden')
                    # A loaded unrelated document must not see or dismiss update state.
                    main.load_html('<html><body>isolated untrusted page</body></html>')
                    wait(lambda: not apis[0]._allowed())
                    assert apis[0].update_notification() is None
                    assert apis[0].dismiss_update() is None
                except BaseException:
                    failures.append(traceback.format_exc())
                finally:
                    for window in list(webview.windows):
                        window.destroy()
            driver = threading.Thread(target=drive, daemon=True)
            driver.start()
            try:
                run_window(runtime)
                driver.join(5)
                if failures:
                    raise AssertionError('\n'.join(failures))
                assert not driver.is_alive()
            finally:
                runtime.close()
        print(json.dumps({'native_startup_update': 'passed', 'automatic_checks': checker.call_count,
                          'automatic_downloads': downloader.call_count}))


if __name__ == '__main__':
    check()
