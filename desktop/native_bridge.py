"""Keep the trusted macOS window bridge usable without allowing JavaScript eval."""
import json

from desktop.runtime import resource_root


def install_csp_bridge(window, api):
    # pywebview 6.2 creates wrappers with new Function and delivers replies via
    # evaluate_js (eval). WKWebView correctly blocks both under our page CSP.
    # Keep the existing transport and Python origin guards, but use closures and
    # execute the already-generated reply directly in this one native window.
    from webview import util
    original_evaluate = window.evaluate_js
    original_loader = util.load_js_files

    def evaluate_reply(script, callback=None):
        if callback is None and script.startswith('window.pywebview._returnValuesCallbacks['):
            if window.events.closed.is_set():
                return None
            return window.run_js(script)
        return original_evaluate(script, callback)

    window.evaluate_js = evaluate_reply
    methods = [name for name in dir(api) if not name.startswith('_') and callable(getattr(api, name))]
    source = (resource_root() / 'desktop/ui/csp-bridge.js').read_text(encoding='utf-8')
    source = source.replace('__WATCHER_BRIDGE_METHODS__', json.dumps(methods))

    def load_scripts(target, platform):
        scripts, finish = original_loader(target, platform)
        if target is window:
            scripts += '\n' + source
        return scripts, finish

    # Install before pywebview finishes initialization, so loaded means the
    # bridge is complete. A second asynchronous loaded handler can otherwise
    # still be waiting on Cocoa after the window's event loop has exited.
    util.load_js_files = load_scripts

    def restore():
        if util.load_js_files is load_scripts:
            util.load_js_files = original_loader
        # Keep rejecting late replies on this closed window during shutdown.

    return restore
