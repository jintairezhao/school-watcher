"""Translate live reader states without translating official article content.

Runs production i18n and content observers against intercepted local responses;
no application profile, database, official website or model is opened.
"""
import json
import os
from pathlib import Path
import re

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
CATALOG = json.loads((ROOT / 'frontend/static/i18n/en.json').read_text('utf8'))
WAITING = [
    '正文已加入读取队列，完成后会自动显示',
    '正在读取官网正文，完成后会自动显示',
    '正文任务已保存，等待采集恢复',
    '官网读取正在自动重试，完成后会自动显示',
    '正在等待官网采集资源，完成后会自动显示',
]
FAILURES = [
    '正文仍在适配中，可先打开官网原文',
    '尚未读取到通知正文，可重试或打开官网原文',
    '正文较大，请在官网阅读',
    '暂时无法读取官网正文，请打开原文查看',
]
FALLBACK = '正文正在后台处理，完成后会自动显示，也可以先打开官网原文。'
CHINESE = re.compile(r'[\u4e00-\u9fff]')
HTML = '''<!doctype html><html lang="en"><meta charset="utf-8"><body>
<script id="uiLocaleData" type="application/json">BOOT</script>
<div data-content-id="1" data-cached="false">
<div class="body-loader-placeholder" hidden></div>
<div class="body-loader-content" data-official-content></div>
<p class="body-loader-status" role="status"></p>
<button class="body-loader-retry" data-i18n="加载正文">Load content</button>
</div></body></html>'''


def check():
    for message in WAITING + FAILURES:
        assert message in CATALOG, 'Missing reader translation: ' + message
        assert not CHINESE.search(CATALOG[message]), message
    boot = json.dumps({'preferences': {'locale': 'en'}, 'english': CATALOG}, ensure_ascii=False)
    html = HTML.replace('BOOT', boot.replace('</', '<\\/'))
    checks, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,
            channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))

        def fixture(state):
            context = browser.new_context()
            calls = []

            def respond(route):
                if route.request.url.endswith('/content'):
                    calls.append(route.request.method)
                    if state.get('disconnected'):
                        state['disconnected'] = False
                        route.abort()
                    elif state.get('malformed'):
                        route.fulfill(content_type='application/json', body='{invalid')
                    else:
                        status = state.get('status', 'loading')
                        route.fulfill(status=202 if status == 'loading' else 200,
                            json={'content_status': status, 'progress_message': state.get('progress', ''),
                                  'error': state.get('error', ''), 'content_html': state.get('html', '')})
                else:
                    route.fulfill(content_type='text/html', body=html)

            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:9482/')
            page.clock.install()
            page.add_script_tag(path=str(ROOT / 'frontend/static/js/i18n.js'))
            page.add_script_tag(path=str(ROOT / 'frontend/static/js/content.js'))
            return context, page, calls

        for message in WAITING + [FALLBACK]:
            context, page, calls = fixture({'progress': '' if message == FALLBACK else message})
            status = page.locator('.body-loader-status')
            expect(status).to_have_text(CATALOG[message])
            expect(page.locator('[data-content-id]')).to_have_attribute('data-load-state', 'loading')
            page.evaluate("I18n.setLocale('zh-CN')")
            expect(status).to_have_text(message)
            page.evaluate("I18n.setLocale('en')")
            expect(status).to_have_text(CATALOG[message])
            assert calls.count('POST') == 1, calls
            checks.append({'state': message, 'english_and_live_switch': True})
            context.close()

        for message in FAILURES:
            context, page, calls = fixture({'status': 'failed', 'error': message})
            status = page.locator('.body-loader-status')
            button = page.locator('.body-loader-retry')
            expect(status).to_have_text(CATALOG[message])
            expect(button).to_be_visible()
            expect(button).to_have_text(CATALOG['重新加载正文'])
            page.evaluate('I18n.apply()')
            expect(button).to_have_text(CATALOG['重新加载正文'])
            page.evaluate("I18n.setLocale('zh-CN')")
            expect(status).to_have_text(message)
            expect(button).to_have_text('重新加载正文')
            page.evaluate("I18n.setLocale('en')")
            expect(status).to_have_text(CATALOG[message])
            expect(button).to_have_text(CATALOG['重新加载正文'])
            assert calls == ['POST'], calls
            checks.append({'failure': message, 'retry_label_survives_locale_refresh': True})
            context.close()

        state = {'disconnected': True, 'progress': WAITING[-1]}
        context, page, calls = fixture(state)
        expect(page.locator('[data-content-id]')).to_have_attribute('data-load-state', 'waiting')
        expect(page.locator('.body-loader-status')).to_have_text(
            CATALOG['正在重新连接应用，后台读取任务会保留，完成后自动显示。'])
        page.clock.run_for(5000)
        expect(page.locator('.body-loader-status')).to_have_text(CATALOG[WAITING[-1]])
        page.clock.fast_forward(65000)
        page.wait_for_timeout(50)
        state['status'] = 'saved'
        state['html'] = '<p>官网正文保持原文，不作界面翻译。</p>'
        page.clock.run_for(4000)
        expect(page.locator('.body-loader-content')).to_have_text('官网正文保持原文，不作界面翻译。')
        assert calls.count('POST') == 1 and 'GET' in calls, calls
        assert page.locator('.body-loader-status').count() == 0
        checks.append({'connection_and_long_queue_recover': True, 'one_post': True,
            'official_content_unchanged': True})
        context.close()
        browser.close()
    assert not errors, errors
    print(json.dumps({'checks': checks, 'browser_errors': errors,
        'actual_official_requests': 0, 'actual_model_requests': 0}, ensure_ascii=False))


if __name__ == '__main__':
    check()
