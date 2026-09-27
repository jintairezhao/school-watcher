"""Check original source signatures using saved official articles in an isolated app."""
from contextlib import closing
from datetime import datetime
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, User
from playwright.sync_api import sync_playwright
from werkzeug.serving import make_server


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    with closing(sqlite3.connect((ROOT / 'data/school_watcher.db').as_uri() + '?mode=ro', uri=True)) as source:
        source.row_factory = sqlite3.Row
        articles = [dict(row) for row in source.execute(
            'SELECT id,title,url,published_at,content_html FROM announcements WHERE id IN (7778,7784) ORDER BY id')]
    assert len(articles) == 2
    output = ROOT / 'data/ui-check'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='article-signature-ui-') as folder:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'article-signature-ui',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(folder) / 'app.db')})
        with app.app_context():
            db.create_all()
            school = School(name='中国石油大学（北京）克拉玛依校区', url='https://www.cupk.edu.cn/', subscriber_count=1)
            user = User(username='来源署名检查', password_hash='unused')
            db.session.add_all([school, user])
            db.session.flush()
            entries = []
            for original, name, channel, signature in zip(articles, ('通知公告', '院内新闻'), ('tzgg', 'ynxw'),
                                                           ('361-地球科学与工程学院', '351-石油学院')):
                department = Department(school_id=school.id, name=name, group_name='地球科学与工程学院',
                                        list_url='https://www.cupk.edu.cn/syxy/' + channel + '/')
                db.session.add(department)
                db.session.flush()
                article = Announcement(school_id=school.id, department_id=department.id, title=original['title'],
                    url=original['url'], published_at=datetime.fromisoformat(original['published_at']),
                    content_html=original['content_html'], summary='原文来源署名检查。')
                db.session.add(article)
                db.session.flush()
                entries.append((article.id, signature))
            db.session.add(Subscription(user_id=user.id, school_id=school.id))
            db.session.commit()
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'signature-check'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='msedge', headless=True)
                page = browser.new_page()
                page.context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                page.route('**/*', lambda route: route.continue_() if route.request.url.startswith(base) else route.abort())
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                for width in (390, 1440):
                    page.set_viewport_size({'width': width, 'height': 950})
                    for article_id, signature in entries:
                        for surface, path in [('reader', f'/?period=all&selected={article_id}'),
                                              ('detail', f'/announcement/{article_id}')]:
                            page.goto(base + path, wait_until='domcontentloaded')
                            provenance = page.locator('.article-origin')
                            assert provenance.inner_text() == '原文署名来源：' + signature
                            assert '地球科学与工程学院' in page.locator('body').inner_text()
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                            provenance.scroll_into_view_if_needed()
                            page.screenshot(path=str(output / f'provenance-{surface}-{article_id}-{width}.png'))
                browser.close()
                assert not errors, errors
                print(json.dumps({'widths': [390, 1440], 'surfaces': ['reader', 'detail'],
                                  'actual_saved_articles': len(entries), 'browser_errors': errors}))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    check()
