"""Localize native windows using the web UI's catalog and selected language."""
import json
import logging
from pathlib import Path
import threading

from desktop.runtime import resource_root
from shared.ui_messages import english_catalog, translate_for_locale


class DesktopLanguage:
    def __init__(self, data):
        self._path = Path(data) / 'desktop-language.json'
        self._lock = threading.Lock()
        self.locale = 'zh-CN'
        try:
            value = json.loads(self._path.read_text(encoding='utf8'))
            if isinstance(value, dict) and value.get('locale') in ('zh-CN', 'en'):
                self.locale = value['locale']
        except (OSError, ValueError):
            pass

    def set_locale(self, locale):
        if locale not in ('zh-CN', 'en'):
            return False
        with self._lock:
            if self.locale == locale:
                return True
            self.locale = locale
            # This is only a launch cache. The signed-in web preference remains
            # authoritative and is synchronized on every page load and switch.
            try:
                temporary = self._path.with_suffix('.tmp')
                temporary.write_text(json.dumps({'locale': locale}), encoding='utf8')
                temporary.replace(self._path)
            except OSError:
                logging.warning('Could not cache the native UI language')
        return True

    def tr(self, message, **variables):
        return translate_for_locale(message, self.locale, **variables)

    def state(self, state):
        result = dict(state, locale=self.locale)
        for key in ('message', 'detail', 'error', 'browser'):
            if isinstance(result.get(key), str):
                result[key] = self.tr(result[key])
        return result


def native_page(name, language):
    if name not in ('updates', 'components', 'migration', 'loading'):
        raise ValueError('Unknown native page')
    root = resource_root()
    source = (root / 'desktop/ui' / (name + '.html')).read_text(encoding='utf8')
    boot = json.dumps({'preferences': {'locale': language.locale}, 'english': english_catalog()}, ensure_ascii=True)
    boot = boot.replace('<', '\\u003c')
    runtime = (root / 'frontend/static/js/i18n.js').read_text(encoding='utf8')
    script = '<script id="uiLocaleData" type="application/json">' + boot + '</script><script>' + runtime + '</script>'
    source = source.replace('lang="zh-CN"', 'lang="' + language.locale + '"', 1)
    # Bootstrap before a dialog's own script, after its marked DOM exists.
    return source.replace('<script>', script + '<script>', 1) if '<script>' in source else source.replace('</html>', script + '</html>')
