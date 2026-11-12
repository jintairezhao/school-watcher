"""Real reader links silently queue local copies; all official requests are fixtures."""
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from playwright.sync_api import expect, sync_playwright
from tests.test_saved_materials import SavedMaterialTests, PDF
from backend.database.models import AnnouncementResource, BackgroundTask
from backend.services.content_cache import prune_content
from backend.worker import dispatch


def check():
    fixture = SavedMaterialTests()
    fixture.setUp()
    errors, dialogs, official = [], [], []
    output = ROOT / '.local/attachment-cache-tests'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='msedge', headless=True)
            context = browser.new_context(accept_downloads=True)

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname == 'files.example.edu.cn':
                    official.append(request.url)
                    return route.fulfill(content_type='text/html', body='<p>Official attachment fixture</p>')
                assert parsed.hostname == 'localhost', request.url
                response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                    method=request.method, data=request.post_data_buffer,
                    headers={key: value for key, value in request.headers.items()
                        if key.lower() in ('content-type', 'x-csrf-token')})
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

            context.route('**/*', serve)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
            page.goto(f'http://localhost/announcement/{fixture.ann.id}')
            attachment = page.locator('.body-loader-content a').filter(has_text='申请附件.pdf')
            expect(attachment).to_have_attribute('href', __import__('re').compile('/attachments/'))
            assert AnnouncementResource.query.count() == 0
            with page.expect_popup() as opened:
                attachment.click()
            opened.value.wait_for_url('https://files.example.edu.cn/award.pdf')
            expect(opened.value.locator('p')).to_have_text('Official attachment fixture')
            assert len(official) == 1
            job = BackgroundTask.query.filter_by(kind='saved_resource').one()
            assert job.payload['cache'] is True
            with patch('backend.services.saved_materials.download_resource', return_value=(PDF, 'application/pdf', '申请附件.pdf')):
                assert dispatch(job.kind, job.payload)['saved']
            fixture.star(True)
            assert prune_content(all_cache=True)['evicted'] == 0
            page.reload()
            attachment = page.locator('.body-loader-content a').filter(has_text='申请附件.pdf')
            expect(attachment).to_have_attribute('href', __import__('re').compile('/resources/'))
            with page.expect_download() as downloaded:
                attachment.click()
            assert Path(downloaded.value.path()).read_bytes() == PDF
            assert len(official) == 1
            assert not errors and not dialogs, (errors, dialogs)
            page.screenshot(path=str(output / 'reader-cached.png'), full_page=True)
            browser.close()
        result = {'status': 'passed', 'silent_background_cache': True, 'official_opened_immediately': True,
            'offline_attachment_bytes_verified': True, 'favorite_survives_cleanup': True,
            'dialogs': dialogs, 'browser_errors': errors, 'actual_official_requests': 0}
        (output / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf8')
        print(json.dumps(result, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    check()
