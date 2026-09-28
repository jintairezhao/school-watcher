"""End-to-end regressions for catalog, original columns and personal organization."""
import sys
import unittest
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, Announcement, Subscription, User, UserRead, UserAnnouncementState
from backend.services.catalog import catalog_entries, MEMBERS_BY_PROVINCE
from backend.scraper.discovery.lightweight import discover_columns
from backend.scraper.http_client import validate_public_url, same_school_url, PublicHTTPClient


class LightweightTests(unittest.TestCase):
    def setUp(self):
        self.source_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.source_temp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'lightweight-tests', 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                               'SOURCE_INVENTORY_PATH': Path(self.source_temp.name) / 'inventory.sqlite3'})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.user = User(username='reader', password_hash='unused')
        self.other = User(username='other', password_hash='unused')
        self.school = School(name='清华大学', url='https://www.tsinghua.edu.cn', subscriber_count=1)
        self.foreign = School(name='北京大学', url='https://www.pku.edu.cn')
        db.session.add_all([self.user, self.other, self.school, self.foreign])
        db.session.flush()
        self.teaching = Department(school_id=self.school.id, name='本科教育', group_name='人才培养', list_url=self.school.url)
        self.news = Department(school_id=self.school.id, name='校园要闻', group_name='清华新闻', list_url=self.school.url + '/news/')
        self.foreign_dept = Department(school_id=self.foreign.id, name='北大新闻')
        db.session.add_all([self.teaching, self.news, self.foreign_dept])
        db.session.flush()
        now = datetime.utcnow()
        self.notice = Announcement(school_id=self.school.id, department_id=self.teaching.id, title='选课通知', published_at=now, content_text='选课办理方式', url=self.school.url + '/notice/1')
        self.other_notice = Announcement(school_id=self.school.id, department_id=self.news.id, title='校园新闻消息', published_at=now)
        self.old = Announcement(school_id=self.school.id, department_id=self.teaching.id, title='历史资料通知', published_at=now - timedelta(days=100))
        self.foreign_notice = Announcement(school_id=self.foreign.id, department_id=self.foreign_dept.id, title='其他学校的通知', published_at=now)
        db.session.add_all([self.notice, self.other_notice, self.old, self.foreign_notice])
        db.session.add(Subscription(user_id=self.user.id, school_id=self.school.id))
        db.session.commit()
        self.client = self.client_as(self.user.id)

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.ctx.pop()

    def test_fragment_navigation_preserves_scope_and_has_no_document_shell(self):
        response = self.client.get(f'/?school={self.school.id}&dept={self.teaching.id}&period=all',
                                   headers={'X-Inbox-Fragment': '1'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('inbox-workspace', response.text)
        self.assertNotIn('<html', response.text)
        self.assertIn('选课通知', response.text)
        self.assertNotIn('校园新闻消息', response.text)
        self.assertNotIn('其他学校的通知', response.text)
        self.assertIn('X-Inbox-Fragment', response.headers['Vary'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        anon = self.app.test_client().get('/?school=1', headers={'X-Inbox-Fragment': '1'})
        self.assertEqual(anon.status_code, 302)

    def test_display_group_includes_explicit_child_without_rewriting_official_metadata(self):
        child = Department(school_id=self.school.id, name='本科教育-选课安排', list_url=self.school.url)
        db.session.add(child); db.session.flush()
        db.session.add(Announcement(school_id=self.school.id, department_id=child.id, title='选课子栏目的通知',
                                   published_at=datetime.utcnow()))
        db.session.commit()
        response = self.client.get(f'/?school={self.school.id}&group=人才培养&period=all')
        self.assertIn('选课子栏目的通知', response.text)
        self.assertIn('data-source-unit=', response.text)
        self.assertIsNone(child.group_name)
        self.assertEqual(child.name, '本科教育-选课安排')

    def test_unrelated_pending_pages_do_not_block_student_source_refresh(self):
        from backend.services.source_refresh import refresh_subscribed_sources
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        inv = Inventory(self.app.config['SOURCE_INVENTORY_PATH'])
        key = inv.ensure_site(self.school.name, self.school.url)
        root = self.school.url + '/'
        inv.finish(key, root, html='<p>旧学校主页</p>', state='fetched', fetched_at='2020-01-01T00:00:00+00:00')
        inv.enqueue(key, root + 'news/', '学校新闻', 'channel', 1, [], 'school_domain')
        seen = []
        def local_crawl(inventory, site, **kwargs):
            self.assertEqual(kwargs['focus'], 'student')
            def fetch(url):
                seen.append(url)
                return {'html': '<p>当前主页</p>', 'url': url, 'status': 200}
            return crawl_site(inventory, site, **kwargs, fetcher=fetch)
        with patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=local_crawl):
            refresh_subscribed_sources(max_pages=1)
        self.assertEqual(seen, [root])
        self.assertEqual(next(p for p in inv.report(key)['pages'] if p['url'].endswith('/news/'))['state'], 'pending')

    def client_as(self, user_id):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
            session['_csrf_token'] = 'test-token'
        return client

    def write(self, path, data=None, method='post', client=None):
        return getattr(client or self.client, method)(path, json=data, headers={'X-CSRF-Token': 'test-token'})

    def state(self, ann, **values):
        response = self.write(f'/api/announcements/{ann.id}/state', values, 'put')
        self.assertEqual(response.status_code, 200)

    def test_catalog_includes_all_147_members_without_creating_schools(self):
        members = {name for names in MEMBERS_BY_PROVINCE.values() for name in names.split()}
        actual = {e['name'] for e in catalog_entries() if e['double_first_class']}
        self.assertEqual(len(members), 147)
        self.assertEqual(actual, members)
        before = School.query.count()
        response = self.client.get('/explore?q=清华&level=double')
        self.assertEqual(response.status_code, 200)
        self.assertIn('清华大学', response.text)
        self.assertEqual(School.query.count(), before)

    def test_background_structure_check_rotates_active_schools_without_importing_candidates(self):
        import tempfile
        from backend.services.source_refresh import refresh_subscribed_sources
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        self.foreign.subscriber_count = 1
        db.session.add(School(name='未订阅大学', url='https://inactive.edu.cn/', subscriber_count=0))
        db.session.commit()
        before = Department.query.count()
        with tempfile.TemporaryDirectory() as folder:
            self.app.config['SOURCE_INVENTORY_PATH'] = Path(folder) / 'inventory.db'
            def local_crawl(inventory, key, **kwargs):
                return crawl_site(inventory, key, **kwargs,
                                  fetcher=lambda url: {'html': '<h1>官方首页</h1>', 'url': url, 'status': 200})
            with patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=local_crawl):
                first = refresh_subscribed_sources(max_pages=1)
                second = refresh_subscribed_sources(max_pages=1)
                self.assertNotEqual(first['site']['site_key'], second['site']['site_key'])
                self.assertIsNone(refresh_subscribed_sources(max_pages=1))
            self.assertEqual(len(Inventory(self.app.config['SOURCE_INVENTORY_PATH']).all_sites()), 2)
        self.assertEqual(Department.query.count(), before)

    def test_revisiting_a_listing_cannot_erase_existing_article_content(self):
        from backend.scraper.change_detector import detect_update
        original = (self.notice.content_text, self.notice.created_at, self.notice.is_updated)
        existing, updated = detect_update(self.notice.url, self.notice.title)
        db.session.commit()
        self.assertFalse(updated)
        self.assertEqual(existing.id, self.notice.id)
        self.assertEqual((self.notice.content_text, self.notice.created_at, self.notice.is_updated), original)

    def test_every_source_needing_review_is_accessible_beyond_the_first_hundred(self):
        import tempfile
        from bs4 import BeautifulSoup
        from backend.services.source_inventory import Inventory
        with tempfile.TemporaryDirectory() as folder:
            self.app.config['SOURCE_INVENTORY_PATH'] = Path(folder) / 'inventory.db'
            store = Inventory(self.app.config['SOURCE_INVENTORY_PATH'])
            key = store.ensure_site(self.school.name, self.school.url)
            expected = {self.school.url + '/unit/' + str(i) for i in range(123)}
            for i, url in enumerate(sorted(expected)):
                store.enqueue(key, url, '待复查单位' + str(i), 'unit', 1, [], 'school_domain')
                store.finish(key, url, state='failed', health='unreachable', error='HTTP 503')
            found = set()
            route = '/schools/' + str(self.school.id) + '/structure'
            while route:
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200)
                soup = BeautifulSoup(response.text, 'lxml')
                found.update(a['href'] for a in soup.select('.structure-issues a'))
                link = soup.select_one('nav[aria-label="待复查来源翻页"] a[rel="next"]')
                route = link['href'].split('#')[0] if link else None
            self.assertEqual(found, expected)

    def test_catalog_subscribe_alias_and_repeat_are_idempotent(self):
        for _ in range(2):
            response = self.write('/api/catalog/subscribe', {'name': '浙大'})
            self.assertEqual(response.status_code, 200)
        school = School.query.filter_by(name='浙江大学').one()
        self.assertEqual(school.subscriber_count, 1)
        self.assertEqual(Subscription.query.filter_by(school_id=school.id).count(), 1)
        self.assertEqual(school.departments.count(), 0)

    def test_catalog_matches_existing_ascii_parentheses(self):
        school = School(name='中国石油大学(北京)', url='https://www.cup.edu.cn')
        db.session.add(school)
        db.session.commit()
        response = self.write('/api/catalog/subscribe', {'name': '中国石油大学（北京）'})
        self.assertEqual(response.json['school_id'], school.id)

    def test_hidden_school_cannot_be_recreated_through_catalog(self):
        self.school.enabled = False
        db.session.commit()
        self.assertEqual(self.write('/api/catalog/subscribe', {'name': '清华'}).status_code, 409)
        self.assertEqual(self.write('/api/subscriptions', {'school_id': self.school.id}).status_code, 404)

    def test_select_columns_persists_and_scopes_inbox(self):
        response = self.client.post(f'/subscriptions/{self.school.id}', data={
            'mode': 'selected', 'department': str(self.teaching.id), 'csrf_token': 'test-token'})
        self.assertEqual(response.status_code, 302)
        page = self.client.get('/?period=all').text
        self.assertIn('选课通知', page)
        self.assertNotIn('校园新闻消息', page)
        self.assertEqual(Subscription.query.one().department_ids, [self.teaching.id])
        self.assertIn('人才培养 / 本科教育', page)

    def test_foreign_and_empty_column_selection_rejected(self):
        for ids in ([], [str(self.foreign_dept.id)]):
            response = self.client.post(f'/subscriptions/{self.school.id}', data={
                'mode': 'selected', 'department': ids, 'csrf_token': 'test-token'})
            self.assertEqual(response.status_code, 200)
            self.assertIsNone(Subscription.query.one().department_ids)

    def test_bulk_read_respects_keyword_column_and_time(self):
        path = f'/api/inbox/read?school={self.school.id}&dept={self.teaching.id}&q=选课'
        response = self.write(path)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['count'], 1)
        self.assertEqual([r.announcement_id for r in UserRead.query.all()], [self.notice.id])
        self.assertEqual(self.write(path).json['count'], 0)
        self.assertEqual(UserRead.query.filter_by(user_id=self.other.id).count(), 0)

    def test_archive_is_personal_and_reversible(self):
        self.state(self.notice, archived=True)
        self.assertNotIn('选课通知', self.client.get('/?period=all').text)
        self.assertIn('选课通知', self.client.get('/?view=archived').text)
        self.assertNotIn('选课通知', self.client_as(self.other.id).get('/?view=archived').text)
        self.state(self.notice, archived=False)
        self.assertIn('选课通知', self.client.get('/?period=all').text)

    def test_saved_survives_unsubscribe_and_does_not_leak(self):
        self.state(self.old, starred=True)
        self.write(f'/api/subscriptions/{self.school.id}', method='delete')
        self.assertIn('历史资料通知', self.client.get('/?view=saved').text)
        self.assertNotIn('历史资料通知', self.client_as(self.other.id).get('/?view=saved').text)
        self.assertEqual(Announcement.query.count(), 4)

    def test_open_requires_explicit_selection_and_marks_only_selected(self):
        self.client.get('/')
        self.assertEqual(UserRead.query.count(), 0)
        self.client.get(f'/?selected={self.notice.id}')
        self.assertEqual([r.announcement_id for r in UserRead.query.all()], [self.notice.id])

    def test_page_two_links_keep_page_number(self):
        for i in range(45):
            db.session.add(Announcement(school_id=self.school.id, department_id=self.teaching.id,
                                       title=f'分页公告{i}', published_at=datetime.utcnow() - timedelta(minutes=i+10)))
        db.session.commit()
        response = self.client.get('/?period=all&page=2')
        self.assertIn('page=2', response.text)

    def test_missing_publish_date_is_visible_using_capture_date(self):
        db.session.add(Announcement(school_id=self.school.id, department_id=self.teaching.id,
                                   title='没有发布日期的通知', created_at=datetime.utcnow()))
        db.session.commit()
        self.assertIn('没有发布日期的通知', self.client.get('/').text)

    def test_state_validation_and_csrf(self):
        path = f'/api/announcements/{self.notice.id}/state'
        self.assertEqual(self.client.put(path, json={'starred': True}).status_code, 403)
        self.assertEqual(self.write(path, {'starred': 'false'}, 'put').status_code, 400)
        self.assertEqual(self.write(path, {'user_id': self.other.id}, 'put').status_code, 400)

    def test_school_input_validation(self):
        for url in ('file:///etc/passwd', 'http://127.0.0.1', 'http://localhost', 'http://[::1]', 'http://10.1.2.3'):
            self.assertEqual(self.write('/api/schools', {'name': '新高校', 'url': url}).status_code, 400)
        self.assertEqual(self.write('/api/subscriptions', {'school_id': []}).status_code, 400)

    def test_source_add_rejects_another_school_domain(self):
        with patch('backend.scraper.http_client.validate_public_url'):
            self.client.post(f'/subscriptions/{self.school.id}/sources', data={
                'name': '招生公告', 'url': self.foreign.url + '/admissions', 'csrf_token': 'test-token'})
        self.assertEqual(Department.query.count(), 3)

    def test_distinct_units_can_keep_identically_named_columns(self):
        from backend.services.source_catalog import apply_source_configs
        from backend.database.source_governance_models import SourceProposal
        import json
        configs = [{'name': '通知公告', 'list_url': self.school.url + path, 'list_selector': 'ul.notices > li'}
                   for path in ('/math/notices/', '/physics/notices/')]
        old_id = self.teaching.id
        self.assertEqual(apply_source_configs(self.school.id, configs), 0)
        self.assertEqual(apply_source_configs(self.school.id, configs), 0)
        self.assertEqual(Department.query.filter_by(school_id=self.school.id, name='通知公告').count(), 0)
        proposals = SourceProposal.query.filter_by(school_id=self.school.id).all()
        self.assertEqual(len(proposals), 2)
        self.assertEqual({json.loads(p.candidate_json)['list_url'] for p in proposals},
                         {c['list_url'] for c in configs})
        self.assertTrue(all(p.state == 'proposed' for p in proposals))
        self.assertIsNotNone(db.session.get(Department, old_id))
        self.assertEqual(Announcement.query.count(), 4)

    def test_original_group_and_homepage_not_discarded(self):
        from backend.services.inbox import source_groups
        groups = source_groups([self.teaching, self.news])
        self.assertEqual(set(groups), {'人才培养', '清华新闻'})
        self.assertIn(self.news, groups['清华新闻'])

    def test_school_preview_and_subscriber_redirect(self):
        preview = self.app.test_client().get(f'/school/{self.school.id}')
        self.assertEqual(preview.status_code, 200)
        self.assertIn('选课通知', preview.text)
        self.assertIn('人才培养', preview.text)
        response = self.client.get(f'/school/{self.school.id}?dept={self.teaching.id}&year=2025')
        self.assertEqual(response.status_code, 302)
        self.assertIn(f'school={self.school.id}', response.location)
        self.assertIn('period=archive', response.location)
        self.school.enabled = False
        db.session.commit()
        self.assertEqual(self.app.test_client().get(f'/school/{self.school.id}').status_code, 404)
        self.assertEqual(self.client.get(f'/subscriptions/{self.school.id}').status_code, 302)

    def test_light_discovery_keeps_original_labels_and_limits_scope(self):
        html = '<nav><ul><li><a href="/admissions">招生就业</a><ul><li><a href="https://zs.tsinghua.edu.cn/">本科招生</a></li></ul></li><li><a href="/notice">通知公告</a></li><li><a href="https://evil.example/">教务通知</a></li></ul></nav>'
        entries = discover_columns(self.school.url, html)
        self.assertEqual({e['name'] for e in entries}, {'招生就业', '本科招生', '通知公告'})
        self.assertEqual(next(e for e in entries if e['name'] == '本科招生')['group_name'], '招生就业')
        self.assertTrue(same_school_url('https://news.tsinghua.edu.cn/list', self.school.url))
        self.assertFalse(same_school_url('https://tsinghua.edu.cn.evil.example', self.school.url))

    def test_http_redirect_to_private_network_is_rejected(self):
        from requests import Response
        response = Response()
        response.status_code = 302
        response.headers['Location'] = 'http://127.0.0.1/secret'
        response._content = b''
        response._content_consumed = True
        with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), patch(
                'backend.scraper.pinned_transport.pinned_request', return_value=response) as request:
            with self.assertRaises(ValueError):
                PublicHTTPClient().get('https://www.tsinghua.edu.cn')
            self.assertEqual(request.call_count, 1)

    def test_batch_summary_requires_explicit_scope_without_paid_calls(self):
        from backend.ai.summarizer import batch_summarize
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid call without explicit scope')):
            with self.assertRaisesRegex(ValueError, '1–50'):
                batch_summarize()

    def test_navigation_page_is_not_an_announcement_list(self):
        from backend.scraper.engine import _probe_selectors
        html = '<div>' + ''.join(f'<a href="/section{i}.html">English Version {i}</a>' for i in range(6)) + '</div>'
        self.assertIsNone(_probe_selectors(html, self.teaching))

    def test_english_switch_never_enters_announcement_store(self):
        from bs4 import BeautifulSoup
        from backend.scraper.engine import _process_announcement_item
        item = BeautifulSoup('<a href="/en/News.htm">English Version</a>', 'lxml').a
        self.assertFalse(_process_announcement_item(item, self.teaching, self.school.url))

    def test_news_cards_choose_article_title_not_image_or_navigation(self):
        from backend.scraper.engine import _probe_selectors
        from bs4 import BeautifulSoup
        html = '<header><a href="/en">English Version</a></header><ul>' + ''.join(
            f'<li><a href="info/1177/{i}.htm"><img src="/photo.jpg"></a><div class="time">2026.09.10</div><div class="name">高校举行教师节庆祝大会{i}</div></li>' for i in range(3)) + '</ul>'
        profile, permanent = _probe_selectors(html, self.teaching)
        nodes = BeautifulSoup(html, 'lxml').select(profile['list_selector'])
        self.assertEqual(len(nodes), 3)
        self.assertEqual(nodes[0].select_one(profile['title_selector']).text, '高校举行教师节庆祝大会0')


if __name__ == '__main__':
    unittest.main(verbosity=2)
