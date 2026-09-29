"""Real analytics endpoint + isolated executions. Never reads user data or calls a provider."""
from datetime import datetime, timedelta
import logging
import os
from pathlib import Path
import sys
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'tests'))
import test_shared_summaries as fixture_module
from test_ai_usage import execution
from backend.ai.models import AIExecution, AIBudget
from backend.database.db import db
from backend.database.models import AppConfig
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def main():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = fixture_module.SharedSummaryTests(); fixture.setUp()
    server = None
    try:
        now = datetime.utcnow()
        for index in range(365):
            if index % 7 in (2, 5) or 32 < index < 80:
                continue
            date = now - timedelta(days=index, seconds=60)
            for purpose in ('directory', 'summary'):
                amount = (1200 + (index * 977) % 17000) if purpose == 'directory' else 3200 + index * 33
                db.session.add(execution(f'{index}-{purpose}', date, tokens=amount, purpose=purpose,
                    model='deepseek-chat' if purpose == 'directory' else 'qwen-plus',
                    provider='deepseek' if purpose == 'directory' else 'dashscope'))
        db.session.add_all([
            execution('today-test', now - timedelta(seconds=30), tokens=12, skill_id='connection-test', purpose='summary'),
            execution('failed', now - timedelta(seconds=20), tokens=80, status='failed'),
            execution('unknown', now - timedelta(seconds=15), status='uncertain', usage={'known': False}),
            AIBudget(key='total:' + now.strftime('%Y-%m'), used_tokens=350000, reserved_tokens=50000),
            AppConfig(key='ai_token_limit_total', value='1000000'),
        ])
        db.session.commit()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps(
            {'user_id': fixture.admin.id, '_csrf_token': 'token'})
        folder = ROOT / '.local' / 'onboarding-checks' / 'ai-usage'; folder.mkdir(parents=True, exist_ok=True)
        with patch('backend.ai.providers.complete', side_effect=AssertionError('no paid calls')) as paid, sync_playwright() as pw:
            browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
            context = browser.new_context(timezone_id='Asia/Shanghai', reduced_motion='reduce')
            context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
            page = context.new_page(); errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            for theme, width in (('dark', 1440), ('light', 1440), ('dark', 390), ('light', 390)):
                page.set_viewport_size({'width': width, 'height': 1050 if width > 1000 else 844})
                page.goto(base + '/admin/ai-usage')
                expect(page.get_by_role('link', name='API 用量', exact=True)).to_have_attribute('aria-current', 'page')
                page.evaluate('(theme) => {localStorage.setItem("theme", theme); document.documentElement.dataset.theme = theme;}', theme)
                expect(page.locator('#aiUsagePanel')).to_have_attribute('aria-busy', 'false')
                expect(page.locator('#aiUsageContent')).to_be_visible()
                expect(page.locator('#aiUsageUnknown')).to_contain_text('1 次调用未返回用量')
                expect(page.locator('#aiUsagePurposes')).to_contain_text('连接测试')
                expect(page.locator('#aiUsageBudgets')).to_contain_text('50,000')
                expect(page.locator('#aiActivityGrid button')).to_have_count(91 if width < 640 else 365)
                expect(page.locator('#aiUsageBars button')).to_have_count(30)
                page.locator('#aiActivityGrid button').last.hover()
                expect(page.locator('#aiUsageTooltip')).to_be_visible()
                expect(page.locator('#aiUsageTooltip')).to_contain_text('Token')
                page.locator('#aiUsageBars button').last.focus()
                expect(page.locator('#aiUsageTooltip')).to_be_visible()
                expect(page.locator('#aiUsageTooltip')).to_contain_text('Token')
                page.keyboard.press('ArrowLeft')
                expect(page.locator('#aiUsageBars button').nth(28)).to_be_focused()
                page.keyboard.press('Escape')
                expect(page.locator('#aiUsageTooltip')).to_be_hidden()
                page.locator('[data-usage-metric="calls"]').click()
                expect(page.locator('[data-usage-metric="calls"]')).to_have_attribute('aria-pressed', 'true')
                page.locator('[data-usage-metric="tokens"]').click()
                page.locator('[data-usage-days="7"]').click()
                expect(page.locator('#aiUsageBars button')).to_have_count(7)
                page.locator('[data-usage-days="90"]').click()
                expect(page.locator('#aiUsageBars button')).to_have_count(90)
                page.locator('[data-usage-days="30"]').click()
                expect(page.locator('#aiUsageBars button')).to_have_count(30)
                if width < 640:
                    page.evaluate('window.scrollTo(0, 0)')
                    page.screenshot(path=str(folder / f'usage-{theme}-{width}.png'), full_page=True)
                else:
                    page.locator('#aiUsagePanel').screenshot(path=str(folder / f'usage-{theme}-{width}.png'))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'page overflow'
            page.locator('#aiUsageConfigure').click()
            expect(page).to_have_url(base + '/admin#platform')
            expect(page.locator('#aiProvider')).to_be_visible()
            expect(page.locator('#aiUsagePanel')).to_have_count(0)
            page.get_by_role('link', name='API 用量', exact=True).click()
            expect(page.locator('#aiUsageContent')).to_be_visible()
            # Reordered responses must never replace a more recent time-range selection.
            page.evaluate('''() => {
                const original = window.fetch;
                window.fetch = async function (url, options) {
                    if (String(url).includes('/api/admin/ai/usage?days=7')) {
                        const response = await original(url, {...options, signal:undefined});
                        await new Promise(resolve => setTimeout(resolve, 250));
                        return response;
                    }
                    return original(url, options);
                };
                document.querySelector('[data-usage-days="7"]').click();
                document.querySelector('[data-usage-days="90"]').click();
                window.__restoreUsageFetch = () => {window.fetch = original;};
            }''')
            expect(page.locator('#aiUsageBars button')).to_have_count(90)
            page.wait_for_timeout(350)
            expect(page.locator('#aiUsageBars button')).to_have_count(90)
            page.evaluate('window.__restoreUsageFetch()')
            page.locator('[data-usage-days="30"]').click()
            expect(page.locator('#aiUsageBars button')).to_have_count(30)
            # Failed reads must not leave old data presented as the newly selected period.
            page.route('**/api/admin/ai/usage?*', lambda route: route.fulfill(status=503, json={'error': 'offline'}))
            page.locator('#aiUsageRefresh').click()
            expect(page.locator('#aiUsageStatus')).to_contain_text('重试')
            expect(page.locator('#aiUsageContent')).to_be_hidden()
            page.unroute('**/api/admin/ai/usage?*')
            page.locator('#aiUsageRefresh').click()
            expect(page.locator('#aiUsageContent')).to_be_visible()
            # Empty history and hostile historical model strings stay safe and legible.
            db.session.query(AIExecution).delete(); db.session.commit()
            page.locator('#aiUsageRefresh').click()
            expect(page.locator('#aiUsageEmpty')).to_contain_text('还没有调用记录')
            expect(page.locator('#aiUsageTokens')).to_have_text('0')
            expect(page.locator('#aiUsageSuccess')).to_have_text('—')
            page.evaluate('window.scrollTo(0, 0)')
            page.screenshot(path=str(folder / 'usage-empty.png'), full_page=True)
            bad_model = '<img src=x onerror=alert(1)>' + 'very-long-model-' * 7
            db.session.add(execution('hostile', datetime.utcnow(), tokens=123456789, model=bad_model)); db.session.commit()
            page.locator('#aiUsageRefresh').click()
            expect(page.locator('#aiUsageModels')).to_contain_text(bad_model)
            expect(page.locator('#aiUsageModels img')).to_have_count(0)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
            assert not errors, errors
            assert paid.call_count == 0
            browser.close()
        print('API usage: real aggregates, themes, responsive charts, keyboard/tooltips, ranges, error recovery, empty state and escaped labels passed.')
    finally:
        if server:
            server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__':
    main()
