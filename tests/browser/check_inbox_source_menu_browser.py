"""Isolated acceptance of source drawers, pinning, search and fragment navigation.

Uses synthetic schools, a temporary database and blocked external requests.
"""
import json
import logging
import os
import re
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, DepartmentDirectoryEntry, School, Subscription, User
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def seed(app):
    with app.app_context():
        db.create_all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.add_all([User(id=1, username='菜单验收', password_hash='unused'),
                           User(id=2, username='另一位读者', password_hash='unused'),
                           School(id=1, name='中国石油大学（北京）克拉玛依校区', url='https://fixture.example/'),
                           School(id=2, name='另一所验收学校', url='https://second.example/'),
                           School(id=3, name='尚无部门的验收学校', url='https://empty.example/')])
        db.session.flush()
        rows = []
        for base, name in [(100, '地球科学与工程学院'), (200, '化学工程与环境学院')] + [
                (300 + i * 10, f'第{i + 1}学院（长名称与大量目录条目验收）') for i in range(20)]:
            rows.extend([(base, name, '院系设置'), (base + 1, name + '-教学通知', '院系设置'),
                         (base + 2, name + '-研究生招生', '院系设置')])
        rows.extend([(700, '教务处', '组织机构'), (701, '教务处-考试安排', '组织机构'),
                     (702, '教务处-学籍管理', '组织机构'),
                     (800, '学院公共目录', '学院目录'), (900, '科研公共目录', '科研机构'),
                     (901, '人工智能交叉研究院', ''), (902, '官网未提供链接的单位', '')])
        for ident, name, group in rows:
            db.session.add(Department(id=ident, school_id=1, name=name, group_name=group,
                                      list_url=None if ident == 902 else f'https://fixture.example/source/{ident}'))
        db.session.add(Department(id=999, school_id=2, name='教务通知', list_url='https://second.example/news'))
        db.session.flush()
        db.session.add_all([DepartmentDirectoryEntry(parent_id=p, department_id=d, position=i)
                           for p in (800, 900) for i, d in enumerate((901, 902))])
        for ident, name, _ in rows:
            if ident in (800, 900, 902):
                continue
            db.session.add(Announcement(id=ident, school_id=1, department_id=ident,
                title=name + '：秋季学期通知（界面验收数据）', url=f'https://fixture.example/notice/{ident}',
                published_at=now, content_html='<p>本条为隔离界面验收数据，用于检查阅读与菜单交互。</p>',
                content_text='隔离界面验收正文', content_cached_at=now))
        db.session.add_all([Subscription(user_id=user_id, school_id=school_id)
                            for user_id in (1, 2) for school_id in (1, 2, 3)])
        db.session.commit()
        return [app.session_interface.get_signing_serializer(app).dumps({'user_id': i, '_csrf_token': 'source-menu-fixture'}) for i in (1, 2)]


