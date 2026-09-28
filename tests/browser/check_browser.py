"""Real browser smoke check on an isolated database with explicitly test-only data."""
import json
import sys
import tempfile
import threading
import logging
from pathlib import Path
from datetime import datetime, timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, User, Subscription, Announcement
from backend.services.announcement_sources import record_source
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright


def check():
    logging.getLogger('werkzeug').setLevel(logging.ERROR)
    out = ROOT / 'data' / 'ui-check'
    out.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='school-watcher-ui-') as directory:
        app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-ui-check',
                          'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(directory) / 'check.db'),
                          'SOURCE_INVENTORY_PATH': Path(directory) / 'sources.db'})
        with app.app_context():
            db.create_all()
            user = User(username='界面测试', password_hash='unused')
            school = School(name='清华大学', url='https://www.tsinghua.edu.cn', subscriber_count=1)
            db.session.add_all([user, school])
            db.session.flush()
            depts = [Department(school_id=school.id, name=name, group_name=group, list_url=school.url) for name, group in (
                ('本科教育', '人才培养'), ('研究生教育', '人才培养'), ('通知公告', '校园信息'), ('学术活动', '科学研究'))]
            db.session.add_all(depts)
            db.session.flush()
            titles = ['关于秋季学期选课安排的通知', '本科生奖学金申请材料提交说明', '图书馆开学服务时间调整', '学术讲座：从问题出发，探索研究方法', '研究生培养计划填报提醒']
            for i, title in enumerate(titles):
                db.session.add(Announcement(school_id=school.id, department_id=depts[i % len(depts)].id,
                    title=title + '（界面测试数据）', content_text='这条信息用于界面回归测试，不是学校正式公告。',
                    content_html='<p>这条信息用于界面回归测试，不是学校正式公告。</p><p>请以学校官网原文为准。</p>',
                    published_at=datetime.utcnow() - timedelta(hours=i), url=school.url))
            db.session.add(Subscription(user_id=user.id, school_id=school.id))
            db.session.flush()
            shared = Announcement.query.order_by(Announcement.id).first()
            record_source(shared, depts[2])
            shared_id, secondary_id = shared.id, depts[2].id
            db.session.commit()
            school_id = school.id
            cookie = app.session_interface.get_signing_serializer(app).dumps({'user_id': user.id, '_csrf_token': 'browser-test'})
        server = make_server('127.0.0.1', 0, app, threaded=True)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='msedge', headless=True)
                context = browser.new_context(viewport={'width': 1440, 'height': 1000}, color_scheme='light')
                context.add_cookies([{'name': 'session', 'value': cookie, 'url': base}])
                page = context.new_page()
                errors = []
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(base + '/explore')
                page.locator('#catalogQuery').fill('清华')
                page.get_by_role('button', name='查找学校').click()
                page.get_by_role('link', name='已订阅 · 选择栏目').click()
                page.locator('input[value="selected"]').check()
                page.locator('input[name="department"]').nth(1).uncheck()
                page.get_by_role('button', name='保存栏目订阅').click()
                page.wait_for_url('**/?school=*')
                page.locator('[data-notice-link]').first.click()
                page.get_by_role('button', name='收藏', exact=True).click()
                page.get_by_role('button', name='取消收藏', exact=True).wait_for()
                page.screenshot(path=str(out / 'inbox-desktop.png'), full_page=True)
                assert page.locator('[data-state-field="archived"]').count() == 0
                page.get_by_role('link', name='我的收藏', exact=True).click()
                assert page.locator('[data-notice-link]').count() == 1
                page.locator('[data-notice-link]').first.click()
                page.get_by_role('button', name='取消收藏', exact=True).click()
                page.wait_for_function('() => !new URL(location.href).searchParams.has("selected")')
                for width in [390, 1440]:
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(base + f'/?school={school_id}&dept={secondary_id}&period=all&selected={shared_id}')
                    assert page.locator('[data-notice-link]').count() == 2
                    page.locator('.article-sources summary').click()
                    assert page.locator('.article-sources li').count() == 2
                    assert page.locator('.article-sources').get_attribute('open') is not None
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Source memberships overflow {width}'
                    page.screenshot(path=str(out / f'article-sources-{width}.png'), full_page=True)
                for width in [390, 768, 1440]:
                    page.set_viewport_size({'width': width, 'height': 900})
                    page.goto(base + f'/?school={school_id}')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Inbox overflow {width}'
                    if width == 390:
                        page.get_by_role('button', name='打开来源筛选').click()
                        page.get_by_role('button', name='关闭筛选').click()
                        page.locator('[data-notice-link]').first.click()
                        page.get_by_role('link', name='返回通知列表').click()
                        page.screenshot(path=str(out / 'inbox-mobile.png'), full_page=True)
                    page.goto(base + '/explore')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Directory overflow {width}'
                    page.screenshot(path=str(out / f'directory-{width}.png'), full_page=True)
                    page.goto(base + f'/subscriptions/{school_id}')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), f'Sources overflow {width}'
                page.goto(base + '/me')
                assert page.locator('.subscription-school').get_attribute('href') == f'/subscriptions/{school_id}'
                public = browser.new_context(viewport={'width': 390, 'height': 900})
                preview = public.new_page()
                preview.on('pageerror', lambda error: errors.append(str(error)))
                preview.goto(base + f'/school/{school_id}')
                preview.get_by_role('link', name='登录后订阅', exact=True).wait_for()
                assert preview.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Public preview overflow'
                preview.screenshot(path=str(out / 'school-preview-mobile.png'), full_page=True)
                preview.goto(base + f'/school/{school_id}?dept={secondary_id}')
                assert preview.locator('.preview-notice').count() == 2
                preview.locator('.preview-notice').filter(has_text=titles[0]).get_by_role('link').click()
                assert '通知公告' in preview.locator('.detail-meta').inner_text()
                preview.locator('.article-sources summary').click()
                assert preview.locator('.article-sources li').count() == 2
                public.close()
                page.emulate_media(color_scheme='dark')
                page.goto(base + '/explore')
                page.screenshot(path=str(out / 'directory-dark.png'), full_page=True)
                browser.close()
                assert not errors, errors
                print(json.dumps({'browser_errors': errors, 'widths': [390, 768, 1440], 'screenshots': str(out)}, ensure_ascii=True))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            with app.app_context():
                db.session.remove()
                db.engine.dispose()


if __name__ == '__main__':
    check()
