"""Storage action labels remain whole at narrow widths and CSS zoom.

Uses real application routes with isolated fixture storage. Only the inventory
classifier is replaced so representative GB totals need no large files.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_storage_management import StorageManagementTests
from backend.database.db import db
from backend.services.localization import stage_preferences
from playwright.sync_api import expect, sync_playwright

GROUPS = {'data': int(1.10 * 1073741824), 'cache': int(53.6 * 1048576),
          'logs': int(31.5 * 1048576), 'backups': int(2.60 * 1073741824),
          'application': int(1.39 * 1073741824)}
VALUES = ['1.10 GB', '53.6 MB', '31.5 MB', '2.60 GB', '1.39 GB']
LABELS = {'zh-CN': ['管理通知', '清理缓存'], 'en': ['Manage notices', 'Clean up cache']}
WIDTHS = [1440, 980, 640, 390, 320]

MEASURE = """() => {
    const table = document.querySelector('.storage-table');
    const box = node => {
        const r = node.getBoundingClientRect();
        return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height};
    };
    const measure = node => {
        const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
        const rects = [];
        for (let text = walker.nextNode(); text; text = walker.nextNode()) {
            if (!text.textContent.trim()) continue;
            const range = document.createRange(); range.selectNodeContents(text);
            for (const rect of range.getClientRects()) {
                if (rect.width > .1 && rect.height > .1)
                    rects.push({left:rect.left, right:rect.right, top:rect.top, bottom:rect.bottom});
            }
        }
        const tops = [];
        for (const r of rects) if (!tops.some(top => Math.abs(top-r.top) < .75)) tops.push(r.top);
        const bounds = box(node), style = getComputedStyle(node);
        return {text:node.textContent.trim(), rect:bounds, rects, lines:tops.length,
            whiteSpace:style.whiteSpace, textOverflow:style.textOverflow,
            overflowX:style.overflowX, scrollWidth:node.scrollWidth, clientWidth:node.clientWidth,
            textContained:rects.every(r => r.left >= bounds.left-.75 && r.right <= bounds.right+.75 &&
                r.top >= bounds.top-.75 && r.bottom <= bounds.bottom+.75)};
    };
    return {innerWidth, documentWidth:document.documentElement.scrollWidth,
        rootZoom:getComputedStyle(document.documentElement).zoom, table:box(table),
        actions:[...table.querySelectorAll('.btn')].map(measure),
        values:[...table.querySelectorAll('[data-storage-kind]')].map(measure),
        cells:[...table.querySelectorAll('th,td')].map(measure)};
}"""


def accessibility_rows(page):
    session = page.context.new_cdp_session(page)
    try:
        nodes = session.send('Accessibility.getFullAXTree')['nodes']
        tables = [node for node in nodes if not node.get('ignored')
                  and node.get('role', {}).get('value') == 'table']
        assert len(tables) == 1, f'Expected one actual AX table: {len(tables)}'
        by_id = {node['nodeId']: node for node in nodes}
        pending = list(tables[0].get('childIds', []))
        rows = 0
        while pending:
            node = by_id[pending.pop()]
            if not node.get('ignored') and node.get('role', {}).get('value') == 'row':
                rows += 1
            pending.extend(node.get('childIds', []))
        return rows
    finally:
        session.detach()


def failures_for(metrics, locale, writes, errors):
    failures = []
    if metrics['documentWidth'] > metrics['innerWidth'] + 1:
        failures.append('Document overflows horizontally')
    if metrics['table']['left'] < -.75 or metrics['table']['right'] > metrics['innerWidth'] + .75:
        failures.append('Table extends outside the viewport')
    if metrics['axRows'] != 6:
        failures.append(f"Actual accessibility table has {metrics['axRows']} rows, expected6")
    if [item['text'] for item in metrics['actions']] != LABELS[locale]:
        failures.append('Action text was changed or removed')
    if [item['text'] for item in metrics['values']] != VALUES:
        failures.append('Space values were changed or removed')
    for item in metrics['actions']:
        if item['whiteSpace'] != 'nowrap':
            failures.append(f"{item['text']}: computed white-space is {item['whiteSpace']}")
        if item['lines'] != 1:
            failures.append(f"{item['text']}: {item['lines']} actual text lines")
        if item['textOverflow'] == 'ellipsis' or not item['textContained']:
            failures.append(f"{item['text']}: action text clipped or ellipsized")
    for item in metrics['values']:
        if item['lines'] != 1 or not item['textContained'] or item['textOverflow'] == 'ellipsis':
            failures.append(f"{item['text']}: value split, clipped or ellipsized")
    for item in metrics['cells'] + metrics['actions']:
        if item['scrollWidth'] > item['clientWidth'] + 1:
            failures.append(f"{item['text']}: horizontal content overflow")
    if writes:
        failures.append(f'Unexpected write requests: {writes}')
    if errors:
        failures.append(f'Browser errors: {errors}')
    return failures


@contextmanager
def storage_fixture(locale):
    fixture = StorageManagementTests(); fixture.setUp()
    try:
        fixture.app.config.update(FETCH_EVIDENCE_DIR=str(fixture.root / 'fetch-evidence'),
            SOURCE_GOVERNANCE_EVIDENCE_PATH=str(fixture.root / 'governance-evidence'))
        fixture.article('table-layout')
        stage_preferences(fixture.admin.id, {'locale':locale}); db.session.commit()
        yield fixture
    finally:
        fixture.tearDown()


def run_case(browser, fixture, width, locale, zoom, screenshot=None):
    context = browser.new_context(viewport={'width':width, 'height':960}, reduced_motion='reduce')
    theme = 'dark' if (locale == 'zh-CN') == (width > 600) else 'light'
    context.add_init_script('localStorage.setItem("theme",' + json.dumps(theme) + ');')
    errors, writes = [], []

    def serve(route):
        request = route.request; parsed = urlsplit(request.url)
        assert parsed.hostname == 'localhost', request.url
        if request.method != 'GET':
            writes.append({'path':parsed.path, 'method':request.method})
        response = fixture.client.open(parsed.path + ('?' + parsed.query if parsed.query else ''),
            method=request.method, data=request.post_data_buffer,
            headers={key:value for key,value in request.headers.items()
                     if key.lower() in ('content-type', 'x-csrf-token')})
        route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

    try:
        context.route('**/*', serve)
        page = context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://localhost/admin/storage')
        expect(page.locator('.storage-page')).to_have_attribute('data-storage-ready', 'true')
        expect(page.locator('html')).to_have_attribute('lang', locale)
        expect(page.locator('#languageSelect')).to_have_value(locale)
        page.locator('#storageDetails > summary').click()
        expect(page.locator('.storage-table tbody tr')).to_have_count(5)
        # CSS zoom changes layout and available content width; deviceScaleFactor does not.
        page.evaluate('(zoom) => { document.documentElement.style.zoom = zoom; }', str(zoom))
        page.evaluate('''async () => {
            await document.fonts.ready;
            await new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
        }''')
        metrics = page.evaluate(MEASURE)
        metrics['axRows'] = accessibility_rows(page)
        failures = failures_for(metrics, locale, writes, errors)
        result = {'case':f'{width}-{locale}-zoom{zoom}', 'width':width, 'locale':locale, 'zoom':zoom,
                  'failures':failures, 'writes':writes, 'errors':errors, 'metrics':metrics}
        if screenshot and not failures:
            page.screenshot(path=str(screenshot), full_page=True)
            result['screenshot'] = str(screenshot)
        return result
    finally:
        context.close()


def check(baseline=False, capture=False, output=None):
    output = Path(output or ROOT / '.local/storage-table-layout-20261020' /
                  ('browser-before' if baseline else 'browser-after'))
    output.mkdir(parents=True, exist_ok=True)
    results, captures = [], []
    stylesheet = ROOT / 'frontend/static/css/storage.css'
    stylesheet_hash = hashlib.sha256(stylesheet.read_bytes()).hexdigest()
    with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid calls')), \
            patch('backend.routes.operations._storage_usage_groups', return_value=GROUPS), \
            sync_playwright() as pw:
        browser = pw.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
        try:
            for locale in (['zh-CN'] if baseline else ['zh-CN', 'en']):
                with storage_fixture(locale) as fixture:
                    cases = [(1440, 1)] if baseline else (
                        [(width, 1) for width in WIDTHS] +
                        [(width, zoom) for width in (390, 980) for zoom in (1.25, 1.5)])
                    for width, zoom in cases:
                        results.append(run_case(browser, fixture, width, locale, zoom))
            # Capture one bounded representative batch only after the complete semantic matrix passes.
            if capture and not baseline and not any(result['failures'] for result in results):
                for locale in ('zh-CN', 'en'):
                    with storage_fixture(locale) as fixture:
                        for width in (1440, 390):
                            path = output / f'{width}-{locale}-zoom1.png'
                            captures.append(run_case(browser, fixture, width, locale, 1, path))
        finally:
            browser.close()
    failed = [result for result in results + captures if result['failures']]
    assert hashlib.sha256(stylesheet.read_bytes()).hexdigest() == stylesheet_hash, 'Stylesheet changed during verification'
    report = {'status':'failed' if failed else 'passed', 'checks':results, 'captures':captures,
              'stylesheet_sha256':stylesheet_hash,
              'failed_cases':len(failed), 'case_count':len(results), 'capture':capture and not baseline}
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print(json.dumps({'status':report['status'], 'case_count':len(results), 'failed_cases':len(failed),
        'failures':[{key:result[key] for key in ('case','failures')} for result in failed],
        'report':str(output / 'result.json')}, ensure_ascii=False))
    assert not failed, f'{len(failed)} storage table case(s) failed; see {output / "result.json"}'


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', action='store_true', help='One old-CSS reproduction, never screenshots.')
    parser.add_argument('--capture', action='store_true', help='Capture exactly four representative passing-width cases.')
    parser.add_argument('--output')
    arguments = parser.parse_args()
    check(arguments.baseline, arguments.capture, arguments.output)
