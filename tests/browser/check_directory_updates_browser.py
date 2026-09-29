"""Live directory input and desktop update reminders on an isolated profile."""
import json
import os
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import sync_playwright, expect
from test_desktop_local import DesktopLocalTests


def check():
    fixture = DesktopLocalTests()
    fixture.setUp()
    fixture.open()
    output = ROOT / '.local/onboarding-checks/directory-updates'
    output.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
            for width, theme, frameless in ((1280, 'dark', True), (760, 'light', True), (1280, 'light', False)):
                fixture.app.config['DESKTOP_FRAMELESS'] = frameless
                page = browser.new_page(viewport={'width': width, 'height': 850}, color_scheme=theme)
                errors, searches, subscriptions = [], [], []
                failed_search = False
                page.on('pageerror', lambda error: errors.append(str(error)))
                def serve(route):
                    nonlocal failed_search
                    request = route.request
                    parsed = urlsplit(request.url)
                    if parsed.path == '/_desktop/components':
                        return route.fulfill(json={'phase': 'ready'})
                    if parsed.path == '/api/catalog/subscribe':
                        subscriptions.append(request.post_data_json)
                        return route.fulfill(status=503, json={'error': '隔离测试：未实际订阅'})
                    if request.headers.get('x-directory-fragment') == '1':
                        searches.append(parse_qs(parsed.query))
                        if failed_search:
                            return route.fulfill(status=503, body='offline fixture')
                    response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
                        method=request.method, data=request.post_data,
                        headers={key: value for key, value in request.headers.items()
                                 if key in ('content-type', 'x-csrf-token', 'x-directory-fragment')})
                    route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)
                page.route('**/*', serve)
                page.add_init_script("""localStorage.setItem('theme', '%s');
                    window.updateFixture={phase:'available',version:'9.0.0',available:!sessionStorage.getItem('dismissed')};
                    window.pywebview={api:{
                      update_notification:async()=>window.updateFixture,
                      dismiss_update:async()=>{sessionStorage.setItem('dismissed','1');return window.updateFixture={...window.updateFixture,available:false};},
                      window_action:async(action)=>{window.nativeAction=action;return true;}
                    }};""" % theme)
                page.goto('http://localhost/explore')
                expect(page.locator('#desktopUpdateNotice')).to_be_visible()
                expect(page.locator('#desktopUpdateMessage')).to_have_text('发现新版本 9.0.0')
                page.locator('#desktopUpdateDismiss').click()
                expect(page.locator('#desktopUpdateNotice')).to_be_hidden()
                initial_count = page.locator('[data-directory-count]').inner_text()
                query = page.locator('#catalogQuery')
                query.fill('中国')
                expect(page.locator('.directory-school h2').first).to_contain_text('中国')
                expect(page.locator('[data-directory-count]')).not_to_have_text(initial_count)
                assert all('中国' in name for name in page.locator('.directory-school h2').all_text_contents())
                expect(query).to_be_focused()
                page.locator('#catalogProvince').select_option('北京')
                expect(page).to_have_url('http://localhost/explore?q=%E4%B8%AD%E5%9B%BD&province=%E5%8C%97%E4%BA%AC')
                assert all('北京' in meta for meta in page.locator('.directory-school p').all_text_contents())
                page.locator('#catalogLevel').select_option('subscribed')
                expect(page.get_by_text('没有找到匹配的学校', exact=True)).to_be_visible()
                page.get_by_text('查看全部学校', exact=True).click()
                expect(page.locator('[data-directory-count]')).to_have_text(initial_count)
                expect(query).to_have_value('')
                page.get_by_role('link', name='下一页', exact=True).click()
                expect(page.locator('.pagination-bar')).to_contain_text('第 2 /')
                page.go_back()
                expect(page.locator('.pagination-bar')).to_contain_text('第 1 /')
                before = len(searches)
                query.evaluate("""input=>{input.dispatchEvent(new CompositionEvent('compositionstart')); input.value='zhong'; input.dispatchEvent(new InputEvent('input',{isComposing:true}));}""")
                page.wait_for_timeout(250)
                assert len(searches) == before, 'search ran before IME committed'
                query.evaluate("""input=>{input.value='中国'; input.dispatchEvent(new CompositionEvent('compositionend',{data:'中国'}));}""")
                expect(page.locator('.directory-school h2').first).to_contain_text('中国')
                # Deliver an obsolete response late even after abort: it must not replace the latest query.
                page.evaluate("""() => { const original=window.fetch;window.fetch=async(...args)=>{
                    const response=await original(...args);
                    if(new URL(String(args[0]),location.href).searchParams.get('q')==='中国石油') {
                        window.delayedSearch=true;await new Promise(resolve=>setTimeout(resolve,650));
                    } return response;
                }; }""")
                query.fill('中国石油')
                page.wait_for_function('() => window.delayedSearch === true')
                query.fill('清华')
                expect(page.locator('.directory-school h2')).to_have_text(['清华大学'])
                page.wait_for_timeout(750)
                expect(page.locator('.directory-school h2')).to_have_text(['清华大学'])
                failed_search = True
                query.fill('中国')
                expect(page.locator('#directoryFeedback')).to_contain_text('匹配未完成')
                failed_search = False
                page.get_by_role('button', name='查找学校', exact=True).click()
                expect(page.locator('.directory-school h2').first).to_contain_text('中国')
                page.locator('[data-subscribe-name]').first.click()
                expect(page.get_by_text('隔离测试：未实际订阅', exact=True)).to_be_visible()
                assert subscriptions and '中国' in subscriptions[-1]['name']
                query.fill('不存在的测试学校')
                expect(page.get_by_text('没有找到匹配的学校', exact=True)).to_be_visible()
                query.fill('')
                expect(page.locator('[data-directory-count]')).to_have_text(initial_count)
                page.goto('http://localhost/admin')
                expect(page.locator('#desktopUpdateNotice')).to_be_hidden()
                # A new launch has a new native state; test the actionable reminder on either OS layout.
                page.evaluate("sessionStorage.removeItem('dismissed')")
                page.goto('http://localhost/explore')
                expect(page.locator('#desktopUpdateNotice')).to_be_visible()
                query.fill('中国')
                expect(page.locator('.directory-school h2').first).to_contain_text('中国')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / f'directory-{width}-{theme}-{frameless}.png'))
                page.locator('#desktopUpdateOpen').click()
                page.wait_for_function("() => window.nativeAction === 'updates'")
                expect(page.locator('#desktopUpdateNotice')).to_be_hidden()
                assert not errors, errors
                page.close()
            browser.close()
        print(json.dumps({'live_search': 'passed', 'startup_notification': 'passed', 'themes': ['dark', 'light']}))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    check()
