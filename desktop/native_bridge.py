"""Keep the trusted macOS window bridge usable without allowing JavaScript eval."""
import json

from desktop.runtime import resource_root


def install_csp_bridge(window, api, allowed):
    # pywebview 6.2 creates wrappers with new Function and delivers replies via
    # evaluate_js (eval). WKWebView correctly blocks both under our page CSP.
    # Keep the existing transport and Python origin guards, but use closures and
    # execute the already-generated reply directly in this one native window.
    original_evaluate = window.evaluate_js

    def evaluate_reply(script, callback=None):
        if callback is None and script.startswith('window.pywebview._returnValuesCallbacks['):
            return window.run_js(script)
        return original_evaluate(script, callback)

    window.evaluate_js = evaluate_reply
    methods = [name for name in dir(api) if not name.startswith('_') and callable(getattr(api, name))]
    source = (resource_root() / 'desktop/ui/csp-bridge.js').read_text(encoding='utf-8')
    source = source.replace('__WATCHER_BRIDGE_METHODS__', json.dumps(methods))

    def loaded():
        if allowed():
            window.run_js(source)

    window.events.loaded += loaded
