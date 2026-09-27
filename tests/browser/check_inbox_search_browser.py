"""Live search and history checks against an isolated app; no external requests."""
import json
import logging
import os
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
from test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement, Subscription, UserRead
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check(capture=True):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = InboxSearchTests()
    fixture.use_file_database = True  # Concurrent HTTP requests need separate connections.
    fixture.setUp()
    app = fixture.app
    # Opening a preview must not start a content worker in this UI fixture.
    for announcement in Announcement.query.all():
        announcement.content_html = '<p>隔离搜索界面验收正文。</p>'
        announcement.content_text = announcement.content_text or '隔离搜索界面验收正文。'
        announcement.content_cached_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.add(Subscription(user_id=2, school_id=1)); db.session.commit()
    cookies = [app.session_interface.get_signing_serializer(app).dumps({'user_id': i, '_csrf_token': 'search-fixture'}) for i in (1, 2)]
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    output = ROOT / 'data/ui-check/inbox-search'; output.mkdir(parents=True, exist_ok=True)
    checks, errors, requests, screenshots = [], [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
            context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            context.route('**/api/inbox/refresh**', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
            context.route('**/api/inbox/sync', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
            page = context.new_page(); page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('response', lambda response: errors.append(f'HTTP {response.status}: {response.url}')
                    if response.status >= 500 and '/search-suggestions' not in response.url else None)
            page.on('request', lambda req: requests.append(req.url) if '/api/inbox/search-suggestions' in req.url else None)
            field = page.locator('#inboxQuery'); panel = page.locator('#inboxSearchPanel')
            choices = page.locator('#inboxSearchOptions [role="option"]')

            def go(query='school=1&period=all'):
                page.goto(base + '/?' + query)
                expect(field).to_be_visible()

            def settled():
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy', 'true')

            go(); field.click()
            expect(panel).to_be_visible()
            expect(page.locator('#inboxSearchStatus')).to_contain_text('暂无搜索记录')
            field.fill('北京')
            expect(choices).to_have_count(5)
            expect(page.locator('#inboxSearchOptions mark').first).to_have_text('北京')
            expect(panel).to_contain_text('北京大学报名截止时间')
            assert 'q=' not in page.url
            assert UserRead.query.count() == 1
            choices.first.click(); settled()
            assert 'selected=1' in page.url
            expect(panel).to_be_hidden()
            checks.append('substring suggestions and safe highlights appear before Enter; previews do not mark read')

            field.click(); field.fill('')
            expect(choices).to_have_count(1); expect(choices.first).to_have_text('北京')
            choices.first.click(); settled()
            assert 'selected=' not in page.url
            field.fill('上海'); field.press('Enter'); settled()
            expect(panel).to_be_hidden()
            page.reload(); field.click(); field.fill('')
            expect(choices).to_have_count(2); expect(choices.first).to_have_text('上海')
            field.press('ArrowDown'); field.press('ArrowDown'); field.press('Enter'); settled()
            expect(field).to_have_value('北京'); expect(panel).to_be_hidden()
            field.click(); field.fill(''); field.press('Escape')
            expect(panel).to_be_hidden(); expect(field).to_be_focused()
            field.click(); expect(panel).to_be_visible()
            checks.append('recent queries persist, deduplicate, reorder and support keyboard reuse and Escape')

            # Background list refreshes retain an unfinished search and the user's caret.
            field.fill('北京')
            expect(choices).to_have_count(5)
            page.evaluate('window.refreshInboxView()'); settled()
            expect(field).to_have_value('北京'); expect(field).to_be_focused()
            expect(choices).to_have_count(5)
            checks.append('fragment refresh preserves draft search, focus and suggestions')

            field.fill(''); count = len(requests)
            field.evaluate("e => e.dispatchEvent(new CompositionEvent('compositionstart', {bubbles:true}))")
            field.evaluate("e => { e.value='北'; e.dispatchEvent(new InputEvent('input', {bubbles:true,isComposing:true})); }")
            page.wait_for_timeout(400)
            assert len(requests) == count
            field.evaluate("e => { e.value='北京'; e.dispatchEvent(new CompositionEvent('compositionend', {bubbles:true})); }")
            expect(choices).to_have_count(5)
            checks.append('Chinese IME waits for composition to finish')

            page.evaluate('''() => {
                const original = window.fetch;
                window.fetch = async (url, init) => {
                    const response = await original(url, init);
                    if (String(url).includes('search-suggestions') && new URL(url, location.origin).searchParams.get('q') === '北')
                        await new Promise(resolve => setTimeout(resolve, 700));
                    return response;
                };
            }''')
            with page.expect_response('**/api/inbox/search-suggestions?**'):
                field.fill('北')
            field.fill('上海'); expect(choices).to_have_count(1)
            expect(choices.first).to_contain_text('上海校区')
            page.wait_for_timeout(800)
            expect(choices.first).to_contain_text('上海校区')
            checks.append('late suggestions never overwrite newer input')

            page.route('**/api/inbox/search-suggestions?**', lambda route: route.fulfill(status=503, body='offline'))
            field.fill('北京'); expect(page.locator('#inboxSearchStatus')).to_contain_text('可以按回车搜索')
            field.press('Enter'); settled(); expect(field).to_have_value('北京')
            page.unroute('**/api/inbox/search-suggestions?**')
            go('school=1&dept=12&period=all')
            field.click(); field.fill('北京'); expect(choices).to_have_count(1)
            expect(choices.first).to_contain_text('北京学院')
            field.fill('不存在的关键词'); expect(page.locator('#inboxSearchStatus')).to_contain_text('没有匹配')
            checks.append('network failure leaves Enter usable; source scope and empty matches remain clear')

            # User strings remain text, even if previously saved by a different client version.
            page.evaluate("localStorage.setItem('inbox-search-history:1', JSON.stringify(['<img src=x onerror=alert(1)>', '北京']))")
            go(); field.click()
            expect(choices.first).to_have_text('<img src=x onerror=alert(1)>')
            expect(panel.locator('img')).to_have_count(0)
            page.locator('#clearInboxSearchHistory').click()
            expect(page.locator('#inboxSearchStatus')).to_contain_text('暂无搜索记录')
            expect(field).to_be_focused()
            checks.append('history can be cleared and untrusted strings cannot inject HTML')

            for number in range(12):
                field.fill('查询' + str(number)); field.press('Enter'); settled()
            field.fill(''); expect(choices).to_have_count(10)
            expect(choices.first).to_have_text('查询11')
            context.add_cookies([{'name': 'session', 'value': cookies[1], 'url': base}])
            go(); field.click(); expect(choices).to_have_count(0)
            context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}])
            go(); field.click(); expect(choices).to_have_count(10)
            checks.append('history is capped at ten and isolated by account')

            # One visual pass for both themes on desktop and mobile, with no list displacement.
            for width, theme in ([(1440, 'light'), (1440, 'dark'), (390, 'light'), (390, 'dark')] if capture else []):
                page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 960})
                go(); page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                top = page.locator('#noticeList').bounding_box()['y']
                field.click(); field.fill('北京'); expect(choices).to_have_count(5)
                assert abs(page.locator('#noticeList').bounding_box()['y'] - top) <= 1
                rect = panel.bounding_box()
                assert rect['x'] >= 0 and rect['x'] + rect['width'] <= width + 1
                assert rect['y'] + rect['height'] <= page.viewport_size['height']
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                path = output / f'{width}-{theme}-matches.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
                field.fill('')
                if theme == 'light':
                    path = output / f'{width}-history.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
            page.locator('.toolbar-topline p').click(); expect(panel).to_be_hidden()
            if capture:
                checks.append('desktop/mobile light/dark popovers fit without shifting the notice list')
            assert not errors, errors
            report = {'checks': checks, 'browser_errors': errors, 'screenshots': screenshots}
            (output / ('report.json' if capture else 'confirmation.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps(report, ensure_ascii=False))
            context.close(); browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); fixture.tearDown()


if __name__ == '__main__': check()
