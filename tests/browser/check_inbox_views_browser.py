"""Reading presentation regression, using disposable data and cached articles only."""
import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
from test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement, UserAnnouncementState
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check(capture=True):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = InboxSearchTests(); fixture.use_file_database = True; fixture.setUp()
    app = fixture.app
    for ident in range(20, 45): fixture.add(ident, f'关于北京校区研究生培养与学生事务安排的通知（{ident}）')
    for announcement in Announcement.query.all():
        announcement.content_html = '<p>隔离界面验收正文，阅读模式切换不重新抓取，也不改变已读状态。</p>' * 60
        announcement.content_text = announcement.content_text or '隔离界面验收正文。'
        announcement.content_cached_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.session.add(UserAnnouncementState(user_id=1, announcement_id=1, starred=True)); db.session.commit()
    cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': 1, '_csrf_token': 'reading-fixture'})
    server = make_server('127.0.0.1', 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    output = ROOT / 'data/ui-check/reading-views'; output.mkdir(parents=True, exist_ok=True)
    checks, errors, requests, screenshots = [], [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
            context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
            context.route('**/api/inbox/refresh**', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
            context.route('**/api/inbox/sync', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda req: requests.append((req.method, req.url)) if req.resource_type in ('fetch', 'xhr') else None)
            workspace = page.locator('.inbox-workspace')
            toggle = page.locator('[data-toggle-reading-view]')

            def go(query='school=1&period=all&selected=1'):
                page.goto(base + '/?' + query)
                expect(workspace).to_be_visible()

            def settled():
                expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy', 'true')

            go()
            width = page.locator('.notice-panel').bounding_box()['width']
            assert 360 <= width <= 460, width
            page.locator('#openFilters').click(); page.locator('#pinSources').click()
            expect(workspace).to_have_attribute('data-source-mode', 'pinned')
            page.locator('#noticeList').evaluate('(e) => e.scrollTop = 420')
            page.locator('.reader-document').evaluate('(e) => e.scrollTop = 360')
            before = len(requests)
            toggle.click()
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            expect(page.locator('.notice-panel')).to_be_hidden()
            expect(page.locator('#sourcePanel')).to_be_hidden()
            expect(toggle).to_contain_text('返回列表')
            assert 'view=focus' in page.url and 'selected=1' in page.url and 'school=1' in page.url
            toggle.click()
            expect(workspace).to_have_attribute('data-source-mode', 'pinned')
            assert page.locator('#noticeList').evaluate('(e) => e.scrollTop') == 420
            assert page.locator('.reader-document').evaluate('(e) => e.scrollTop') == 360
            assert len(requests) == before, requests[before:]
            checks.append('focus entry/exit keeps selection, scroll and pin preference without requests')

            page.go_back(); expect(workspace).to_have_attribute('data-reading-view', 'focus')
            page.go_forward(); expect(workspace).to_have_attribute('data-reading-view', 'split')
            assert len(requests) == before
            checks.append('Back and Forward between presentation modes do not fetch or mark read')

            toggle.click(); page.reload()
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            expect(page.locator('.reading-title')).to_contain_text('北京学术交流')
            page.locator('.reader-source-trigger').click()
            expect(workspace).to_have_attribute('data-source-mode', 'overlay')
            page.keyboard.press('Escape')
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            expect(page.locator('#sourcePanel')).to_be_hidden()
            page.keyboard.press('Escape')
            expect(workspace).to_have_attribute('data-reading-view', 'split')
            expect(workspace).to_have_attribute('data-source-mode', 'pinned')
            checks.append('focus reload works; Escape closes the source drawer before leaving focus')

            go('view=saved&period=all&selected=1')
            toggle.click()
            assert 'view=focus' in page.url and 'mailbox=saved' in page.url
            page.reload(); expect(workspace).to_have_attribute('data-view', 'saved')
            toggle.click(); assert 'view=saved' in page.url and 'mailbox=' not in page.url
            go('view=focus&mailbox=archived&period=all&selected=7')
            expect(workspace).to_have_attribute('data-view', 'inbox')
            expect(page.locator('.reading-title')).to_contain_text('已归档')
            assert 'mailbox=' not in page.url
            expect(page.locator('[data-state-field="archived"]')).to_have_count(0)
            checks.append('favorites keep focus mode; legacy archive links restore the notice in the inbox')

            go(); toggle.click()
            page.locator('.reader-document').evaluate('(e) => e.scrollTop = 320')
            page.evaluate('window.refreshInboxView()'); settled()
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            assert page.locator('.reader-document').evaluate('(e) => e.scrollTop') == 320
            toggle.click()
            page.locator('#inboxQuery').fill('北京')
            page.locator('#inboxQuery').press('Escape')
            page.evaluate('window.refreshInboxView()'); settled()
            expect(page.locator('#inboxQuery')).to_have_value('北京')
            checks.append('fragment refresh preserves reading mode, article scroll and search draft')

            # View changes must not be reverted by a late filter response.
            go()
            page.evaluate('''() => {
                const original = window.fetch;
                window.fetch = async (url, init) => {
                    const response = await original(url, init);
                    if (init?.headers?.['X-Inbox-Fragment']) await new Promise(resolve => setTimeout(resolve, 500));
                    return response;
                };
            }''')
            page.evaluate('void window.refreshInboxView()')
            toggle.click()
            expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            assert 'period=all' in page.url and 'view=focus' in page.url
            toggle.click()
            page.locator('#inboxQuery').fill('上海'); page.locator('#inboxQuery').press('Enter')
            page.go_back()
            expect(workspace).to_have_attribute('data-reading-view', 'focus')
            page.wait_for_timeout(650)
            assert 'q=' not in page.url
            expect(page.locator('.reading-title')).to_contain_text('北京学术交流')
            checks.append('late filter responses retain the latest view; history cancels obsolete pending filters')

            go()
            page.locator('#rowStar1').focus(); page.locator('#rowStar1').click()
            expect(page.locator('#readerStar')).to_have_attribute('aria-pressed', 'false')
            expect(page.locator('#rowStar1')).to_have_attribute('aria-pressed', 'false')
            page.locator('#rowStar3').focus(); page.locator('#rowStar3').click()
            expect(page.locator('#rowStar3')).to_have_attribute('aria-pressed', 'true')
            expect(page.locator('.reading-title')).to_contain_text('北京学术交流')
            assert 'selected=1' in page.url
            page.locator('#rowStar1').focus(); page.locator('#rowStar1').click()
            expect(page.locator('#rowStar1')).to_have_attribute('aria-pressed', 'true')
            go('view=saved&period=all&selected=1')
            page.locator('#rowStar3').focus(); page.locator('#rowStar3').click()
            expect(page.locator('#rowStar3')).to_have_count(0)
            expect(page.locator('.reading-title')).to_contain_text('北京学术交流')
            assert 'selected=1' in page.url
            expect(page.locator('.mailbox-views a')).to_have_count(3)
            expect(page.locator('.mailbox-views').get_by_role('link', name='按发布渠道', exact=True)).to_be_visible()
            checks.append('favorites synchronize and unfavoriting another row preserves the current article')

            for width, theme in ([(1440, 'light'), (1440, 'dark'), (1200, 'light'), (1200, 'dark'), (1024, 'light'), (1024, 'dark'), (390, 'light'), (390, 'dark')] if capture else [(1024, 'light'), (390, 'light')]):
                page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 960})
                go('school=1&period=all' + ('&selected=1' if width >= 1024 else ''))
                page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme)
                if width >= 1024:
                    expect(page.locator('.reader-panel')).to_be_visible()
                    list_width = page.locator('.notice-panel').bounding_box()['width']
                    assert 340 <= list_width <= 460
                    if width >= 1200: assert page.locator('#sourcePanel').bounding_box()['width'] == 300
                else:
                    expect(page.locator('.reader-panel')).to_be_hidden()
                    page.locator('[data-notice-link]').first.click(); settled()
                    expect(page.locator('.reader-panel')).to_be_visible()
                    expect(page.locator('.reader-back')).to_be_visible()
                    assert page.locator('#inboxQuery').evaluate('(e) => getComputedStyle(e).fontSize') == '16px'
                if capture:
                    path = output / f'{width}-{theme}-reading.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
                if width == 390:
                    page.locator('.reader-back').click(); settled()
                    expect(page.locator('.notice-panel')).to_be_visible()
                    expect(page.locator('.reader-panel')).to_be_hidden()
                    if capture:
                        path = output / f'{width}-{theme}-list.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
            checks.append('1440/1200/1024/390 layouts fit; mobile has separate list and reader with source access')
            if capture:
                page.set_viewport_size({'width': 1440, 'height': 960})
                go(); toggle.click()
                for theme in ('light', 'dark'):
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    path = output / f'1440-{theme}-focus.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
            assert not errors, errors
            report = {'checks': checks, 'browser_errors': errors, 'screenshots': screenshots}
            (output / ('report.json' if capture else 'confirmation.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
            print(json.dumps(report, ensure_ascii=False))
            context.close(); browser.close()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5); fixture.tearDown()


if __name__ == '__main__': check()
