"""Fresh desktop screens, live discovery transitions and narrow sidebars."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/onboarding-checks/browser-data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/onboarding-checks/browser-data/.env'))
from playwright.sync_api import sync_playwright
from test_desktop_onboarding import FirstSubscriptionTests
from backend.database.db import db
from backend.database.models import BackgroundTask, Department

output = ROOT / '.local/onboarding-checks/screenshots'
output.mkdir(parents=True, exist_ok=True)
fixture = FirstSubscriptionTests()
fixture.setUp()
fixture.fixture.app.config['DESKTOP_FRAMELESS'] = True
results = []
try:
    with patch('backend.services.onboarding_progress.ai_available', return_value=False) as ai_ready, sync_playwright() as p:
        browser = p.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, height in [(1280, 800), (980, 680), (390, 844)]:
            ai_ready.return_value = False
            row = BackgroundTask.query.filter_by(kind='discover').one()
            row.state, row.checkpoint = 'pending', {}
            db.session.commit()
            page = browser.new_page(viewport={'width': width, 'height': height})
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                parsed = urlsplit(route.request.url)
                if parsed.path == '/_desktop/components':
                    return route.fulfill(status=200, json={'phase': 'ready', 'message': '可用'})
                response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=route.request.method, data=route.request.post_data,
                    headers={k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script("localStorage.setItem('theme','dark')")
            page.goto('http://localhost/admin#schools')
            page.locator('.admin-sidebar').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('admin overflow', width)
            assert page.locator('.admin-sidebar').evaluate('(el) => el.scrollWidth <= el.clientWidth'), ('sidebar overflow', width)
            assert '本机应用' not in page.locator('.admin-account').inner_text()
            page.screenshot(path=str(output / f'admin-{width}.png'), full_page=True)
            page.goto(f'http://localhost/subscriptions/{fixture.school.id}')
            page.locator('#discoverySetup').wait_for()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('sources overflow', width)
            assert page.locator('[data-window-action="close"]').count() == 1
            row = BackgroundTask.query.filter_by(kind='discover').one()
            row.state = 'running'
            ai_ready.return_value = True
            row.checkpoint = {'discovery_progress': {'phase': 'ai', 'ai_state': 'running', 'checked_pages': 2, 'pending_pages': 6}}
            db.session.commit()
            page.locator('#discoveryRefresh').click()
            page.wait_for_function("() => document.getElementById('discoveryCounts').textContent.includes('已检查 2 页')")
            assert page.locator('#discoveryProgress').is_visible()
            page.screenshot(path=str(output / f'subscription-{width}.png'), full_page=True)
            if width == 1280:
                department = Department(school_id=fixture.school.id, name='测试学院', list_url='https://www.shu.edu.cn/test/')
                db.session.add(department); db.session.commit()
                page.locator('#discoveryRefresh').click()
                page.locator('[name="department"]').wait_for()
                page.locator('[name="mode"][value="selected"]').check()
                page.locator('[name="department"]').uncheck()
                row.state = 'failed'; db.session.commit()
                page.locator('#discoveryRefresh').click()
                page.wait_for_function("() => document.getElementById('discoveryMessage').textContent.includes('未完成')")
                assert not page.locator('[name="department"]').is_checked(), 'polling overwrote unsaved choices'
                assert not page.locator('#discoveryProgress').is_visible()
                with fixture.client.session_transaction() as session:
                    session['_flashes'] = [('success', '栏目订阅已保存'), ('error', '需要处理的问题')]
                page.goto('http://localhost/admin#scrape')
                page.locator('.flash-success').wait_for()
                page.locator('.flash-success').wait_for(state='hidden', timeout=6500)
                assert page.locator('.flash-error').is_visible(), 'errors must remain readable'
                page.locator('#sinceMonth').fill('2024-06')
                page.get_by_role('button', name='保存设置', exact=True).click()
                page.locator('.toast-success').wait_for()
                page.reload()
                page.wait_for_function("() => document.getElementById('sinceMonth').value === '2024-06'")
                page.screenshot(path=str(output / 'collection-settings.png'), full_page=True)
            assert not errors, errors
            results.append({'width': width, 'overflow': False, 'errors': errors, 'live_progress': True})
            page.close()
        browser.close()
finally:
    fixture.tearDown()
print(json.dumps(results))
