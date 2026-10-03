"""Explicit analysis and manual change-check controls on desktop and mobile."""
from datetime import datetime
from pathlib import Path
import os
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_school_structure import SchoolStructureTests, CS
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Subscription
from backend.database.student_information_models import StudentAssessment
from backend.services.student_information import material

fixture = SchoolStructureTests(); fixture.setUp()
try:
    fixture.publish(); fixture.connect(CS + 'notices/')
    ann = Announcement.query.first()
    quote = '本科生申请截止2024年9月30日，需提交成绩单。'
    ann.content_text = quote; ann.content_html = '<p>' + quote + '</p>'
    ann.content_cached_at = datetime.utcnow(); db.session.commit()
    ann_id, school_id = ann.id, ann.school_id
    client = fixture.fixture.app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = Subscription.query.one().user_id; session['_csrf_token'] = 'token'
    output = ROOT / '.local/onboarding-checks/change-discovery'; output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, theme in [(1280, 'dark'), (390, 'light')]:
            StudentAssessment.query.delete(); BackgroundTask.query.delete(); db.session.commit()
            page = browser.new_page(viewport={'width': width, 'height': 950})
            errors, paid_requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                parsed = urlsplit(route.request.url)
                with patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}):
                    response = client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                        method=route.request.method, data=route.request.post_data,
                        headers={k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token')})
                if parsed.path.endswith('/student-information') and route.request.method == 'POST':
                    paid_requests.append(parsed.path)
                    assert response.status_code == 202
                    with fixture.fixture.app.app_context():
                        db.session.add(StudentAssessment(subject_key='article:' + str(ann_id), school_id=school_id,
                            announcement_id=ann_id, input_hash=material('article', ann_id)[2], state='ready', result={
                                'value': 'relevant', 'historical': 'high', 'facts': [
                                    {'kind': 'deadline', 'text': '往年申请截止', 'quote': quote, 'evidence_id': 'p0'}]}))
                        db.session.commit()
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script(f"localStorage.setItem('theme','{theme}')")
            address = f'http://localhost/?school={school_id}&period=all&selected={ann_id}&view=focus'
            page.goto(address)
            expect(page.get_by_role('button', name='分析用途与时间')).to_be_visible()
            assert not paid_requests
            assert BackgroundTask.query.filter_by(kind='student_assessment').count() == 0
            page.get_by_role('button', name='分析用途与时间').click()
            expect(page.locator('.student-insight')).to_contain_text('可作历史参考', timeout=10000)
            assert len(paid_requests) == 1
            page.reload()
            expect(page.locator('.student-insight')).to_contain_text('可作历史参考')
            assert len(paid_requests) == 1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'explicit-analysis-{width}.png'), full_page=True)
            BackgroundTask.query.delete(); db.session.commit()
            page.goto(f'http://localhost/subscriptions/{school_id}')
            expect(page.get_by_role('button', name='再找一次', exact=True)).to_be_visible()
            expect(page.locator('.discovery-status')).to_contain_text('无需逐个确认部门或栏目')
            page.get_by_role('button', name='再找一次', exact=True).click()
            expect(page.locator('#discoveryRetry')).to_be_hidden()
            job = BackgroundTask.query.filter_by(kind='discover').one()
            assert job.payload['trigger'] == 'manual_changes' and job.payload['refresh']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'manual-check-{width}.png'), full_page=True)
            assert not errors, errors
            print(f'{width}: explicit AI, cached reuse, manual change check and layout passed', flush=True)
            page.close()
        browser.close()
finally:
    fixture.tearDown()
