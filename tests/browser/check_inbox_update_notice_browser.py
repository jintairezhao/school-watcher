"""Refresh prompts must correspond to new rows in the current, filtered page."""
from datetime import datetime
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from playwright.sync_api import expect, sync_playwright
from tests.test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, UserRead


def check():
    fixture = InboxSearchTests()
    fixture.setUp()
    for notice in Announcement.query.all():
        notice.content_html = '<p>Isolated cached article</p>'
        notice.content_cached_at = datetime.utcnow()
    db.session.commit()
    status = {'revision': 1}
    requests, errors, checks = [], [], []

    def progress():
        return {'tracked': True, 'tracking': {'scope': 'all', 'scope_label': '全部订阅'},
            'active': 1, 'total': 2, 'done': 1, 'failed': 0, 'scheduled': 0,
            'sources': [dict(id=11, name='教务处', state='done', new_count=10,
                updated_at=f'2026-11-04T00:00:{status["revision"]:02}', status_label='', message=''),
                dict(id=12, name='学院', state='running', new_count=0,
                updated_at='2026-11-04T00:00:00', status_label='更新中', message='')]}

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                assert parsed.hostname == 'localhost', request.url
                requests.append({'method': request.method, 'path': parsed.path, 'query': parsed.query})
                if parsed.path in ('/api/inbox/refresh', '/api/inbox/sync'):
                    return route.fulfill(json=progress())
                response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=request.method, data=request.post_data_buffer,
                    headers={key: value for key, value in request.headers.items()
                        if key.lower() in ('content-type', 'x-csrf-token', 'x-inbox-fragment')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

            context.route('**/*', serve)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://localhost/?school=1&dept=11&period=week&q=北京&selected=1')
            page.clock.install()
            page.evaluate('''() => {
                const original = window.notifyInboxUpdates;
                window.fixtureChecks = 0;
                window.notifyInboxUpdates = (...args) => {
                    window.fixtureChecks++;
                    return window.fixtureCheck = Promise.resolve(original(...args));
                };
            }''')
            banner = page.locator('#inboxNewNotices')
            initial = page.locator('[data-notice-link]').evaluate_all('rows => rows.map(row => row.dataset.announcementId)')
            reads = UserRead.query.count()
            jobs = BackgroundTask.query.count()

            def poll():
                previous = page.evaluate('window.fixtureChecks')
                status['revision'] += 1
                page.clock.run_for(2100)
                page.wait_for_function('(count) => window.fixtureChecks > count', arg=previous)
                page.evaluate('async () => { await window.fixtureCheck; }')

            poll()
            expect(banner).to_be_hidden()
            checks.append('task metadata changes alone do not announce new notices')

            fixture.add(100, '北京旧通知', days=90)
            fixture.add(101, '北京其他学校通知', school=2, dept=21)
            fixture.add(102, '北京其他栏目通知', dept=12)
            db.session.commit()
            poll()
            expect(banner).to_be_hidden()
            assert page.locator('[data-notice-link]').evaluate_all('rows => rows.map(row => row.dataset.announcementId)') == initial
            assert UserRead.query.count() == reads
            assert BackgroundTask.query.count() == jobs
            checks.append('old notices and out-of-scope schools and columns do not trigger the prompt')

            fixture.add(103, '北京新通知')
            db.session.get(Announcement, 103).published_at = datetime.utcnow()
            db.session.commit()
            poll()
            expect(banner).to_be_visible()
            expect(page.locator('[data-notice-link][data-announcement-id="103"]')).to_have_count(0)
            page.locator('#inboxNewNotices').click()
            expect(page.locator('[data-notice-link][data-announcement-id="103"]')).to_have_count(1)
            expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy', 'true')
            expect(banner).to_be_hidden()
            poll()
            expect(banner).to_be_hidden()
            checks.append('a matching new notice prompts once and refresh displays it without prompting again')

            # A preview arriving after a successful refresh must not revive the prompt.
            page.evaluate('''() => {
                const fetchOriginal = window.fetch;
                window.fetch = async (url, options) => {
                    const response = await fetchOriginal(url, options);
                    if (options?.headers?.['X-Inbox-Fragment'] && !new URL(url, location.href).searchParams.has('selected'))
                        await new Promise(resolve => setTimeout(resolve, 400));
                    return response;
                };
            }''')
            fixture.add(104, '北京稍后到达的新通知')
            db.session.get(Announcement, 104).published_at = datetime.utcnow()
            db.session.commit()
            page.evaluate('void window.notifyInboxUpdates()')
            page.locator('#refreshCurrentSources').click()
            expect(page.locator('[data-notice-link][data-announcement-id="104"]')).to_have_count(1)
            page.clock.run_for(1000)
            page.evaluate('async () => { await window.fixtureCheck; }')
            expect(banner).to_be_hidden()
            assert UserRead.query.count() == reads
            checks.append('a late preview cannot restore a prompt after refresh or change read state')

            page.goto('http://localhost/?school=1&dept=11&period=week&q=北京&read=unread')
            page.clock.install()
            fixture.add(105, '北京已经读过的通知')
            db.session.add(UserRead(user_id=1, announcement_id=105))
            db.session.commit()
            page.evaluate('async () => { await window.notifyInboxUpdates(); }')
            expect(page.locator('#inboxNewNotices')).to_be_hidden()
            checks.append('read-state filters apply to update checks')
            assert not errors, errors
            browser.close()
        result = {'status': 'passed', 'checks': checks, 'browser_errors': errors,
            'actual_official_requests': 0, 'actual_model_requests': 0}
        output = ROOT / '.local/inbox-update-notice-20261104'
        output.mkdir(parents=True, exist_ok=True)
        (output / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    check()
