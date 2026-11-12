"""Observe slow saves to completion without enqueueing extra downloads.

Uses the production observer and a browser clock with local intercepted responses;
no application profile, database, official website, or model request is opened.
"""
import json
import os
from pathlib import Path

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[2]
HTML = '''<!doctype html><html><body>
<section data-saved-materials="1" data-active="true">
<p data-saved-message></p><ul data-saved-files></ul>
<button data-retry-saved hidden>Retry</button></section>
</body></html>'''


def check():
    checks, errors = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))

        def fixture():
            context = browser.new_context()
            state = {'value': 'saving', 'calls': []}

            def respond(route):
                if route.request.url.endswith('/saved-materials'):
                    state['calls'].append(route.request.method)
                    status = state['value']
                    route.fulfill(json={'state': status, 'saved': int(status == 'ready'),
                        'total': 1, 'resources': ([{'kind': 'attachment', 'state': 'failed',
                        'filename': 'award.pdf', 'error': 'Temporary website failure'}]
                        if status == 'failed' else [])})
                else:
                    route.fulfill(content_type='text/html', body=HTML)

            context.route('**/*', respond)
            page = context.new_page()
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto('http://127.0.0.1:9481/')
            page.clock.install()
            page.evaluate('''() => {
                window.uiT = value => value;
                window.fixtureHidden = false;
                Object.defineProperty(document, 'hidden', {get: () => window.fixtureHidden});
            }''')
            page.add_script_tag(path=str(ROOT / 'frontend/static/js/saved-materials.js'))
            expect(page.locator('[data-saved-message]')).to_contain_text('正在后台保存')
            return context, page, state

        for terminal, text in [('ready', '已保存在本地'), ('failed', '保存未完成')]:
            context, page, state = fixture()
            # Jump beyond the old one-minute cutoff while the durable save is
            # still running, then let it finish on a subsequent observation.
            page.clock.fast_forward(65000)
            page.wait_for_function('() => document.querySelector("[data-saved-message]").textContent.includes("正在后台保存")')
            state['value'] = terminal
            page.clock.run_for(5000)
            expect(page.locator('[data-saved-message]')).to_contain_text(text)
            expect(page.locator('[data-retry-saved]')).to_be_hidden() if terminal == 'ready' else expect(page.locator('[data-retry-saved]')).to_be_visible()
            if terminal == 'failed':
                expect(page.locator('[data-saved-files]')).to_contain_text('Temporary website failure')
            observed = len(state['calls'])
            page.clock.run_for(20000)
            assert len(state['calls']) == observed, state
            assert set(state['calls']) == {'GET'}, state
            checks.append({'after_65_seconds': terminal, 'requests': observed, 'terminal_stopped': True})
            context.close()

        context, page, state = fixture()
        page.evaluate("window.fixtureHidden = true; document.dispatchEvent(new Event('visibilitychange'))")
        initial = len(state['calls'])
        page.clock.run_for(65000)
        assert len(state['calls']) == initial, state
        state['value'] = 'ready'
        page.evaluate("window.fixtureHidden = false; document.dispatchEvent(new Event('visibilitychange'))")
        page.clock.run_for(5000)
        expect(page.locator('[data-saved-message]')).to_contain_text('已保存在本地')
        assert set(state['calls']) == {'GET'}, state
        checks.append({'hidden_page_has_no_requests': True, 'visible_resumes': True})
        context.close()

        context, page, state = fixture()
        page.locator('[data-saved-materials]').evaluate('panel => panel.remove()')
        initial = len(state['calls'])
        page.clock.run_for(65000)
        assert len(state['calls']) == initial, state
        checks.append({'removed_panel_stops': True})
        context.close()

        context, page, state = fixture()
        page.evaluate('''() => window.dispatchEvent(new CustomEvent('notice:favorite-changed', {detail:{id:1}}))''')
        # Flush the intercepted response so its async JSON handler has installed
        # the current session's timer before advancing the browser clock.
        page.wait_for_timeout(100)
        initial = len(state['calls'])
        for _ in range(2):
            page.clock.run_for(2000)
            page.wait_for_timeout(100)
        # Restarting an observer invalidates the old timer; only the current
        # session continues. Observation never creates a POST download request.
        assert 1 <= len(state['calls']) - initial <= 2, state
        assert set(state['calls']) == {'GET'}, state
        checks.append({'replaced_session_has_one_observer': True})
        context.close()
        browser.close()
    assert not errors, errors
    print(json.dumps({'checks': checks, 'browser_errors': errors,
                      'actual_official_requests': 0, 'actual_model_requests': 0}, ensure_ascii=False))


if __name__ == '__main__':
    check()
