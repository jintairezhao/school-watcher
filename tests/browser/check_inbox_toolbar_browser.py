"""Isolated real-browser checks for compact filters and resumable refresh status."""
import json
import logging
import re
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, User
from PIL import Image
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server


class DurableStatus:
    """Represents server state across documents, without running any collector."""
    def __init__(self):
        self.posts = []
        self.post_attempts = []
        self.fail_posts = 0
        self.failed_posts = 0
        self.gets = []
        self.syncs = []
        self.active = False
        self.complete = False
        self.fail_gets = 0
        self.scope = 'all'
        self.started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()

    def result(self, scope):
        sources = [{'id': 11, 'name': '信息与通信工程学院', 'state': 'done',
                    'status_label': '', 'new_count': 0, 'message': '',
                    'updated_at': '2026-09-22T08:00:00', 'last_synced_at': '2026-09-22T08:00:00'}]
        if self.active and not self.complete and self.scope == 'current':
            sources[0].update(state='running', status_label='更新中')
        if scope == 'all':
            running = self.active and not self.complete
            sources.append({'id': 21, 'name': '另一所学校的教务处',
                'state': 'running' if running else 'done',
                'status_label': '更新中' if running else '', 'new_count': 2 if self.complete else 0,
                'message': '', 'updated_at': '2026-09-22T08:05:00' if self.complete else '2026-09-22T08:00:00',
                'last_synced_at': None if running else '2026-09-22T08:05:00'})
        active = sum(s['state'] == 'running' for s in sources)
        return {'sources': sources, 'total': len(sources), 'active': active,
                'failed': 0, 'done': len(sources) - active,
                'new_count': sum(s['new_count'] for s in sources),
                'tracked': self.active,
                'tracking': {'scope': self.scope, 'scope_label': '全部订阅' if self.scope == 'all' else '本次更新',
                             'started_at': self.started_at if self.active else None}}

    def handle(self, route):
        request = route.request
        if urlsplit(request.url).path == '/api/inbox/sync':
            assert request.method == 'POST'
            self.syncs.append(request.post_data_json)
            route.fulfill(json={**self.result(self.scope), 'scheduled': 0})
            return
        if request.method == 'POST':
            payload = request.post_data_json
            self.post_attempts.append(payload)
            if self.fail_posts:
                self.fail_posts -= 1
                self.failed_posts += 1
                route.abort('connectionfailed')
                return
            self.posts.append(payload)
            self.active = True
            self.complete = False
            self.scope = payload.get('scope', 'current')
            self.started_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
            route.fulfill(json=self.result(payload.get('scope', 'current')))
        else:
            query = parse_qs(urlsplit(request.url).query)
            resume = query.get('resume') == ['1']
            scope = self.scope if resume and self.active else query.get('scope', ['current'])[0]
            self.gets.append({'scope': scope, 'resume': resume, 'url': request.url, 'failed': bool(self.fail_gets)})
            if self.fail_gets:
                self.fail_gets -= 1
                route.fulfill(status=503, json={'error': '状态网络暂时中断'})
            else:
                route.fulfill(json=self.result(scope))


def seed(app):
    with app.app_context():
        db.create_all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        user = User(username='刷新与筛选验收', password_hash='unused', role='admin')
        ordinary = User(username='普通读者验收', password_hash='unused', role='user')
        db.session.add_all([user, ordinary,
            School(id=1, name='中国石油大学（北京）克拉玛依校区', url='https://synthetic.example.edu.cn/', subscriber_count=1),
            School(id=2, name='另一所测试学校', url='https://second.example.edu.cn/', subscriber_count=1)])
        db.session.flush()
        db.session.add_all([
            Department(id=11, school_id=1, name='信息与通信工程学院', group_name='院系设置',
                       list_url='https://synthetic.example.edu.cn/news', last_scraped_at=now),
            Department(id=21, school_id=2, name='教务处', group_name='组织机构',
                       list_url='https://second.example.edu.cn/news', last_scraped_at=now)])
        db.session.flush()
        for index in range(5):
            db.session.add(Announcement(school_id=1, department_id=11,
                title=f'秋季学期第 {index + 1} 项教学与课程安排（隔离验收数据）',
                url=f'https://synthetic.example.edu.cn/notices/{index}',
                published_at=now - timedelta(hours=index * 3),
                summary='请关注办理日期与课程安排。此条为隔离浏览器验收数据。',
                content_html='<p>隔离工具栏与刷新验收正文。</p>',
                content_text='隔离工具栏与刷新验收正文。', content_cached_at=now))
        db.session.add_all([Subscription(user_id=reader.id, school_id=i)
                            for reader in (user, ordinary) for i in (1, 2)])
        db.session.commit()
        return app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'toolbar-fixture'})


