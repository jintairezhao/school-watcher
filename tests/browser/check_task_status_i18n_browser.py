"""Real workflow/log/verification metadata translates; observed names do not."""
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_storage_management import StorageManagementTests
from backend.database.db import db
from backend.database.models import ScrapeLog, VerificationSession
from backend.database.source_governance_models import SourceProposal, SourceReviewEvent
from backend.services.localization import stage_preferences
from backend.services.source_governance import _snapshot
from playwright.sync_api import expect, sync_playwright

MANUAL = '官网需要人工完成访问验证；已有通知仍保留'
REPAIR = '官网页面已变化，正在自动恢复读取；已有消息仍保留'
ORIGINAL = '程序执行失败'


def no_chinese(page, selector):
    value = page.locator(selector).inner_text()
    assert not re.search('[\u4e00-\u9fff]', value), (selector, value)


def check():
    report = []
    with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for width in (1440, 390):
                fixture = StorageManagementTests(); fixture.setUp()
                page = None
                try:
                    evidence = _snapshot('<article>' + ORIGINAL + '</article>', 'https://example.edu.cn/list', role='list')
                    proposal = SourceProposal(school_id=fixture.school.id, identity_key='identity', proposal_key='proposal',
                        origin='observed', state='needs_review', expected_config_hash='hash', evidence_hash='hash',
                        candidate_json=json.dumps({'name':ORIGINAL, 'list_url':'https://example.edu.cn/list'}),
                        evidence_json=json.dumps({'list':evidence}),
                        validation_json=json.dumps({'errors':[], 'workflow':{'status':'program_error',
                            'reason':'程序尚未完成下一页检查；日期格式尚未正确解析', 'ai_reason':MANUAL,
                            'needed_evidence':['程序尚未完成下一页检查']}}))
                    db.session.add(proposal); db.session.flush()
                    db.session.add(SourceReviewEvent(proposal_id=proposal.id, action='program_failed',
                        detail_json=json.dumps({'reason':REPAIR})))
                    stamp = datetime.utcnow()
                    for index, status in enumerate(('running', 'success', 'waiting', 'failed', 'partial', 'interrupted')):
                        db.session.add(ScrapeLog(school_id=fixture.school.id, source_name=ORIGINAL,
                            started_at=stamp - timedelta(minutes=index),
                            finished_at=None if status == 'running' else stamp,
                            status=status, error_message=MANUAL if index == 0 else REPAIR if index == 1 else None))
                    db.session.add(VerificationSession(id='manual-session', source_id='public-source',
                        origin='https://example.edu.cn', url='https://example.edu.cn/verification',
                        status='required', error_message=MANUAL))
                    stage_preferences(fixture.admin.id, {'locale':'en'}); db.session.commit()
                    page = browser.new_page(viewport={'width':width, 'height':960}, reduced_motion='reduce')
                    errors, writes = [], []
                    page.on('pageerror', lambda error:errors.append(str(error)))
                    def serve(route):
                        request=route.request; parsed=urlsplit(request.url)
                        assert parsed.hostname == 'localhost', request.url
                        if request.method != 'GET': writes.append(parsed.path)
                        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                            method=request.method, data=request.post_data,
                            headers={key:value for key,value in request.headers.items()
                                if key.lower() in ('content-type','x-csrf-token')})
                        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                    page.route('**/*',serve)
                    page.goto('http://localhost/admin/sources')
                    expect(page.locator('#proposalList .text-muted')).to_contain_text('Processing failed')
                    no_chinese(page, '#proposalList .text-muted')
                    expect(page.locator('#proposalList .admin-user-row > span').first).to_contain_text(ORIGINAL)
                    page.locator('#proposalList button').click()
                    expect(page.locator('#proposalWorkflow')).to_contain_text('Processing failed')
                    for selector in ('#proposalWorkflow','#proposalNext','#proposalErrors','#proposalMissing','#proposalHistory'):
                        no_chinese(page, selector)
                    expect(page.locator('#proposalEvidence p')).to_have_text(ORIGINAL)
                    expect(page.locator('#proposalTitle')).to_contain_text(ORIGINAL)

                    # Changing locale re-renders metadata, never the captured official text.
                    page.locator('#languageSelect').select_option('zh-CN')
                    expect(page.locator('#proposalWorkflow')).to_contain_text('程序执行失败')
                    page.locator('#languageSelect').select_option('en')
                    expect(page.locator('#proposalWorkflow')).to_contain_text('Processing failed')
                    expect(page.locator('#proposalEvidence p')).to_have_text(ORIGINAL)

                    page.goto('http://localhost/admin#logs')
                    expect(page.locator('#recentLogSchools tr[data-log-id]')).to_have_count(6)
                    statuses = page.locator('.scrape-log-badge').all_inner_texts()
                    assert set(statuses) == {'In progress','Success','Awaiting processing','Failed','Partially completed','Interrupted'}, statuses
                    for node in page.locator('.scrape-log-message').all():
                        assert not re.search('[\u4e00-\u9fff]',node.inner_text()), node.inner_text()
                    expect(page.locator('.scrape-log-source strong').first).to_have_text(ORIGINAL)
                    page.locator('#languageSelect').select_option('zh-CN')
                    expect(page.locator('.scrape-log-badge').first).to_have_text('进行中')
                    page.locator('#languageSelect').select_option('en')
                    expect(page.locator('.scrape-log-badge').first).to_have_text('In progress')

                    page.locator('[data-tab="overview"]').click()
                    expect(page.locator('#recentLogs .log-error')).to_have_count(1)
                    no_chinese(page, '#recentLogs')
                    page.locator('#languageSelect').select_option('zh-CN')
                    expect(page.locator('#recentLogs .log-error')).to_contain_text('官网页面已变化')
                    page.locator('#languageSelect').select_option('en')
                    expect(page.locator('#recentLogs .log-error')).not_to_contain_text('官网页面已变化')
                    no_chinese(page, '#recentLogs')

                    page.goto('http://localhost/admin/access-verification')
                    expect(page.locator('#verification-list > li')).to_have_count(1)
                    no_chinese(page, '#verification-list .verification-source p')
                    expect(page.locator('#verification-list .verification-source p')).to_contain_text('manual access verification')
                    assert not errors, errors
                    assert all(path == '/api/preferences/localization' for path in writes), writes
                    report.append({'width':width,'workflow_and_history_translated':True,
                        'log_states_and_reasons_translated':True,'overview_rerenders_on_locale_change':True,
                        'manual_verification_reason_translated':True,
                        'official_names_and_evidence_preserved':True,'locale_switch_rerenders':True,
                        'page_errors':errors,'writes':writes})
                finally:
                    if page:page.close()
                    fixture.tearDown()
        finally:
            browser.close()
    print(json.dumps({'status':'passed','checks':report,'paid_calls':0},ensure_ascii=False))


if __name__ == '__main__':
    check()
