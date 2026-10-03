"""Window controls remain operable while the source drawer makes app content inert."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/window-checks/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/window-checks/data/.env'))
from playwright.sync_api import sync_playwright, expect
from tests.test_page_recovery import SchoolNavigationTests


def check():
    fixture = SchoolNavigationTests(); fixture.setUp()
    fixture.app.config['DESKTOP_FRAMELESS'] = True
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
            page = browser.new_page(viewport={'width': 1280, 'height': 850})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                parsed = urlsplit(route.request.url)
                if parsed.hostname != 'localhost':
                    route.abort(); return
                path = parsed.path + ('?' + parsed.query if parsed.query else '')
                response = fixture.client.get(path, headers={
                    key: value for key, value in route.request.headers.items()
                    if key.lower() == 'x-inbox-fragment'})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script('''window.windowActions = [];
                window.pywebview = {api: {window_action: async name => {
                    window.windowActions.push(name); return true;
                }}};''')
            page.goto(f'http://localhost/?school={fixture.school.id}')
            page.locator('#openFilters').click()
            expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode', 'overlay')
            for action in ('minimize', 'maximize', 'maximize', 'close'):
                page.locator(f'[data-window-action="{action}"]').click(timeout=1500)
            assert page.evaluate('window.windowActions') == ['minimize', 'maximize', 'maximize', 'close']
            assert page.locator('.desktop-drag').evaluate('el => !el.closest("[inert]")')
            # Normal app navigation stays unavailable behind the modal drawer.
            assert page.locator('.nav-links').evaluate('el => !!el.closest("[inert]")')
            assert page.locator('.notice-panel').evaluate('el => !!el.closest("[inert]")')
            page.locator('#schoolFilter').select_option('')
            expect(page.locator('#sourcePanel')).to_have_attribute('data-source-scope', f'{fixture.user.id}:0')
            page.locator('[data-window-action="minimize"]').click()
            page.keyboard.press('Tab')
            expect(page.locator('#closeFilters')).to_be_focused()
            page.locator('#desktopChrome summary').click()
            page.keyboard.press('Escape')
            expect(page.locator('#desktopChrome details')).not_to_have_attribute('open', '')
            expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode', 'overlay')
            page.locator('#pinSources').focus()
            page.keyboard.press('Shift+Tab')
            expect(page.locator('[data-close-sources]')).to_be_focused()
            page.keyboard.press('Tab')
            expect(page.locator('#pinSources')).to_be_focused()
            page.keyboard.press('Escape')
            expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode', 'closed')
            assert page.locator('.nav-links').evaluate('el => !el.closest("[inert]")')
            # Switching to the pinned panel must not leave native controls inert.
            page.locator('#openFilters').click()
            page.locator('#pinSources').click()
            expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode', 'pinned')
            page.locator('[data-window-action="minimize"]').click()
            page.locator('#closeFilters').click()
            assert not errors, errors
            browser.close()
            print(json.dumps({'drawer_native_controls': True, 'background_inert': True,
                              'fragment_navigation': True, 'menu_escape': True,
                              'focus_trap': True, 'pinning': True}))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    check()
