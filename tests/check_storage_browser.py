"""Exercise retention, downloads and additive upload through the actual admin UI."""
import io
import json
import logging
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tests'))
from test_storage_management import StorageManagementTests, packed
from backend.database.db import db
from backend.database.models import Announcement
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = StorageManagementTests()
    fixture.setUp()
    server = None
    output = ROOT / '.impeccable' / 'review'
    output.mkdir(parents=True, exist_ok=True)
    try:
        fixture.article('fixture')
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        incoming = packed(fixture.sample()).getvalue()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        with sync_playwright() as p:
            browser = p.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/admin/storage')
            expect(page.get_by_role('heading', name='存储管理', exact=True)).to_be_visible()
            expect(page.locator('#bodyCacheDays')).to_have_value('30')
            page.locator('#bodyCacheDays').fill('90')
            page.locator('[data-cleanup="expired"]').click()
            expect(page.locator('#cleanupStatus')).to_contain_text('请先保存')
            page.get_by_role('button', name='保存保留规则').click()
            expect(page.locator('#policyStatus')).to_contain_text('保留规则已保存')
            page.reload()
            expect(page.locator('#bodyCacheDays')).to_have_value('90')

            # A cancel must make no request; accept then retain the notification row.
            page.once('dialog', lambda dialog: dialog.dismiss())
            page.locator('[data-cleanup="all_cache"]').click()
            expect(page.locator('#cleanupStatus')).to_be_empty()
            page.once('dialog', lambda dialog: dialog.accept())
            page.locator('[data-cleanup="all_cache"]').click()
            expect(page.locator('#cleanupStatus')).to_contain_text('1 条正文缓存')
            expect(page.locator('#announcementCount')).to_have_text('1')

            with page.expect_download() as download_info:
                page.locator('#exportStorage').click()
            download = download_info.value
            assert download.suggested_filename.startswith('school-watcher-data-')
            assert download.failure() is None
            expect(page.locator('#exportStatus')).to_contain_text('数据备份已生成')

            # Corrupt input is visible and retry leaves the file control usable.
            page.locator('#storageBackupFile').set_input_files({'name': 'broken.zip', 'mimeType': 'application/zip', 'buffer': b'broken'})
            page.get_by_role('button', name='导入并合并').click()
            expect(page.locator('#importStatus')).to_contain_text('损坏')
            for added in (1, 0):
                page.locator('#storageBackupFile').set_input_files({'name': 'history.zip', 'mimeType': 'application/zip', 'buffer': incoming})
                page.get_by_role('button', name='导入并合并').click()
                expect(page.locator('#importStatus')).to_contain_text(f'新增 {added} 条通知')
                expect(page.locator('#announcementCount')).to_have_text('2')
            with fixture.app.app_context():
                assert Announcement.query.count() == 2
                assert Announcement.query.filter_by(url='https://example.edu.cn/history').one().published_at.year == 2020

            # Preserve useful result states while viewing the whole desktop/mobile surface.
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(output / 'storage-desktop.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.screenshot(path=str(output / 'storage-mobile.png'), full_page=True)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.evaluate("document.documentElement.dataset.theme='dark'")
            page.screenshot(path=str(output / 'storage-mobile-dark.png'), full_page=True)
            assert not errors, errors
            browser.close()
        print(json.dumps({'browser_errors': errors, 'checks': ['saved policy', 'unsaved changes guard',
            'cleanup cancel and confirm', 'download', 'corrupt-file retry', 'historical merge', 'repeated upload',
            'desktop/mobile layout', 'dark theme'], 'screenshots': str(output)}, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
        fixture.tearDown()


if __name__ == '__main__':
    check()
