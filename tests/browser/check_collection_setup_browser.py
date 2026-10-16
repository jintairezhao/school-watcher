"""Real first-run clicks with isolated state and no provider or university requests."""
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_collection_setup import CollectionSetupTests
from backend.database.models import BackgroundTask, Subscription, AppConfig
from playwright.sync_api import expect, sync_playwright


def check():
    output = ROOT / '.local/onboarding-checks/collection-setup'
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='collection-setup-check-') as scratch, \
            patch.dict(os.environ, {'WATCHER_DATA_DIR':scratch, 'WATCHER_ENV_FILE':str(Path(scratch) / '.env'),
                                    'FIELD_ENC_KEY':'isolated-browser-fixture-field-key'}), \
            patch('backend.ai.providers.complete', side_effect=AssertionError('No paid requests')), sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for width, theme in ((1440, 'dark'), (390, 'dark'), (1440, 'light'), (390, 'light')):
            fixture = CollectionSetupTests(); fixture.setUp()
            try:
                page = browser.new_page(viewport={'width':width, 'height':950}, reduced_motion='reduce')
                errors, calls = [], []
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    req = route.request; parsed = urlsplit(req.url)
                    assert parsed.hostname == 'localhost', 'No external requests'
                    if parsed.path == '/_desktop/components':
                        return route.fulfill(json={'phase':'ready', 'message':'可用'})
                    if parsed.path == '/api/schools/suggest-url':
                        return route.fulfill(json={'found':True, 'url':'https://www.cupk.edu.cn', 'message':'已匹配克拉玛依校区官网'})
                    calls.append((req.method, parsed.path))
                    response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                        method=req.method, data=req.post_data, headers={k:v for k,v in req.headers.items()
                            if k.lower() in ('content-type', 'x-csrf-token')})
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                page.route('**/*', serve)
                page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
                page.goto('http://localhost/admin#schools')
                page.get_by_role('button', name='添加学校', exact=True).click()
                expect(page.locator('#setupSchoolName')).to_be_visible()
                page.locator('#setupSchoolName').fill('中国石油大学（北京）克拉玛依校区')
                expect(page.locator('#setupSchoolUrl')).to_have_value('https://www.cupk.edu.cn')
                page.locator('#setupSchoolUrl').fill('https://manual.edu.cn/')
                page.locator('#setupSchoolName').fill('自填学校')
                expect(page.locator('#setupSchoolUrl')).to_have_value('https://manual.edu.cn/')
                assert not Subscription.query.count() and not BackgroundTask.query.count()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / f'add-{theme}-{width}.png'), full_page=True)
                page.get_by_role('link', name='取消', exact=True).click()
                assert not Subscription.query.count() and not BackgroundTask.query.count()
                page.goto('http://localhost/collection/setup?school_id=' + str(fixture.school.id))
                page.locator('#setupAIDetails > summary').click()
                expect(page.locator('#aiProvider')).not_to_have_value('')
                for provider, domain in [('deepseek','platform.deepseek.com'), ('dashscope','help.aliyun.com'),
                                         ('ark','console.volcengine.com'), ('bigmodel','open.bigmodel.cn')]:
                    page.locator('#aiProvider').select_option(provider)
                    expect(page.locator('#aiKeyLink')).to_have_attribute('href', __import__('re').compile('https://' + domain.replace('.', r'\.')))
                page.locator('[name="ai_mode"][value="ai"]').check()
                page.locator('#setupStart').click()
                expect(page.locator('#setupFeedback')).to_contain_text('请先测试并选择')
                assert not Subscription.query.count() and not BackgroundTask.query.count()
                page.locator('[name="ai_mode"][value="rules"]').check()
                page.locator('#setupSinceDate').fill('2024-06-15')
                page.locator('#setupInterval').fill('45')
                page.evaluate('scrollTo(0, 0)')
                page.screenshot(path=str(output / f'subscribe-{theme}-{width}.png'), full_page=True)
                page.locator('#setupStart').click()
                page.wait_for_url('**/subscriptions/' + str(fixture.school.id))
                assert Subscription.query.count() == 1
                assert AppConfig.get('scrape_since_date') == '2024-06-15'
                assert AppConfig.get('scrape_interval') == '45'
                assert BackgroundTask.query.filter_by(kind='discover').one().payload['ai_assist'] is False
                assert calls.index(('POST','/api/collection/setup')) < calls.index(('POST','/api/subscriptions'))
                page.goto('http://localhost/collection/setup')
                expect(page.locator('#setupSinceDate')).to_have_value('2024-06-15')
                expect(page.locator('#setupInterval')).to_have_value('45')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                tasks_before = BackgroundTask.query.count()
                page.goto('http://localhost/admin#scrape')
                expect(page.locator('#interval')).to_have_value('45')
                page.locator('#interval').fill('60')
                page.get_by_role('button', name='保存设置', exact=True).click()
                expect(page.locator('.toast-success')).to_be_visible()
                assert AppConfig.get('scrape_since_date') == '2024-06-15', 'interval-only edits must preserve the day'
                assert BackgroundTask.query.count() == tasks_before, 'settings-only edits must not start new jobs'
                assert not errors, errors
                results.append({'width':width, 'theme':theme, 'save_precedes_start':True,
                    'cancel_no_work':True, 'key_links':4, 'exact_date_retained':True,
                    'legacy_interval_edit_preserves_exact_day':True, 'page_errors':errors})
                page.close()
            finally:
                fixture.tearDown()
        browser.close()
    (output / 'report.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(results))


if __name__ == '__main__':
    check()
