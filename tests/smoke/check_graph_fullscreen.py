"""Hidden WebView2 window: HTML fullscreen covers the display and restores it."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def check():
    import webview
    from desktop.window_bounds import install_browser_fullscreen, install_work_area

    class Page(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers()
            self.wfile.write(b'<html><body><div id="scene">Isolated fullscreen check</div></body></html>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    origin = f'http://127.0.0.1:{server.server_port}'
    window = webview.create_window('Isolated graph fullscreen check', url=origin,
        width=1000, height=700, min_size=(760, 520), hidden=True, frameless=True)
    failures, results = [], []

    def wait(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if predicate(): return
            time.sleep(.05)
        raise AssertionError('Native fullscreen transition timed out')

    def drive():
        try:
            from System import Action
            from System.Windows.Forms import Screen, FormWindowState
            print('Native fullscreen: waiting for isolated page',flush=True)
            wait(lambda: window.events.loaded.is_set())
            print('Native fullscreen: attaching window event',flush=True)
            window.native.Invoke(Action(lambda: setattr(window.native, 'Opacity', 0)))
            # Keep rendering active at zero opacity. A truly hidden WebView defers
            # the DOM fullscreen exit until its next frame, which never paints.
            window.show()
            install_work_area(window)
            install_browser_fullscreen(window, lambda url: urlsplit(url).netloc==urlsplit(origin).netloc)

            def bounds():
                r=window.native.Bounds
                return (r.X,r.Y,r.Width,r.Height)

            normal_bounds = bounds()
            for maximized in (False, True):
                print(f'Native fullscreen: starting maximized={maximized}',flush=True)
                window.maximize() if maximized else window.restore()
                wait(lambda: (window.native.WindowState==FormWindowState.Maximized)==maximized)
                previous=bounds()
                tasks=[]
                # DevTools supplies genuine user activation without showing or focusing a window.
                def enter():
                    tasks.append(window.native.webview.CoreWebView2.CallDevToolsProtocolMethodAsync(
                        'Runtime.evaluate', json.dumps({'expression':"document.getElementById('scene').requestFullscreen()",
                                                       'userGesture':True,'awaitPromise':True})))
                window.native.Invoke(Action(enter))
                wait(lambda: tasks[0].IsCompleted)
                response=json.loads(str(tasks[0].Result))
                print('Native fullscreen: HTML request completed',flush=True)
                assert 'exceptionDetails' not in response, response
                wait(lambda: bool(getattr(window,'_browser_fullscreen',False)))
                screen=Screen.FromHandle(window.native.Handle).Bounds
                expected=(screen.X,screen.Y,screen.Width,screen.Height)
                wait(lambda: bounds()==expected)
                assert window.run_js('document.fullscreenElement?.id')=='scene'
                window.run_js('document.exitFullscreen()')
                wait(lambda: not getattr(window,'_browser_fullscreen',False))
                wait(lambda: (window.native.WindowState==FormWindowState.Maximized)==maximized)
                print(f'Native fullscreen: restored {bounds()}, expected {previous}',flush=True)
                wait(lambda: bounds()==previous)
                wait(lambda: window.run_js('document.fullscreenElement===null'))
                results.append({'starting_maximized':maximized,'physical_display_bounds':True,'restored_bounds':True})
            window.restore()
            wait(lambda: bounds()==normal_bounds)
        except BaseException:
            failures.append(traceback.format_exc())
        finally:
            window.destroy()

    try:
        with tempfile.TemporaryDirectory(prefix='graph-native-fullscreen-',ignore_cleanup_errors=True) as folder:
            webview.start(drive,gui='edgechromium',storage_path=folder,debug=False)
        if failures: raise AssertionError('\n'.join(failures))
        print(json.dumps({'native_fullscreen':results}))
    finally:
        server.shutdown(); server.server_close()


if __name__=='__main__':
    if os.name!='nt': raise SystemExit('This check requires Windows WebView2.')
    check()