def check(capture=True):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check/source-menu'
    output.mkdir(parents=True, exist_ok=True)
    errors, checks, screenshots, metrics = [], [], [], []
    with tempfile.TemporaryDirectory(prefix='source-menu-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'source-menu-isolated',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookies = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, channel=os.environ.get('PLAYWRIGHT_CHANNEL') or None)
                context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}])
                context.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base + '/') else route.abort())
                context.route('**/api/inbox/refresh**', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'tracked': False}))
                context.route('**/api/inbox/sync', lambda route: route.fulfill(json={'sources': [], 'active': 0, 'total': 0, 'scheduled': 0}))
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))

                def go(query='school=1&period=all'):
                    page.goto(base + '/?' + query)
                    expect(page.locator('#openFilters')).to_be_visible()

                def settled():
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    expect(page.locator('.notice-panel')).not_to_have_attribute('aria-busy', 'true')

                def mode(value):
                    expect(page.locator('.inbox-workspace')).to_have_attribute('data-source-mode', value)

                go()
                mode('closed')
                expect(page.locator('#sourcePanel')).to_be_hidden()
                expect(page.locator('.mailbox-views a')).to_have_count(3)
                expect(page.locator('#groupFilter')).to_have_count(0)
                page.locator('#openFilters').click()
                mode('overlay')
                expect(page.locator('#sourcePanel')).to_have_attribute('aria-modal', 'true')
                expect(page.locator('#closeFilters')).to_be_focused()
                page.keyboard.press('Shift+Tab')
                expect(page.locator('#pinSources')).to_be_focused()
                page.keyboard.press('Shift+Tab')
                expect(page.locator('[data-close-sources]')).to_be_focused()
                page.keyboard.press('Tab')
                expect(page.locator('#pinSources')).to_be_focused()
                page.keyboard.press('Escape')
                mode('closed')
                expect(page.locator('#openFilters')).to_be_focused()
                checks.append('default closed, direct mailbox navigation, focus trap and Escape return')

                page.locator('#openFilters').click()
                page.locator('#pinSources').click()
                mode('pinned')
                expect(page.locator('#filterScrim')).to_be_hidden()
                assert not page.locator('.notice-panel').evaluate('(e) => e.inert')
                group = page.locator('[data-source-group="院系设置"]')
                group.locator('.source-group-toggle').click()
                unit = page.locator('[data-source-unit="200"]')
                unit.locator('.source-unit-toggle').click()
                first = unit.locator('.department-option:has(input[value="201"])')
                first.scroll_into_view_if_needed()
                top = page.locator('#sourceTreeScroll').evaluate('(e) => e.scrollTop')
                first.click(); settled()
                assert abs(page.locator('#sourceTreeScroll').evaluate('(e) => e.scrollTop') - top) <= 2
                assert unit.locator('[data-unit-select]').evaluate('(e) => e.indeterminate')
                expect(page.locator('.toolbar-topline')).to_contain_text('已选 1 个栏目')
                unit.locator('[data-unit-select]').check(); settled()
                expect(page.locator('.toolbar-topline')).to_contain_text('已选 3 个栏目')
                page.go_back(); settled()
                expect(page.locator('.toolbar-topline')).to_contain_text('已选 1 个栏目')
                page.go_forward(); settled()
                page.reload(); mode('pinned')
                expect(unit.locator('.source-unit-columns')).to_be_visible()
                checks.append('pin persistence, multiselect, scroll preservation, Back/Forward')

                before = page.url
                expansion = group.locator('.source-group-toggle').get_attribute('aria-expanded')
                page.locator('#sourceQuery').fill('化学 招生')
                expect(unit).to_be_visible()
                expect(unit.locator('.department-option:visible')).to_have_count(1)
                expect(page.locator('[data-source-unit="100"]')).to_be_hidden()
                assert page.url == before
                page.locator('#sourceQuery').fill('不存在的部门')
                expect(page.locator('#sourceSearchEmpty')).to_be_visible()
                page.locator('#clearSourceSearch').click()
                expect(group.locator('.source-group-toggle')).to_have_attribute('aria-expanded', expansion)
                expect(page.locator('input[value="201"][data-department]')).to_be_checked()
                checks.append('directory search preserves filters and restores expansion')

                group.locator('[data-source-group-filter]').click(); settled()
                expect(page.locator('.toolbar-topline')).to_contain_text('院系设置')
                expect(page.locator('[data-department]:checked')).to_have_count(0)
                page.locator('#clearDepartments').click(); settled()
                page.locator('#schoolFilter').select_option('2'); settled()
                mode('pinned')
                expect(page.locator('[data-department]')).to_have_count(1)
                page.locator('#schoolFilter').select_option('3'); settled()
                expect(page.locator('#sourceTreeScroll')).to_contain_text('还没有可用')
                page.locator('#schoolFilter').select_option('1'); settled()
                expect(group.locator('.source-group-items')).to_be_visible()
                page.locator('#schoolFilter').select_option(''); settled()
                expect(page.locator('#sourceTreeScroll')).to_contain_text('选择一所学校')
                page.locator('#schoolFilter').select_option('1'); settled()
                checks.append('group links, clearing filters, school changes, empty school and all-school scope')

                page.locator('#sourceQuery').fill('人工智能交叉研究院')
                duplicates = page.locator('[data-department][value="901"]')
                expect(duplicates).to_have_count(2)
                page.locator('.department-option:has(input[value="901"])').first.click(); settled()
                for item in duplicates.all():
                    expect(item).to_be_checked()
                expect(page.locator('.toolbar-topline')).to_contain_text('已选 1 个栏目')
                page.locator('#sourceQuery').fill('官网未提供链接')
                for item in page.locator('[data-department][value="902"]').all():
                    expect(item).to_be_disabled()
                expect(page.locator('.department-option:visible').first).to_contain_text('官网未提供链接')
                page.locator('#clearSourceSearch').click()
                page.locator('#clearDepartments').click(); settled()
                checks.append('shared source identities, deduplicated counts, unavailable directory entries')

                def fail_fragment(route):
                    if route.request.headers.get('x-inbox-fragment') == '1':
                        route.fulfill(status=503, body='offline')
                    else:
                        route.continue_()
                page.route(base + '/?**', fail_fragment)
                first.click()
                expect(page.locator('#sourcePanelFeedback')).to_be_visible()
                expect(page.locator('input[value="201"][data-department]')).not_to_be_checked()
                page.unroute(base + '/?**', fail_fragment)
                page.locator('[data-retry-source]').click(); settled()
                expect(page.locator('input[value="201"][data-department]')).to_be_checked()
                expect(page.locator('#inboxFilterFeedback')).to_be_hidden(timeout=3500)
                checks.append('failed updates roll back selection, retry works, success feedback expires')

                page.evaluate('''() => {
                    const original = window.fetch; let n = 0;
                    window.fetch = async (url, init) => {
                        const order = init?.headers?.['X-Inbox-Fragment'] ? ++n : 0;
                        const response = await original(url, init);
                        if (order === 1) await new Promise(resolve => setTimeout(resolve, 500));
                        return response;
                    };
                }''')
                first.click()
                unit.locator('.department-option:has(input[value="202"])').click(); settled()
                page.wait_for_timeout(600)
                assert 'dept=202' in page.url and 'dept=201' not in page.url
                checks.append('late response cannot overwrite the latest selection')

                page.set_viewport_size({'width': 1024, 'height': 900}); mode('closed')
                page.locator('#openFilters').click(); mode('overlay')
                expect(page.locator('#pinSources')).to_be_hidden()
                page.set_viewport_size({'width': 1200, 'height': 900}); mode('pinned')
                page.locator('#closeFilters').click(); mode('closed')
                page.reload(); mode('closed')
                checks.append('narrow screens suspend pinning, wide screens restore it, explicit close persists')

                page.set_viewport_size({'width': 390, 'height': 844})
                page.locator('#openFilters').click()
                first.click(); settled(); mode('overlay')
                expect(page.locator('#closeFilters')).not_to_be_focused()
                page.locator('[data-close-sources]').click(); mode('closed')
                expect(page.locator('.toolbar-topline')).to_contain_text('已选 2 个栏目')
                page.locator('[data-notice-link]').first.click(); settled()
                expect(page.locator('.reader-panel')).to_be_visible()
                page.locator('.reader-source-trigger').click(); mode('overlay')
                page.keyboard.press('Escape'); mode('closed')
                expect(page.locator('.reader-source-trigger')).to_be_focused()
                page.keyboard.press('Escape'); settled()
                checks.append('mobile multiselect, Done, detail source entry and keyboard return')

                # The same browser's other account must not inherit this account's pin setting.
                page.set_viewport_size({'width': 1440, 'height': 960})
                page.locator('#openFilters').click(); page.locator('#pinSources').click()
                context.add_cookies([{'name': 'session', 'value': cookies[1], 'url': base}]); go(); mode('closed')
                context.add_cookies([{'name': 'session', 'value': cookies[0], 'url': base}]); go(); mode('pinned')
                page.locator('#closeFilters').click()
                checks.append('pin preferences are scoped to the signed-in account')

                # One batched visual pass: actual rendered light/dark layouts and source states.
                for width in ((1440, 1200, 1024, 390) if capture else ()):
                    page.set_viewport_size({'width': width, 'height': 844 if width == 390 else 960})
                    for theme in ('light', 'dark'):
                        go('school=1&period=all&selected=901')
                        page.evaluate('(theme) => document.documentElement.dataset.theme = theme', theme)
                        if width == 390:
                            page.locator('.reader-back').click(); settled()
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), (width, theme, 'overflow')
                        name = f'{width}-{theme}-reading'
                        path = output / (name + '.png'); page.screenshot(path=str(path)); screenshots.append(str(path))
                        metrics.append({'view': name, 'source_hidden': page.locator('#sourcePanel').is_hidden(),
                                        'toolbar_height': page.locator('.notice-toolbar').bounding_box()['height']})
                        page.locator('#openFilters').click()
                        if width >= 1200:
                            page.locator('#pinSources').click()
                        page.locator('#sourceQuery').fill('化学')
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        if (width, theme) in [(1440, 'dark'), (1200, 'light'), (1024, 'light'), (390, 'dark')]:
                            path = output / f'{width}-{theme}-sources.png'; page.screenshot(path=str(path)); screenshots.append(str(path))
                        page.locator('#closeFilters').click()
                # 200% zoom equivalent CSS viewport; keyboard can still reach every action.
                page.set_viewport_size({'width': 720, 'height': 480})
                go(); page.locator('#openFilters').click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                expect(page.locator('[data-close-sources]')).to_be_in_viewport()
                checks.append('1440/1200/1024/390 light and dark layouts, reduced viewport, no horizontal overflow' if capture else 'reduced viewport keeps source actions available without horizontal overflow')
                assert not errors, errors
                report = {'checks': checks, 'browser_errors': errors, 'screenshots': screenshots, 'metrics': metrics}
                (output / ('report.json' if capture else 'confirmation.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(report, ensure_ascii=False))
                context.close(); browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    check()
