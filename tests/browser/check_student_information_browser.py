"""Read expired but useful information and explain an unconnected department."""
from datetime import datetime
from pathlib import Path
import os
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_school_structure import SchoolStructureTests, CS
from backend.database.db import db
from backend.database.models import Announcement, Subscription
from backend.database.student_information_models import StudentAssessment
from backend.services.student_information import material


fixture = SchoolStructureTests(); fixture.setUp()
try:
    fixture.publish(); fixture.connect(CS + 'notices/')
    ann = Announcement.query.first()
    quote = '本科生申请截止2024年9月30日，需提交成绩单与申请材料。'
    ann.content_text = quote; ann.content_html = '<p>' + quote + '</p>'
    ann.content_cached_at = datetime.utcnow()
    db.session.commit()
    fingerprint = material('article', ann.id)[2]
    db.session.add(StudentAssessment(subject_key='article:' + str(ann.id), school_id=ann.school_id,
        announcement_id=ann.id, input_hash=fingerprint, state='ready', result={
            'value': 'relevant', 'historical': 'high', 'eligibility': 'unknown', 'policy_validity': 'unknown',
            'facts': [{'kind': 'deadline', 'text': '申请截止2024年9月30日', 'quote': quote, 'evidence_id': 'p0'}]}))
    db.session.commit()
    client = fixture.fixture.app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = Subscription.query.one().user_id; session['_csrf_token'] = 'token'
    output = ROOT / '.local/onboarding-checks/student-value'; output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, theme in [(1280, 'dark'), (390, 'light')]:
            page = browser.new_page(viewport={'width': width, 'height': 950})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                parsed = urlsplit(route.request.url)
                response = client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=route.request.method, data=route.request.post_data,
                    headers={k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script(f"localStorage.setItem('theme','{theme}')")
            page.goto(f'http://localhost/?school={fixture.school.id}&period=all&selected={ann.id}&view=focus')
            panel = page.locator('.student-insight')
            expect(panel).to_be_visible()
            expect(panel).to_contain_text('可作历史参考')
            expect(panel).to_contain_text('已到截止时间')
            panel.locator('summary').click()
            expect(panel.locator('blockquote')).to_contain_text(quote)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('overflow', width)
            page.screenshot(path=str(output / f'historical-reader-{width}.png'), full_page=True)
            panel.get_by_role('link', name='查看已收录的历年通知').click()
            expect(page).to_have_url(f'http://localhost/?school={fixture.school.id}&period=all')
            page.locator('#openFilters').click()
            group = page.locator('.source-group-toggle').filter(has_text='院系设置')
            if group.get_attribute('aria-expanded') != 'true':
                group.click()
            unit = page.locator('[data-source-unit-name="外国语学院"]')
            unit.locator('.source-unit-toggle').click()
            expect(unit.locator('.source-unit-empty')).to_be_visible()
            expect(unit).to_contain_text('栏目待接入')
            expect(unit).not_to_contain_text('0 个单位')
            page.screenshot(path=str(output / f'pending-department-{width}.png'), full_page=True)
            assert not errors, errors
            print(f'{width}: historical value, quotes, history link and empty department passed', flush=True)
            page.close()
        browser.close()
finally:
    fixture.tearDown()
