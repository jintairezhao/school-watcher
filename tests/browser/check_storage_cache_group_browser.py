"""One cache action uses exact server totals and preserves non-cache fixture data."""
from datetime import datetime,timedelta
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_storage_management import StorageManagementTests
from backend.database.db import db
from backend.database.models import ScrapeLog,UserAnnouncementState
from backend.services.localization import stage_preferences
from backend.services.storage_policy import policy,save_policy
from backend.services.runtime_catalog import RuntimeCatalog
from playwright.sync_api import expect,sync_playwright


def human_bytes(value):
    if value<1024:return f'{value} B'
    if value<1048576:return f'{value/1024:.1f} KB'
    if value<1073741824:return f'{value/1048576:.1f} MB'
    return f'{value/1073741824:.2f} GB'


def show_control(page,selector):
    for disclosure in page.locator('.storage-page details').all():
        if disclosure.locator(selector).count() and not disclosure.evaluate('node=>node.open'):
            disclosure.locator(':scope > summary').click()


def check():
    report=[]
    with patch('backend.ai.providers.complete',side_effect=AssertionError('No paid calls')),sync_playwright() as pw:
        browser=pw.chromium.launch(channel='msedge',headless=True)
        try:
            for width,locale in ((1440,'zh-CN'),(390,'en')):
                fixture=StorageManagementTests();fixture.setUp();page=None
                try:
                    fixture.app.config.update(FETCH_EVIDENCE_DIR=str(fixture.root/'fetch-evidence'),
                        SOURCE_GOVERNANCE_EVIDENCE_PATH=str(fixture.root/'source-governance-evidence'))
                    ordinary=fixture.article('ordinary');starred=fixture.article('starred');trash=fixture.article('trash')
                    db.session.add_all([UserAnnouncementState(user_id=fixture.reader.id,announcement_id=starred.id,starred=True),
                        UserAnnouncementState(user_id=fixture.reader.id,announcement_id=trash.id,trashed_at=datetime.utcnow()),
                        ScrapeLog(status='success',started_at=datetime.utcnow()-timedelta(days=400))])
                    stage_preferences(fixture.admin.id,{'locale':locale});db.session.commit()
                    save_policy({**policy(),'backup_keep_count':1,'scrape_log_retention_days':12})
                    retained={}
                    for name in ('backups/watcher-one.zip','backups/watcher-two.zip','logs/old.log',
                            'updates/downloaded-installer.exe','catalog-generations/site-'+'a'*32+'.sqlite3'):
                        path=fixture.root/name;path.parent.mkdir(parents=True,exist_ok=True)
                        if path.suffix == '.sqlite3':
                            with RuntimeCatalog(path, versioned=False).connect(write=True):
                                pass
                        else:
                            path.write_bytes(b'keep fixture '+name.encode())
                        retained[path]=path.read_bytes()
                    cache=fixture.root/'fetch-evidence/old.json';cache.parent.mkdir();cache.write_bytes(b'cache fixture')
                    page=browser.new_page(viewport={'width':width,'height':900})
                    errors,writes=[],[]
                    page.on('pageerror',lambda error:errors.append(str(error)))
                    def serve(route):
                        request=route.request;parsed=urlsplit(request.url)
                        assert parsed.hostname=='localhost',request.url
                        if request.method!='GET':writes.append({'path':parsed.path,'body':request.post_data})
                        response=fixture.client.open(parsed.path+('?' + parsed.query if parsed.query else ''),
                            method=request.method,data=request.post_data_buffer,
                            headers={key:value for key,value in request.headers.items() if key.lower() in ('content-type','x-csrf-token')})
                        route.fulfill(status=response.status_code,headers=dict(response.headers),body=response.data)
                    page.route('**/*',serve);page.goto('http://localhost/admin/storage')
                    assert not writes
                    initial=fixture.client.get('/api/storage').json
                    assert sum(initial['usage_groups'].values())==initial['total_bytes']
                    expect(page.locator('.storage-table tbody tr')).to_have_count(5)
                    for key,value in initial['usage_groups'].items():
                        expect(page.locator(f'[data-storage-kind="{key}"]')).to_have_text(human_bytes(value))
                    show_control(page,'#policy-scrape_log_retention_days')
                    expect(page.locator('#storagePolicyForm input:visible')).to_have_count(2)
                    expect(page.locator('#policy-scrape_log_retention_days')).to_have_value('12')
                    expect(page.locator('#policy-backup_keep_count')).to_have_value('1')
                    saved_summary=page.locator('#cacheRetentionSummary').inner_text()
                    show_control(page,'#policy-body_cache_days')
                    page.locator('#policy-body_cache_days').fill('0')
                    expect(page.locator('#cacheRetentionSummary')).to_have_text(saved_summary)
                    page.locator('#storageCacheRules>summary').click();page.locator('#storageRetention>summary').click()
                    show_control(page,'[data-cleanup="cache"]')
                    page.locator('[data-cleanup="cache"]').click()
                    assert not writes
                    page.locator('#cleanupStatus a').click()
                    expect(page.locator('#policy-body_cache_days')).to_be_visible()
                    expect(page.locator('#policy-body_cache_days')).to_be_focused()
                    page.locator('#storagePolicyForm button[type=submit]').click()
                    expect(page.locator('#policyStatus')).to_contain_text('saved' if locale=='en' else '已保存')
                    expect(page.locator('#cacheRetentionSummary')).to_contain_text('Unlimited' if locale=='en' else '不限制')
                    assert policy()['body_cache_days']==0 and policy()['scrape_log_retention_days']==12
                    before_cleanup=policy()
                    page.once('dialog',lambda dialog:dialog.dismiss())
                    page.locator('[data-cleanup="cache"]').click()
                    assert not any(item['path']=='/api/storage/cleanup' for item in writes)
                    page.once('dialog',lambda dialog:dialog.accept())
                    page.locator('[data-cleanup="cache"]').click()
                    expect(page.locator('#cleanupStatus')).to_contain_text('Eligible cache was removed' if locale=='en' else '已清理可移除的缓存')
                    cleanup=[item for item in writes if item['path']=='/api/storage/cleanup']
                    assert len(cleanup)==1 and json.loads(cleanup[0]['body'])=={'mode':'cache'}
                    assert policy()==before_cleanup and ScrapeLog.query.count()==1
                    assert all(path.read_bytes()==content for path,content in retained.items())
                    assert not cache.exists()
                    for announcement in (ordinary,starred,trash):db.session.refresh(announcement)
                    assert not ordinary.content_html and starred.content_html and trash.content_html
                    updated=fixture.client.get('/api/storage').json
                    for key,value in updated['usage_groups'].items():
                        expect(page.locator(f'[data-storage-kind="{key}"]')).to_have_text(human_bytes(value))
                    page.locator('#languageSelect').select_option('zh-CN' if locale=='en' else 'en')
                    expect(page.locator('#cacheRetentionSummary')).to_contain_text('不限制' if locale=='en' else 'Unlimited')
                    assert page.locator('[data-cleanup="all_cache"]').count()==0
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
                    assert not errors,errors
                    report.append({'width':width,'locale':locale,'group_totals_from_server':True,
                        'single_cache_action':True,'preserved_logs_backups_catalog_application':True,
                        'saved_summary_and_deep_unsaved_link':True,'protected_favorites_and_trash':True,'errors':errors})
                finally:
                    if page:page.close()
                    fixture.tearDown()
        finally:browser.close()
    output=ROOT/'.local/storage-cache-group-review';output.mkdir(parents=True,exist_ok=True)
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
    print(json.dumps({'status':'passed','checks':report},ensure_ascii=False))


if __name__=='__main__':check()
