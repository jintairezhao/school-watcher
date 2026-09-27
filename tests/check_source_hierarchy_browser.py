"""Browser regression for evidence-based recursive university source placement.

Uses a temporary database and a mocked, explicit official-placement result. All
network requests outside the temporary local application are blocked. Production
accounts, source memberships, notifications and subscriptions are never changed.
"""
import json
import logging
import re
import sqlite3
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timezone
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, School, Subscription, User
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


EARTH = {'key': 'earth', 'name': '地球科学与工程学院'}
OFFICE = {'key': 'teaching', 'name': '教务部'}
INNOVATION = {'key': 'innovation', 'name': '创新创业学院'}


def placement(group, nodes, label):
    return {'group': group, 'nodes': nodes, 'label': label}


PLACEMENTS = {
    11: [placement('院系设置', [EARTH], '通知公告')],
    12: [placement('院系设置', [EARTH], '院内新闻')],
    13: [placement('院系设置', [EARTH], '跨学科合作讲座'),
         placement('组织机构', [OFFICE, INNOVATION], '跨学科合作讲座')],
    21: [placement('组织机构', [OFFICE], '教学通知')],
    22: [placement('组织机构', [OFFICE], '教改项目')],
    31: [placement('组织机构', [OFFICE, INNOVATION], '通知公告')],
    32: [placement('组织机构', [OFFICE, INNOVATION], '办学动态')],
}
for index, name in enumerate([
    '化工与环境学院', '机械与电气工程学院', '计算机科学与工程学院',
    '资源与能源学院', '经济与管理学院', '外国语学院', '理学院', '人文学院',
    '材料科学与工程学院', '马克思主义学院', '继续教育学院',
], 40):
    PLACEMENTS[index] = [placement('院系设置', [{'key': f'unit-{index}', 'name': name}], '通知公告')]


