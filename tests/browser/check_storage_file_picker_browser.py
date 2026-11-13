"""Localized backup picker uses the real file input and isolated import route."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
from test_storage_management import StorageManagementTests,packed
from backend.database.db import db
from backend.database.models import Announcement
from backend.services.localization import stage_preferences
from playwright.sync_api import expect,sync_playwright


def check():
    results=[]
    with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid calls')),sync_playwright() as pw:
        browser=pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL','msedge'),headless=True)
        try:
            for width in (1280,390):
                fixture=StorageManagementTests();fixture.setUp()
                page=None
                try:
                    fixture.article('existing')
                    stage_preferences(fixture.admin.id,{'locale':'en'});db.session.commit()
                    page=browser.new_page(viewport={'width':width,'height':900})
                    errors,writes=[],[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def serve(route):
                        request=route.request;parsed=urlsplit(request.url)
                        assert parsed.hostname=='localhost',request.url
                        if request.method!='GET':writes.append(parsed.path)
                        response=fixture.client.open(parsed.path+('?' + parsed.query if parsed.query else ''),
                            method=request.method,data=request.post_data_buffer,
                            headers={key:value for key,value in request.headers.items() if key.lower() in ('content-type','x-csrf-token')})
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    page.route('**/*',serve)
                    page.goto('http://localhost/admin/storage')
                    page.locator('#storageBackup>summary').click()
                    picker=page.locator('#chooseStorageBackup')
                    filename=page.locator('#storageBackupFilename')
                    actual=page.locator('#storageBackupFile')
                    expect(picker).to_have_text('Choose file')
                    expect(filename).to_have_text('No file selected')
                    expect(actual).to_be_hidden()
                    assert actual.get_attribute('name')=='file'
                    page.locator('#storageImportForm button[type=submit]').click()
                    expect(page.locator('#importStatus')).to_have_text('Select a ZIP backup file first.')
                    expect(picker).to_be_focused()
                    expect(picker).to_have_attribute('aria-invalid','true')
                    page.locator('#languageSelect').select_option('zh-CN')
                    expect(picker).to_have_text('选择文件')
                    expect(filename).to_have_text('未选择文件')
                    expect(page.locator('#importStatus')).to_have_text('请先选择 ZIP 备份文件')
                    # Both native keyboard activation keys open the actual picker.
                    picker.focus()
                    with page.expect_file_chooser() as chosen:
                        picker.press('Enter')
                    long_name='备份-original-'+('long-filename-'*8)+'.zip'
                    chosen.value.set_files({'name':long_name,'mimeType':'application/zip','buffer':packed(fixture.sample()).getvalue()})
                    expect(filename).to_have_text(long_name)
                    expect(page.locator('#importStatus')).to_be_empty()
                    assert '/api/storage/import' not in writes
                    page.locator('#languageSelect').select_option('en')
                    expect(picker).to_have_text('Choose file')
                    expect(filename).to_have_text(long_name)
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    actual.dispatch_event('cancel')
                    expect(filename).to_have_text(long_name)
                    page.locator('#storageImportForm').evaluate('form=>form.reset()')
                    expect(filename).to_have_text('No file selected')
                    assert actual.input_value()==''
                    picker.focus()
                    with page.expect_file_chooser() as chosen:
                        picker.press('Space')
                    chosen.value.set_files({'name':'valid-history.zip','mimeType':'application/zip','buffer':packed(fixture.sample()).getvalue()})
                    expect(filename).to_have_text('valid-history.zip')
                    page.locator('#storageImportForm button[type=submit]').click()
                    expect(page.locator('#importStatus')).to_contain_text('1 notices added')
                    expect(filename).to_have_text('No file selected')
                    assert actual.input_value()==''
                    assert writes.count('/api/storage/import')==1
                    assert Announcement.query.count()==2
                    assert not errors,errors
                    results.append({'width':width,'keyboard_picker':True,'localized_empty_and_validation':True,
                        'selected_filename_preserved_during_locale_change':True,'reset_and_import_clear_name':True,
                        'no_automatic_upload':True,'page_errors':errors})
                finally:
                    if page:page.close()
                    fixture.tearDown()
        finally:browser.close()
    output=ROOT/'.local/storage-file-picker-review'
    output.mkdir(parents=True,exist_ok=True)
    (output/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps({'status':'passed','checks':results},ensure_ascii=False))


if __name__=='__main__':check()
