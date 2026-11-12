"""Inspect actual private admission/resume responses at narrow and wide widths."""
from contextlib import ExitStack
from datetime import datetime
import json
import faulthandler
import os
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / '.local/refresh-accounting-20261110'
OUT.mkdir(exist_ok=True)
os.environ.update(WATCHER_ENV_FILE=str(OUT/'no-env'), WATCHER_DATA_DIR=str(OUT/'browser-private'),
    WATCHER_SEED_ON_START='0', WATCHER_BROWSER='0',
    TEMP=str(ROOT/'.local/t-picker-1108'), TMP=str(ROOT/'.local/t-picker-1108'))
sys.path[:0] = [str(ROOT), str(ROOT/'tests')]
from playwright.sync_api import sync_playwright, expect
from test_refresh_scope_accounting import RefreshScopeAccountingTests
from backend.database.db import db
from backend.database.models import BackgroundTask

results = []
faulthandler.dump_traceback_later(25)
print('Starting private browser review.', flush=True)
with ExitStack() as guards, sync_playwright() as pw:
    guards.enter_context(patch('backend.ai.providers.complete', side_effect=AssertionError('No AI')))
    guards.enter_context(patch('backend.scraper.engine._fetch_html', side_effect=AssertionError('No websites')))
    browser = pw.chromium.launch(channel='msedge', headless=True, timeout=15000)
    print('Browser launched.', flush=True)
    try:
        for width, locale in [(390, 'zh-CN'), (1440, 'en')]:
            print(f'Checking {width} {locale}.', flush=True)
            case = RefreshScopeAccountingTests()
            case.setUp()
            try:
                f = case.fixture
                case.client = f.client_as(f.other.id)
                f.a.list_selector = f.b.list_selector = 'li'
                db.session.commit()
                case.recent(f.a, outcome='waiting')
                case.client.set_cookie('watcher_locale', locale)
                page = browser.new_page(viewport={'width':width, 'height':920}, reduced_motion='reduce')
                errors, external = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    request = route.request
                    url = urlsplit(request.url)
                    if url.hostname != 'localhost':
                        external.append(url.hostname)
                        route.abort()
                        return
                    response = case.client.open(url.path + ('?'+url.query if url.query else ''),
                        method=request.method, data=request.post_data_buffer,
                        headers={k:v for k,v in request.headers.items()
                                 if k.lower() in ('content-type', 'x-csrf-token', 'x-inbox-fragment')})
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                page.route('**/*', serve)
                page.goto(f'http://localhost/?school={f.school.id}', timeout=15000)
                expected = '1 个未参加本轮' if locale == 'zh-CN' else '1 not included in this batch'
                expect(page.locator('#inboxRefreshStatus')).to_contain_text(expected)
                page.locator('#inboxRefreshSummary').click()
                expect(page.locator('#inboxRefreshScope')).to_contain_text(
                    '已启用 2 个，本轮处理其中 1 个' if locale == 'zh-CN'
                    else '2 enabled sources, 1 included in this batch')
                expect(page.locator('#inboxRefreshScope')).to_contain_text(
                    '待识别入口' if locale == 'zh-CN' else 'entrances awaiting identification')
                page.locator('#inboxRefreshOutsideSummary').click()
                expect(page.locator('#inboxRefreshOutsideSources')).to_contain_text(
                    '等待重试' if locale == 'zh-CN' else 'Awaiting retry')
                expect(page.locator('#inboxRefreshOutsideSources')).to_contain_text(
                    '近期尝试过' if locale == 'zh-CN' else 'Recently attempted')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(OUT/f'scope-{width}-{locale}.png'))
                # A later all-cached open must still explain the omitted sources,
                # and must not announce that zero new tasks updated all sources.
                for task in BackgroundTask.query.all():
                    if task.payload['department_id'] == f.a.id:
                        continue
                    task.state = 'done'
                    task.checked_at = task.finished_at = datetime.utcnow()
                    task.result = {'new_count':0}
                db.session.commit()
                count = BackgroundTask.query.count()
                page.reload()
                expect(page.locator('#inboxRefreshStatus')).to_contain_text(
                    '本轮未安排新的抓取任务' if locale == 'zh-CN'
                    else 'No new collection tasks scheduled')
                expect(page.locator('#inboxRefreshStatus')).not_to_contain_text(
                    '更新完成' if locale == 'zh-CN' else 'Update complete')
                page.locator('#inboxRefreshSummary').click()
                expect(page.locator('#inboxRefreshScope')).to_contain_text(
                    '本轮处理其中 0 个' if locale == 'zh-CN' else '0 included in this batch')
                assert BackgroundTask.query.count() == count
                assert not errors and not external, (errors, external)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                results.append({'width':width, 'locale':locale, 'school_counts_separate':True,
                    'failed_omission_visible':True, 'all_cached_open_explained':True,
                    'external_requests':len(external), 'page_errors':errors})
                page.close()
            finally:
                case.tearDown()
    finally:
        browser.close()
(OUT/'browser-result.json').write_text(json.dumps({'status':'passed', 'cases':results},
    ensure_ascii=False, indent=2)+'\n', 'utf-8')
print('Refresh scope accounting browser checks passed.')
faulthandler.cancel_dump_traceback_later()
