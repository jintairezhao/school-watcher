"""Choose a department, exclude a column, and save the intended subscription."""
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_school_structure import SchoolStructureTests, CS, LANG, HTML
from backend.database.db import db
from backend.database.models import Department, Subscription
from backend.services.directory_options import expand_directory_ids


fixture = SchoolStructureTests(); fixture.setUp()
try:
    fixture.publish(); fixture.connect(CS + 'notices/'); fixture.connect(LANG + 'notices/')
    cs = Department.query.filter_by(kind='unit', name='计算机学院').one()
    lang = Department.query.filter_by(kind='unit', name='外国语学院').one()
    notice = Department.query.filter_by(kind='column', list_url=CS + 'notices/').one()
    # The browser exercises parent/child selection with a second observed column.
    from backend.scraper.discovery.inventory_crawler import inspect_page
    address = CS + 'news/'
    fixture.pages[CS] = fixture.pages[CS].replace('</nav>', '<a href="/news/">学院新闻</a></nav>')
    for url, label, kind, html in [(CS, cs.name, 'unit', fixture.pages[CS]),
            (address, '学院新闻', 'channel', HTML.replace('通知公告', '学院新闻'))]:
        fixture.inventory.enqueue(fixture.key, url, label, kind, 1, [], 'school_domain')
        inspect_page(fixture.inventory, fixture.inventory.report(fixture.key)['site'],
            fixture.inventory.get_page(fixture.key, url), fetcher=lambda _: {'url': url, 'html': html, 'status': 200})
    fixture.catalog.publish(fixture.inventory, fixture.key)
    from backend.services.source_onboarding import onboard_page
    onboard_page({'school_id': fixture.school.id, 'url': address}, fetcher=lambda url, purpose:
        HTML.replace('通知公告', '学院新闻') if url == address else fixture.fixture.fetch(url, purpose))
    news = Department.query.filter_by(kind='column', list_url=address).one()
    client = fixture.fixture.app.test_client()
    with client.session_transaction() as session:
        session['user_id'] = Subscription.query.one().user_id; session['_csrf_token'] = 'token'
    output = ROOT / '.local/onboarding-checks/structure'; output.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, theme in [(1280, 'dark'), (390, 'light')]:
            sub = Subscription.query.one(); sub.department_ids = None; db.session.commit()
            page = browser.new_page(viewport={'width': width, 'height': 950})
            errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
            def serve(route):
                parsed = urlsplit(route.request.url)
                response = client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=route.request.method, data=route.request.post_data,
                    headers={k: v for k, v in route.request.headers.items() if k.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
            page.route('**/*', serve)
            page.add_init_script(f"try {{ localStorage.setItem('theme','{theme}'); }} catch {{}}")
            page.goto(f'http://localhost/subscriptions/{fixture.school.id}')
            page.locator('[name="mode"][value="selected"]').check()
            page.locator('#sourceClearSelection').click()
            page.locator(f'[name="department"][value="{cs.id}"]').check()
            expect(page.locator(f'[name="department"][value="{notice.id}"]')).to_be_checked()
            expect(page.locator(f'[name="department"][value="{news.id}"]')).to_be_checked()
            expect(page.locator(f'[name="department"][value="{lang.id}"]')).not_to_be_checked()
            page.locator(f'[name="department"][value="{notice.id}"]').uncheck()
            expect(page.locator(f'[name="department"][value="{cs.id}"]')).not_to_be_checked()
            expect(page.locator(f'[name="department"][value="{news.id}"]')).to_be_checked()
            page.locator('#discoveryRefresh').click()
            expect(page.locator(f'[name="department"][value="{notice.id}"]')).not_to_be_checked()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), ('overflow', width)
            page.screenshot(path=str(output / f'subscription-tree-{width}.png'), full_page=True)
            page.locator('.source-save button').click()
            page.wait_for_url('**/?school=*')
            db.session.expire_all()
            saved = expand_directory_ids(fixture.school.id, Subscription.query.one().department_ids)
            assert news.id in saved and notice.id not in saved and lang.id not in saved, saved
            assert not errors, errors
            print(f'{width}: department selection, column exclusion, refresh and save passed', flush=True)
            page.close()
        browser.close()
finally:
    fixture.tearDown()
