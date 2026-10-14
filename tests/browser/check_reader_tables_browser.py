"""Table values stay intact in split, focused, standalone and narrow readers."""
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from backend.database.db import db
from backend.database.models import Announcement
from test_inbox_search_suggestions import InboxSearchTests
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server

PROSE = '<p>以下为排版验收用的虚构数据，不代表实际学生或评审结果。' + '正文段落保持适合阅读的行宽。' * 30 + '</p>'
# Reproduce the Word-exported six-column layout, without real personal data.
TABLE = '''<table width="568" style="width:568px;table-layout:fixed"><colgroup>
<col width="40"><col width="98"><col width="188"><col width="62"><col width="52"><col width="128">
</colgroup><tr><th>序号</th><th>学院</th><th>班级</th><th>学号</th><th>姓名</th><th>奖项</th></tr>'''
for index in range(141, 153):
    TABLE += '<tr>' + ''.join(f'<td><p><span>{text}</span></p></td>' for text in (
        str(index), '示例数理与人工智能学院', '示例数据科学与大数据技术24-1班',
        '2099017489', '示例甲', '本科生国家奖学金')) + '</tr>'
TABLE += '</table>'
NOTES = '''<table><tr><th>项目</th><th>说明</th></tr><tr><td>长段说明</td><td>''' + (
    '较长的表格说明应正常换行，不能因为保护短姓名而将整段强制排在同一行。' * 15
) + '''</td></tr><tr><td colspan="2"><p>第一段明确分隔。</p><p>第二段保留。</p></td></tr>
<tr><td rowspan="2">合并行</td><td>第一行<br>第二行</td></tr><tr><td>结束</td></tr></table>'''
HTML = PROSE + TABLE + NOTES


