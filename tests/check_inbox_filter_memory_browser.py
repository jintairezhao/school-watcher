"""Filter inheritance across source changes and fresh notification entry points."""
import json
import logging
import os
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from check_inbox_source_menu_browser import seed
from backend import create_app
from backend.database.db import db
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    checks, failures, errors = [], [], []
    with tempfile.TemporaryDirectory(prefix='inbox-filter-memory-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'filter-memory-test',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'test.db')})
        cookies = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel=os.environ.get('PLAYWRIGHT_CHANNEL', 'msedge'))
                context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}])
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
                for pattern in ('**/api/inbox/refresh**', '**/api/inbox/sync'):
                    context.route(pattern, lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
                page = context.new_page()
                page.on('pageerror', lambda e: errors.append(str(e)))

                def settled():
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy', 'true')

                def params(**expected):
                    page.wait_for_function('(want) => Object.entries(want).every(([k,v]) => new URL(location.href).searchParams.get(k) === v)', arg=expected, timeout=5000)

                def choose_filters():
                    page.goto(base + '/?school=1&period=week&read=all')
                    page.locator('#toggleNoticeFilters').click()
                    page.locator('[data-period="all"]').click(); settled()
                    page.locator('[data-read="unread"]').click(); settled()
                    page.keyboard.press('Escape')

                def open_sources():
                    if not page.locator('#sourcePanel').is_visible():
                        page.locator('#openFilters').click()

                def within_page():
                    choose_filters(); open_sources()
                    page.locator('#schoolFilter').select_option('2'); settled()
                    page.locator('.source-group-toggle').click()
                    page.locator('.department-option').click(); settled()
                    params(school='2', dept='999', period='all', read='unread')
                    page.locator('#clearDepartments').click(); settled()
                    params(period='all', read='unread')
                    page.locator('#schoolFilter').select_option('1'); settled()
                    page.locator('[data-source-group="院系设置"] .source-group-toggle').click()
                    page.locator('[data-source-group="院系设置"] [data-source-group-filter]').click(); settled()
                    params(period='all', read='unread', group='院系设置')
                    page.locator('[data-source-unit="200"] [data-unit-select]').check(); settled()
                    assert set(parse_qs(urlsplit(page.url).query)['dept']) == {'200', '201', '202'}
                    params(period='all', read='unread')
                    page.locator('#schoolFilter').select_option('2'); settled()
                    assert 'dept' not in parse_qs(urlsplit(page.url).query)
                    page.keyboard.press('Escape')
                    expect(page.locator('#activeFilterSummary')).to_contain_text('全部时间')
                    expect(page.locator('#activeFilterSummary')).to_contain_text('未读')

                def absent_year():
                    year = str(datetime.now().year)
                    page.goto(base + f'/?school=1&period=archive&year={year}&month=9&read=unread')
                    open_sources(); page.locator('#schoolFilter').select_option('2'); settled()
                    expect(page.locator('#yearFilter')).to_have_value(year)
                    page.keyboard.press('Escape'); page.locator('#toggleNoticeFilters').click()
                    page.locator('#monthFilter').select_option('10'); settled()
                    params(period='archive', year=year, month='10', read='unread')
                    expect(page.locator('#activeFilterSummary')).to_contain_text(year + ' 年 10 月')

                def fresh_entry():
                    choose_filters()
                    page.goto(base + '/explore')
                    page.goto(base + '/?school=2&dept=999')
                    params(school='2', dept='999', period='all', read='unread')
                    expect(page.locator('#activeFilterSummary')).to_contain_text('全部时间')
                    page.reload(); params(period='all', read='unread')
                    page.go_back()
                    expect(page).to_have_url(base + '/explore')

                def override_reset_account():
                    choose_filters()
                    page.goto(base + '/?school=2&period=week&read=read')
                    expect(page.locator('[data-period="week"]')).to_have_attribute('aria-pressed', 'true')
                    expect(page.locator('[data-read="read"]')).to_have_attribute('aria-pressed', 'true')
                    page.goto(base + '/?school=1'); params(period='week', read='read')
                    page.locator('#toggleNoticeFilters').click()
                    page.locator('#resetNoticeConditions').click(); settled()
                    page.goto(base + '/?school=2')
                    expect(page.locator('[data-period="week"]')).to_have_attribute('aria-pressed', 'true')
                    expect(page.locator('[data-read="all"]')).to_have_attribute('aria-pressed', 'true')
                    choose_filters()
                    context.add_cookies([{'name': 'session', 'value': cookies[1], 'url': base}])
                    page.goto(base + '/?school=2')
                    expect(page.locator('[data-period="week"]')).to_have_attribute('aria-pressed', 'true')
                    expect(page.locator('[data-read="all"]')).to_have_attribute('aria-pressed', 'true')
                    context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}])
                    page.goto(base + '/?school=2'); params(period='all', read='unread')
                    page.locator('#toggleNoticeActions').click()
                    page.locator('.clear-filters').click(); settled()
                    page.goto(base + '/?school=1')
                    expect(page.locator('[data-period="week"]')).to_have_attribute('aria-pressed', 'true')
                    expect(page.locator('[data-read="all"]')).to_have_attribute('aria-pressed', 'true')

                def failed_filter():
                    choose_filters()
                    def fail(route):
                        if route.request.headers.get('x-inbox-fragment') == '1':
                            route.fulfill(status=503, body='offline')
                        else:
                            route.continue_()
                    page.route(base + '/?**', fail)
                    page.locator('#toggleNoticeFilters').click()
                    page.locator('[data-period="week"]').click()
                    expect(page.locator('#retryInboxFilter')).to_be_visible()
                    page.unroute(base + '/?**', fail)
                    page.goto(base + '/?school=2'); params(period='all', read='unread')

                def browser_history():
                    choose_filters()
                    page.locator('#toggleNoticeFilters').click()
                    page.locator('[data-period="week"]').click(); settled()
                    page.go_back(); settled(); params(period='all', read='unread')
                    page.go_forward(); settled(); params(period='week', read='unread')
                    page.goto(base + '/?school=2'); params(period='week', read='unread')

                def mailbox_and_late_response():
                    choose_filters()
                    for label in ('我的收藏', '已归档', '收件箱'):
                        page.locator('.mailbox-views a').filter(has_text=label).click(); settled()
                        params(period='all', read='unread')
                    page.evaluate('''() => {
                        const original = window.fetch; let first = true;
                        window.filterDelayedFinished = false;
                        window.fetch = async (url, init) => {
                            const delay = first && init?.headers?.['X-Inbox-Fragment'];
                            if (delay) first = false;
                            const response = await original(url, init);
                            if (delay) {
                                await new Promise(resolve => setTimeout(resolve, 500));
                                window.filterDelayedFinished = true;
                            }
                            return response;
                        };
                    }''')
                    page.locator('#toggleNoticeFilters').click()
                    with page.expect_response(lambda r: r.request.headers.get('x-inbox-fragment') == '1'):
                        page.locator('[data-period="week"]').click()
                    page.locator('[data-period="all"]').click(); settled()
                    page.wait_for_function('() => window.filterDelayedFinished')
                    params(period='all', read='unread')
                    page.goto(base + '/?school=2'); params(period='all', read='unread')

                def narrow_and_storage_failure():
                    choose_filters(); page.set_viewport_size({'width': 390, 'height': 844})
                    open_sources(); page.locator('#schoolFilter').select_option('2'); settled()
                    page.keyboard.press('Escape'); params(period='all', read='unread')
                    page.evaluate("localStorage.setItem('inbox-conditions:1', '{broken')")
                    page.goto(base + '/?school=1')
                    expect(page.locator('[data-period="week"]')).to_have_attribute('aria-pressed', 'true')
                    # Storage restrictions must not prevent normal filtering/navigation.
                    context.add_init_script("Storage.prototype.getItem = Storage.prototype.setItem = () => { throw new Error('storage disabled'); };")
                    page.goto(base + '/?school=1&period=all&read=unread')
                    open_sources(); page.locator('#schoolFilter').select_option('2'); settled()
                    params(period='all', read='unread')

                for name, test in [('source changes and group/multiselect', within_page),
                                   ('selected year absent from next school', absent_year),
                                   ('fresh school/department entry and reload', fresh_entry),
                                   ('explicit URL, reset and account isolation', override_reset_account),
                                   ('failed filter does not overwrite preference', failed_filter),
                                   ('Back/Forward are authoritative', browser_history),
                                   ('mailboxes and late responses preserve conditions', mailbox_and_late_response),
                                   ('mobile and unavailable browser storage', narrow_and_storage_failure)]:
                    try:
                        test(); checks.append(name)
                    except Exception as error:
                        failures.append({'case': name, 'error': str(error)})
                context.close(); browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()
    report = {'checks': checks, 'failures': failures, 'browser_errors': errors}
    output = ROOT / 'data/ui-check/filter-memory'
    output.mkdir(parents=True, exist_ok=True)
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert not failures and not errors, 'See data/ui-check/filter-memory/report.json'


if __name__ == '__main__':
    check()
