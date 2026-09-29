"""Visual and interaction checks for the native migration dialog's local page."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[2]
output = ROOT / '.local/onboarding-checks/migration-review'
output.mkdir(parents=True, exist_ok=True)
html = (ROOT / 'desktop/ui/migration.html').read_text(encoding='utf-8')
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
    for theme in ('light', 'dark'):
        page = browser.new_page(viewport={'width': 460, 'height': 401}, color_scheme=theme)
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.set_content(html)
        page.evaluate("""() => {
            window.migrationState={phase:'pausing',step:0,message:'正在暂停抓取并保存进度…',detail:'正在保存 2 项进行中的任务',can_cancel:true,done:false};
            window.pywebview={api:{state:async()=>window.migrationState,
                cancel:async()=>{window.didCancel=true;window.migrationState={...window.migrationState,phase:'cancelled',message:'已取消迁移',detail:'继续使用原来的文件位置',done:true,can_cancel:false};return window.migrationState;},
                finish:async()=>{window.didFinish=true;return true;}}};
            window.dispatchEvent(new Event('pywebviewready'));
        }""")
        expect(page.get_by_role('button', name='取消迁移')).to_be_enabled()
        page.screenshot(path=str(output / f'pause-{theme}.png'))
        page.get_by_role('button', name='取消迁移').click()
        expect(page.get_by_role('button', name='返回应用')).to_be_enabled()
        assert page.evaluate('window.didCancel')
        page.get_by_role('button', name='返回应用').click()
        assert page.evaluate('window.didFinish')
        page.evaluate("""window.migrationState={phase:'copying',step:2,message:'正在迁移并校验数据…',detail:'已校验 12 个文件 · source_catalog.sqlite3',can_cancel:false,done:false};""")
        expect(page.get_by_role('button', name='取消迁移')).to_be_disabled()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.get_by_role('button').bounding_box()['y'] + page.get_by_role('button').bounding_box()['height'] <= 401
        page.screenshot(path=str(output / f'copying-{theme}.png'))
        page.evaluate("""window.migrationState={phase:'error',step:2,message:'迁移未完成',detail:'空间不足，原数据仍保留。重新打开后继续。',can_cancel:false,done:true,restart:true};""")
        expect(page.get_by_role('button', name='重新打开')).to_be_enabled()
        page.screenshot(path=str(output / f'error-{theme}.png'))
        page.evaluate("""window.didFinish=false;window.migrationState={...window.migrationState,phase:'complete',step:3,message:'迁移完成'};""")
        page.wait_for_function('window.didFinish === true')
        assert not errors, errors
        page.close()
    browser.close()
print(json.dumps({'migration_ui': 'passed', 'themes': ['light', 'dark']}))
