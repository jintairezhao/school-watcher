"""Native storage disclosures stay discoverable without automatic writes."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_storage_management import StorageManagementTests
from backend.auth.desktop import ensure_local_owner
from backend.database.db import db
from backend.services.localization import stage_preferences
from playwright.sync_api import expect, sync_playwright

DISCLOSURES = ['storageDetails', 'storageBackup', 'storageLocations', 'storageRetention', 'storageAdvanced']
CASES = [(1440, 'zh-CN', 'dark'), (390, 'zh-CN', 'light'), (1440, 'en', 'light'), (390, 'en', 'dark')]
NATIVE = """window.pywebview={api:{
location_state:async()=>({program:'D:/Programs/School Watcher',data:'D:/IsolatedWatcher',
cache:'D:/IsolatedWatcher',backups:'D:/IsolatedWatcher/backups',downloads:'D:/IsolatedWatcher/updates'}),
window_action:async()=>({cancelled:true}),open_location:async()=>true}};"""


def assert_defaults(page):
    expect(page.locator('.storage-page')).to_have_attribute('data-storage-ready', 'true')
    for ident in DISCLOSURES:
        disclosure = page.locator('#' + ident)
        expect(disclosure).to_have_count(1)
        assert not disclosure.evaluate('node => node.open'), ident
        expect(disclosure.locator(':scope > summary')).to_be_visible()
    expect(page.locator('.storage-page button:visible')).to_have_count(1)
    expect(page.locator('[data-cleanup="expired"]')).to_be_visible()
    assert page.locator('.storage-page input:visible').count() == 0
    expect(page.locator('#storagePolicyForm input')).to_have_count(7)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def check():
    output = Path(os.environ.get('WATCHER_STORAGE_LAYOUT_OUTPUT', str(ROOT / '.local/storage-layout-review')))
    output.mkdir(parents=True, exist_ok=True)
    report = []
    with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for width, locale, theme in CASES:
                fixture = StorageManagementTests(); fixture.setUp()
                context = None
                try:
                    fixture.app.config.update(DESKTOP_MODE=True, DESKTOP_FRAMELESS=True,
                        DESKTOP_TOKEN='b' * 64, DESKTOP_ORIGIN='http://localhost')
                    ensure_local_owner()
                    fixture.client.get('/_desktop/open?token=' + 'b' * 64)
                    stage_preferences(fixture.admin.id, {'locale':locale}); db.session.commit()
                    context = browser.new_context(viewport={'width':width, 'height':960}, reduced_motion='reduce')
                    context.add_init_script('localStorage.setItem("theme",' + json.dumps(theme) + ');' + NATIVE)
                    errors, writes = [], []
                    def serve(route):
                        request = route.request; parsed = urlsplit(request.url)
                        assert parsed.hostname == 'localhost', request.url
                        if parsed.path == '/_desktop/components':
                            return route.fulfill(json={'phase':'ready'})
                        if request.method != 'GET':
                            writes.append({'path':parsed.path, 'method':request.method})
                        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                            method=request.method, data=request.post_data,
                            headers={key:value for key,value in request.headers.items()
                                if key.lower() in ('content-type', 'x-csrf-token')})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                    context.route('**/*', serve)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
                    page.goto('http://localhost/admin/storage')
                    expect(page.locator('html')).to_have_attribute('lang', locale)
                    assert_defaults(page)
                    assert not writes, writes
                    label = f'{width}-{locale}-{theme}'
                    if not os.environ.get('SKIP_STORAGE_SCREENSHOTS'):
                        page.screenshot(path=str(output / f'{label}-initial.png'), full_page=True)

                    page.locator('#storageDetails > summary').click()
                    expect(page.locator('.storage-table tbody tr')).to_have_count(5)
                    assert page.locator('[data-storage-kind]').evaluate_all('nodes=>nodes.map(n=>n.dataset.storageKind)') == ['data','cache','logs','backups','application']
                    expect(page.locator('#storageDetails [data-cleanup]')).to_have_count(1)
                    expect(page.locator('[data-cleanup="cache"]')).to_be_visible()
                    expect(page.locator('[data-cleanup="all_cache"]')).to_have_count(0)
                    usage = fixture.client.get('/api/storage').get_json()
                    assert sum(usage['usage_groups'].values()) == usage['total_bytes']
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    if not os.environ.get('SKIP_STORAGE_SCREENSHOTS'):
                        page.screenshot(path=str(output / f'{label}-space.png'), full_page=True)
                    page.locator('#storageDetails > summary').click()

                    # The built-in disclosure responds to the keyboard without a custom widget.
                    summary = page.locator('#storageRetention > summary')
                    summary.focus(); page.keyboard.press('Enter')
                    expect(page.locator('#policy-body_cache_mb')).to_be_hidden()
                    assert page.locator('#storagePolicyForm input:visible').count() == 2
                    cache_summary = page.locator('#storageCacheRules > summary')
                    cache_summary.focus(); page.keyboard.press('Enter')
                    expect(page.locator('#policy-body_cache_mb')).to_be_visible()
                    assert page.locator('#storagePolicyForm input:visible').count() == 7
                    cache_summary.focus(); page.keyboard.press('Space')
                    expect(page.locator('#policy-body_cache_mb')).to_be_hidden()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    if not os.environ.get('SKIP_STORAGE_SCREENSHOTS'):
                        page.screenshot(path=str(output / f'{label}-settings.png'), full_page=True)
                    summary.focus(); page.keyboard.press('Space')
                    expect(page.locator('#policy-body_cache_mb')).to_be_hidden()

                    # A link in one group reveals and focuses its target in another.
                    page.locator('#storageDetails > summary').click()
                    page.locator('#storageDetails a[href="#noticeCleanup"]').click()
                    expect(page.locator('#noticeCleanupSchool')).to_be_visible()
                    page.wait_for_function("""() => {
                        const target = document.getElementById('noticeCleanup');
                        return target === document.activeElement || target.contains(document.activeElement);
                    }""")
                    assert page.locator('#storageAdvanced').evaluate('node => node.open')
                    page.locator('#storageDetails > summary').click()
                    page.locator('#noticeCleanupSchool').select_option(str(fixture.school.id))
                    page.locator('#noticeCleanupBefore').fill('2026-01-01')
                    expect(page.locator('#deleteNoticeCleanup')).to_be_disabled()
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.evaluate('window.scrollTo(0,0)')
                    assert not writes, writes

                    # A direct fragment URL must work even when its group starts closed.
                    page.goto('http://localhost/')
                    page.goto('http://localhost/admin/storage#locations')
                    expect(page.locator('#location-data')).to_be_visible()
                    expect(page.locator('#location-data')).to_have_value('D:/IsolatedWatcher')
                    assert page.locator('#storageLocations').evaluate('node => node.open')
                    assert not page.locator('#storageAdvanced').evaluate('node => node.open')

                    # A normal visit and returning to it both restore the concise default.
                    page.goto('http://localhost/')
                    page.goto('http://localhost/admin/storage')
                    assert_defaults(page)
                    page.locator('#storageRetention > summary').click()
                    expect(page.locator('#policy-body_cache_mb')).to_be_hidden()
                    page.locator('#storageCacheRules > summary').click()
                    expect(page.locator('#policy-body_cache_mb')).to_be_visible()
                    page.goto('http://localhost/')
                    page.go_back()
                    assert_defaults(page)
                    assert not writes, writes
                    assert not errors, errors

                    # Native details remain usable if page scripting cannot run.
                    nojs_context = browser.new_context(viewport={'width':width, 'height':960}, java_script_enabled=False)
                    try:
                        nojs_context.route('**/*', serve)
                        nojs = nojs_context.new_page(); nojs.goto('http://localhost/admin/storage')
                        expect(nojs.locator('#policy-body_cache_mb')).to_be_hidden()
                        nojs.locator('#storageRetention > summary').click()
                        expect(nojs.locator('#policy-body_cache_mb')).to_be_hidden()
                        nojs.locator('#storageCacheRules > summary').click()
                        expect(nojs.locator('#policy-body_cache_mb')).to_be_visible()
                        assert not writes, writes
                    finally:
                        nojs_context.close()
                    report.append({'width':width, 'locale':locale, 'theme':theme, 'default_groups_closed':5,
                        'default_visible_buttons':1, 'hidden_settings_inputs':7,
                        'keyboard_disclosure':True, 'cross_group_notice_link':True,
                        'direct_locations_fragment':True, 'return_restores_default':True,
                        'native_nojs_disclosure':True, 'automatic_writes':writes, 'errors':errors})
                finally:
                    if context: context.close()
                    fixture.tearDown()
        finally:
            browser.close()
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'status':'passed', 'cases':report, 'screenshots':str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    check()
