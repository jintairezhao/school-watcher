"""Isolated browser regression for grouped sources and navigation without reloads."""
import json
import logging
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, User, Subscription, Announcement
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check'
    output.mkdir(exist_ok=True)
    # Copy only public names and group labels, never accounts or user activity.
    with closing(sqlite3.connect((ROOT / 'data/school_watcher.db').as_uri() + '?mode=ro', uri=True)) as source:
        names = source.execute('SELECT id,name,group_name FROM departments WHERE school_id=1 ORDER BY id').fetchall()
    with tempfile.TemporaryDirectory(prefix='watcher-inbox-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'inbox-ui-fixture',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        with app.app_context():
            db.create_all()
            user = User(username='交互测试', password_hash='unused')
            school = School(id=1, name='中国石油大学（北京）克拉玛依校区', url='https://example.edu.cn', subscriber_count=1)
            other = School(id=2, name='另一所测试学校', url='https://second.example.edu.cn', subscriber_count=1)
            db.session.add_all([user, school, other]); db.session.flush()
            for ident, name, group in names:
                db.session.add(Department(id=ident, school_id=school.id, name=name, group_name=group,
                                          list_url=f'https://example.edu.cn/columns/{ident}'))
                db.session.flush()
                db.session.add(Announcement(school_id=1, department_id=ident, title=name + '的通知（交互测试）',
                    url=f'https://example.edu.cn/notices/{ident}', published_at=datetime.utcnow(),
                    content_html='<p>本条为交互回归测试数据，不是学校正式通知。</p>', content_text='交互回归测试正文'))
            db.session.add(Department(id=9999, school_id=2, name='教务通知', list_url=other.url))
            db.session.add_all([Subscription(user_id=user.id, school_id=1), Subscription(user_id=user.id, school_id=2)])
            db.session.commit()
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'ui-fixture'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 940}, color_scheme='dark', reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                page = context.new_page(); errors = []; documents = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: documents.append(request.url) if request.resource_type == 'document' else None)
                page.goto(base + '/?school=1&period=all')
                page.locator('#openFilters').click()
                page.locator('#pinSources').click()
                page.evaluate('window.inboxDocumentIdentity = "same-document"')
                group = page.locator('[data-source-group="院系设置"]')
                organization = page.locator('[data-source-group="组织机构"]')
                expect(group.locator('.source-group-toggle')).to_have_attribute('aria-expanded', 'false')
                expect(organization.locator('.source-group-items')).to_be_hidden()
                organization.locator('.source-group-toggle').click()
                expect(organization.locator('.source-group-items')).to_be_visible()
                organization.locator('.source-group-toggle').press('Space')
                expect(organization.locator('.source-group-items')).to_be_hidden()
                group.locator('.source-group-toggle').click()
                unit = page.locator('[data-source-unit="22"]')
                expect(unit.locator('.source-unit-name')).to_contain_text('化工与环境学院')
                unit.locator('.source-unit-toggle').click()
                expect(unit.locator('.source-unit-columns')).to_be_visible()
                first = unit.locator('[data-department][value="201"]')
                label = unit.locator('.department-option:has(input[value="201"])')
                label.scroll_into_view_if_needed()
                before_top = page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop')
                label.click()
                page.wait_for_url('**&dept=201')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                assert page.evaluate('window.inboxDocumentIdentity') == 'same-document'
                assert abs(page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop') - before_top) <= 2
                expect(unit.locator('.source-unit-toggle')).to_have_attribute('aria-expanded', 'true')
                assert unit.locator('[data-unit-select]').evaluate('(input) => input.indeterminate')
                expect(page.locator('[data-notice-link]')).to_have_count(1)
                # Complete unit selection shares the same source IDs and does not duplicate notices.
                unit.locator('[data-unit-select]').check()
                page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").length === 5')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(page.locator('[data-notice-link]')).to_have_count(5)
                page.go_back()
                page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").length === 1')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(first).to_be_checked()
                page.go_forward()
                page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").length === 5')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                # Reading and personal actions still work after partial replacement.
                page.locator('[data-notice-link]').first.click()
                expect(page.get_by_role('button', name='收藏', exact=True)).to_be_visible()
                page.get_by_role('button', name='收藏', exact=True).click()
                expect(page.get_by_role('button', name='取消收藏', exact=True)).to_be_visible()
                page.get_by_role('button', name='归档', exact=True).click()
                page.wait_for_function('() => !new URL(location.href).searchParams.has("selected")')
                expect(page.locator('[data-notice-link]')).to_have_count(4)
                assert page.evaluate('document.documentElement.scrollHeight <= innerHeight + 2'), 'Sidebar controls escaped the viewport'
                page.screenshot(path=str(output / 'inbox-grouped-desktop.png'), full_page=True)
                # A rejected update leaves the previous selection/results and offers a working retry.
                def fail_fragment(route):
                    if route.request.headers.get('x-inbox-fragment') == '1': route.fulfill(status=503, body='offline')
                    else: route.continue_()
                page.route(base + '/?**', fail_fragment)
                unit.locator('.department-option:has(input[value="201"])').click()
                expect(page.locator('#retryInboxFilter')).to_be_visible()
                expect(first).to_be_checked()
                page.unroute(base + '/?**', fail_fragment)
                page.locator('#retryInboxFilter').click()
                page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").length === 4')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(first).not_to_be_checked()
                # A late first response must not overwrite the more recent selection.
                page.evaluate('''() => {
                    const original = window.fetch; let number = 0;
                    window.fetch = async (url, init) => {
                        const order = init?.headers?.['X-Inbox-Fragment'] ? ++number : 0;
                        const response = await original(url, init);
                        if (order === 1) await new Promise(resolve => setTimeout(resolve, 600));
                        return response;
                    };
                }''')
                unit.locator('.department-option:has(input[value="201"])').click()
                unit.locator('.department-option:has(input[value="202"])').click()
                page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").includes("201") && !new URL(location.href).searchParams.getAll("dept").includes("202")')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(unit.locator('input[value="201"]')).to_be_checked()
                expect(unit.locator('input[value="202"]')).not_to_be_checked()
                # Replaced school controls and then returning restore the correct source tree.
                page.locator('#schoolFilter').select_option('2')
                page.wait_for_url('**school=2**')
                expect(page.locator('[data-department]')).to_have_count(1)
                page.locator('#schoolFilter').select_option('1')
                page.wait_for_url('**school=1**')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                expect(unit.locator('.source-unit-toggle')).to_have_attribute('aria-expanded', 'true')
                expect(group.locator('.source-group-toggle')).to_have_attribute('aria-expanded', 'true')
                expect(organization.locator('.source-group-toggle')).to_have_attribute('aria-expanded', 'false')
                # Mobile keeps the filter drawer open and stationary during multi-selection.
                page.set_viewport_size({'width': 390, 'height': 844})
                page.get_by_role('button', name='打开来源筛选').click()
                unit.locator('.department-option:has(input[value="203"])').scroll_into_view_if_needed()
                mobile_top = page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop')
                unit.locator('.department-option:has(input[value="203"])').click()
                page.wait_for_url('**dept=203')
                expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                assert abs(page.locator('#sourceTreeScroll').evaluate('(panel) => panel.scrollTop') - mobile_top) <= 2
                expect(page.locator('#sourcePanel')).to_have_class('source-panel is-open')
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                group.locator('.source-group-toggle').click()
                expect(group.locator('.source-group-items')).to_be_hidden()
                expect(group.locator('[data-group-selection]')).to_have_text('已选 1')
                expect(unit.locator('input[value="203"]')).to_be_checked()
                page.screenshot(path=str(output / 'inbox-grouped-mobile.png'), full_page=True)
                page.keyboard.press('Escape')
                page.locator('[data-notice-link]').first.click()
                expect(page.locator('.reader-panel')).to_be_visible()
                expect(page.locator('.body-loader-content')).to_contain_text('交互回归测试数据')
                page.keyboard.press('Escape')
                expect(page.locator('.inbox-workspace')).not_to_have_class('inbox-workspace detail-requested')
                assert len(documents) == 1, documents
                page.reload()
                expect(group.locator('.source-group-toggle')).to_have_attribute('aria-expanded', 'false')
                expect(group.locator('[data-group-selection]')).to_have_text('已选 1')
                assert not errors, errors
                result = {'browser_errors': errors, 'document_requests_before_manual_reload': 1, 'widths': [1440, 390],
                          'checks': ['grouped columns', 'unit selection', 'scroll preservation', 'back and forward',
                                     'reading and saved state', 'failed request and retry', 'rapid selection', 'school change', 'mobile drawer',
                                     'parent group collapse', 'keyboard group toggle', 'collapsed selection badge', 'group state after reload']}
                (output / 'inbox-navigation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps(result, ensure_ascii=False))
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context(): db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    check()
