"""Exercise official roster options using public metadata in an isolated account."""
import json
import logging
import sqlite3
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, DepartmentDirectoryEntry, User, Subscription
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check'
    output.mkdir(exist_ok=True)
    with closing(sqlite3.connect((ROOT / 'data/school_watcher.db').as_uri() + '?mode=ro', uri=True)) as source:
        rows = source.execute('SELECT id,name,group_name,list_url FROM departments WHERE school_id=6 ORDER BY id').fetchall()
        links = source.execute('SELECT e.parent_id,e.department_id,e.position FROM department_directory_entries e '
                               'JOIN departments d ON d.id=e.parent_id WHERE d.school_id=6').fetchall()
    academic = [link for link in links if link[0] == 291]
    assert len(academic) == 43
    shared = next(ident for parent, ident, _ in academic if any(p != parent and i == ident for p, i, _ in links))
    with tempfile.TemporaryDirectory(prefix='watcher-directory-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'directory-browser-test',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        with app.app_context():
            db.create_all()
            user = User(username='目录交互测试', password_hash='unused')
            db.session.add_all([user, School(id=6, name='电子科技大学', url='https://www.uestc.edu.cn/', subscriber_count=1)])
            db.session.flush()
            for ident, name, group, url in rows:
                db.session.add(Department(id=ident, school_id=6, name=name, group_name=group, list_url=url))
            db.session.flush()
            for parent, ident, position in links:
                db.session.add(DepartmentDirectoryEntry(parent_id=parent, department_id=ident, position=position))
            db.session.add(Subscription(user_id=user.id, school_id=6)); db.session.commit()
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'ui-fixture'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        results = []
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                for width, height in [(1440, 940), (390, 844)]:
                    context = browser.new_context(viewport={'width': width, 'height': height}, color_scheme='dark', reduced_motion='reduce')
                    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                    page = context.new_page(); errors = []; documents = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: documents.append(request.url) if request.resource_type == 'document' else None)
                    page.goto(base + '/?school=6&period=all')
                    page.evaluate('window.directoryDocumentIdentity = "retained"')
                    page.locator('#openFilters').click()
                    if width >= 1200:
                        page.locator('#pinSources').click()
                    group = page.locator('[data-source-group="学院部门"]')
                    group.locator('.source-group-toggle').click()
                    unit = page.locator('[data-source-unit="291"]')
                    expect(unit.locator('.source-unit-name')).to_contain_text('43 个单位')
                    unit.locator('.source-unit-toggle').click()
                    expect(unit.locator('[data-department]')).to_have_count(43)
                    expect(unit.get_by_label('信息与通信工程学院', exact=True)).to_be_visible()
                    expect(unit.get_by_label('电子信息智能研究院', exact=True)).to_be_disabled()
                    first = unit.get_by_label('信息与通信工程学院', exact=True)
                    unit.locator('.department-option[title="信息与通信工程学院"]').click()
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    expect(first).to_be_checked()
                    page.screenshot(path=str(output / f'uestc-directory-{width}.png'), full_page=True)
                    last = unit.locator('.department-option').last
                    last.scroll_into_view_if_needed()
                    before = page.locator('#sourceTreeScroll').evaluate('(p) => p.scrollTop')
                    last.click()
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    assert abs(page.locator('#sourceTreeScroll').evaluate('(p) => p.scrollTop') - before) <= 2
                    assert page.evaluate('window.directoryDocumentIdentity') == 'retained'
                    # One unit listed in two official directories has one selected identity.
                    shared_choice = unit.locator(f'[data-department][value="{shared}"]')
                    if not shared_choice.is_checked():
                        unit.locator(f'.department-option:has(input[value="{shared}"])').click()
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    for checkbox in page.locator(f'[data-department][value="{shared}"]').all():
                        expect(checkbox).to_be_checked()
                    unit.locator('[data-unit-select]').check()
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    ids = page.evaluate('new URL(location.href).searchParams.getAll("dept")')
                    assert len(ids) == len(set(ids)) == 42
                    unit.locator('.source-unit-toggle').click()
                    expect(unit.locator('.source-unit-columns')).to_be_hidden()
                    unit.locator('.source-unit-toggle').click()
                    expect(first).to_be_checked()
                    assert len(documents) == 1 and not errors, (documents, errors)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    results.append({'width': width, 'units': 43, 'selectable': 42, 'document_requests': len(documents), 'errors': errors})
                    context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        print(json.dumps(results, ensure_ascii=False))


if __name__ == '__main__':
    check()
