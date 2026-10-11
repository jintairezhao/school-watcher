"""Real publication sources live in the existing sidebar on every viewport."""
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
            DepartmentDirectoryEntry.query.filter_by(department_id=fixture.column.id).delete()
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
            library_publication_url = 'https://lib.seu.edu.cn/open/holiday-2026.htm'
            record_source(shared, student, article_url=secondary_url)
            record_source(library, single, article_url=library_publication_url)
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
            page.goto(f'http://localhost/?school={school_id}&period=all')
            expect(page.locator('.mailbox-views a')).to_have_count(2)
            expect(page.locator('a[href^="/channels"]')).to_have_count(0)
            expect(page.get_by_role('link', name='按发布渠道', exact=True)).to_have_count(0)
            page.locator('#openFilters').click()
            group = page.locator('[data-source-group="组织机构"]')
            group.locator('.source-group-toggle').click()
            teaching_branch = group.locator('[data-source-unit-name="教务处"]')
            student_branch = group.locator('[data-source-unit-name="学生工作处"]')
            teaching_branch.locator('.source-unit-toggle').click()
            student_branch.locator('.source-unit-toggle').click()
            expect(teaching_branch.locator(f'[data-department][value="{fixture.column.id}"]')).to_be_visible()
            expect(student_branch.locator(f'[data-department][value="{student.id}"]')).to_be_visible()
            expect(teaching_branch.locator('.department-count')).to_have_text('2')
            expect(student_branch.locator('.department-count')).to_have_text('1')
            assert '未订阅的测试学校' not in page.locator('main').inner_text()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'source-sidebar-{width}-{theme}.png'), full_page=True)

            # Same-named columns stay under their real publishers; search and selection
            # filter this inbox, including an article also published by another unit.
            page.locator('#sourceQuery').fill('学生工作处')
            expect(teaching_branch).to_be_hidden()
            student_branch.locator(f'.department-option:has([data-department][value="{student.id}"])').click()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            expect(page.locator('[data-notice-link]')).to_contain_text(shared.title)
            params = parse_qs(urlsplit(page.url).query)
            assert params['dept'] == [str(student.id)] and params['period'] == ['all']
            assert page.locator('[data-notice-link] [data-channel-link]').count() == 0
            expect(page.locator('.notice-source-label')).to_contain_text('学生工作处')
            page.locator('[data-close-sources]').click()
            page.locator('[data-notice-link]').click()
            page.locator('.article-sources summary').click()
            expect(page.locator('.article-sources li')).to_have_count(2)
            expect(page.locator(f'.article-sources a[href="{shared.url}"]')).to_have_text('此处刊载原文')
            expect(page.locator(f'.article-sources a[href="{secondary_url}"]')).to_have_text('此处刊载原文')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.screenshot(path=str(output / f'publications-{width}-{theme}.png'), full_page=True)

            def check_single_source(saved=False):
                header = page.locator('.reader-document .reading-header')
                expect(header.locator('.reading-meta .reader-source')).to_have_count(1)
                breadcrumb = header.locator('.reader-source').inner_text()
                assert '图书馆' in breadcrumb and '开放时间' in breadcrumb
                assert header.inner_text().count(breadcrumb) == 1
                expect(header.locator('details.article-sources, .article-sources summary, .publication-name')).to_have_count(0)
                expect(header.locator('.article-sources a')).to_have_count(3)
                channel = header.locator('.article-sources [data-channel-link]')
                expect(channel).to_be_visible()
                channel_url = urlsplit(channel.get_attribute('href'))
                channel_params = parse_qs(channel_url.query)
                assert channel_url.path == '/' and channel_url.fragment == 'sources'
                assert channel_params['school'] == [str(school_id)]
                assert channel_params['dept'] == [str(single.id)]
                assert channel_params['period'] == ['all']
                assert channel_params.get('view', ['inbox']) == ['saved' if saved else 'inbox']
                expect(header.get_by_role('link', name='此处刊载原文', exact=True)).to_have_attribute('href', library_publication_url)
                expect(header.get_by_role('link', name='官网栏目', exact=True)).to_have_attribute('href', single.list_url)
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')

            # The shared reading header stays concise in all three reading surfaces.
            for reading_mode, target in (
                    ('inbox', f'/?school={school_id}&period=all&selected={library.id}'),
                    ('standalone', f'/announcement/{library.id}?source={single.id}'),
                    ('focus', f'/?school={school_id}&period=all&selected={library.id}&view=focus')):
                page.goto('http://localhost' + target)
                check_single_source()
                page.screenshot(path=str(output / f'single-{reading_mode}-{width}-{theme}.png'), full_page=True)

            # One source still has a visible channel action; leaving focus mode reveals its list.
            page.locator('.article-sources [data-channel-link]').click()
            expect(page.locator('#sourcePanel')).to_be_visible()
            expect(page.locator(f'[data-department][value="{single.id}"]')).to_be_visible()
            expect(page.locator(f'[data-department][value="{single.id}"]')).to_be_checked()
            page.locator('[data-close-sources]').click()
            expect(page.locator('.notice-panel')).to_be_visible()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            expect(page.locator('[data-notice-link]')).to_contain_text(library.title)

            # Saved scope and a bookmarked source link work with the same drawer.
            page.goto(f'http://localhost/?school={school_id}&dept={student.id}&period=all&view=saved#sources')
            expect(page.locator('#sourcePanel')).to_be_visible()
            expect(page.locator(f'[data-department][value="{student.id}"]')).to_be_visible()
            expect(page.locator(f'[data-department][value="{student.id}"]')).to_be_checked()
            page.locator('#sourceQuery').fill('不存在的发布站点')
            expect(page.locator('#sourceSearchEmpty')).to_be_visible()
            page.locator('[data-close-sources]').click()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']
            page.locator('.notice-channel-link').click()
            expect(page.locator('#sourcePanel')).to_be_visible()
            expect(page.locator('#sourceQuery')).to_have_value('')
            expect(page.locator(f'[data-department][value="{student.id}"]')).to_be_visible()
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']

            # Old links reach the inbox; a separate channel page is no longer needed.
            # Playwright does not route the follow-up request after a fulfilled redirect.
            legacy = fixture.client.get(f'/channels?school={school_id}&view=saved')
            assert legacy.status_code == 302
            page.goto('http://localhost' + legacy.headers['Location'])
            assert urlsplit(page.url).path == '/'
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']
            expect(page.locator('#sourcePanel')).to_be_visible()
            expect(page.locator('a[href^="/channels"]')).to_have_count(0)
            expect(page.locator('[data-notice-link]')).to_have_count(1)

            # A single-source action keeps the saved mailbox, rather than broadening it.
            db.session.add(UserAnnouncementState(user_id=fixture.user.id, announcement_id=library.id, starred=True))
            db.session.commit()
            page.goto(f'http://localhost/?school={school_id}&period=all&selected={library.id}&view=saved')
            check_single_source(saved=True)
            page.locator('.article-sources [data-channel-link]').click()
            expect(page.locator('#sourcePanel')).to_be_visible()
            expect(page.locator(f'[data-department][value="{single.id}"]')).to_be_checked()
            assert parse_qs(urlsplit(page.url).query).get('view') == ['saved']
            page.locator('[data-close-sources]').click()
            expect(page.locator('[data-notice-link]')).to_have_count(1)
            expect(page.locator('[data-notice-link]')).to_contain_text(library.title)
            assert not errors and not external, (errors, external)
            results.append({'width': width, 'theme': theme, 'distinct_publishers': True,
                            'multiple_publications': True, 'single_source_navigation': True,
                            'single_source_not_repeated': ['inbox', 'focus', 'standalone'],
                            'actual_publication_links': True, 'single_source_saved_scope': True,
                            'saved_scope_preserved': True, 'sidebar_only': True, 'overflow': False})
            page.close()
        finally:
            fixture.tearDown()
    browser.close()

print(json.dumps(results))
