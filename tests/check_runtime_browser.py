"""Final batched browser check: queued bodies, retry, compact catalogue, mobile and desktop."""
from datetime import datetime
import json
import logging
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import User, School, Department, Subscription, Announcement, BackgroundTask
from backend.services import tasks
from backend.worker import execute
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    output = ROOT / 'data/ui-check'
    catalog = ROOT / 'data/source_catalog.build.sqlite3'
    if not catalog.exists():
        catalog = ROOT / 'data/source_catalog.sqlite3'
    with tempfile.TemporaryDirectory(prefix='watcher-runtime-browser-') as temp:
        folder = Path(temp)
        app = create_app({'TESTING': True, 'SECRET_KEY': 'runtime-browser-only',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(folder / 'browser.db'),
            'SOURCE_CATALOG_PATH': str(catalog), 'STORAGE_ROOT': str(folder)})
        with app.app_context():
            db.create_all()
            user = User(username='运行检查', password_hash='unused', role='admin')
            school = School(name='中国石油大学（北京）克拉玛依校区', url='https://www.cupk.edu.cn/', subscriber_count=1)
            db.session.add_all([user, school]); db.session.flush()
            dept = Department(school_id=school.id, name='教务通知', list_url='https://www.cupk.edu.cn/jwb/', content_selector='article')
            db.session.add(dept); db.session.flush()
            ann = Announcement(school_id=school.id, department_id=dept.id, title='按需正文读取（界面测试）',
                               published_at=datetime.utcnow(), url='https://www.cupk.edu.cn/jwb/test.htm')
            db.session.add_all([ann, Subscription(user_id=user.id, school_id=school.id)])
            db.session.commit()
            school_id, ann_id = school.id, ann.id
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'ui-check'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 900}, reduced_motion='reduce')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                page = context.new_page(); errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + f'/?period=all&selected={ann_id}')
                page.get_by_text('正在读取官网正文，你也可以先打开原文阅读。').wait_for()
                for width in (390, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / f'body-loading-{width}.png'), full_page=True)
                with app.app_context():
                    task = BackgroundTask.query.filter_by(identity=f'content:{ann_id}').one()
                    task.state = 'failed'; task.error = '测试离线状态'
                    db.session.get(Announcement, ann_id).content_error = '测试离线状态'
                    db.session.commit()
                page.get_by_role('button', name='重新加载正文').wait_for()
                page.get_by_role('button', name='重新加载正文').click()
                page.get_by_text('正在读取官网正文，你也可以先打开原文阅读。').wait_for()
                with app.app_context():
                    handle = tasks.claim()
                with patch('backend.scraper.engine._fetch_html', return_value='<article><p>正文已通过独立任务加载。</p><script>bad()</script></article>'):
                    execute(app, handle)
                page.get_by_text('正文已通过独立任务加载。').wait_for()
                for width in (390, 1440):
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(base + f'/announcement/{ann_id}')
                    assert page.get_by_text('正文已通过独立任务加载。').count() == 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / f'body-loaded-{width}.png'), full_page=True)
                    page.goto(base + f'/schools/{school_id}/structure')
                    assert page.locator('h1').count() == 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.goto(base + '/admin/storage')
                    assert page.get_by_role('heading', name='存储管理').count() == 1
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    page.screenshot(path=str(output / f'storage-{width}.png'), full_page=True)
                assert not errors, errors
                context.close(); browser.close()
                print(json.dumps({'browser_errors': errors, 'widths': [390, 1440],
                                  'checks': ['queued_body', 'failed_retry', 'sanitized_loaded_body', 'compact_catalogue', 'storage']}, ensure_ascii=False))
        finally:
            server.shutdown(); thread.join(timeout=5); server.server_close()
            with app.app_context():
                db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    check()
