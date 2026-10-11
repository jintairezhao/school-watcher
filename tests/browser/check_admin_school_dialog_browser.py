"""Click real school controls against isolated HTML; intercept every write and AI call."""
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

with tempfile.TemporaryDirectory(prefix='school-dialog-browser-') as scratch:
    os.environ['WATCHER_DATA_DIR'] = scratch
    os.environ['WATCHER_ENV_FILE'] = str(Path(scratch) / '.env')
    from playwright.sync_api import expect, sync_playwright
    from test_desktop_local import DesktopLocalTests
    from backend.database.db import db
    from backend.database.models import BackgroundTask, School

    output = ROOT / '.local/admin-school-dialog-checks'
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('AI is forbidden in this browser check')), sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, theme in ((1280, 'dark'), (390, 'light')):
            fixture = DesktopLocalTests()
            fixture.setUp()
            try:
                fixture.open()
                existing = School(name='隔离按钮测试学校', url='https://fixture.example.edu.cn/', enabled=False)
                db.session.add(existing)
                db.session.commit()
                initial_school_count = School.query.count()
                initial_tasks = BackgroundTask.query.count()
                page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
                errors, external, writes = [], [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                state = {'reject_save': True}

                def serve(route):
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.hostname != 'localhost':
                        external.append(parsed.hostname)
                        route.abort()
                        return
                    if request.method not in ('GET', 'HEAD'):
                        assert request.method in ('POST', 'PUT') and parsed.path.startswith('/api/schools')
                        writes.append({'method': request.method, 'path': parsed.path,
                                       'payload': request.post_data_json,
                                       'csrf_present': bool(request.headers.get('x-csrf-token'))})
                        route.fulfill(status=400 if state['reject_save'] else 200,
                                      json={'error': '学校网址暂不可用'} if state['reject_save'] else {'success': True})
                        return
                    if parsed.path == '/api/schools/suggest-url':
                        route.fulfill(status=200, json={'found': False})
                        return
                    path = parsed.path + ('?' + parsed.query if parsed.query else '')
                    response = fixture.client.get(path)
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

                page.route('**/*', serve)
                page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
                page.goto('http://localhost/admin#schools')
                add = page.get_by_role('button', name='添加学校', exact=True)
                expect(add).to_be_visible()
                add.click()
                assert not errors, 'Clicking 添加学校 raised: ' + '; '.join(errors)
                dialog = page.locator('#schoolModal')
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role('heading', name='添加学校', exact=True)).to_be_visible()
                expect(page.locator('#schoolName')).to_have_value('')
                expect(page.locator('#schoolUrl')).to_have_value('')
                expect(page.locator('#schoolEnabled')).to_be_checked()
                expect(page.locator('#schoolName')).to_be_focused()
                bounds = dialog.bounding_box()
                assert bounds and bounds['x'] >= 15 and bounds['y'] >= 15, bounds
                assert bounds['x'] + bounds['width'] <= width - 15, bounds
                assert abs(bounds['x'] * 2 + bounds['width'] - width) <= 2, bounds
                dialog.get_by_role('button', name='取消', exact=True).click()
                expect(dialog).to_be_hidden()
                expect(add).to_be_focused()
                assert writes == []

                add.click()
                page.locator('#schoolName').fill('本轮隔离新增学校')
                page.locator('#schoolUrl').fill('https://new.example.edu.cn/')
                page.locator('#schoolEnabled').uncheck()
                dialog.get_by_role('button', name='保存', exact=True).click()
                expect(page.locator('.toast-error')).to_contain_text('学校网址暂不可用')
                expect(dialog).to_be_visible()
                assert writes[-1] == {'method': 'POST', 'path': '/api/schools',
                    'payload': {'name': '本轮隔离新增学校', 'url': 'https://new.example.edu.cn/', 'enabled': False},
                    'csrf_present': True}
                page.keyboard.press('Escape')
                expect(dialog).to_be_hidden()

                page.locator(f'[data-school-id="{existing.id}"]').click()
                expect(dialog).to_be_visible()
                expect(dialog.get_by_role('heading', name='编辑学校', exact=True)).to_be_visible()
                expect(page.locator('#schoolName')).to_have_value(existing.name)
                expect(page.locator('#schoolUrl')).to_have_value(existing.url)
                expect(page.locator('#schoolEnabled')).not_to_be_checked()
                dialog.get_by_role('button', name='保存', exact=True).click()
                expect(page.locator('.toast-error').last).to_contain_text('学校网址暂不可用')
                assert writes[-1] == {'method': 'PUT', 'path': f'/api/schools/{existing.id}',
                    'payload': {'name': existing.name, 'url': existing.url, 'enabled': False},
                    'csrf_present': True}
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.toast').evaluate_all('(items) => items.forEach(item => item.remove())')
                page.screenshot(path=str(output / f'edit-{width}-{theme}.png'), full_page=True)
                dialog.get_by_role('button', name='取消', exact=True).click()

                # Reopening Add must clear the edited ID and prior values.
                add.click()
                expect(page.locator('#editSchoolId')).to_have_value('')
                expect(page.locator('#schoolName')).to_have_value('')
                expect(page.locator('#schoolEnabled')).to_be_checked()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / f'add-{width}-{theme}.png'), full_page=True)
                page.locator('#schoolName').fill('已拦截保存的测试学校')
                page.locator('#schoolUrl').fill('https://saved.example.edu.cn/')
                state['reject_save'] = False
                dialog.get_by_role('button', name='保存', exact=True).click()
                expect(dialog).to_be_hidden()
                assert writes[-1]['method'] == 'POST' and writes[-1]['csrf_present']
                assert School.query.count() == initial_school_count
                assert BackgroundTask.query.count() == initial_tasks
                assert not errors and not external, (errors, external)
                results.append({'width': width, 'theme': theme, 'add_and_edit_open': True,
                                'cancel_and_escape': True, 'save_payload_and_error': True,
                                'database_unchanged': True})
                page.close()
            finally:
                fixture.tearDown()
        browser.close()
    print(json.dumps(results))
