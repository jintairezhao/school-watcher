"""One bounded desktop/mobile pass on isolated fixture data; no provider traffic."""
import logging
import os
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
from test_shared_summaries import SharedSummaryTests, BODY
from backend.database.db import db
from backend.database.models import Announcement
from backend.ai.providers import ProviderError, ProviderResult
from backend.ai.models import AIExecution
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = SharedSummaryTests(); fixture.setUp()
    server = None
    try:
        row = fixture.request(); fixture.run_row(row)
        other = Announcement(school_id=fixture.ann.school_id, department_id=fixture.ann.department_id,
            title='尚未生成摘要的通知', url='https://example.edu.cn/notice/2', content_text=BODY,
            content_cached_at=fixture.ann.content_cached_at)
        fixture.ann.summary = '这是一份旧的未核对摘要。'
        db.session.add(other); db.session.commit()
        other_id = other.id
        from backend.services.source_governance import propose_source
        proposal = propose_source(fixture.ann.school_id, {'name': '学院本科教学通知',
            'list_url': 'https://example.edu.cn/teaching', 'list_selector': 'ul.notices li',
            'title_selector': 'a', 'link_selector': 'a', 'group_name': '学院信息'})
        from backend.services.source_governance import _snapshot
        from test_source_picker import HTML
        fixture.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH'] = str(Path(fixture.temp.name) / 'evidence')
        proposal.evidence_json = json.dumps({'list': _snapshot(HTML, 'https://example.edu.cn/teaching')})
        proposal.state = 'needs_review'; db.session.commit()
        fixture.binding.stop()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        folder = ROOT / '.impeccable' / 'review'; folder.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {'FIELD_ENC_KEY': 'isolated-browser-fixture-only'}), \
             patch('backend.ai.providers.complete', side_effect=ProviderError('fixture_offline')) as paid, \
             sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True, chromium_sandbox=True)
            context = browser.new_context(reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            page = context.new_page(); errors = []
            outgoing = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: outgoing.append(request.url))
            for label, size in [('desktop', {'width': 1440, 'height': 1000}),
                                ('mobile', {'width': 390, 'height': 844})]:
                page.set_viewport_size(size)
                page.goto(base + f'/announcement/{fixture.ann_id}')
                expect(page.locator('[data-summary-text]')).to_contain_text('2026年10月1日')
                page.get_by_text('查看历史摘要', exact=True).click()
                expect(page.get_by_text('这是一份旧的未核对摘要。', exact=True)).to_be_visible()
                page.screenshot(path=str(folder / f'shared-summary-{label}.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.goto(base + f'/announcement/{other_id}')
                page.get_by_role('button', name='生成摘要', exact=True).click()
                expect(page.locator('[data-summary-message]')).to_contain_text('配置')
                expect(page.get_by_role('button', name='生成摘要', exact=True)).to_be_enabled()
                page.goto(base + '/admin#platform')
                expect(page.locator('#aiProvider')).to_be_visible()
                expect(page.locator('#aiProvider option')).to_have_count(4)
                expect(page.locator('#aiName')).to_have_count(0)
                expect(page.locator('#aiRegionGroup')).to_be_hidden()
                expect(page.locator('#aiModelHelp')).not_to_contain_text('接入点')
                page.locator('#aiProvider').select_option('dashscope')
                expect(page.get_by_label('API 密钥所属区域', exact=True)).to_be_visible()
                page.locator('#aiRegion').select_option(label='新加坡')
                expect(page.locator('#aiRegion')).to_have_value('ap-southeast-1')
                page.locator('#aiProvider').select_option('ark')
                expect(page.locator('#aiRegionGroup')).to_be_hidden()
                expect(page.locator('#aiRegion')).to_have_value('cn-beijing')
                expect(page.locator('#aiModelHelp')).to_contain_text('ep-')
                page.locator('#aiProvider').select_option('bigmodel')
                expect(page.locator('#aiRegionGroup')).to_be_hidden()
                expect(page.locator('#aiModelHelp')).not_to_contain_text('接入点')
                page.locator('#aiProvider').select_option('deepseek')
                expect(page.locator('#aiRegion')).to_have_value('default')
                if label == 'desktop':
                    page.locator('#aiModel').fill('fixture-model')
                    page.locator('#aiKey').fill('fixture-never-sent')
                    page.get_by_role('button', name='保存服务', exact=True).click()
                    expect(page.locator('#aiProfiles')).to_contain_text('DeepSeek · fixture-model · 待测试')
                    expect(page.locator('#aiKey')).to_have_value('')
                    assert paid.call_count == 0
                    page.get_by_role('button', name='测试连接（调用 API）', exact=True).click()
                    expect(page.locator('#aiStatus')).not_to_contain_text('正在测试')
                    assert paid.call_count == 1
                    paid.side_effect = None
                    paid.return_value = ProviderResult(content='{"ok":true}', finish_reason='stop',
                        usage={'known': True, 'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12},
                        request_id='fixture-connection')
                    page.get_by_role('button', name='测试连接（调用 API）', exact=True).click()
                    expect(page.locator('#aiProfiles')).to_contain_text('DeepSeek · fixture-model · 可用')
                    assert paid.call_count == 2
                    for selector in ('#aiDirectory', '#aiSummary'):
                        expect(page.locator(selector + ' option').last).to_have_text('DeepSeek · fixture-model')
                    page.locator('#aiProfiles').get_by_role('button', name='编辑', exact=True).click()
                    expect(page.locator('#aiFormTitle')).to_have_text('修改 AI 服务')
                    expect(page.locator('#aiKeyHelp')).to_contain_text('留空继续使用原密钥')
                    expect(page.locator('#aiKey')).to_have_value('')
                    page.get_by_role('button', name='保存服务', exact=True).click()
                    expect(page.locator('#aiFormTitle')).to_have_text('添加 AI 服务')
                    expect(page.locator('#aiProfiles')).to_contain_text('DeepSeek · fixture-model · 可用')
                    expect(page.locator('#aiStatus')).to_have_text('服务已保存，可在下方选择使用它的功能。')
                    assert paid.call_count == 2
                page.locator('#aiProvider').select_option('dashscope')
                page.locator('#aiRegion').select_option('ap-southeast-1')
                page.screenshot(path=str(folder / f'instance-ai-regions-{label}.png'), full_page=True)
                page.locator('#aiProvider').select_option('deepseek')
                page.screenshot(path=str(folder / f'instance-ai-{label}.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.goto(base + '/admin/sources')
                expect(page.locator('#proposalList')).to_contain_text('学院本科教学通知')
                page.get_by_role('button', name='查看详情', exact=True).click()
                expect(page.locator('#proposalDetail')).to_be_visible()
                expect(page.locator('#manualSourceReview, #pickerFrame')).to_have_count(0)
                page.get_by_role('button', name='自动检查', exact=True).click()
                expect(page.locator('#reviewStatus')).to_contain_text('已加入检查队列')
                assert page.evaluate('window.pwned') is None
                assert not [url for url in outgoing if url.startswith('https://attacker.invalid')]
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(folder / f'source-review-{label}.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert not errors, errors
            browser.close()
        print('Passed: shared summaries, provider-specific fields, automatic service labels, save/edit without API calls, explicit mock connection tests, desktop/mobile overflow and JS errors.')
    finally:
        if server:
            server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
