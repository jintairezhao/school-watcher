"""Real font delivery and shared reader checks, using an isolated application."""
import json
import logging
import os
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement
from check_apple_ui_browser import seed


def check(capture=True):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check/visual-system'
    output.mkdir(parents=True, exist_ok=True)
    report = {'checks': [], 'fonts': [], 'geometry': [], 'screenshots': [], 'errors': []}
    with tempfile.TemporaryDirectory(prefix='watcher-visual-system-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'visual-fixture',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookie = seed(app)
        with app.app_context():
            article = db.session.get(Announcement, 1)
            article.content_html += '<p style="font: bold 32px SimSun;line-height:4">字体归一 Campus 2026</p><p><font face="SimSun" size="6">中文阅读与英文 English</font></p><p><strong>重要安排</strong>和<em>强调说明</em></p>'
            article.content_html += '<table style="min-width:1100px"><tr>' + '<th>课程与安排</th>' * 12 + '</tr><tr>' + '<td>隔离验收数据</td>' * 12 + '</tr></table>'
            db.session.commit()
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL', 'msedge'), headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                external = []
                def local_only(route):
                    if route.request.url.startswith(base + '/') or route.request.url.startswith('data:'):
                        route.continue_()
                    else:
                        external.append(route.request.url)
                        route.abort()
                context.route('**/*', local_only)
                page = context.new_page()
                page.on('pageerror', lambda error: report['errors'].append(str(error)))
                cdp = context.new_cdp_session(page)
                cdp.send('DOM.enable'); cdp.send('CSS.enable')

                def visit(path):
                    response = page.goto(base + path)
                    if response is not None:
                        assert response.status == 200, (path, response.status)
                    page.evaluate('document.fonts.ready')

                def fonts(selector, label):
                    root = cdp.send('DOM.getDocument')['root']['nodeId']
                    node = cdp.send('DOM.querySelector', {'nodeId': root, 'selector': selector})['nodeId']
                    used = cdp.send('CSS.getPlatformFontsForNode', {'nodeId': node})['fonts']
                    report['fonts'].append({'sample': label, 'used': used})
                    assert used and all(item['isCustomFont'] for item in used), (label, used)

                def inspect(label, take_picture=True):
                    if page.locator('.reading-title').count():
                        expected_size = '24px' if page.viewport_size['width'] < 1024 else '28px'
                        expect(page.locator('.reading-title')).to_have_css('font-size', expected_size)
                    geometry = page.evaluate('''() => ({width:innerWidth,documentWidth:document.documentElement.scrollWidth,
                        titleSize:getComputedStyle(document.querySelector('.reading-title') || document.querySelector('h1')).fontSize,
                        rootSize:getComputedStyle(document.documentElement).fontSize})''')
                    report['geometry'].append({'sample': label, **geometry})
                    assert geometry['documentWidth'] <= geometry['width'] + 1, (label, geometry)
                    if page.locator('.reading-title').count():
                        assert geometry['titleSize'] == ('24px' if geometry['width'] < 1024 else '28px'), (label, geometry)
                    if capture and take_picture:
                        path = output / f'{label}.png'
                        page.screenshot(path=str(path), full_page=True)
                        report['screenshots'].append(str(path))

                for width in [1440, 1200, 1024, 390]:
                    page.set_viewport_size({'width': width, 'height': 960 if width > 390 else 844})
                    for theme in ['light', 'dark']:
                        visit('/?school=1&period=all&selected=1')
                        page.evaluate('(theme) => {localStorage.setItem("theme",theme);document.documentElement.dataset.theme=theme}', theme)
                        inspect(f'{width}-{theme}-reader')
                        body = page.locator('.body-loader-content')
                        assert body.evaluate('(el) => getComputedStyle(el).fontSize') == '17px'
                        assert body.locator('font').evaluate('(el) => getComputedStyle(el).fontSize') == '17px'
                        fonts('.reading-title', f'{width}-{theme}-title')
                        fonts('.body-loader-content font', f'{width}-{theme}-foreign-font')
                        table = page.locator('.reading-table-scroll')
                        expect(table).to_have_attribute('tabindex', '0')
                        assert table.evaluate('(el) => el.scrollWidth > el.clientWidth')
                report['checks'].append('four widths, both themes, actual web fonts, foreign HTML and local table scrolling')

                page.set_viewport_size({'width': 1440, 'height': 960})
                for route, label in [('/announcement/1', 'standalone'), ('/explore?level=subscribed', 'directory'),
                    ('/me', 'account'), ('/me#security', 'security'), ('/admin', 'admin'), ('/admin/storage', 'storage'),
                    ('/admin/sources', 'source-review'), ('/admin/access-verification', 'verification')]:
                    visit(route)
                    fonts('h1', label)
                    inspect(f'1440-{label}', take_picture=label in ['standalone', 'account', 'security', 'admin'])
                report['checks'].append('consistent actual heading fonts across reader, schools, account and all admin shells')

                visit('/?school=1&period=all&selected=1&view=focus')
                inspect('1440-focus')
                # A 720 CSS-pixel viewport exercises 200% zoom's layout on a 1440 display.
                page.set_viewport_size({'width': 720, 'height': 480})
                inspect('200-percent-layout')
                report['checks'].append('focused deep link and 200 percent equivalent CSS viewport')

                context.clear_cookies()
                page.set_viewport_size({'width': 1440, 'height': 960})
                visit('/explore')
                fonts('h1', 'directory-title')
                fonts('#catalogQuery', 'directory-input')
                inspect('1440-directory')
                report['checks'].append('directory composition and control fonts')
                assert not external, external
                report['checks'].append('all fonts and app resources available with external network blocked')

                fallback = browser.new_context(viewport={'width': 390, 'height': 844})
                fallback.route('**/*.woff2', lambda route: route.abort())
                fallback_page = fallback.new_page()
                fallback_page.goto(base + '/explore')
                expect(fallback_page.locator('#catalogQuery')).to_be_visible()
                assert fallback_page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                report['checks'].append('font failure keeps directory text and controls visible')
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()
            (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert not report['errors'], report['errors']
    print(json.dumps({'checks': report['checks'], 'screenshots': len(report['screenshots']), 'errors': report['errors']}, ensure_ascii=False))


if __name__ == '__main__':
    check()
