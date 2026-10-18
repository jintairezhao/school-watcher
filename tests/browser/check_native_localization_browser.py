"""Native page states use the same catalog as the app; no real download or API."""
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from desktop.localization import DesktopLanguage, native_page


def main():
    output = ROOT / '.local/native-language-fix/screenshots'
    output.mkdir(parents=True, exist_ok=True)
    errors = []
    checked = 0
    with tempfile.TemporaryDirectory() as folder, sync_playwright() as pw:
        language = DesktopLanguage(Path(folder)); language.set_locale('en')
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        for theme in ('light', 'dark'):
            page = browser.new_page(viewport={'width':480,'height':500}, color_scheme=theme)
            page.on('pageerror', lambda error: errors.append(str(error)))
            for name, states in (
                ('updates', [
                    {'phase':'idle', 'message':'当前版本 2.24'},
                    {'phase':'checking', 'message':'正在检查 GitHub Release…'},
                    {'phase':'latest', 'message':'已经是最新版本'},
                    {'phase':'available', 'message':'发现新版本 9.0.0'},
                    {'phase':'downloading', 'message':'正在下载安装包…', 'progress':57},
                    {'phase':'ready', 'message':'下载完成，完整性校验通过'},
                    {'phase':'installing', 'message':'正在关闭服务并准备更新…'},
                    {'phase':'error', 'message':'暂时无法连接 GitHub，请检查网络后重试。'},
                    {'phase':'download-error', 'message':'安装包校验失败，请重新下载。'},
                ]),
                ('components', [
                    {'phase':'checking','message':'正在检查采集组件…'},
                    {'phase':'downloading','message':'正在下载独立采集组件，首次准备可能需要几分钟。你可以继续查看通知与管理设置。'},
                    {'phase':'ready','message':'采集组件已就绪','browser':'独立采集组件'},
                    {'phase':'error','message':'采集组件准备失败。请检查网络后点击重新检测；已保存的通知与设置仍可使用。'},
                ]),
                ('migration', [
                    {'phase':'pausing','step':0,'message':'正在暂停抓取并保存进度…','detail':'正在保存 2 项进行中的任务'},
                    {'phase':'copying','step':2,'message':'正在迁移并校验数据…','detail':'已校验 12 个文件 · source_catalog.sqlite3'},
                    {'phase':'cancelled','step':2,'message':'已取消迁移','detail':'继续使用原来的文件位置','done':True},
                    {'phase':'error','step':2,'message':'迁移未完成','detail':'原数据仍保留，重新打开后继续。','done':True,'restart':True},
                ]),
            ):
                page.set_content(native_page(name, language))
                first = language.state(dict({'current':'2.24', 'detail':'', 'can_cancel':True}, **states[0]))
                page.evaluate('''s => {
                    window.nativeState=s; window.nativeCalls=[];
                    window.pywebview={api:Object.fromEntries(['state','check','download','install','retry','cancel','finish']
                        .map(key=>[key,async()=>{window.nativeCalls.push(key);return window.nativeState;}]))};
                    window.dispatchEvent(new Event('pywebviewready'));
                }''', first)
                for index, state in enumerate(states):
                    value = language.state(dict({'current':'2.24','detail':'','can_cancel':True}, **state))
                    page.evaluate('s=>{window.nativeState=s;paint(s);}', value)
                    expect(page.locator('#message')).to_have_text(value['message'])
                    assert not re.search(r'[\u4e00-\u9fff]', page.locator('body').inner_text()), (name, state)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not page.locator('[title],[aria-label]').evaluate_all('items=>items.some(el=>/[\u4e00-\u9fff]/.test((el.title||"")+(el.getAttribute("aria-label")||"")))')
                    checked += 1
                    if (name=='updates' and state['phase']=='latest') or (name=='components' and state['phase']=='ready'):
                        page.screenshot(path=str(output / f'{name}-{theme}-en.png'), full_page=True)
                if name == 'updates':
                    # Backend phase/messages stay canonical and switch both ways.
                    language.set_locale('zh-CN')
                    page.evaluate('s=>{window.nativeState=s;paint(s);}', language.state({'current':'2.24','phase':'latest','message':'已经是最新版本'}))
                    expect(page.locator('h1')).to_have_text('软件更新')
                    language.set_locale('en')
                    page.evaluate('s=>{window.nativeState=s;paint(s);}', language.state({'current':'2.24','phase':'latest','message':'已经是最新版本'}))
                    expect(page.locator('h1')).to_have_text('Software update')
                    page.evaluate("() => {window.pywebview.api.check=async()=>{throw new Error('fixture');};}")
                    page.locator('#action').click()
                    expect(page.locator('#message')).to_have_text('Operation incomplete. Try again.')
            page.set_content(native_page('loading', language))
            expect(page.locator('html')).to_have_attribute('lang', 'en')
            assert not re.search(r'[\u4e00-\u9fff]', page.locator('body').inner_text())
            page.close()
        browser.close()
    assert not errors, errors
    print(json.dumps({'native_localization':'passed','state_checks':checked,'themes':['light','dark']}))


if __name__ == '__main__':
    main()
