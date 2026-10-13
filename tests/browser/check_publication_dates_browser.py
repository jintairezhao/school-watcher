"""Verified publication dates survive list moves, AJAX reading and navigation."""
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_inbox_search_suggestions import InboxSearchTests
from test_publication_dates import ARTICLE, TITLE, URL
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, UserAnnouncementState, UserRead
from backend.services.content_cache import fetch_content
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = InboxSearchTests(); fixture.use_file_database = True; fixture.setUp()
    server = None
    try:
        first, second = db.session.get(Announcement, 1), db.session.get(Announcement, 2)
        first.title = TITLE; first.url = URL; first.published_at = datetime(2026, 10, 16)
        first.content_html = ARTICLE; first.content_cached_at = datetime.utcnow()
        second.title = TITLE + '第二场'; second.url = URL.replace('123.html', '124.html')
        second.published_at = datetime(2026, 10, 15); second.content_html = None
        second.content_cached_at = None
        second_html = ARTICLE.replace(TITLE, second.title).replace('09月30日', '09月29日')
        read_at = datetime(2026, 10, 12, 3, 0)
        db.session.add(UserRead(user_id=1, announcement_id=1, read_at=read_at))
        db.session.add_all([UserAnnouncementState(user_id=1, announcement_id=i, starred=True) for i in (1, 2)])
        for i in range(40):
            db.session.add(Announcement(school_id=1, department_id=11, title=f'近期其他通知{i}',
                url=f'https://example.test/recent/{i}', published_at=datetime(2026, 10, 10)))
        db.session.commit()
        app = fixture.app
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': 1, '_csrf_token': 'date-fixture'})
        output = ROOT / '.local/publication-date-check'; output.mkdir(parents=True, exist_ok=True)
        checks, errors = [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))
            context = browser.new_context(reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
            context.route('**/api/inbox/sync', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
            context.route('**/api/inbox/refresh**', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
            page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
            page.goto(base + '/?school=1&dept=11&period=all&selected=2')
            expect(page.locator('[data-content-id="2"]')).to_have_attribute('data-load-state', 'loading')
            expect(page.locator('.reading-meta time')).to_have_text('2026年10月15日')
            # Run the actual body task against controlled HTML; browser polling,
            # response serialization and the list refresh all remain real.
            with patch('backend.scraper.engine._fetch_html', return_value=second_html) as fetch:
                fetch_content(2)
            assert fetch.call_count == 1
            expect(page.locator('.reading-meta time')).to_have_text('2026年09月29日', timeout=15000)
            expect(page.locator('[data-content-id="2"]')).to_have_attribute('data-cached', 'true')
            expect(page.locator('[data-notice-link][data-announcement-id="2"]')).to_have_count(0)
            assert page.url == base + '/?school=1&dept=11&period=all&selected=2'
            expect(page.locator('.reading-meta time')).to_have_attribute('datetime', '2026-09-29')
            checks.append({'actual_body_task_updates_ajax_reader': True, 'date_reorders_list': True,
                           'offpage_selected_reader_and_filters_preserved': True})
            for width, theme in ((1440, 'dark'), (390, 'light')):
                page.set_viewport_size({'width': width, 'height': 950})
                address = base + '/?school=1&dept=11&period=all&selected=1'
                page.goto(address)
                page.evaluate('(value) => document.documentElement.dataset.theme = value', theme)
                expect(page.locator('.reading-title')).to_have_text(TITLE)
                expect(page.locator('.reading-meta time')).to_have_text('2026年09月30日')
                expect(page.locator('.reading-meta time')).to_have_attribute('datetime', '2026-09-30')
                expect(page.locator('[data-notice-link][data-announcement-id="1"]')).to_have_count(0)
                page.screenshot(path=str(output / f'corrected-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.reload()
                expect(page.locator('.reading-meta time')).to_have_text('2026年09月30日')
                page.goto(base + '/announcement/1')
                expect(page.locator('.reading-meta time')).to_have_text('2026年09月30日')
                checks.append({'width': width, 'theme': theme, 'cached_correction': True,
                               'reload_and_standalone_correct': True, 'no_invented_midnight': True})
            assert not errors, errors
            browser.close()
        db.session.expire_all()
        assert db.session.get(Announcement, 1).content_html == ARTICLE
        assert UserRead.query.filter_by(user_id=1, announcement_id=1).one().read_at == read_at
        assert UserAnnouncementState.query.filter_by(user_id=1, starred=True).count() == 2
        assert BackgroundTask.query.filter_by(kind='publication_date').count() == 2
        report = {'checks': checks, 'read_and_favorite_state_preserved': True,
                  'saved_body_unchanged': True, 'page_errors': errors,
                  'actual_model_requests': 0, 'actual_official_requests': 0}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
        print(json.dumps(report))
    finally:
        if server: server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__': check()
