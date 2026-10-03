"""Exercise themed deletion, keyboard cancellation, recovery and actual API cleanup."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/deletion-checks/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/deletion-checks/data/.env'))
from playwright.sync_api import sync_playwright, expect
from tests.test_school_deletion import SchoolDeletionTests
from backend.database.db import db
from backend.database.models import School

output = ROOT / '.local/deletion-checks/screenshots'
output.mkdir(parents=True, exist_ok=True)
results = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
    for width, theme in ((1280, 'dark'), (1280, 'light'), (390, 'dark'), (390, 'light')):
        fixture = SchoolDeletionTests(); fixture.setUp()
        try:
            school_id, other_id = fixture.prepare()
            name = '测试大学（教育与学生发展学院）'
            fixture.school.name = name; db.session.commit()
            page = browser.new_page(viewport={'width': width, 'height': 850}, reduced_motion='reduce')
            errors, native, deletes, pending = [], [], [], []
            state = {'hold': True}
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: (native.append(dialog.type), dialog.dismiss()))
            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if request.method == 'DELETE':
                    deletes.append(parsed.path)
                    if state['hold']:
                        pending.append(route)
                        return
                response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=request.method, data=request.post_data,
                    headers={k: v for k, v in request.headers.items() if k.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
            page.goto('http://localhost/admin#schools')
            button = page.locator('.school-manage-list .manage-card').filter(has=page.get_by_role('heading', name=name, exact=True)).get_by_role('button', name='删除', exact=True)
            dialog = page.get_by_role('dialog', name='删除学校？')
            button.click()
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role('button', name='取消', exact=True)).to_be_focused()
            assert not deletes and not native
            page.keyboard.press('Escape')
            expect(dialog).not_to_be_visible()
            expect(button).to_be_focused()
            button.click()
            page.keyboard.press('Shift+Tab')
            expect(dialog.get_by_role('button', name='删除学校', exact=True)).to_be_focused()
            page.keyboard.press('Tab')
            expect(dialog.get_by_role('button', name='取消', exact=True)).to_be_focused()
            assert dialog.evaluate('(el) => el.scrollWidth <= el.clientWidth')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'confirmation-{width}-{theme}.png'))
            dialog.get_by_role('button', name='取消', exact=True).click()
            assert not deletes
            button.click()
            dialog.get_by_role('button', name='删除学校', exact=True).click()
            expect(dialog.get_by_role('button', name='正在删除…', exact=True)).to_be_disabled()
            page.keyboard.press('Escape')
            expect(dialog).to_be_visible()
            expect(dialog.get_by_role('button', name='取消', exact=True)).to_be_disabled()
            assert len(pending) == len(deletes) == 1
            pending.pop().fulfill(status=500, content_type='text/html', body='<html>Internal error</html>')
            expect(dialog.get_by_role('alert')).to_have_text('删除未完成，请稍后重试。')
            assert db.session.get(School, school_id)
            assert page.locator('#deleteDialogError').evaluate('(el) => el.scrollWidth <= el.clientWidth')
            if width == 1280 and theme == 'dark':
                page.screenshot(path=str(output / 'failure-dark.png'))
            state['hold'] = False
            dialog.get_by_role('button', name='重试删除', exact=True).click()
            expect(dialog).not_to_be_visible()
            assert len(deletes) == 2
            # Browser callbacks use their own request session; query the database
            # instead of the fixture's cached ORM object after the request.
            assert db.session.query(School.id).filter_by(id=school_id).scalar() is None
            assert db.session.query(School.id).filter_by(id=other_id).scalar() == other_id
            assert not native and not errors, (native, errors)
            results.append({'width': width, 'theme': theme, 'cancel': True, 'retry': True, 'deleted': True})
            page.close()
        finally:
            fixture.tearDown()
    browser.close()
print(json.dumps(results))