def geometry(page):
    return page.locator('.body-loader-content').evaluate('''body => {
        const lines = cell => {
            const walk = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
            const tops = new Set(); let node;
            while ((node = walk.nextNode())) {
                if (!node.textContent.trim()) continue;
                const range = document.createRange(); range.selectNodeContents(node);
                for (const box of range.getClientRects()) if (box.width) tops.add(Math.round(box.top));
            }
            return tops.size;
        };
        const table = body.querySelector('table');
        const scroll = table.parentElement;
        return {viewport: innerWidth, documentWidth: document.documentElement.scrollWidth,
            reader: body.closest('.reader-document').clientWidth, body: body.clientWidth,
            prose: body.querySelector('p').clientWidth,
            table: table.clientWidth, scroll: scroll.clientWidth, scrollWidth: scroll.scrollWidth,
            values: [...table.rows].slice(1).filter(row => row.cells.length >= 6).map(row => [0, 3, 4].map(i => lines(row.cells[i]))),
            descriptionLines: lines(table.rows[1].cells[2]),
            longCellLines: body.querySelectorAll('table')[1] ? lines(body.querySelectorAll('table')[1].rows[1].cells[1]) : null};
    }''')


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    fixture = InboxSearchTests(); fixture.use_file_database = True; fixture.setUp()
    server = None
    output = ROOT / '.local/reader-table-check'; output.mkdir(parents=True, exist_ok=True)
    checks, errors = [], []
    try:
        ann = db.session.get(Announcement, 1)
        ann.content_html = HTML
        ann.content_cached_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        server = make_server('127.0.0.1', 0, fixture.app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        cookie = fixture.app.session_interface.get_signing_serializer(fixture.app).dumps({'user_id': 1})
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'))
            try:
                context = browser.new_context(reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                context.route('**/*', lambda r: r.continue_() if r.request.url.startswith(base + '/') else r.abort())
                context.route('**/api/inbox/sync', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
                context.route('**/api/inbox/refresh**', lambda r: r.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
                page = context.new_page(); page.on('pageerror', lambda e: errors.append(str(e)))
                for width, mode, theme in ((1882, 'split', 'dark'), (1882, 'focus', 'dark'),
                    (1882, 'standalone', 'light'), (1200, 'split', 'light'),
                    (720, 'focus', 'light'), (390, 'split', 'dark'), (390, 'standalone', 'light')):
                    page.set_viewport_size({'width': width, 'height': 950 if width > 720 else 844})
                    path = '/announcement/1' if mode == 'standalone' else '/?school=1&period=all&selected=1' + ('&view=focus' if mode == 'focus' else '')
                    page.goto(base + path)
                    if mode == 'split' and page.locator('.inbox-workspace').get_attribute('data-reading-view') == 'focus':
                        page.locator('[data-toggle-reading-view]').click()
                    page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                    page.evaluate('document.fonts.ready')
                    expect(page.locator('.reading-table-scroll')).to_have_count(2)
                    result = geometry(page)
                    assert all(n == 1 for row in result['values'] for n in row), result
                    assert result['documentWidth'] <= width + 1, result
                    assert result['longCellLines'] > 1, result
                    assert result['descriptionLines'] <= 3, result
                    assert result['prose'] <= 720 + 1, result
                    if width == 1882: assert result['reader'] > 1000, result
                    if width <= 720:
                        assert result['scrollWidth'] > result['scroll'], result
                        scroll = page.locator('.reading-table-scroll').first
                        scroll.focus(); page.keyboard.press('ArrowRight')
                        page.wait_for_function("() => document.querySelector('.reading-table-scroll').scrollLeft > 0")
                    expect(page.locator('td[colspan="2"]')).to_have_count(1)
                    expect(page.locator('td[rowspan="2"]')).to_have_count(1)
                    checks.append({'mode': mode, 'theme': theme, **result})
                    page.screenshot(path=str(output / f'{width}-{mode}.png'))

                # The same normalization also applies to a body arriving through AJAX.
                page.evaluate('''html => {
                    const body = document.querySelector('.body-loader-content'); body.innerHTML = html;
                    window.normalizeArticleTypography(body); window.normalizeArticleTypography(body);
                }''', HTML)
                expect(page.locator('.reading-table-scroll')).to_have_count(2)
                assert all(n == 1 for row in geometry(page)['values'] for n in row)
                assert page.locator('.body-loader-content table').first.evaluate('(t) => !t.hasAttribute("width") && !t.style.width && !t.style.tableLayout')
                checks.append({'arriving_html_and_repeated_normalization': True})

                # Optional local replay of the full observed table; never committed or printed.
                retained = os.environ.get('WATCHER_READER_RETAINED_HTML')
                if retained:
                    from bs4 import BeautifulSoup
                    from backend.scraper.sanitizer import sanitize_html
                    table = BeautifulSoup(Path(retained).read_text('utf8'), 'html.parser').select_one('table')
                    for width in (1882, 390):
                        page.set_viewport_size({'width': width, 'height': 950})
                        page.evaluate('''html => { const body = document.querySelector('.body-loader-content');
                            body.innerHTML = html; window.normalizeArticleTypography(body); }''', sanitize_html(str(table)))
                        result = geometry(page)
                        assert all(n == 1 for row in result['values'] for n in row), result
                        assert result['documentWidth'] <= width + 1, result
                        page.screenshot(path=str(output / f'private-real-{width}.png'))
                        checks.append({'retained_real_table': True, 'rows': len(result['values']),
                            'width': width, 'all_identifiers_and_names_on_one_line': True})
            finally:
                browser.close()
        db.session.expire_all()
        assert db.session.get(Announcement, 1).content_html == HTML
        assert not errors, errors
        report = {'checks': checks, 'stored_html_unchanged': True, 'page_errors': errors,
            'actual_model_requests': 0, 'actual_official_requests': 0}
        (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), 'utf8')
        print(json.dumps({'states': len(checks), 'stored_html_unchanged': True, 'page_errors': errors}))
    finally:
        if server: server.shutdown(); server.server_close()
        fixture.tearDown()


if __name__ == '__main__': check()