def check_baseline(baseline=True):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    evidence, failures, screenshots = [], [], []
    with tempfile.TemporaryDirectory(prefix='watcher-toolbar-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'toolbar-isolated-fixture',
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
                for scenario in ('leave-and-return', 'temporary-get-failure', 'bfcache-pageshow'):
                    status = DurableStatus()
                    context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                    context.route('**/*', lambda route: route.continue_()
                                  if route.request.url.startswith(base + '/') else route.abort())
                    context.route('**/api/inbox/refresh**', status.handle)
                    context.route('**/api/inbox/sync', status.handle)
                    page = context.new_page()
                    page.goto(base + '/?school=1&dept=11')
                    expect(page.locator('#inboxRefreshActivity')).to_be_hidden()
                    page.locator('#toggleNoticeActions').click()
                    page.locator('#collectAllSources').click()
                    expect(page.locator('#inboxRefreshStatus')).to_contain_text('全部订阅')
                    assert status.active and not status.complete
                    if scenario == 'leave-and-return':
                        page.goto(base + '/explore')
                        page.goto(base + '/?school=1&dept=11')
                        page.wait_for_timeout(400)
                        evidence.append({'scenario': scenario, 'status': page.locator('#inboxRefreshStatus').inner_text(),
                            'last_get_scope': status.gets[-1]['scope'], 'post_count': len(status.posts),
                            'server_global_active': status.result('all')['active']})
                    elif scenario == 'temporary-get-failure':
                        status.fail_gets = 1
                        expect(page.locator('#inboxRefreshStatus')).to_contain_text('连接中断', timeout=8000)
                        before = len(status.gets)
                        page.wait_for_timeout(4500)
                        evidence.append({'scenario': scenario, 'get_count_after_error': before,
                            'get_count_after_recovery_window': len(status.gets), 'post_count': len(status.posts),
                            'status': page.locator('#inboxRefreshStatus').inner_text()})
                    else:
                        page.evaluate('''() => {
                            window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
                            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
                        }''')
                        before = len(status.gets)
                        page.wait_for_timeout(2600)
                        evidence.append({'scenario': scenario, 'get_count_at_restore': before,
                            'get_count_after_restore': len(status.gets), 'post_count': len(status.posts),
                            'status': page.locator('#inboxRefreshStatus').inner_text()})
                    context.close()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()
    report = {'baseline': baseline, 'evidence': evidence, 'failures': failures, 'screenshots': screenshots}
    path = output / ('inbox-toolbar-baseline.json' if baseline else 'inbox-toolbar-browser-report.json')
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


def check(confirm_only=False):
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    checks, failures, screenshots, metrics, evidence, errors, external = [], [], [], [], [], [], []

    def photograph(page, label):
        if confirm_only:
            return
        path = output / f'inbox-toolbar-{label}.png'
        page.screenshot(path=str(path), full_page=True)
        screenshots.append(str(path))
        if label.endswith('-default'):
            # The native search cancel control can reserve width even when
            # empty, so computed text-align alone does not prove visual centering.
            rect = page.locator('.inbox-search').bounding_box()
            picture = Image.open(path).convert('RGB')
            dark = page.locator('html').get_attribute('data-theme') == 'dark'
            ink = []
            for y in range(int(rect['y']) + 9, int(rect['y'] + rect['height']) - 9):
                for x in range(int(rect['x']) + 46, int(rect['x'] + rect['width']) - 46):
                    rgb = picture.getpixel((x, y))
                    if (min(rgb) > 80 if dark else max(rgb) < 180):
                        ink.append(x)
            assert ink, 'Search placeholder did not fit visibly inside the field'
            offset = abs((min(ink) + max(ink)) / 2 - (rect['x'] + rect['width'] / 2))
            metrics.append({'view': label, 'placeholder_visual_center_delta': offset})
            assert offset <= 2, f'Search placeholder is visually off center by {offset:.1f}px'

    def wait_until(page, predicate, message, timeout=10):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            if predicate():
                return
            page.wait_for_timeout(100)
        assert predicate(), message

    def geometry(page, label):
        result = page.evaluate('''() => {
            const field = document.querySelector('#inboxQuery');
            const form = field.closest('.inbox-search').getBoundingClientRect();
            const box = field.getBoundingClientRect();
            const css = getComputedStyle(field);
            const contentLeft = box.left + parseFloat(css.paddingLeft) + parseFloat(css.borderLeftWidth);
            const contentRight = box.right - parseFloat(css.paddingRight) - parseFloat(css.borderRightWidth);
            return {width:innerWidth, height:innerHeight,
            documentWidth:document.documentElement.scrollWidth,
            toolbarHeight:document.querySelector('.notice-toolbar').getBoundingClientRect().height,
            listTop:document.querySelector('#noticeList').getBoundingClientRect().top,
            searchAlign:css.textAlign,
            searchCenterDelta:Math.abs((contentLeft + contentRight)/2 - (form.left + form.right)/2),
            searchVerticalDelta:Math.abs((box.top + box.bottom)/2 - (form.top + form.bottom)/2)};
        }''')
        metrics.append({'view': label, **result})
        assert result['documentWidth'] <= result['width'], result
        assert result['searchAlign'] == 'center', result
        assert result['searchCenterDelta'] <= 1 and result['searchVerticalDelta'] <= 1, result
        return result

    with tempfile.TemporaryDirectory(prefix='watcher-toolbar-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'toolbar-isolated-fixture',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookie = seed(app)
        with app.app_context():
            ordinary = User.query.filter_by(role='user').one()
            ordinary_cookie = app.session_interface.get_signing_serializer(app).dumps(
                {'user_id': ordinary.id, '_csrf_token': 'ordinary-toolbar-fixture'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                scenarios = ((1440, 960, 'light'),) if confirm_only else (
                    (1440, 960, 'light'), (1440, 960, 'dark'), (390, 844, 'light'), (390, 844, 'dark'))
                for width, height, theme in scenarios:
                    name = ('desktop' if width == 1440 else 'mobile') + '-' + theme
                    status = DurableStatus()
                    documents = []
                    context = browser.new_context(viewport={'width': width, 'height': height}, reduced_motion='reduce')
                    context.set_default_timeout(8000)
                    context.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
                    context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])

                    def local_only(route):
                        if route.request.url.startswith(base + '/'):
                            route.continue_()
                        else:
                            external.append(route.request.url)
                            route.abort()

                    context.route('**/*', local_only)
                    context.route('**/api/inbox/refresh**', status.handle)
                    context.route('**/api/inbox/sync', status.handle)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda req: documents.append(req.url) if req.resource_type == 'document' else None)
                    try:
                        page.goto(base + '/?school=1&dept=11')
                        page.evaluate('window.toolbarDocumentIdentity="same-document"')
                        expect(page.locator('html')).to_have_attribute('data-theme', theme)
                        expect(page.locator('#inboxQuery')).to_be_visible()
                        wait_until(page, lambda: len(status.syncs) == 1, 'Opening inbox did not check the shared collection interval')
                        assert status.syncs == [{'scope': 'all'}] and not status.posts
                        expect(page.locator('#toggleNoticeFilters')).to_be_visible()
                        expect(page.locator('#refreshCurrentSources')).to_be_visible()
                        expect(page.locator('#toggleNoticeActions')).to_be_visible()
                        expect(page.locator('#noticeFilterPanel')).to_be_hidden()
                        expect(page.locator('#noticeActionsMenu')).to_be_hidden()
                        expect(page.locator('#yearFilter')).to_be_hidden()
                        expect(page.locator('[data-read="unread"]')).to_be_hidden()
                        expect(page.locator('#inboxRefreshActivity')).to_be_hidden()
                        before = geometry(page, name + '-default')
                        photograph(page, name + '-default')

                        page.locator('#toggleNoticeFilters').click()
                        expect(page.locator('#noticeFilterPanel')).to_be_visible()
                        expect(page.locator('#toggleNoticeFilters')).to_have_attribute('aria-expanded', 'true')
                        expect(page.locator('#yearFilter')).to_be_visible()
                        after = geometry(page, name + '-filters')
                        assert abs(after['listTop'] - before['listTop']) <= 1, 'Filter popover pushed the list down'
                        bounds = page.locator('#noticeFilterPanel').bounding_box()
                        assert bounds['x'] >= 0 and bounds['x'] + bounds['width'] <= width + 1, bounds
                        photograph(page, name + '-filters')
                        page.keyboard.press('Escape')
                        expect(page.locator('#noticeFilterPanel')).to_be_hidden()
                        expect(page.locator('#toggleNoticeFilters')).to_be_focused()
                        page.locator('#toggleNoticeFilters').click()
                        page.locator('.toolbar-topline p').click()
                        expect(page.locator('#noticeFilterPanel')).to_be_hidden()

                        page.locator('#toggleNoticeFilters').click()
                        page.locator('#noticeFilterPanel [data-period="all"]').click()
                        page.wait_for_function('() => new URL(location.href).searchParams.get("period")==="all"')
                        expect(page.locator('#activeFilterSummary')).to_contain_text('全部时间')
                        if not page.locator('#noticeFilterPanel').is_visible():
                            page.locator('#toggleNoticeFilters').click()
                        page.locator('#noticeFilterPanel [data-read="unread"]').click()
                        page.wait_for_function('() => new URL(location.href).searchParams.get("read")==="unread"')
                        expect(page.locator('#activeFilterSummary')).to_contain_text('未读')
                        page.keyboard.press('Escape')
                        assert page.evaluate('window.toolbarDocumentIdentity') == 'same-document'
                        assert len(documents) == 1, documents
                        assert not status.posts, 'Changing view-only filters submitted a collection'
                        assert len(status.syncs) == 1, 'Changing view-only filters repeated the interval check'

                        page.locator('#toggleNoticeActions').click()
                        expect(page.locator('#noticeActionsMenu')).to_be_visible()
                        expect(page.locator('#collectAllSources')).to_be_visible()
                        expect(page.locator('#markScopeRead')).to_be_visible()
                        page.keyboard.press('Escape')
                        expect(page.locator('#noticeActionsMenu')).to_be_hidden()
                        expect(page.locator('#toggleNoticeActions')).to_be_focused()
                        page.locator('#toggleNoticeActions').click()
                        page.locator('#collectAllSources').click()
                        expect(page.locator('#inboxRefreshActivity')).to_be_visible()
                        expect(page.locator('#inboxRefreshStatus')).to_contain_text('全部订阅')
                        expect(page.locator('#inboxRefreshProgress')).to_have_attribute('max', '2')
                        expect(page.locator('#inboxRefreshProgress')).to_have_attribute('value', '1')
                        expect(page.locator('#inboxRefreshStatus')).not_to_contain_text('更新完成')
                        assert len(status.posts) == 1 and status.posts[0] == {'scope': 'all'}
                        progress_position = geometry(page, name + '-progress')
                        photograph(page, name + '-progress')
                        page.locator('#inboxRefreshSummary').click()
                        expect(page.locator('#inboxRefreshSources')).to_be_visible()
                        expect(page.locator('#inboxRefreshSources')).to_contain_text('另一所学校的教务处')
                        expanded_position = geometry(page, name + '-progress-details')
                        assert abs(expanded_position['listTop'] - progress_position['listTop']) <= 1, 'Progress details pushed the list down'
                        photograph(page, name + '-progress-details')
                        page.keyboard.press('Escape')

                        return_url = page.url
                        page.goto(base + '/explore')
                        page.goto(return_url)
                        expect(page.locator('#inboxRefreshStatus')).to_contain_text('全部订阅')
                        expect(page.locator('#inboxRefreshProgress')).to_have_attribute('value', '1')
                        assert len(status.posts) == 1, 'Returning to inbox submitted another refresh'
                        assert len(status.syncs) == 2, 'A fresh document must consult the shared interval once'
                        page.reload()
                        expect(page.locator('#inboxRefreshStatus')).to_contain_text('全部订阅')
                        expect(page.locator('#inboxRefreshProgress')).to_have_attribute('value', '1')
                        assert len(status.posts) == 1, 'Reloading an active refresh duplicated its POST'
                        assert len(status.syncs) == 3, 'F5 did not consult the shared interval once'

                        if name == 'desktop-light':
                            status.fail_gets = 1
                            wait_until(page, lambda: any(item['failed'] for item in status.gets), 'No simulated network failure occurred')
                            failed_gets = len(status.gets)
                            expect(page.locator('#inboxRefreshStatus')).not_to_contain_text('更新完成')
                            expect(page.locator('#inboxRefreshProgress')).to_have_attribute('value', '1')
                            wait_until(page, lambda: len(status.gets) > failed_gets, 'Polling did not recover after a temporary failure', timeout=12)
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('全部订阅')
                            before_restore = len(status.gets)
                            checks_before_restore = len(status.syncs)
                            page.evaluate('''() => {
                                window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
                                window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
                            }''')
                            wait_until(page, lambda: len(status.gets) > before_restore, 'pageshow did not resume status queries', timeout=4)
                            assert status.gets[-1]['resume'] and len(status.posts) == 1
                            assert len(status.syncs) == checks_before_restore, 'bfcache restoration rechecked collection eligibility'
                            status.complete = True
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('完成', timeout=8000)
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('2')
                            assert len(status.posts) == 1, 'Resuming or recovering submitted a duplicate refresh'
                            evidence.append({'case': 'temporary network and persisted pageshow',
                                'get_count_at_failure': failed_gets, 'get_count_after_recovery': len(status.gets),
                                'post_count': len(status.posts), 'completed_status': page.locator('#inboxRefreshStatus').inner_text()})
                            # Reading navigation must not submit collection work,
                            # even when returning from an unscoped saved view.
                            posts_before_navigation = len(status.post_attempts)
                            documents_before_navigation = len(documents)
                            page.locator('.mailbox-views a').filter(has_text='我的收藏').click()
                            page.wait_for_function('() => new URL(location.href).searchParams.get("view")==="saved"')
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('完成')
                            page.locator('.mailbox-views a').filter(has_text='收件箱').click()
                            page.wait_for_function('() => !new URL(location.href).searchParams.has("view")')
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('完成')
                            page.go_back()
                            page.wait_for_function('() => new URL(location.href).searchParams.get("view")==="saved"')
                            expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                            page.go_back()
                            page.wait_for_function('() => new URL(location.href).searchParams.get("school")==="1"')
                            expect(page.locator('#sourceFilterStatus')).to_have_text('已更新')
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('完成')
                            assert len(status.post_attempts) == posts_before_navigation, 'Saved/inbox/history navigation submitted collection work'
                            assert len(status.syncs) == 3, 'Saved/inbox/history navigation repeated the interval check'
                            assert len(documents) == documents_before_navigation, 'Reading navigation reloaded the document'
                            evidence.append({'case': 'saved to inbox and two history returns',
                                'post_count_before': posts_before_navigation,
                                'post_count_after': len(status.post_attempts), 'extra_documents': 0})

                            # A failed POST never reaches the durable task store;
                            # querying the old completed tracker cannot confirm it.
                            old_started_at = status.started_at
                            status.fail_posts = 1
                            page.locator('#toggleNoticeActions').click()
                            page.locator('#collectCurrentSources').click()
                            wait_until(page, lambda: status.failed_posts == 1, 'Simulated POST did not fail')
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('未确认')
                            page.wait_for_timeout(2300)
                            expect(page.locator('#inboxRefreshStatus')).to_contain_text('未确认')
                            expect(page.locator('#inboxRefreshStatus')).not_to_contain_text('更新完成')
                            expect(page.locator('#collectCurrentSources')).to_be_enabled()
                            assert len(status.posts) == 1 and status.complete and status.started_at == old_started_at
                            evidence.append({'case': 'POST not delivered with old completed tracker',
                                'attempt_count': len(status.post_attempts), 'accepted_post_count': len(status.posts),
                                'status': page.locator('#inboxRefreshStatus').inner_text(), 'old_tracker_unchanged': True})
                            page.locator('#toggleNoticeActions').click()
                            page.locator('#collectCurrentSources').click()
                            expect(page.locator('#inboxRefreshProgress')).to_have_attribute('max', '1')
                            expect(page.locator('#inboxRefreshProgress')).to_have_attribute('value', '0')
                            expect(page.locator('#inboxRefreshStatus')).not_to_contain_text('未确认')
                            assert len(status.posts) == 2 and len(status.post_attempts) == 3
                            assert status.scope == 'current' and status.started_at != old_started_at
                            evidence.append({'case': 'explicit retry after unconfirmed POST',
                                'attempt_count': len(status.post_attempts), 'accepted_post_count': len(status.posts),
                                'progress': '0/1', 'scope': status.scope})
                        evidence.append({'case': name, 'post_count': len(status.posts),
                            'resume_gets': sum(item['resume'] for item in status.gets),
                            'source_scope_after_return': status.scope})
                        checks.append(name + ': compact filters, overlay geometry, keyboard focus, global refresh restoration')
                    except Exception as exc:
                        failures.append({'view': name, 'error': str(exc), 'trace': traceback.format_exc()[-2600:]})
                        photograph(page, name + '-failure')
                    finally:
                        context.close()
                if not confirm_only:
                    status = DurableStatus()
                    context = browser.new_context(viewport={'width': 1440, 'height': 960}, reduced_motion='reduce')
                    context.add_cookies([{'name': 'session', 'value': ordinary_cookie, 'url': base}])
                    context.route('**/*', local_only)
                    context.route('**/api/inbox/refresh**', status.handle)
                    context.route('**/api/inbox/sync', status.handle)
                    page = context.new_page()
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    ordinary_requests = []
                    page.on('request', lambda req: ordinary_requests.append({'url': req.url, 'method': req.method}))
                    try:
                        page.goto(base + '/?school=1&dept=11')
                        wait_until(page, lambda: len(status.syncs) == 1, 'Ordinary opening did not check the interval')
                        expect(page.locator('#collectCurrentSources')).to_have_count(0)
                        expect(page.locator('#collectAllSources')).to_have_count(0)
                        expect(page.locator('#inboxRefreshActivity')).to_be_hidden()
                        title = '后台新保存的通知应由普通刷新同步显示'
                        with app.app_context():
                            db.session.add(Announcement(school_id=1, department_id=11, title=title,
                                url='https://synthetic.example.edu.cn/notices/saved-after-open',
                                published_at=datetime.now(timezone.utc).replace(tzinfo=None)))
                            db.session.commit()
                        expect(page.locator('#noticeList')).not_to_contain_text(title)
                        for _ in range(3):
                            page.locator('#refreshCurrentSources').click()
                            expect(page.locator('#refreshCurrentSources')).to_be_enabled()
                        expect(page.locator('#noticeList')).to_contain_text(title)
                        assert not status.post_attempts and len(status.syncs) == 1, 'Ordinary manual refresh attempted collection'
                        page.locator('#toggleNoticeFilters').click()
                        page.locator('[data-period="all"]').click()
                        page.wait_for_function('() => new URL(location.href).searchParams.get("period")==="all"')
                        page.keyboard.press('Escape')
                        assert not status.post_attempts and len(status.syncs) == 1
                        page.reload()
                        wait_until(page, lambda: len(status.syncs) == 2, 'Ordinary F5 did not check the interval')
                        assert not status.post_attempts, 'Ordinary F5 called the administrator collection endpoint'
                        unexpected = [item for item in ordinary_requests if item['method'] == 'POST'
                                      and not item['url'].endswith('/api/inbox/sync')]
                        assert not unexpected, unexpected
                        evidence.append({'case': 'ordinary refresh is display synchronization',
                            'manual_refreshes': 3, 'collection_posts': len(status.post_attempts),
                            'interval_checks_before_and_after_F5': [1, len(status.syncs)],
                            'new_saved_notice_visible': True})
                        checks.append('ordinary reader: repeated refresh and filtering read saved data; F5 only checks interval')
                    except Exception as exc:
                        failures.append({'view': 'ordinary-reader', 'error': str(exc), 'trace': traceback.format_exc()[-2600:]})
                        photograph(page, 'ordinary-reader-failure')
                    finally:
                        context.close()
                browser.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
            with app.app_context():
                db.session.remove(); db.engine.dispose()
    report = {'checks': checks, 'failures': failures, 'browser_errors': errors,
              'external_requests': external, 'metrics': metrics,
              'evidence': evidence, 'screenshots': screenshots}
    report_name = 'inbox-toolbar-confirmation.json' if confirm_only else 'inbox-toolbar-browser-report.json'
    (output / report_name).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert not failures and not errors and not external, 'See ' + report_name


if __name__ == '__main__':
    if '--baseline' in sys.argv:
        check_baseline()
    else:
        check(confirm_only='--confirm-behavior' in sys.argv)
