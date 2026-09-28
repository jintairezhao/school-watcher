"""Observe real streaming progress and recovery; paced fixture work makes it visible."""
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tests'))
from test_storage_management import StorageManagementTests, packed
from backend.services import data_transfer
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = StorageManagementTests(); fixture.setUp()
    server = None
    output = ROOT / '.local/onboarding-checks/transfer-review'
    output.mkdir(parents=True, exist_ok=True)
    original = data_transfer._report
    def paced(progress, phase, done=0, total=0, processed_bytes=None):
        if phase in ('compressing', 'merging_announcements'):
            time.sleep(.025)
        return original(progress, phase, done, total, processed_bytes)
    try:
        for index in range(50):
            fixture.article(str(index), content_text='用于验证进度的正文' * 1000)
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        with data_transfer.export_data() as source:
            incoming = source.read()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        with patch.object(data_transfer, '_report', paced), sync_playwright() as p:
            browser = p.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL', 'msedge'), headless=True)
            context = browser.new_context(viewport={'width': 1000, 'height': 800}, reduced_motion='reduce')
            context.add_cookies([{'name':'session', 'value':cookie, 'url':base}])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(base + '/admin/storage')
            with page.expect_download() as downloaded:
                page.locator('#exportStorage').click()
                page.wait_for_function("() => document.querySelector('#exportProgress progress').value > 0 && document.querySelector('#exportProgress progress').value < 100")
                expect(page.locator('#exportProgress [data-speed]')).to_contain_text('/s')
                page.locator('#exportProgress').screenshot(path=str(output / 'export-progress.png'))
            assert downloaded.value.failure() is None
            expect(page.locator('#exportStatus')).to_contain_text('数据备份已生成')
            page.locator('#storageBackupFile').set_input_files({'name':'notices.zip', 'mimeType':'application/zip', 'buffer':incoming})
            page.get_by_role('button', name='导入并合并').click()
            page.wait_for_function("() => document.querySelector('#importProgress [data-phase]').textContent === '正在合并通知' && document.querySelector('#importProgress progress').value > 0")
            expect(page.locator('#importProgress [data-speed]')).to_contain_text('条/s')
            page.locator('#importProgress').screenshot(path=str(output / 'import-progress.png'))
            expect(page.locator('#importStatus')).to_contain_text('去重 50 条', timeout=15000)
            sample = fixture.sample()
            sample['announcements'][0]['content_text'] = 'a' * 1100000
            page.locator('.storage-import-options summary').click()
            page.locator('#importExpandedMB').fill('1')
            page.locator('#storageBackupFile').set_input_files({'name':'large.zip', 'mimeType':'application/zip', 'buffer':packed(sample).getvalue()})
            page.get_by_role('button', name='导入并合并').click()
            expect(page.locator('#importStatus')).to_contain_text('额度')
            expect(page.locator('#importProgress [data-phase]')).to_have_text('未完成')
            page.locator('#importExpandedMB').fill('4')
            page.get_by_role('button', name='导入并合并').click()
            expect(page.locator('#importStatus')).to_contain_text('新增 1 条', timeout=15000)
            page.set_viewport_size({'width':390, 'height':844})
            page.evaluate("document.documentElement.dataset.theme = 'dark'")
            page.locator('.storage-import').screenshot(path=str(output / 'import-mobile-dark.png'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert not errors, errors
            browser.close()
        print(json.dumps({'checks':['live export bytes/s','live import rows/s','real download',
            'adjustable expansion limit','failure and retry','mobile no overflow'], 'errors':errors}, ensure_ascii=False))
    finally:
        if server:
            server.shutdown()
        fixture.tearDown()


if __name__ == '__main__':
    check()
