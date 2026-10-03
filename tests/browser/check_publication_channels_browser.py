"""Actual publication channels stay distinct, navigable and safe on every viewport."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
os.environ.setdefault('WATCHER_DATA_DIR', str(ROOT / '.local/publication-channel-checks/data'))
os.environ.setdefault('WATCHER_ENV_FILE', str(ROOT / '.local/publication-channel-checks/data/.env'))

from playwright.sync_api import expect, sync_playwright
from test_page_recovery import SchoolNavigationTests
from backend.database.db import db
from backend.database.models import Announcement, Department, DepartmentDirectoryEntry, School, UserAnnouncementState, UserRead
from backend.services.announcement_sources import record_source

output = ROOT / '.local/publication-channel-checks/screenshots'
output.mkdir(parents=True, exist_ok=True)
results = []

with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Browsing must not invoke AI')), sync_playwright() as playwright:
    browser = playwright.chromium.launch(channel=os.environ.get('WATCHER_TEST_BROWSER_CHANNEL', 'msedge'), headless=True)
    for width, theme in ((1280, 'light'), (1280, 'dark'), (390, 'light'), (390, 'dark')):
        fixture = SchoolNavigationTests()
        fixture.setUp()
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            school_id = fixture.school.id
            fixture.column.name = '通知公告'
            fixture.column.group_name = '组织机构'
            fixture.column.list_url = 'https://jwc.seu.edu.cn/tzgg/'
            fixture.column.list_selector = 'li'
            student = Department(school_id=school_id, name='通知公告', group_name='组织机构',
                list_url='https://xsc.seu.edu.cn/tzgg/', list_selector='li')
            teaching_unit = Department(school_id=school_id, name='教务处', kind='unit',
                group_name='组织机构', list_url='https://jwc.seu.edu.cn/')
            student_unit = Department(school_id=school_id, name='学生工作处', kind='unit',
                group_name='组织机构', list_url='https://xsc.seu.edu.cn/')
            single = Department(school_id=school_id, name='图书馆-开放时间', group_name='公共服务',
                list_url='https://lib.seu.edu.cn/open/', list_selector='li')
            hidden_school = School(name='未订阅的测试学校', url='https://other.example.edu.cn/')
            db.session.add_all([student, single, hidden_school, teaching_unit, student_unit])
            db.session.flush()
            db.session.add_all([
                DepartmentDirectoryEntry(parent_id=teaching_unit.id, department_id=fixture.column.id),
                DepartmentDirectoryEntry(parent_id=student_unit.id, department_id=student.id)])
            hidden_source = Department(school_id=hidden_school.id, name='不应显示的栏目',
                list_url='https://other.example.edu.cn/notices/', list_selector='li')
            db.session.add(hidden_source)
            db.session.flush()
            shared = Announcement(school_id=school_id, department_id=fixture.column.id,
                title='学期奖学金申请安排', url='https://jwc.seu.edu.cn/notice/1.htm',
                published_at=now, content_text='隔离测试通知正文。', content_html='<p>隔离测试通知正文。</p>', content_cached_at=now)
            teaching = Announcement(school_id=school_id, department_id=fixture.column.id,
                title='公共选修课安排', url='https://jwc.seu.edu.cn/notice/2.htm', published_at=now)
            library = Announcement(school_id=school_id, department_id=single.id,
                title='图书馆假期开放时间', url='https://lib.seu.edu.cn/notice/3.htm',
                published_at=now, content_text='隔离测试开放安排。', content_html='<p>隔离测试开放安排。</p>', content_cached_at=now)
            hidden = Announcement(school_id=hidden_school.id, department_id=hidden_source.id,
                title='范围外通知', url='https://other.example.edu.cn/notice/4.htm', published_at=now)
            db.session.add_all([shared, teaching, library, hidden])
            db.session.flush()
            secondary_url = 'https://xsc.seu.edu.cn/student/award-copy.htm'
            record_source(shared, student, article_url=secondary_url)
            db.session.add(UserRead(user_id=fixture.user.id, announcement_id=shared.id))
            db.session.add(UserAnnouncementState(user_id=fixture.user.id, announcement_id=shared.id, starred=True))
            db.session.commit()

            page = browser.new_page(viewport={'width': width, 'height': 900}, reduced_motion='reduce')
            errors, external = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))

            def serve(route):
                request = route.request
                parsed = urlsplit(request.url)
                if parsed.hostname != 'localhost':
                    external.append(parsed.hostname)
                    route.abort()
                    return
                headers = {key: value for key, value in request.headers.items()
                           if key.lower() in ('content-type', 'x-csrf-token', 'x-inbox-fragment')}
                path = parsed.path + ('?' + parsed.query if parsed.query else '')
                response = fixture.client.open(path, method=request.method, data=request.post_data, headers=headers)
                route.fulfill(status=response.status_code, headers=dict(response.headers), body=response.data)

            page.route('**/*', serve)
            page.add_init_script('localStorage.setItem("theme", ' + json.dumps(theme) + ')')
            page.goto('http://localhost/channels')
            expect(page.get_by_role('heading', name='按发布渠道', exact=True)).to_be_visible()
            expect(page.locator('.channel-school')).to_have_count(1)
            expect(page.locator('.channel-summary')).to_contain_text('3 条已收录通知')
            expect(page.locator(f'[data-channel-id="{fixture.column.id}"] .channel-count')).to_contain_text('2 条')
            expect(page.locator(f'[data-channel-id="{fixture.column.id}"] .channel-count')).to_contain_text('1 条未读')
            assert '未订阅的测试学校' not in page.locator('main').inner_text()
            expect(page.locator(f'[data-channel-id="{fixture.column.id}"] .channel-name')).to_contain_text('教务处')
            expect(page.locator(f'[data-channel-id="{student.id}"] .channel-name')).to_contain_text('学生工作处')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'channels-{width}-{theme}.png'), full_page=True)

            # A secondary publication opens exactly that source, including the shared article.
            page.locator(f'[data-channel-id="{student.id}"] .channel-row').click()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            expect(page.locator('[data-notice-link]')).to_contain_text(shared.title)
            params = parse_qs(urlsplit(page.url).query)
            assert params['dept'] == [str(student.id)] and params['period'] == ['all']
            assert page.locator('[data-notice-link] [data-channel-link]').count() == 0
            expect(page.locator('.notice-source-label')).to_contain_text('学生工作处')
            page.locator('[data-notice-link]').click()
            page.locator('.article-sources summary').click()
            expect(page.locator('.article-sources li')).to_have_count(2)
            expect(page.locator(f'.article-sources a[href="{secondary_url}"]')).to_have_text('此处刊载原文')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'publications-{width}-{theme}.png'), full_page=True)

            # One source still has a visible channel action; leaving focus mode reveals its list.
            page.goto(f'http://localhost/?school={school_id}&period=all&selected={library.id}&view=focus')
            expect(page.locator('.article-sources')).to_have_attribute('open', '')
            expect(page.locator('.article-sources [data-channel-link]')).to_be_visible()
            page.locator('.article-sources [data-channel-link]').click()
            expect(page.locator('.notice-panel')).to_be_visible()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            expect(page.locator('[data-notice-link]')).to_contain_text(library.title)

            # Saved channel navigation stays inside saved notices.
            page.goto('http://localhost/channels?view=saved')
            expect(page.locator('.channel-summary')).to_contain_text('1 条已收藏通知')
            page.locator(f'[data-channel-id="{student.id}"] .channel-row').click()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']
            page.locator('.notice-channel-link').click()
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']

            page.goto('http://localhost/channels')
            page.locator('#channelQuery').fill('不存在的发布站点')
            page.get_by_role('button', name='查找渠道', exact=True).click()
            expect(page.get_by_role('heading', name='没有匹配的发布渠道')).to_be_visible()
            page.get_by_role('link', name='查看全部渠道', exact=True).click()
            expect(page.locator('.channel-school')).to_have_count(1)
            assert not errors and not external, (errors, external)
            results.append({'width': width, 'theme': theme, 'distinct_publishers': True,
                            'multiple_publications': True, 'single_source_navigation': True,
                            'saved_scope_preserved': True, 'overflow': False})
            page.close()
        finally:
            fixture.tearDown()
    browser.close()

print(json.dumps(results))
