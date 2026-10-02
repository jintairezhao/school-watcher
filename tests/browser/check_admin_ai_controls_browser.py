"""Exercise service deletion and credential visibility with isolated, fictional data."""
import logging
import os
from pathlib import Path
import re
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from test_shared_summaries import SharedSummaryTests
from backend.ai.providers import ProviderResult
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = SharedSummaryTests()
    fixture.setUp()
    server = None
    try:
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        folder = ROOT / 'data' / 'ui-check' / 'admin-ai-controls'
        folder.mkdir(parents=True, exist_ok=True)
        result = ProviderResult(content='{"ok":true}', finish_reason='stop',
            usage={'known': True, 'input_tokens': 8, 'output_tokens': 4, 'total_tokens': 12},
            request_id='fixture-only')
        with patch.dict(os.environ, {'FIELD_ENC_KEY': 'isolated-browser-test-key'}), \
             patch('backend.ai.providers.complete', return_value=result) as paid, \
             sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get('PLAYWRIGHT_CHANNEL', 'msedge'), headless=True)
            context = browser.new_context(reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            page = context.new_page()
            errors, requests = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: requests.append(request.url))
            for label, size in [('desktop', {'width': 1440, 'height': 1000}),
                                ('mobile', {'width': 390, 'height': 844})]:
                page.set_viewport_size(size)
                page.goto(base + '/admin#schools')
                expect(page.get_by_role('button', name=re.compile('自动发现|重新发现'))).to_have_count(0)
                assert page.evaluate('typeof startDiscovery') == 'undefined'
                page.get_by_role('button', name='部门', exact=True).click()
                expect(page.locator('#deptList')).to_contain_text('学院通知')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(folder / f'schools-{label}.png'), full_page=True)
                assert not [url for url in requests if '/discover' in url]
                page.goto(base + '/admin#platform')
                expect(page.locator('#aiProvider option')).to_have_count(4)
                expect(page.get_by_label('模型名称', exact=True)).to_be_visible()
                expect(page.locator('#aiProfiles')).to_contain_text('还没有连接 AI')
                page.locator('#aiProvider').select_option('deepseek')
                page.locator('#aiModel').fill('fixture-model')
                page.locator('#aiKey').fill('fixture-never-sent')
                page.get_by_role('button', name='显示 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_attribute('type', 'text')
                page.get_by_role('button', name='隐藏 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_attribute('type', 'password')
                calls_before = paid.call_count
                page.get_by_role('button', name='保存服务', exact=True).click()
                expect(page.locator('#aiProfiles')).to_contain_text('fixture-model · 待测试')
                expect(page.get_by_role('button', name='启用', exact=True)).to_be_disabled()
                assert paid.call_count == calls_before
                page.get_by_role('button', name='测试连接（调用 API）', exact=True).click()
                expect(page.locator('#aiProfiles')).to_contain_text('fixture-model · 可用')
                assert paid.call_count == calls_before + 1
                for selector in ('#aiDirectory', '#aiSummary'):
                    page.locator(selector).select_option(label='DeepSeek · fixture-model')
                page.get_by_role('button', name='保存功能设置', exact=True).click()
                expect(page.locator('#aiStatus')).to_have_text('功能设置已保存')
                page.get_by_role('button', name='编辑', exact=True).click()
                expect(page.locator('#aiKey')).to_have_value('')
                expect(page.locator('#aiKey')).to_have_attribute('type', 'password')
                page.get_by_role('button', name='显示 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_value('fixture-never-sent')
                expect(page.locator('#aiKey')).to_have_attribute('type', 'text')
                page.get_by_role('button', name='隐藏 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_attribute('type', 'password')
                page.get_by_role('button', name='保存服务', exact=True).click()
                expect(page.locator('#aiStatus')).to_have_text('服务已保存，可在下方选择使用它的功能。')
                expect(page.locator('#aiKey')).to_have_value('')
                assert paid.call_count == calls_before + 1
                active = page.locator('#aiProfiles .admin-user-row').filter(has_text='fixture-model')
                bound_id = page.locator('#aiSummary').input_value()
                active.get_by_role('button', name='停用', exact=True).click()
                expect(active).to_contain_text('已停用')
                expect(active.get_by_role('button', name='启用', exact=True)).to_be_enabled()
                page.reload()
                expect(active).to_contain_text('已停用')
                for selector in ('#aiDirectory', '#aiSummary'):
                    expect(page.locator(selector)).to_have_value(bound_id)
                    expect(page.locator(selector + ' option:checked')).to_contain_text('已停用')
                page.get_by_role('button', name='保存功能设置', exact=True).click()
                expect(page.locator('#aiStatus')).to_have_text('功能设置已保存')
                active.get_by_role('button', name='编辑', exact=True).click()
                page.get_by_role('button', name='显示 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_value('fixture-never-sent')
                page.get_by_role('button', name='保存服务', exact=True).click()
                expect(page.locator('#aiStatus')).to_have_text('服务已保存，当前已停用。点击“启用”即可继续使用。')
                active.get_by_role('button', name='启用', exact=True).click()
                expect(active).to_contain_text('可用')
                for selector in ('#aiDirectory', '#aiSummary'):
                    expect(page.locator(selector)).to_have_value(bound_id)
                    expect(page.locator(selector + ' option:checked')).to_have_text('DeepSeek · fixture-model')
                assert paid.call_count == calls_before + 1
                page.locator('#aiModel').fill('unused-model')
                page.locator('#aiKey').fill('unused-fixture-key')
                page.get_by_role('button', name='保存服务', exact=True).click()
                unused = page.locator('#aiProfiles .admin-user-row').filter(has_text='unused-model')
                unused.get_by_role('button', name='测试连接（调用 API）').click()
                expect(unused).to_contain_text('可用')
                unused.get_by_role('button', name='停用', exact=True).click()
                expect(unused).to_contain_text('已停用')
                expect(unused.get_by_role('button', name='启用', exact=True)).to_be_enabled()
                expect(page.get_by_role('button', name='停用并移除密钥', exact=True)).to_have_count(0)
                active.get_by_role('button', name='编辑', exact=True).click()
                page.get_by_role('button', name='显示 API 密钥', exact=True).click()
                expect(page.locator('#aiKey')).to_have_value('fixture-never-sent')
                assert page.locator('#aiKeyToggle').evaluate('''button => {
                    const input = document.querySelector('#aiKey').getBoundingClientRect();
                    const toggle = button.getBoundingClientRect();
                    return Math.abs(input.top + input.height / 2 - toggle.top - toggle.height / 2) < 1;
                }''')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(folder / f'key-visible-{label}.png'), full_page=True)
                page.get_by_role('button', name='隐藏 API 密钥', exact=True).click()
                page.evaluate('scrollTo(0, 0)')
                if label == 'mobile':
                    page.evaluate("document.documentElement.setAttribute('data-theme', 'dark')")
                page.screenshot(path=str(folder / f'key-hidden-{label}.png'), full_page=True)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.once('dialog', lambda dialog: dialog.dismiss())
                unused.get_by_role('button', name='删除', exact=True).click()
                expect(unused).to_have_count(1)
                page.once('dialog', lambda dialog: dialog.accept())
                unused.get_by_role('button', name='删除', exact=True).click()
                expect(unused).to_have_count(0)
                page.get_by_role('button', name='取消修改', exact=True).click()
                expect(page.locator('#aiKey')).to_have_value('')
                expect(page.locator('#aiKey')).to_have_attribute('type', 'password')
                active.get_by_role('button', name='编辑', exact=True).click()
                page.once('dialog', lambda dialog: dialog.accept())
                active.get_by_role('button', name='删除', exact=True).click()
                expect(page.locator('#aiProfiles')).to_contain_text('还没有连接 AI')
                expect(page.locator('#aiProfileId')).to_have_value('')
                for selector in ('#aiDirectory', '#aiSummary'):
                    expect(page.locator(selector)).to_have_value('')
                page.reload()
                expect(page.locator('#aiProfiles')).to_contain_text('还没有连接 AI')
                assert paid.call_count == calls_before + 2
            assert not errors, errors
            browser.close()
        print('Passed desktop/mobile: pause/resume retains keys, validation and bindings without API calls; paused save/reload; deletion clears keys and bindings; key visibility; discovery removed.')
    finally:
        if server:
            server.shutdown()
            server.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
