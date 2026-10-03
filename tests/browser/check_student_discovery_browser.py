"""Check automatic school onboarding without requiring source choices or reviews."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/student-discovery-checks/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/student-discovery-checks/data/.env'))

from playwright.sync_api import expect, sync_playwright
from test_page_recovery import SchoolNavigationTests
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Subscription
from backend.database.student_information_models import StudentAssessment
from backend.services import student_information

output = ROOT / '.local/student-discovery-checks/screenshots'
output.mkdir(parents=True, exist_ok=True)
results = []

binding = {'id': 1, 'version': 1}
with patch('backend.services.onboarding_progress.ai_available', return_value=True), \
        patch('backend.services.student_information._enabled_binding', return_value=binding), \
        patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Reads must not invoke AI')), \
        sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
    for width, theme in ((1280, 'light'), (390, 'dark')):
        fixture = SchoolNavigationTests()
        fixture.setUp()
        try:
            fixture.column.list_selector = '.notices li'
            task = BackgroundTask(identity=f'discover:{fixture.school.id}', kind='discover',
                state='running', payload={'school_id': fixture.school.id},
                lease_until=datetime.utcnow() + timedelta(minutes=5))
            db.session.add(task)
            db.session.commit()
            page = browser.new_page(viewport={'width': width, 'height': 850}, reduced_motion='reduce')
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname != 'localhost':
                    external.append(parsed.hostname)
                    route.abort()
                    return
                headers = {key: value for key, value in request.headers.items()
                           if key.lower() in ('content-type', 'x-csrf-token', 'x-inbox-fragment')}
                path = parsed.path + ('?' + parsed.query if parsed.query else '')
                response = fixture.client.open(path, method=request.method, data=request.post_data, headers=headers)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

            page.route('**/*', serve)
            page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
            page.goto(f'http://localhost/subscriptions/{fixture.school.id}')
            expect(page.get_by_role('link', name='自动找通知', exact=True)).to_be_visible()
            expect(page.locator('#discoveryNotices')).to_be_visible()
            expect(page.locator('#sourcePreferences')).not_to_have_attribute('open', '')
            expect(page.locator('#discoveryCounts')).to_contain_text('已收录 0 条通知')
            expect(page.locator('#discoveryProgress')).to_be_visible()
            assert '待核实' not in page.locator('main').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'automatic-{width}-{theme}.png'), full_page=True)

            # A result becomes readable while discovery is still running.
            announcement = Announcement(school_id=fixture.school.id, department_id=fixture.column.id,
                title='本科生奖学金申请通知', url='https://yzb.seu.edu.cn/student-scholarship.htm',
                published_at=datetime.utcnow())
            db.session.add(announcement)
            db.session.commit()
            reason = '可准备申请材料 <img src=x onerror="window.unescapedValue=true">'
            db.session.add(StudentAssessment(subject_key=f'listing:{announcement.id}',
                school_id=fixture.school.id, announcement_id=announcement.id,
                input_hash=student_information.material('listing', announcement.id)[2], state='ready',
                result={'value': 'relevant', 'historical': 'possible', 'facts': [],
                        'reason': reason, 'basis': 'title', '_binding': binding}))
            db.session.commit()
            ai_kinds = ('student_assessment', 'summary', 'navigation_review', 'source_grouping', 'onboard', 'discover')
            ai_task_count = BackgroundTask.query.filter(BackgroundTask.kind.in_(ai_kinds)).count()
            page.locator('.discovery-details > summary').click()
            page.locator('#discoveryRefresh').click()
            expect(page.locator('#discoveryCounts')).to_contain_text('已收录 1 条通知')
            page.locator('#discoveryNotices').click()
            expect(page.locator('.notice-row')).to_contain_text('本科生奖学金申请通知')
            expect(page.locator('.notice-value')).to_contain_text('按标题判断')
            expect(page.locator('.notice-value')).to_contain_text(reason)
            expect(page.locator('.notice-value img')).to_have_count(0)
            assert not page.evaluate('Boolean(window.unescapedValue)')
            assert BackgroundTask.query.filter(BackgroundTask.kind.in_(ai_kinds)).count() == ai_task_count, 'Reading queued AI work'

            # Explicit existing scope stays intact, including unsaved changes during polling.
            subscription = Subscription.query.filter_by(school_id=fixture.school.id).one()
            subscription.department_ids = [fixture.column.id]
            db.session.commit()
            page.goto(f'http://localhost/subscriptions/{fixture.school.id}')
            expect(page.locator('#sourcePreferences')).to_have_attribute('open', '')
            selected = page.locator(f'[name="department"][value="{fixture.column.id}"]')
            expect(selected).to_be_checked()
            selected.uncheck()
            page.locator('.discovery-details > summary').click()
            page.locator('#discoveryRefresh').click()
            expect(selected).not_to_be_checked()
            task.state = 'done'
            db.session.commit()
            page.locator('#discoveryRefresh').click()
            expect(page.locator('#discoveryRetry')).to_be_visible()
            expect(page.locator('#discoveryProgress')).to_be_hidden()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'preferences-{width}-{theme}.png'), full_page=True)
            assert not errors and not external, (errors, external)
            results.append({'width': width, 'theme': theme, 'automatic_receiving': True,
                'live_notice_count': True, 'subset_preserved': True, 'errors': errors})
            page.close()
        finally:
            fixture.tearDown()
    browser.close()

print(json.dumps(results))
