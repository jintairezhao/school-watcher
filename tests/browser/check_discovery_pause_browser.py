"""Pause/resume against a real isolated queue, without external crawling or AI calls."""
import logging
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_discovery_pause import DiscoveryPauseTests
from backend.database.db import db
from backend.services import tasks
from backend.services.onboarding_progress import record_progress
from backend.services.discovery_control import pause_if_requested
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = DiscoveryPauseTests(); fixture.setUp()
    app = fixture.fixture.app
    server = None
    try:
        server = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': fixture.fixture.admin.id, '_csrf_token':'token'})
        folder = ROOT / '.local/onboarding-checks/discovery-pause'; folder.mkdir(parents=True, exist_ok=True)
        with patch('backend.services.onboarding_progress.ai_available', return_value=True), \
             patch('backend.ai.providers.complete', side_effect=AssertionError('no paid API calls')), sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
            context = browser.new_context(reduced_motion='reduce')
            context.add_cookies([{'name':'session', 'value':cookie, 'url':base}])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for theme, width in (('dark', 1280), ('light', 390)):
                page.set_viewport_size({'width':width, 'height':850})
                page.goto(base + f'/subscriptions/{fixture.school_id}')
                page.locator('.discovery-details > summary').click()
                page.evaluate('(theme) => {localStorage.setItem("theme",theme);document.documentElement.dataset.theme=theme;}', theme)
                expect(page.locator('#discoveryPause')).to_have_text('暂停')
                page.locator('#discoveryPause').click()
                expect(page.locator('#discoveryPause')).to_have_text('继续')
                expect(page.locator('#discoveryMessage')).to_have_text('已暂停，进度已保存')
                expect(page.locator('#discoveryProgress')).to_be_hidden()
                expect(page.locator('#discoveryCounts')).to_contain_text('已接入')
                page.reload()
                page.locator('.discovery-details > summary').click()
                expect(page.locator('#discoveryPause')).to_have_text('继续')
                page.locator('#discoveryStatus').screenshot(path=str(folder / f'paused-{theme}-{width}.png'))
                page.locator('#discoveryPause').click()
                expect(page.locator('#discoveryPause')).to_have_text('暂停')
                expect(page.locator('#discoveryProgress')).to_be_hidden()
                expect(page.locator('#discoveryActivity')).to_have_text('排队中')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            handle = tasks.claim(capabilities=['directory'])
            page.locator('#sourcePreferences > summary').click()
            page.locator('[name="mode"][value="selected"]').check()
            page.locator('[name="department"]').first.uncheck()
            page.locator('#discoveryPause').click()
            expect(page.locator('#discoveryPause')).to_have_text('正在暂停…')
            expect(page.locator('#discoveryPause')).to_be_disabled()
            expect(page.locator('#discoveryMessage')).to_contain_text('等待当前处理完成')
            page.locator('#discoveryStatus').screenshot(path=str(folder / 'pausing.png'))
            with tasks.execution_scope(handle):
                record_progress(checked_pages=609, pending_pages=391)
                try:
                    pause_if_requested()
                except tasks.TaskDeferred as deferred:
                    db.session.rollback(); tasks.handoff(handle, deferred)
            page.locator('#discoveryRefresh').click()
            expect(page.locator('#discoveryPause')).to_have_text('继续')
            expect(page.locator('#discoveryCounts')).to_contain_text('已接入')
            expect(page.locator('[name="department"]').first).not_to_be_checked()
            # Failed resume stays paused, with an actionable error and a retryable control.
            page.route('**/api/subscriptions/*/discovery', lambda route: route.fulfill(status=503, json={'error':'请重试'}))
            page.locator('#discoveryPause').click()
            expect(page.locator('#discoveryError')).to_contain_text('请重试')
            expect(page.locator('#discoveryPause')).to_have_text('继续')
            expect(page.locator('#discoveryPause')).to_be_enabled()
            page.unroute('**/api/subscriptions/*/discovery')
            page.locator('#discoveryPause').click()
            expect(page.locator('#discoveryPause')).to_have_text('暂停')
            next_handle = tasks.claim(capabilities=['directory'])
            tasks.finish(next_handle, {'complete':True})
            page.locator('#discoveryRefresh').click()
            expect(page.locator('#discoveryPause')).to_be_hidden()
            # A web reader cannot control the school's shared worker.
            reader_cookie = app.session_interface.get_signing_serializer(app).dumps(
                {'user_id':fixture.fixture.reader.id, '_csrf_token':'token'})
            context.add_cookies([{'name':'session', 'value':reader_cookie, 'url':base}])
            page.reload()
            expect(page.locator('#discoveryPause')).to_have_count(0)
            assert not errors, errors
            browser.close()
        print('Discovery controls passed: queued/running pause, resume, reload persistence, retained selections, error recovery, permissions and narrow layout.')
    finally:
        if server:
            server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
