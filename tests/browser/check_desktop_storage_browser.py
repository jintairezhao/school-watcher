"""Desktop storage/navigation UI with an isolated backend and a fake native picker."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_storage_management import StorageManagementTests
from backend.auth.desktop import ensure_local_owner

fixture = StorageManagementTests(); fixture.setUp()
fixture.app.config.update(DESKTOP_MODE=True, DESKTOP_FRAMELESS=True, DESKTOP_TOKEN='a'*64, DESKTOP_ORIGIN='http://localhost')
ensure_local_owner()
fixture.client.get('/_desktop/open?token=' + 'a'*64)
output = ROOT / '.local/onboarding-checks/desktop-storage-review'; output.mkdir(parents=True, exist_ok=True)
try:
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'), headless=True)
        for width in (1280, 980, 760):
            page = browser.new_page(viewport={'width':width,'height':850})
            errors=[]; page.on('pageerror',lambda e:errors.append(str(e)))
            def serve(route):
                parsed=urlsplit(route.request.url)
                if parsed.path=='/_desktop/components': return route.fulfill(json={'phase':'ready'})
                response=fixture.client.open(parsed.path+('?' + parsed.query if parsed.query else ''), method=route.request.method,
                    data=route.request.post_data, headers={k:v for k,v in route.request.headers.items() if k.lower() in ('content-type','x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*',serve)
            page.add_init_script("""localStorage.setItem('theme','dark');window.pywebview={api:{
                location_state:async()=>({program:'D:/Programs/School Watcher',data:'D:/SchoolWatcher',cache:'D:/SchoolWatcher',backups:'D:/SchoolWatcher/backups',downloads:'D:/SchoolWatcher/updates'}),
                choose_location:async()=> 'E:/SchoolWatcher', change_locations:async(values)=>{window.chosen=values;return {cancelled:true}},
                window_action:async()=>true, open_location:async()=>true, reinstall:async()=>({cancelled:true})}};""")
            page.goto('http://localhost/admin/storage')
            expect(page.locator('#location-data')).to_have_value('D:/SchoolWatcher')
            page.locator('[data-location-change=data]').click()
            expect(page.locator('#location-backups')).to_have_value('E:/SchoolWatcher/backups')
            page.locator('#saveLocations').click()
            expect(page.locator('#locationStatus')).to_have_text('已取消')
            assert page.evaluate("window.chosen.data === 'E:/SchoolWatcher'")
            page.locator('#resetLocations').click()
            expect(page.locator('#location-data')).to_have_value('D:/SchoolWatcher')
            page.locator('#policy-body_cache_mb').fill('1024')
            page.locator('#policy-backup_keep_count').fill('250')
            page.locator('#policy-scrape_log_retention_days').fill('12')
            page.get_by_role('button',name='保存保留规则').click()
            expect(page.locator('#policyStatus')).to_have_text('保留规则已保存')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate('window.scrollTo(0,0)')
            page.screenshot(path=str(output/f'storage-{width}.png'),full_page=True)
            page.goto('http://localhost/')
            assert page.locator('#desktopChrome').count()==1
            rect=page.locator('.navbar').bounding_box()
            assert rect['y']==0 and rect['height']==56,rect
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output/f'inbox-{width}.png'))
            assert not errors,errors
            page.close()
        browser.close()
    print(json.dumps({'desktop_storage':'passed','widths':[1280,980,760]}))
finally: fixture.tearDown()
