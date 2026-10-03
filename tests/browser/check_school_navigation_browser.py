"""Reproduce school navigation, fragment retry, empty states and error recovery."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/navigation-checks/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/navigation-checks/data/.env'))
from playwright.sync_api import sync_playwright, expect
from flask import request as flask_request
from werkzeug.serving import make_server
from tests.test_page_recovery import SchoolNavigationTests

output = ROOT / '.local/navigation-checks/screenshots'
output.mkdir(parents=True, exist_ok=True)
results = []
logging.getLogger('werkzeug').setLevel(logging.ERROR)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
    for width, theme in ((1280, 'dark'), (1280, 'light'), (390, 'dark'), (390, 'light')):
        fixture = SchoolNavigationTests(); fixture.setUp()
        @fixture.app.before_request
        def fail_requested_page():
            if flask_request.args.get('test_failure') == '1':
                raise RuntimeError('isolated navigation failure')
        # This fixture uses one in-memory SQLite connection; serve it serially.
        server = make_server('127.0.0.1', 0, fixture.app)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            page = browser.new_page(viewport={'width': width, 'height': 850}, reduced_motion='reduce')
            with fixture.client.session_transaction() as session:
                session['_csrf_token'] = 'isolated-navigation-test'
                cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(dict(session))
            page.context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            errors, external, navigation = [], [], []
            state = {'fail_fragment': False}
            page.on('pageerror', lambda error: errors.append(error.stack))
            page.on('response', lambda response: navigation.append({'url': response.url, 'status': response.status})
                    if response.request.is_navigation_request() else None)
            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.netloc != urlsplit(base).netloc:
                    external.append(parsed.hostname); route.abort(); return
                headers = {k: v for k, v in request.headers.items()
                           if k.lower() in ('content-type', 'x-csrf-token', 'x-inbox-fragment', 'x-directory-fragment')}
                if headers.get('x-inbox-fragment') == '1' and state['fail_fragment']:
                    state['fail_fragment'] = False
                    route.fulfill(status=500, body='Temporary failure'); return
                # Let Chromium follow real HTTP redirects. A fulfilled redirect
                # can bypass Playwright's handler for its next request on CI.
                route.continue_()
            page.route('**/*', serve)
            page.add_init_script('try { localStorage.setItem("theme", ' + json.dumps(theme) + '); } catch (_) {}')
            school_id = fixture.school.id
            page.goto(f'{base}/subscriptions/{school_id}')
            expect(page.get_by_role('heading', name='东南大学', exact=True)).to_be_visible()
            page.get_by_role('link', name='本校通知', exact=True).click()
            try:
                expect(page.locator('.inbox-workspace')).to_be_visible()
            except AssertionError:
                print(json.dumps({'url': page.url, 'navigation': navigation, 'errors': errors,
                                  'body': page.locator('body').inner_text()[:4000]}, ensure_ascii=True), flush=True)
                raise
            expect(page.get_by_role('heading', name='本校尚未收录通知')).to_be_visible()
            page.reload()
            expect(page.locator('.inbox-workspace')).to_be_visible()
            expect(page.locator('a.nav-brand')).to_have_count(0)
            expect(page.locator('.nav-links a').filter(has_text='收件箱')).to_have_count(1)
            page.screenshot(path=str(output / f'empty-{width}-{theme}.png'))
            page.locator('.source-settings-link').click()
            expect(page.get_by_role('heading', name='东南大学', exact=True)).to_be_visible()

            page.goto(base + '/')
            page.locator('#openFilters').click()
            state['fail_fragment'] = True
            page.locator('#schoolFilter').select_option(str(school_id))
            expect(page.locator('#sourcePanelStatus')).to_contain_text('筛选未更新')
            expect(page.locator('.inbox-workspace')).to_be_visible()
            page.locator('[data-retry-source]').click()
            expect(page.locator('#schoolFilter')).to_have_value(str(school_id))
            expect(page.locator('.source-panel .source-school-name')).to_have_text('东南大学')
            group = page.locator('.source-group-toggle').filter(has_text='招生就业')
            if group.get_attribute('aria-expanded') != 'true':
                group.click()
            expect(page.locator(f'[data-department][value="{fixture.column.id}"]')).to_be_enabled()
            page.get_by_role('link', name='自动找通知与来源设置', exact=True).click()
            expect(page.get_by_role('heading', name='东南大学', exact=True)).to_be_visible()
            expect(page.locator(f'input[name="department"][value="{fixture.unit.id}"]')).to_have_count(1)

            with fixture.assertLogs(fixture.app.logger, level='ERROR'):
                response = page.goto(base + '/?test_failure=1')
            assert response.status == 500
            expect(page.get_by_role('heading', name='页面暂时无法打开')).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'error-{width}-{theme}.png'))
            page.get_by_role('link', name='返回学校目录', exact=True).click()
            expect(page.locator('#directoryResults')).to_be_visible()
            response = page.goto(base + '/missing-page')
            assert response.status == 404
            expect(page.get_by_role('heading', name='页面不存在')).to_be_visible()
            page.get_by_role('link', name='返回收件箱', exact=True).click()
            expect(page.locator('.inbox-workspace')).to_be_visible()
            assert not errors and not external, (errors, external)
            results.append({'width': width, 'theme': theme, 'school_navigation': True,
                            'fragment_retry': True, 'error_recovery': True})
            page.close()
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=5)
            fixture.tearDown()
    browser.close()
print(json.dumps(results))
