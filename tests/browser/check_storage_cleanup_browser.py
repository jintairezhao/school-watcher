"""Exercise explicit notice cleanup and compaction against isolated real routes."""
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_storage_management import StorageManagementTests
from backend.database.db import db
from backend.database.models import Announcement, Department, School, UserAnnouncementState
from backend.services.localization import stage_preferences
from backend.services.database_space import BUSY_MESSAGE, DatabaseSpaceError
from playwright.sync_api import expect, sync_playwright


def show_control(page, selector):
    """Open the enclosing native disclosures with actual pointer input."""
    for disclosure in page.locator('.storage-page details').all():
        if disclosure.locator(selector).count() and not disclosure.evaluate('node => node.open'):
            disclosure.locator(':scope > summary').click()


def check():
    output = ROOT / '.local/storage-cleanup-review'
    output.mkdir(parents=True, exist_ok=True)
    report = []
    with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        try:
            for width, locale, theme in [(1440, 'zh-CN', 'dark'), (390, 'en', 'light')]:
                fixture = StorageManagementTests()
                fixture.setUp()
                page = None
                try:
                    old = datetime(2020, 1, 2)
                    first = fixture.article('old-one', published_at=old, title='Historical notice <b>literal title</b>')
                    second = fixture.article('old-two', published_at=old)
                    starred = fixture.article('favorite', published_at=old)
                    trash = fixture.article('recent-trash', published_at=old)
                    unknown = fixture.article('unknown', published_at=None)
                    boundary = fixture.article('cutoff-day', published_at=datetime(2026, 1, 1))
                    db.session.add_all([
                        UserAnnouncementState(user_id=fixture.reader.id, announcement_id=starred.id, starred=True),
                        UserAnnouncementState(user_id=fixture.reader.id, announcement_id=trash.id, trashed_at=datetime.utcnow())])
                    other_school = School(name='Another university', url='https://other.example.edu/')
                    db.session.add(other_school); db.session.flush()
                    other_department = Department(school_id=other_school.id, name='Academic affairs', list_url='https://other.example.edu/notices')
                    db.session.add(other_department); db.session.flush()
                    other = Announcement(school_id=other_school.id, department_id=other_department.id, title='Other school old notice',
                        url='https://other.example.edu/old', published_at=old, created_at=old)
                    db.session.add(other)
                    stage_preferences(fixture.admin.id, {'locale':locale})
                    db.session.commit()
                    preserved_ids = [starred.id, trash.id, unknown.id, boundary.id, other.id]
                    delete_ids = [first.id, second.id]
                    page = browser.new_page(viewport={'width':width, 'height':980}, reduced_motion='reduce')
                    errors, writes, responses = [], [], []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    def serve(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        assert parsed.hostname == 'localhost', request.url
                        if request.method != 'GET':
                            writes.append({'path':parsed.path, 'body':request.post_data})
                        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                            method=request.method, data=request.post_data,
                            headers={key:value for key,value in request.headers.items() if key.lower() in ('content-type','x-csrf-token')})
                        if parsed.path.startswith('/api/storage'):
                            responses.append({'path':parsed.path, 'status':response.status_code, 'data':response.get_json(silent=True)})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                    page.route('**/*', serve)
                    page.add_init_script('localStorage.setItem("theme",' + json.dumps(theme) + ')')
                    page.goto('http://localhost/admin/storage')
                    expect(page.locator('.storage-page')).to_have_attribute('data-storage-ready', 'true')
                    expect(page.locator('html')).to_have_attribute('lang', locale)
                    delete_button = page.locator('#deleteNoticeCleanup')
                    expect(delete_button).to_be_disabled()
                    # Saving a separate form must not enable an unpreviewed destructive action.
                    show_control(page, '#storagePolicyForm button[type=submit]')
                    page.locator('#storagePolicyForm button[type=submit]').click()
                    expect(page.locator('#policyStatus')).not_to_be_empty()
                    expect(delete_button).to_be_disabled()
                    before_preview = len(writes)
                    show_control(page, '#previewNoticeCleanup')
                    page.locator('#previewNoticeCleanup').click()
                    assert len(writes) == before_preview
                    expect(page.locator('#noticeCleanupSchool')).to_be_focused()
                    # Use the real inputs and keyboard submit, without calling page internals.
                    page.locator('#noticeCleanupSchool').select_option(str(fixture.school.id))
                    page.locator('#noticeCleanupBefore').fill('2026-01-01')
                    page.locator('#previewNoticeCleanup').focus()
                    page.keyboard.press('Enter')
                    expect(delete_button).to_be_enabled()
                    for key, count in [('matched_count','4'), ('deletable_count','2'), ('protected_count','2'), ('unknown_date_count','1')]:
                        expect(page.locator(f'[data-notice-count="{key}"]')).to_have_text(count)
                    assert page.locator('#noticeCleanupSamples li').count() == 2
                    assert page.locator('#noticeCleanupSamples b').count() == 0
                    assert not any(row['path'].endswith('/notices/cleanup') for row in writes)
                    # Cancel is genuinely read-only.
                    page.once('dialog', lambda dialog: dialog.dismiss())
                    delete_button.click()
                    assert not any(row['path'].endswith('/notices/cleanup') for row in writes)
                    # Editing either scope control invalidates the preview immediately.
                    page.locator('#noticeCleanupBefore').fill('2025-01-01')
                    expect(delete_button).to_be_disabled()
                    expect(page.locator('#noticeCleanupPreview')).to_be_hidden()
                    page.locator('#noticeCleanupBefore').fill('2026-01-01')
                    page.locator('#previewNoticeCleanup').click()
                    expect(delete_button).to_be_enabled()
                    # A real state change invalidates the server snapshot with 409.
                    db.session.get(Announcement, delete_ids[0]).title = 'Changed since preview'
                    db.session.commit()
                    page.once('dialog', lambda dialog: dialog.accept())
                    delete_button.click()
                    expect(page.locator('#noticeCleanupStatus')).to_contain_text('Preview again' if locale == 'en' else '重新预览')
                    expect(delete_button).to_be_disabled()
                    assert any(row['path'].endswith('/notices/cleanup') and row['status'] == 409 for row in responses)
                    assert Announcement.query.count() == 7
                    page.locator('#previewNoticeCleanup').click()
                    expect(delete_button).to_be_enabled()
                    # One bounded screenshot batch covers both viewports and languages.
                    page.evaluate('window.scrollTo(0,0)')
                    if not os.environ.get('SKIP_STORAGE_SCREENSHOTS'):
                        page.screenshot(path=str(output / f'{width}-{locale}-preview.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.once('dialog', lambda dialog: dialog.accept())
                    delete_button.click()
                    expect(page.locator('#noticeCleanupStatus')).to_contain_text('Permanently deleted 2' if locale == 'en' else '已永久删除 2')
                    expect(delete_button).to_be_disabled()
                    expect(page.locator('#announcementCount')).to_have_text('5')
                    assert all(db.session.get(Announcement, ident) is not None for ident in preserved_ids)
                    assert all(db.session.get(Announcement, ident) is None for ident in delete_ids)
                    # Usage groups retain canonical keys when refreshed in English.
                    assert page.locator('[data-storage-group="通知与目录"]').inner_text() not in ('0 B', '0.0 MB')
                    assert page.locator('[data-storage-group]').count() == 5
                    # Empty previews stay disabled; choosing all schools is explicit.
                    page.locator('#previewNoticeCleanup').click()
                    expect(page.locator('[data-notice-count="deletable_count"]')).to_have_text('0')
                    expect(delete_button).to_be_disabled()
                    page.locator('#noticeCleanupSchool').select_option('all')
                    expect(page.locator('#noticeCleanupPreview')).to_be_hidden()
                    page.locator('#previewNoticeCleanup').click()
                    expect(page.locator('[data-notice-count="deletable_count"]')).to_have_text('1')
                    expect(delete_button).to_be_enabled()
                    # Produce reclaimable SQLite pages only inside this fixture, then use real compaction.
                    db.session.remove()
                    with db.engine.begin() as connection:
                        connection.exec_driver_sql('CREATE TABLE browser_reclaim_fixture (payload BLOB)')
                        connection.exec_driver_sql('INSERT INTO browser_reclaim_fixture VALUES (zeroblob(1048576))')
                        connection.exec_driver_sql('DELETE FROM browser_reclaim_fixture')
                    page.reload()
                    expect(page.locator('.storage-page')).to_have_attribute('data-storage-ready', 'true')
                    show_control(page, '#compactDatabase')
                    compact = page.locator('#compactDatabase')
                    expect(compact).to_be_enabled()
                    lock = sqlite3.connect(fixture.root / 'main.db')
                    try:
                        lock.execute('BEGIN IMMEDIATE')
                        compact.click()
                        expect(page.locator('#databaseSpaceStatus')).to_contain_text('Try again' if locale == 'en' else '稍后重试')
                        assert any(row['path'].endswith('/database/compact') and row['status'] == 409 for row in responses)
                        expect(compact).to_be_enabled()
                    finally:
                        lock.rollback(); lock.close()
                    compact.click()
                    expect(page.locator('#databaseSpaceStatus')).to_contain_text('Reclaimed' if locale == 'en' else '已回收')
                    assert Announcement.query.count() == 5
                    assert all(db.session.get(Announcement, ident) is not None for ident in preserved_ids)
                    # A statistics failure affects only its own section, never the page or a completed action.
                    completed_compaction = page.locator('#databaseSpaceStatus').inner_text()
                    show_control(page, '[data-cleanup="body"]')
                    with patch('backend.services.database_space.database_stats', side_effect=DatabaseSpaceError(BUSY_MESSAGE, 409)):
                        page.once('dialog', lambda dialog: dialog.accept())
                        page.locator('[data-cleanup="body"]').click()
                        expect(page.locator('#databaseReclaimable')).to_have_text('—')
                        expect(compact).to_be_disabled()
                        expect(page.locator('#databaseSpaceAvailability')).to_be_visible()
                        expect(page.locator('#databaseSpaceAvailability')).to_contain_text('Try reclaiming' if locale == 'en' else '稍后重试')
                        expect(page.locator('#databaseSpaceStatus')).to_have_text(completed_compaction)
                    page.once('dialog', lambda dialog: dialog.accept())
                    page.locator('[data-cleanup="body"]').click()
                    expect(page.locator('#databaseSpaceAvailability')).to_be_hidden()
                    expect(page.locator('#databaseSpaceAvailability')).to_be_empty()
                    expect(page.locator('#databaseSpaceStatus')).to_have_text(completed_compaction)
                    expect(page.locator('#databaseReclaimable')).not_to_have_text('—')
                    with patch('backend.services.database_space.database_stats', side_effect=DatabaseSpaceError(BUSY_MESSAGE, 409)):
                        response = page.reload()
                        assert response.status == 200
                        expect(page.locator('.storage-page')).to_have_attribute('data-storage-ready', 'true')
                        show_control(page, '#compactDatabase')
                        expect(page.locator('#databaseReclaimable')).to_have_text('—')
                        expect(compact).to_be_disabled()
                        expect(page.locator('#databaseSpaceAvailability')).to_be_visible()
                        expect(page.locator('#previewNoticeCleanup')).to_be_enabled()
                    page.once('dialog', lambda dialog: dialog.accept())
                    show_control(page, '[data-cleanup="body"]')
                    page.locator('[data-cleanup="body"]').click()
                    expect(page.locator('#databaseSpaceAvailability')).to_be_hidden()
                    expect(page.locator('#databaseReclaimable')).not_to_have_text('—')
                    if locale == 'en':
                        texts = page.locator('#noticeCleanup,#databaseSpaceTitle,#databaseSpaceStatus').all_inner_texts()
                        # School/source titles retain their original language; application controls do not.
                        assert '永久删除' not in '\n'.join(texts)
                    assert not errors, errors
                    report.append({'width':width, 'locale':locale, 'errors':errors,
                        'deleted':2, 'preserved':5, 'explicit_all_schools':True,
                        'requires_preview':True, 'cancel_keeps_data':True, 'changed_snapshot_409':True,
                        'compaction_keeps_rows':True, 'busy_compaction_retries_manually':True,
                        'unavailable_stats_do_not_block_page_or_overwrite_result':True,
                        'writes':[row['path'] for row in writes]})
                finally:
                    if page: page.close()
                    fixture.tearDown()
        finally:
            browser.close()
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'status':'passed', 'checks':report, 'screenshots':str(output)}, ensure_ascii=False))


if __name__ == '__main__':
    check()