def seed(app):
    with app.app_context():
        db.create_all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        user = User(username='层级验收', password_hash='unused')
        school = School(id=1, name='中国石油大学（北京）克拉玛依校区',
                        url='https://synthetic.example.edu.cn/', subscriber_count=1)
        db.session.add_all([user, school])
        db.session.flush()
        for ident, paths in PLACEMENTS.items():
            path = paths[0]
            parent = path['nodes'][-1]['name']
            # The old data deliberately has the wrong group. Official placements
            # must supply the true parent without rewriting persisted source IDs.
            db.session.add(Department(id=ident, school_id=1,
                name=parent + '-' + path['label'], group_name=parent,
                list_url=f'https://synthetic.example.edu.cn/source/{ident}', last_scraped_at=now))
        db.session.flush()
        for ident, paths in PLACEMENTS.items():
            label = paths[0]['label']
            db.session.add(Announcement(school_id=1, department_id=ident,
                title=f'{label}的第 {ident} 号通知（隔离验收数据）',
                url=f'https://synthetic.example.edu.cn/notice/{ident}', published_at=now,
                content_html='<p>隔离层级回归测试正文，不是学校正式通知。</p>',
                content_text='隔离层级回归测试正文，不是学校正式通知。', content_cached_at=now))
            db.session.add(BackgroundTask(identity=f'collect:{ident}', kind='collect',
                payload={'school_id': 1, 'department_id': ident}, state='done',
                result={'new_count': 0}, updated_at=now, finished_at=now))
        db.session.add(Subscription(user_id=user.id, school_id=1))
        db.session.commit()
        return app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': user.id, '_csrf_token': 'synthetic-hierarchy-fixture'})


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    checks, failures, shots, errors, external, documents, positions = [], [], [], [], [], [], []

    def screenshot(page, name):
        path = output / f'source-hierarchy-{name}.png'
        page.screenshot(path=str(path), full_page=True)
        shots.append(str(path))

    def record(name, callback):
        try:
            callback()
            checks.append(name)
        except Exception as exc:
            failures.append({'check': name, 'error': str(exc)[:2000],
                             'trace': traceback.format_exc()[-2500:]})
            try:
                screenshot(page, f'failure-{len(failures)}')
            except Exception:
                pass

    def unit(page, name):
        return page.get_by_role('button', name=re.compile('^' + re.escape(name)), include_hidden=True).locator(
            'xpath=ancestor::*[@data-source-unit][1]')

    def unit_toggle(node):
        return node.locator(':scope > .source-unit-row > .source-unit-toggle')

    def contents(page, button):
        return page.locator('[id="' + button.get_attribute('aria-controls') + '"]')

    def selection(page):
        return page.evaluate('() => new URL(location.href).searchParams.getAll("dept").map(Number).sort((a,b)=>a-b)')

    def expect_selection(page, expected):
        page.wait_for_function('expected => { const ids=new URL(location.href).searchParams.getAll("dept").map(Number).sort((a,b)=>a-b); return JSON.stringify(ids)===JSON.stringify(expected); }', arg=sorted(expected))
        expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
        assert selection(page) == sorted(set(expected)), 'URL contains duplicate source IDs'
        expect(page.locator('[data-notice-link]')).to_have_count(len(set(expected)))
        assert page.evaluate('window.hierarchyDocumentIdentity') == 'same-document', 'The document reloaded'

    def click_stationary(page, row, label):
        row.scroll_into_view_if_needed()
        before = page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop')
        prior_url = page.url
        row.click()
        page.wait_for_function('previous => location.href !== previous', arg=prior_url)
        expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
        after = page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop')
        positions.append({'action': label, 'before': before, 'after': after})
        assert abs(after - before) <= 2, f'{label}: scrollTop changed from {before} to {after}'

    def validate_ids(page):
        result = page.evaluate('''() => {
            const ids=[...document.querySelectorAll('[id]')].map(el=>el.id);
            return { duplicates:[...new Set(ids.filter((id,i)=>ids.indexOf(id)!==i))],
                badTargets:[...document.querySelectorAll('.source-group-toggle,.source-unit-toggle')]
                    .map(el=>el.getAttribute('aria-controls'))
                    .filter(id=>!id||document.querySelectorAll('#'+CSS.escape(id)).length!==1),
                width:innerWidth, documentWidth:document.documentElement.scrollWidth };
        }''')
        assert not result['duplicates'] and not result['badTargets'], result
        assert result['documentWidth'] <= result['width'], result

    with tempfile.TemporaryDirectory(prefix='watcher-hierarchy-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'synthetic-hierarchy-ui',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookie = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with patch('backend.services.source_placements.official_source_placements', return_value=PLACEMENTS):
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel='msedge', headless=True)
                    context = browser.new_context(viewport={'width': 1440, 'height': 960},
                                                  color_scheme='light', reduced_motion='reduce')
                    context.set_default_timeout(8000)
                    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])

                    def local_only(route):
                        if route.request.url.startswith(base + '/'):
                            route.continue_()
                        else:
                            external.append(route.request.url)
                            route.abort()

                    context.route('**/*', local_only)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda req: documents.append(req.url) if req.resource_type == 'document' else None)
                    page.goto(base + '/?school=1&period=all&dept=11')
                    page.locator('#openFilters').click()
                    page.locator('#pinSources').click()
                    page.evaluate('window.hierarchyDocumentIdentity = "same-document"')

                    def desktop_structure():
                        earth = unit(page, '地球科学与工程学院')
                        expect(earth).to_have_count(1)
                        expect(earth.locator('xpath=ancestor::*[@data-source-group][1]')).to_have_attribute('data-source-group', '院系设置')
                        expect(page.locator('[data-source-group="地球科学与工程学院"]')).to_have_count(0)
                        expect(earth.locator('[data-department]')).to_have_count(3)
                        expect(earth.locator('.department-name').filter(has_text='通知公告')).to_have_count(1)
                        expect(earth.locator('.department-name').filter(has_text='院内新闻')).to_have_count(1)
                        button = unit_toggle(earth)
                        expect(button).to_have_attribute('aria-expanded', 'true')
                        button.press('Space')
                        expect(contents(page, button)).to_be_hidden()
                        button.press('Space')
                        expect(contents(page, button)).to_be_visible()
                        screenshot(page, 'desktop-earth')

                        group = page.locator('[data-source-group="组织机构"]')
                        group.locator('.source-group-toggle').click()
                        office = unit(page, '教务部')
                        unit_toggle(office).click()
                        innovation = unit(page, '创新创业学院')
                        expect(innovation.locator('xpath=ancestor::*[@data-source-unit][1]')).to_have_attribute(
                            'data-source-unit', office.get_attribute('data-source-unit'))
                        unit_toggle(innovation).click()
                        expect(innovation.locator('[data-department]')).to_have_count(3)
                        expect(office.locator('[data-department]')).to_have_count(5)
                        expect(page.locator('[data-department][value="13"]')).to_have_count(2)
                        # Closing the ancestor hides the descendant but retains its open state.
                        unit_toggle(office).click()
                        expect(contents(page, unit_toggle(innovation))).to_be_hidden()
                        unit_toggle(office).click()
                        expect(contents(page, unit_toggle(innovation))).to_be_visible()
                        validate_ids(page)
                        screenshot(page, 'desktop-nested')

                    record('official group, Earth columns, recursive office/college folds, unique DOM IDs', desktop_structure)

                    def desktop_selection():
                        office = unit(page, '教务部')
                        innovation = unit(page, '创新创业学院')
                        click_stationary(page, innovation.locator('.department-option:has(input[value="31"])'), 'desktop nested notice')
                        expect_selection(page, [11, 31])
                        click_stationary(page, innovation.locator('.department-option:has(input[value="13"])'), 'desktop shared source')
                        expect_selection(page, [11, 13, 31])
                        for checkbox in page.locator('[data-department][value="13"]').all():
                            expect(checkbox).to_be_checked()
                        assert office.locator(':scope > .source-unit-row [data-unit-select]').evaluate('(input) => input.indeterminate')
                        # Select a parent with its own columns plus nested child columns.
                        office.locator(':scope > .source-unit-row [data-unit-select]').check()
                        expect_selection(page, [11, 13, 21, 22, 31, 32])
                        expect(innovation.locator(':scope > .source-unit-row [data-unit-select]')).to_be_checked()
                        unit(page, '地球科学与工程学院').locator(':scope > .source-unit-row [data-unit-select]').check()
                        expect_selection(page, [11, 12, 13, 21, 22, 31, 32])
                        # Unchecking either appearance must update both paths and both parents.
                        unit(page, '地球科学与工程学院').locator('.department-option:has(input[value="13"])').click()
                        expect_selection(page, [11, 12, 21, 22, 31, 32])
                        for checkbox in page.locator('[data-department][value="13"]').all():
                            expect(checkbox).not_to_be_checked()
                        page.go_back()
                        expect_selection(page, [11, 12, 13, 21, 22, 31, 32])
                        for checkbox in page.locator('[data-department][value="13"]').all():
                            expect(checkbox).to_be_checked()
                        validate_ids(page)
                        assert len(documents) == 1, documents

                    record('stationary partial selection, shared source synchronization, parent all-selection, history', desktop_selection)

                    def mobile_selection():
                        page.set_viewport_size({'width': 390, 'height': 844})
                        page.locator('#openFilters').click()
                        expect(page.locator('#sourcePanel')).to_have_class('source-panel is-open')
                        page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop = 0')
                        screenshot(page, 'mobile-earth')
                        innovation = unit(page, '创新创业学院')
                        current = selection(page)
                        target = innovation.locator('.department-option:has(input[value="31"])')
                        click_stationary(page, target, 'mobile nested notice')
                        wanted = [ident for ident in current if ident != 31] if 31 in current else current + [31]
                        expect_selection(page, wanted)
                        expect(page.locator('#sourcePanel')).to_have_class('source-panel is-open')
                        screenshot(page, 'mobile-nested')
                        group = page.locator('[data-source-group="组织机构"]')
                        group.locator('.source-group-toggle').click()
                        expect(contents(page, unit_toggle(innovation))).to_be_hidden()
                        group.locator('.source-group-toggle').click()
                        expect(contents(page, unit_toggle(innovation))).to_be_visible()
                        validate_ids(page)
                        page.keyboard.press('Escape')
                        page.locator('[data-notice-link]').first.click()
                        expect(page.locator('.body-loader-content')).to_contain_text('隔离层级回归测试正文')
                        assert page.evaluate('window.hierarchyDocumentIdentity') == 'same-document'
                        assert len(documents) == 1, documents
                        validate_ids(page)

                    record('390px nested drawer selection, scroll retention, group folding and reading', mobile_selection)
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    report = {'checks': checks, 'failures': failures, 'browser_errors': errors,
              'external_requests': external, 'document_requests': len(documents),
              'scroll_positions': positions, 'viewports': [1440, 390], 'screenshots': shots}
    (output / 'source-hierarchy-browser-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert not failures and not errors and not external, 'See source-hierarchy-browser-report.json'


def real_cupk_snapshot():
    """Optional local evidence: public source metadata only, never user records."""
    output = ROOT / 'data' / 'ui-check'
    database = ROOT / 'data' / 'school_watcher.db'
    catalog = ROOT / 'data' / 'source_catalog.sqlite3'
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        school = dict(source.execute('SELECT id,name,url FROM schools WHERE id=1').fetchone())
        assert '克拉玛依' in school['name']
        departments = [dict(row) for row in source.execute(
            'SELECT id,school_id,name,list_url,group_name FROM departments WHERE school_id=1 ORDER BY id')]
    with tempfile.TemporaryDirectory(prefix='watcher-public-cupk-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'public-cupk-fixture',
            'SOURCE_CATALOG_PATH': str(catalog),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        with app.app_context():
            db.create_all()
            user = User(username='目录只读验收', password_hash='unused')
            db.session.add_all([user, School(**school, subscriber_count=1)])
            db.session.flush()
            db.session.add_all([Department(**row) for row in departments])
            db.session.add(Subscription(user_id=user.id, school_id=1))
            db.session.commit()
            cookie = app.session_interface.get_signing_serializer(app).dumps(
                {'user_id': user.id, '_csrf_token': 'public-cupk-fixture'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                context.route('**/*', lambda route: route.continue_()
                              if route.request.url.startswith(base + '/') else route.abort())
                page = context.new_page()
                page.goto(base + '/?school=1&group=院系设置')
                earth_button = page.get_by_role('button', name=re.compile('^地球科学与工程学院'))
                earth_button.click()
                earth = earth_button.locator('xpath=ancestor::*[@data-source-unit][1]')
                expect(earth.locator('xpath=ancestor::*[@data-source-group][1]')).to_have_attribute(
                    'data-source-group', '院系设置')
                expect(earth.locator('.department-name').filter(has_text='通知公告')).to_have_count(1)
                expect(earth.locator('.department-name').filter(has_text='院内新闻')).to_have_count(1)
                path = output / 'source-hierarchy-real-cupk.png'
                page.screenshot(path=str(path), full_page=True)
                result = {'school': school['name'], 'source_metadata_count': len(departments),
                          'verified_path': ['院系设置', '地球科学与工程学院'],
                          'columns': earth.locator('.department-name').all_inner_texts(),
                          'production_access': 'read-only public school/source metadata and runtime catalog',
                          'notice_data': 'not copied; empty isolated preview', 'screenshot': str(path)}
                (output / 'source-hierarchy-real-cupk.json').write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False))
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    if '--real-cupk' in sys.argv:
        real_cupk_snapshot()
    else:
        check()
