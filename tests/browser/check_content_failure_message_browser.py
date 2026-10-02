"""Two failures a reader must be able to tell apart.

A notice behind the school's sign-in and a notice the site failed to serve both
end at the same body loader. The loader used to print one generic "try again"
line for both, so a sign-in restriction looked like a program fault the reader
could retry away. This drives the real browser and checks the panel reports the
reason the server worked out, and that the two reasons do not read the same.

Uses a temporary database. Every request outside the temporary local application
is blocked; the notices never leave the machine.
"""
import json
import logging
import sys
import tempfile
import threading
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, User
from backend.scraper.auth_routes import LOGIN_REQUIRED_MESSAGE
from playwright.sync_api import expect, sync_playwright
from werkzeug.serving import make_server


TIMEOUT_ERROR = '连接或读取官网超时，请稍后重试；已有消息仍保留'
GENERIC_FAILURE = '正文暂时无法加载，可以重试或打开官网原文。'
LOGIN_NOTICE, TIMEOUT_NOTICE = 1, 2


def seed(app):
    with app.app_context():
        db.create_all()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        user = User(username='正文提示验收', password_hash='unused')
        school = School(id=1, name='示例大学', url='https://synthetic.example.edu.cn/', subscriber_count=1)
        db.session.add_all([user, school])
        db.session.flush()
        db.session.add(Department(id=1, school_id=1, name='教务处-通知公告', group_name='教务处',
                                  list_url='https://synthetic.example.edu.cn/notices', last_scraped_at=now))
        db.session.flush()
        db.session.add(Announcement(id=LOGIN_NOTICE, school_id=1, department_id=1,
            title='需要校内账号才能查看的通知', url='https://synthetic.example.edu.cn/notice/1',
            published_at=now, content_error=LOGIN_REQUIRED_MESSAGE))
        db.session.add(Announcement(id=TIMEOUT_NOTICE, school_id=1, department_id=1,
            title='官网读取超时的通知', url='https://synthetic.example.edu.cn/notice/2',
            published_at=now, content_error=TIMEOUT_ERROR))
        db.session.add(Subscription(user_id=user.id, school_id=1))
        db.session.commit()
        return app.session_interface.get_signing_serializer(app).dumps(
            {'user_id': user.id, '_csrf_token': 'synthetic-content-fixture'})


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data' / 'ui-check'
    output.mkdir(parents=True, exist_ok=True)
    checks, failures, errors, external, shots = [], [], [], [], []

    def screenshot(page, name):
        path = output / f'content-failure-{name}.png'
        page.screenshot(path=str(path), full_page=True)
        shots.append(str(path))

    def record(name, callback):
        try:
            callback()
            checks.append(name)
        except Exception as exc:
            failures.append({'check': name, 'error': str(exc)[:2000], 'trace': traceback.format_exc()[-2500:]})
            try:
                screenshot(page, f'failure-{len(failures)}')
            except Exception:
                pass

    with tempfile.TemporaryDirectory(prefix='watcher-content-ui-') as scratch:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'synthetic-content-ui',
            'SOURCE_CATALOG_PATH': str(Path(scratch) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(scratch) / 'ui.db')})
        cookie = seed(app)
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with nullcontext():
                with sync_playwright() as p:
                    browser = p.chromium.launch(channel='msedge', headless=True)
                    context = browser.new_context(viewport={'width': 1280, 'height': 900},
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

                    def open_notice(ann_id, label):
                        page.goto(base + '/?school=1&period=all&dept=1')
                        page.locator(f'[data-notice-link][data-announcement-id="{ann_id}"]').first.click()
                        status = page.locator('.body-loader-status')
                        expect(status).to_be_visible()
                        # Wait for the loader to settle on a reason rather than the
                        # placeholder it shows before the request comes back.
                        page.wait_for_function(
                            '() => { const el = document.querySelector(".body-loader-status");'
                            ' return el && !el.textContent.includes("正文尚未加载")'
                            ' && !el.textContent.includes("正在读取官网正文"); }')
                        screenshot(page, label)
                        return page.locator('.body-loader-status').inner_text()

                    def login_notice_names_the_sign_in():
                        seen = open_notice(LOGIN_NOTICE, 'login-notice')
                        assert seen == LOGIN_REQUIRED_MESSAGE, seen
                        assert GENERIC_FAILURE not in seen, seen

                    def broken_read_still_names_the_site_problem():
                        seen = open_notice(TIMEOUT_NOTICE, 'timeout-notice')
                        assert seen == TIMEOUT_ERROR, seen
                        assert seen != LOGIN_REQUIRED_MESSAGE, seen
                        assert GENERIC_FAILURE not in seen, seen

                    # Opening a notice POSTs and would queue a fresh read, which
                    # puts the panel back to "正在读取官网正文" and hides the finished
                    # failure. This check is about what the panel shows once the
                    # server reports that failure, so no new read is queued; the
                    # reason itself comes from the real route and the real browser.
                    with patch('backend.routes.content.request_content', return_value=None):
                        record('需要登录的通知显示登录限制，不是通用故障', login_notice_names_the_sign_in)
                        record('官网读取失败的通知显示官网问题，与登录限制不同', broken_read_still_names_the_site_problem)
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()

    report = {'checks': checks, 'failures': failures, 'browser_errors': errors,
              'external_requests': external, 'screenshots': shots}
    (output / 'content-failure-browser-report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    assert not failures and not errors and not external, 'See content-failure-browser-report.json'


if __name__ == '__main__':
    check()
