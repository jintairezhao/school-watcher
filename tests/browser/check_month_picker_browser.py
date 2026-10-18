"""Exercise the real collection setting without contacting any outside service."""
import json
import os
import re
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from playwright.sync_api import expect, sync_playwright
from test_storage_management import StorageManagementTests
from backend.database.models import AppConfig


def contrast(foreground, background):
    def luminance(color):
        rgb = [int(value) / 255 for value in re.findall(r'\d+', color)[:3]]
        linear = [value / 12.92 if value <= .04045 else ((value + .055) / 1.055) ** 2.4 for value in rgb]
        return sum(value * weight for value, weight in zip(linear, (.2126, .7152, .0722)))
    values = sorted((luminance(foreground), luminance(background)))
    return (values[1] + .05) / (values[0] + .05)


def check():
    fixture = StorageManagementTests()
    fixture.setUp()
    output = ROOT / '.impeccable/review/month-picker'
    output.mkdir(parents=True, exist_ok=True)
    today = date.today()
    current = today.strftime('%Y-%m')
    errors, results = [], []
    saved_requests = []

    def serve(route):
        from urllib.parse import urlsplit
        parsed = urlsplit(route.request.url)
        if route.request.method == 'POST' and parsed.path == '/api/settings':
            saved_requests.append(route.request.post_data_json)
        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
            method=route.request.method, data=route.request.post_data,
            headers={key: value for key, value in route.request.headers.items()
                if key.lower() in ('content-type', 'x-csrf-token')})
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))
            for width, height, theme, name in ((1280, 900, 'dark', 'desktop'),
                    (390, 844, 'dark', 'mobile'), (1280, 900, 'light', 'desktop-light'),
                    (390, 844, 'light', 'mobile-light')):
                AppConfig.set('scrape_since_month', '2023-01')
                context = browser.new_context(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                context.add_init_script(f"localStorage.setItem('theme', '{theme}')")
                context.route('**/*', serve)
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto('http://localhost/admin#scrape')
                field = page.locator('#sinceMonth')
                toggle = page.get_by_role('button', name='选择起始年月', exact=True)
                panel = page.get_by_role('dialog', name='选择起始发布年月')
                year = panel.get_by_role('spinbutton', name='年份')
                expect(field).to_have_value('2023-01')
                toggle.click()
                expect(panel).to_be_visible()
                expect(year).to_have_value('2023')
                expect(panel.locator('[aria-pressed="true"]')).to_have_accessible_name('2023年1月')
                expect(panel.locator('[data-month]')).to_have_count(12)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, 'overflow')
                geometry = panel.evaluate('''el => {
                    const rect = el.getBoundingClientRect();
                    return {left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom,
                        cells: [...el.querySelectorAll('[data-month]')].map(button => button.getBoundingClientRect().height)};
                }''')
                assert geometry['left'] >= 0 and geometry['right'] <= width, geometry
                assert geometry['top'] >= 0 and geometry['bottom'] <= height, geometry
                assert all(size >= 44 for size in geometry['cells']), geometry
                styles = panel.evaluate('''el => {
                    const selected = getComputedStyle(el.querySelector('[aria-pressed="true"]'));
                    return {text: getComputedStyle(el).color, background: getComputedStyle(el).backgroundColor,
                        selectedText: selected.color, selectedBackground: selected.backgroundColor,
                        hint: getComputedStyle(el.querySelector('.month-picker-hint')).color,
                        shortcut: getComputedStyle(el.querySelector('[data-month-shortcut]')).color,
                        yearDisplay: getComputedStyle(el.querySelector('.month-picker-year-field')).display};
                }''')
                ratios = {key: round(contrast(styles[key], styles['selectedBackground' if key == 'selectedText' else 'background']), 2)
                    for key in ('text', 'selectedText', 'hint', 'shortcut')}
                assert min(ratios.values()) >= 4.5, (theme, ratios, styles)
                assert styles['yearDisplay'] == 'flex', styles
                page.screenshot(path=str(output / f'{name}.png'), full_page=True)
                if name == 'desktop':
                    panel.screenshot(path=str(output / 'detail-dark.png'))

                # Direct year entry and month selection use the real save endpoint.
                year.fill('2024')
                panel.get_by_role('button', name='2024年6月', exact=True).click()
                expect(panel).not_to_be_visible()
                expect(field).to_have_value('2024-06')
                expect(field).to_be_focused()
                page.get_by_role('button', name='保存设置', exact=True).click()
                expect(page.locator('.toast-success')).to_be_visible()
                assert saved_requests[-1]['since_month'] == '2024-06'
                page.reload()
                expect(field).to_have_value('2024-06')

                # Keyboard navigation starts at the selected month and restores focus on Escape.
                field.focus()
                field.press('Alt+ArrowDown')
                expect(panel.get_by_role('button', name='2024年6月', exact=True)).to_be_focused()
                page.keyboard.press('ArrowRight')
                expect(panel.get_by_role('button', name='2024年7月', exact=True)).to_be_focused()
                page.keyboard.press('ArrowDown')
                expect(panel.get_by_role('button', name='2024年11月', exact=True)).to_be_focused()
                page.keyboard.press('Home')
                expect(panel.get_by_role('button', name='2024年1月', exact=True)).to_be_focused()
                page.keyboard.press('PageUp')
                expect(year).to_have_value('2023')
                page.keyboard.press('End')
                expect(panel.get_by_role('button', name='2023年12月', exact=True)).to_be_focused()
                page.keyboard.press('Escape')
                expect(panel).not_to_be_visible()
                expect(toggle).to_be_focused()
                expect(toggle).to_have_attribute('aria-expanded', 'false')

                # Quick choices and both boundaries remain usable without saving.
                for shortcut, value in (('今年一月', f'{today.year}-01'),
                        ('去年一月', f'{today.year - 1}-01'), ('本月', current)):
                    toggle.click()
                    panel.get_by_role('button', name=shortcut, exact=True).click()
                    expect(field).to_have_value(value)
                toggle.click()
                expect(panel.get_by_role('button', name='下一年', exact=True)).to_be_disabled()
                for month in range(today.month + 1, 13):
                    expect(panel.locator(f'[data-month="{month}"]')).to_be_disabled()
                year.fill('1900')
                expect(panel.get_by_role('button', name='上一年', exact=True)).to_be_disabled()
                year.fill('1899')
                expect(panel.locator('[data-month]:enabled')).to_have_count(0)
                year.fill('2e3')
                panel.get_by_role('button', name='2000年3月', exact=True).click()
                expect(field).to_have_value('2000-03')
                toggle.click()
                page.locator('#adminPageTitle').click()
                expect(panel).not_to_be_visible()
                expect(toggle).to_have_attribute('aria-expanded', 'false')

                # Invalid text never reaches the server; six digits work on a numeric keyboard.
                before = len(saved_requests)
                for value in ('2023-13', '1899-12', f'{today.year + 1}-01', ''):
                    field.fill(value)
                    page.get_by_role('button', name='保存设置', exact=True).click()
                    expect(page.locator('#sinceMonthError')).to_be_visible()
                    expect(field).to_have_attribute('aria-invalid', 'true')
                    assert len(saved_requests) == before
                field.fill('202306')
                field.press('Tab')
                expect(field).to_have_value('2023-06')
                expect(page.locator('#sinceMonthError')).not_to_be_visible()
                toggle.click()
                page.evaluate("location.hash = 'overview'")
                expect(panel).not_to_be_visible()
                results.append({'width': width, 'theme': theme, 'save_and_reload': True,
                    'keyboard': True, 'shortcuts': True, 'validation': True, 'no_overflow': True, 'contrast': ratios})
                context.close()
            browser.close()
        assert not errors, errors
        report = {'checks': results, 'errors': errors, 'external_requests': 0}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False))
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    check()
