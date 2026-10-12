"""Clipboard integration on real reader surfaces, using only isolated fixtures."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
from datetime import datetime

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_inbox_search_suggestions import InboxSearchTests
from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.services.announcement_sources import record_source
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = InboxSearchTests(); fixture.use_file_database = True; fixture.setUp()
    server = None
    try:
        first, second = db.session.get(Announcement, 1), db.session.get(Announcement, 2)
        first.title = '2026—2027学年本科生各类企业奖学金获奖名单（校内公示）'
        first.url = 'https://fixture.example.edu.cn/info/1.htm?from=student&lang=zh#正文'
        second.title = '关于 <img src=x onerror="window.sharePwned=true"> 的通知'
        second.url = 'https://fixture.example.edu.cn/info/2.htm'
        third = db.session.get(Announcement, 3); third.url = 'javascript:alert(1)'
        for ann in Announcement.query.all():
            ann.content_html = '<p>隔离分享功能验证正文。</p>'
            ann.content_text = '隔离分享功能验证正文。'; ann.content_cached_at = datetime.utcnow()
        secondary = db.session.get(Department, 12)
        secondary_url = 'https://college.example.edu.cn/notice/award-copy.htm'
        record_source(first, secondary, article_url=secondary_url)
        db.session.commit()
        first_text, second_text = first.title + '\n' + first.url, second.title + '\n' + second.url
        source_text = first.title + '\n' + secondary_url
        app = fixture.app
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': 1, '_csrf_token': 'share-fixture'})
        output = ROOT / '.local/share-notice-check'; output.mkdir(parents=True, exist_ok=True)
        checks, errors = [], []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))
            context = browser.new_context(reduced_motion='reduce')
            context.grant_permissions(['clipboard-read', 'clipboard-write'], origin=base)
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
            context.route('**/api/inbox/sync', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
            context.route('**/api/inbox/refresh**', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
            page = context.new_page(); requests = []
            page.on('pageerror', lambda e: errors.append(str(e)))
            page.on('request', lambda r: requests.append(r.url))

            def clipboard(expected):
                page.wait_for_function('(text) => navigator.clipboard.readText().then(value => value === text)', arg=expected)
                expect(page.locator('.toast-success').last).to_have_text('标题和官网链接已复制')

            for width, theme in ((1440, 'light'), (390, 'dark')):
                page.set_viewport_size({'width': width, 'height': 950})
                page.goto(base + '/?school=1&period=all&selected=1')
                page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                share = page.get_by_role('button', name='分享', exact=True)
                expect(share).to_be_visible(); expect(share).to_be_enabled()
                before = list(requests); share.click(); clipboard(first_text)
                assert requests == before, 'Sharing must not send data or call AI'
                page.screenshot(path=str(output / f'reader-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                # The new button must also work after AJAX replaces the reader.
                page.goto(base + '/?school=1&period=all')
                page.locator('[data-notice-link][data-announcement-id="2"]').click()
                share.click(); clipboard(second_text)
                assert page.evaluate('window.sharePwned') is None
                page.goto(base + '/?school=1&dept=12&period=all&selected=1&view=focus')
                share.click(); clipboard(source_text)
                page.goto(base + '/announcement/1?source=12')
                share.click(); clipboard(source_text)
                page.goto(base + '/?school=1&period=all&selected=3')
                expect(share).to_be_disabled()
                expect(share).to_have_attribute('data-share-url', '')
                checks.append({'width': width, 'theme': theme, 'clipboard_exact': True,
                    'ajax_reader': True, 'current_publication': True, 'standalone': True,
                    'unusable_url_disabled': True, 'no_horizontal_overflow': True})

            page.goto(base + '/?school=1&period=all&selected=1')
            share = page.get_by_role('button', name='分享', exact=True)
            # Real legacy copy command after a rejected modern write; focus and
            # an existing DOM selection must survive the temporary textarea.
            page.evaluate('''() => {
                window.writeText = navigator.clipboard.writeText.bind(navigator.clipboard);
                navigator.clipboard.writeText = () => Promise.reject(new DOMException('Denied', 'NotAllowedError'));
                const selection = getSelection(), range = document.createRange();
                range.selectNodeContents(document.querySelector('.reading-title'));
                selection.removeAllRanges(); selection.addRange(range);
                window.priorSelection = selection.toString();
            }''')
            share.focus(); share.press('Enter'); clipboard(first_text)
            expect(share).to_be_focused()
            assert page.evaluate('getSelection().toString() === window.priorSelection')
            expect(page.locator('textarea[aria-label="待复制的通知"]')).to_have_count(0)
            # Failed copy must not claim success or leave the button locked.
            page.evaluate('''() => {
                window.execCopy = document.execCommand.bind(document);
                document.execCommand = () => false;
                document.querySelectorAll('.toast').forEach(toast => toast.remove());
            }''')
            share.click()
            expect(page.locator('.toast-error').last).to_contain_text('复制未成功')
            expect(page.locator('.toast-success')).to_have_count(0)
            expect(share).not_to_have_attribute('aria-busy', 'true')
            page.evaluate('''() => {
                document.execCommand = window.execCopy;
                navigator.clipboard.writeText = window.writeText;
            }''')
            share.click(); clipboard(first_text)
            assert not errors, errors
            checks.append({'real_legacy_clipboard': True, 'keyboard_focus_and_selection': True,
                'honest_failure_and_retry': True, 'no_page_errors': True})
            browser.close()
        report = {'checks': checks, 'page_errors': errors, 'actual_model_requests': 0, 'actual_official_requests': 0}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
        print(json.dumps(report))
    finally:
        if server:
            server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    check()
