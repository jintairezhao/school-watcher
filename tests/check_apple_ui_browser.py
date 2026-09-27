"""Isolated, synthetic browser acceptance for the restrained visual redesign.

No production records, browser profile, credentials, or official-site requests are
used. The temporary application serves real templates, APIs, and static assets.
"""
import json
import logging
import sys
import tempfile
import threading
import traceback
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import create_app
from backend.database.db import db
from backend.database.models import (
    Announcement, BackgroundTask, Department, DepartmentDirectoryEntry,
    School, ScrapeLog, Subscription, User, UserAnnouncementState,
)
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


def seed(app):
    """Include real-world lengths and states, with explicitly synthetic content."""
    with app.app_context():
        db.create_all()
        now = datetime.utcnow()
        user = User(username='界面验收', password_hash='unused', role='admin')
        schools = [
            School(id=1, name='中国石油大学（北京）克拉玛依校区',
                   url='https://synthetic.example.edu.cn/', subscriber_count=1),
            School(id=2, name='电子科技大学',
                   url='https://second.example.edu.cn/', subscriber_count=1),
        ]
        db.session.add_all([user, *schools])
        db.session.flush()
        units = [
            '信息与通信工程学院', '计算机科学与工程学院（网络空间安全学院）',
            '材料与能源学院', '电子信息与智能技术研究院', '化工与环境学院',
            '公共管理学院', '经济与管理学院', '外国语学院', '数学科学学院',
            '物理学院', '机械与电气工程学院', '自动化工程学院',
            '生命科学与技术学院', '航空航天学院', '国家卓越工程师学院',
            '集成电路科学与工程学院（示范性微电子学院）',
        ]
        db.session.add(Department(id=10, school_id=1, name='教学科研单位、研究机构',
            group_name='学院部门', list_url='https://synthetic.example.edu.cn/units'))
        for ident, name in enumerate(units, 11):
            db.session.add(Department(id=ident, school_id=1, name=name,
                group_name='学院部门',
                list_url='' if ident == 14 else f'https://synthetic.example.edu.cn/unit/{ident}',
                last_scraped_at=now if ident in (11, 26) else None))
        db.session.add_all([
            Department(id=100, school_id=1, name='学生工作与安全保卫部',
                       group_name='组织机构', list_url='https://synthetic.example.edu.cn/student'),
            Department(id=101, school_id=1, name='学生工作与安全保卫部-通知公告',
                       group_name='组织机构', list_url='https://synthetic.example.edu.cn/student/notices',
                       last_scraped_at=now),
            Department(id=102, school_id=1, name='学生工作与安全保卫部-学生管理',
                       group_name='组织机构', list_url='https://synthetic.example.edu.cn/student/management'),
            Department(id=201, school_id=2, name='教务处', list_url=schools[1].url),
        ])
        db.session.flush()
        db.session.add_all([DepartmentDirectoryEntry(parent_id=10, department_id=i, position=i - 11)
                            for i in range(11, 27)])
        titles = [
            '关于开展 2026 年秋季学期本科生选课与课程调整的通知',
            '2026 年研究生国家奖学金申请安排',
            '学术讲座：信息技术与可持续发展的交叉研究',
            '关于开放实验室预约及安全培训的通知',
        ]
        for source_id, count in [(11, 3), (26, 4), (101, 2)]:
            for index in range(count):
                db.session.add(Announcement(
                    school_id=1, department_id=source_id,
                    title=titles[index] + '（交互验收数据）',
                    url=f'https://synthetic.example.edu.cn/notices/{source_id}/{index}',
                    published_at=now - timedelta(hours=index * 8 + source_id % 5),
                    summary='请关注办理时间与申请要求，相关安排以学校正式通知为准。此条仅为隔离界面验收数据。',
                    content_html='<h3>办理安排</h3><p>这是一条隔离浏览器验收通知，'
                                 '用于验证正文阅读、收藏与界面响应，不是学校正式公告。</p>'
                                 '<p>请在规定时间内准备材料，并通过学校官网查看完整说明。</p>',
                    content_text='这是一条隔离浏览器验收通知，用于验证正文阅读与收藏。',
                    content_cached_at=now,
                ))
            db.session.add(BackgroundTask(identity=f'collect:{source_id}', kind='collect',
                payload={'school_id': 1, 'department_id': source_id}, state='done',
                result={'new_count': 0}, updated_at=now, finished_at=now))
        db.session.add(BackgroundTask(identity='collect:12', kind='collect',
            payload={'school_id': 1, 'department_id': 12}, state='failed',
            error='官网当前返回访问校验页面，暂时无法读取通知；已有消息仍保留',
            updated_at=now, finished_at=now))
        db.session.add_all([Subscription(user_id=user.id, school_id=s.id) for s in schools])
        db.session.add_all([
            ScrapeLog(school_id=1, source_name=units[0], started_at=now,
                finished_at=now + timedelta(seconds=3), status='success', new_count=2, total_count=18),
            ScrapeLog(school_id=1, source_name=units[1], started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=2) + timedelta(seconds=5), status='failed',
                error_message='官网当前返回访问校验页面，暂时无法读取通知；已有消息仍保留'),
            ScrapeLog(school_id=2, source_name='教务处', started_at=now - timedelta(days=9),
                finished_at=now - timedelta(days=9) + timedelta(seconds=2), status='success',
                new_count=3, total_count=22),
        ])
        db.session.commit()
        return app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': user.id, '_csrf_token': 'synthetic-ui-check'})


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    failures, checks, shots, layouts, browser_errors, external_requests = [], [], [], [], [], []

    def record(name, fn):
        try:
            fn()
            checks.append(name)
        except Exception as exc:
            failures.append({'check': name, 'error': str(exc)[:1800],
                             'trace': traceback.format_exc()[-2500:]})
            try:
                photograph(page, f'failure-{len(failures)}')
            except Exception:
                pass

    def photograph(page, name):
        filename = output / f'apple-{name}.png'
        page.screenshot(path=str(filename), full_page=True)
        shots.append(str(filename))

    def layout(page, name):
        details = page.evaluate('''() => ({
            width: innerWidth, height: innerHeight,
            documentWidth: document.documentElement.scrollWidth,
            documentHeight: document.documentElement.scrollHeight,
            background: getComputedStyle(document.body).backgroundColor,
            font: getComputedStyle(document.body).fontFamily,
            overflowing: [...document.querySelectorAll('main button, main input, main select, main h1, main h2')]
                .filter(el => { const r=el.getBoundingClientRect();
                    return r.width && r.height && (r.right > innerWidth + 2 || r.left < -2)
                        && !el.closest('[hidden]') && getComputedStyle(el).visibility !== 'hidden'; })
                .map(el => ({tag:el.tagName, id:el.id, text:(el.innerText||el.getAttribute('aria-label')||'').slice(0,80)}))
        })''')
        layouts.append({'page': name, **details})
        assert details['documentWidth'] <= details['width'] + 1, details

    with tempfile.TemporaryDirectory(prefix='watcher-apple-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'synthetic-ui-fixture',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookie = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 960},
                    color_scheme='light', reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])

                def block_external(route):
                    if route.request.url.startswith(base + '/'):
                        route.continue_()
                    else:
                        external_requests.append(route.request.url)
                        route.abort()

                context.route('**/*', block_external)
                page = context.new_page()
                documents = []
                page.on('pageerror', lambda error: browser_errors.append(str(error)))
                page.on('request', lambda request: documents.append(request.url)
                        if request.resource_type == 'document' else None)
                page.goto(base + '/?school=1&period=all&dept=11')
                page.locator('#openFilters').click()
                page.locator('#pinSources').click()
                page.evaluate('window.appleUiDocument = "same-document"')

                def desktop_interaction():
                    group = page.locator('[data-source-group="学院部门"]')
                    unit = page.locator('[data-source-unit="10"]')
                    layout(page, 'desktop-inbox-light')
                    photograph(page, 'desktop-inbox-light')
                    expect(group.locator('.source-group-toggle')).to_have_attribute('aria-expanded', 'true')
                    group.locator('.source-group-toggle').press('Space')
                    expect(group.locator('.source-group-items')).to_be_hidden()
                    expect(group.locator('[data-group-selection]')).to_have_text('已选 1')
                    group.locator('.source-group-toggle').press('Space')
                    expect(unit.locator('.source-unit-columns')).to_be_visible()
                    blocked = page.locator('.department-option:has(input[value="12"])')
                    expect(blocked.locator('[data-source-status]')).to_have_text('访问受限')
                    expect(blocked.locator('.department-count')).to_have_text('—')
                    expect(page.locator('.department-option:has(input[value="13"]) [data-source-status]')).to_have_text('待采集')
                    expect(page.locator('[data-department][value="14"]')).to_be_disabled()
                    expect(page.locator('.department-option:has(input[value="11"]) .department-count')).to_have_text('3')
                    target = page.locator('.department-option:has(input[value="26"])')
                    target.scroll_into_view_if_needed()
                    before = page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop')
                    target.click()
                    page.wait_for_function('() => new URL(location.href).searchParams.getAll("dept").includes("26")')
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    assert page.evaluate('window.appleUiDocument') == 'same-document'
                    after = page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop')
                    if abs(after - before) > 2:
                        failures.append({'check': 'desktop stationary source selection',
                                         'error': f'sourcePanel scrollTop moved from {before} to {after}'})
                    expect(unit.locator('.source-unit-toggle')).to_have_attribute('aria-expanded', 'true')
                    expect(page.locator('[data-notice-link]')).to_have_count(7)
                    page.locator('[data-notice-link]').first.click()
                    expect(page.locator('.body-loader-content')).to_contain_text('隔离浏览器验收通知')
                    page.get_by_role('button', name='收藏', exact=True).click()
                    expect(page.get_by_role('button', name='取消收藏', exact=True)).to_be_visible()
                    with app.app_context():
                        assert UserAnnouncementState.query.filter_by(starred=True).count() == 1
                    assert len(documents) == 1, documents
                    page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop = 0')
                    layout(page, 'desktop-reader-light')
                    photograph(page, 'desktop-reader-light')
                    light_bg = page.evaluate('getComputedStyle(document.body).backgroundColor')
                    page.locator('#themeToggle').click()
                    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                    page.wait_for_function('previous => getComputedStyle(document.body).backgroundColor !== previous', arg=light_bg)
                    photograph(page, 'desktop-reader-dark')
                    page.locator('#themeToggle').click()
                    expect(page.locator('html')).to_have_attribute('data-theme', 'light')

                record('desktop hierarchy, source states, partial navigation, scroll, reading, saved state, themes', desktop_interaction)

                def mobile_interaction():
                    page.set_viewport_size({'width': 390, 'height': 844})
                    if page.locator('.inbox-workspace').get_attribute('class').find('detail-requested') >= 0:
                        page.keyboard.press('Escape')
                    expect(page.locator('#openFilters')).to_be_visible()
                    layout(page, 'mobile-inbox')
                    photograph(page, 'mobile-inbox')
                    page.locator('#openFilters').click()
                    expect(page.locator('#sourcePanel')).to_have_class('source-panel is-open')
                    page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop = 0')
                    photograph(page, 'mobile-filters')
                    target = page.locator('.department-option:has(input[value="26"])')
                    target.scroll_into_view_if_needed()
                    before = page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop')
                    target.click()
                    page.wait_for_function('() => !new URL(location.href).searchParams.getAll("dept").includes("26")')
                    expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                    after = page.locator('#sourceTreeScroll').evaluate('(el) => el.scrollTop')
                    assert abs(after - before) <= 2, f'sourcePanel scrollTop moved from {before} to {after}'
                    expect(page.locator('#sourcePanel')).to_have_class('source-panel is-open')
                    assert page.evaluate('window.appleUiDocument') == 'same-document'
                    page.keyboard.press('Escape')
                    page.locator('[data-notice-link]').first.click()
                    expect(page.locator('.reader-panel')).to_be_visible()
                    expect(page.locator('.body-loader-content')).to_contain_text('隔离浏览器验收通知')
                    layout(page, 'mobile-reader')
                    photograph(page, 'mobile-reader')
                    page.keyboard.press('Escape')
                    expect(page.locator('#openFilters')).to_be_visible()

                record('mobile drawer, stationary multi-selection, reader and Escape', mobile_interaction)

                for width, height, size in [(1440, 960, 'desktop'), (390, 844, 'mobile')]:
                    page.set_viewport_size({'width': width, 'height': height})
                    for route, name, ready in [
                        ('/explore?level=subscribed', 'directory', '.directory-row'),
                        ('/me', 'account', '.subscription-list'),
                        ('/admin', 'admin', '#statGrid .stat-card'),
                        ('/admin#logs', 'admin-logs', '#panel-logs:not([hidden])'),
                    ]:
                        def inspect_route(route=route, name=name, ready=ready, size=size):
                            page.goto(base + route)
                            expect(page.locator(ready).first).to_be_visible()
                            expect(page.locator('h1').first).to_be_visible()
                            expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                            expect(page.locator('#themeToggle svg:visible')).to_have_count(1)
                            if name == 'admin':
                                page.locator('#themeToggle').click()
                                expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                                expect(page.locator('#themeToggle svg:visible')).to_have_count(1)
                                page.locator('#themeToggle').click()
                            if name == 'admin-logs':
                                expect(page.locator('#panel-logs')).to_contain_text('信息与通信工程学院')
                                assert page.locator('#archiveLogSection').get_attribute('open') is None
                            layout(page, f'{size}-{name}')
                            photograph(page, f'{size}-{name}')
                            if name == 'admin-logs':
                                page.locator('#archiveLogSection summary').click()
                                expect(page.locator('#archiveLogSchools')).to_contain_text('电子科技大学')
                        record(f'{size} {name} layout', inspect_route)
                for width in (981, 1024, 768):
                    def inspect_breakpoint(width=width):
                        page.set_viewport_size({'width': width, 'height': 960})
                        page.goto(base + '/?school=1&period=all&dept=11&selected=1')
                        expect(page.locator('.body-loader-content')).to_contain_text('隔离浏览器验收通知')
                        layout(page, f'breakpoint-{width}-reader')
                    record(f'{width} reader breakpoint geometry', inspect_breakpoint)
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
    report = {'checks': checks, 'failures': failures, 'browser_errors': browser_errors,
              'external_requests': external_requests, 'viewports': [1440, 390],
              'layouts': layouts, 'screenshots': shots}
    (output / 'apple-browser-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert not failures and not browser_errors and not external_requests, 'See apple-browser-report.json'


if __name__ == '__main__':
    check()
