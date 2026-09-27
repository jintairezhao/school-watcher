"""Bounded visual check of the two admin entry changes, using only fixture data."""
import logging
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))
from test_storage_management import StorageManagementTests
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = StorageManagementTests(); fixture.setUp()
    server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    folder = ROOT/'.impeccable/review'; folder.mkdir(parents=True, exist_ok=True)
    try:
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True, chromium_sandbox=True)
            context = browser.new_context(reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for label, size in [('desktop', {'width':1440,'height':1000}), ('mobile', {'width':390,'height':844})]:
                page.set_viewport_size(size)
                page.goto(base+'/admin#scrape')
                expect(page.locator('#interval')).to_be_visible()
                page.locator('#interval').fill('5')
                page.get_by_role('button', name='保存间隔').click()
                page.reload(); expect(page.locator('#interval')).to_have_value('5')
                page.screenshot(path=str(folder/f'runtime-admin-{label}.png'), full_page=True)
                page.get_by_role('link', name='处理访问验证', exact=True).click()
                expect(page.get_by_role('heading', name='处理访问验证')).to_be_visible()
                page.screenshot(path=str(folder/f'runtime-verification-{label}.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert not errors, errors
            browser.close()
        print('Admin navigation, 5-minute setting persistence, and desktop/mobile checks passed.')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3); fixture.tearDown()


if __name__ == '__main__': main()
