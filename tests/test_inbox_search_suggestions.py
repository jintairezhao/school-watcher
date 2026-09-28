"""Search previews share inbox visibility and do not mark announcements as read."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, Department, School, Subscription, User, UserRead, UserAnnouncementState


class InboxSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'search-test',
            'SOURCE_CATALOG_PATH': str(Path(self.temp.name) / 'catalog.db'),
            'SQLALCHEMY_DATABASE_URI': ('sqlite:///' + str(Path(self.temp.name) / 'search.db'))
            if getattr(self, 'use_file_database', False) else 'sqlite://'})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        db.session.add_all([User(id=1, username='reader', password_hash='unused'),
            User(id=2, username='other', password_hash='unused'),
            School(id=1, name='已订阅学校', url='https://subscribed.example'),
            School(id=2, name='未订阅学校', url='https://other.example')])
        db.session.flush()
        db.session.add_all([Department(id=11, school_id=1, name='教务处', group_name='组织机构'),
            Department(id=12, school_id=1, name='学院', group_name='院系设置'), Department(id=21, school_id=2, name='教务处')])
        db.session.flush()
        self.add(1, '关于北京学术交流的通知')
        self.add(2, '夏令营安排', content='前文' * 200 + '北京大学报名截止时间说明')
        self.add(3, '上海校区课程安排')
        self.add(4, '北京未订阅通知', school=2, dept=21)
        self.add(5, '北京学院通知', dept=12)
        self.add(6, '北京历史通知', days=40)
        self.add(7, '北京已归档通知')
        self.add(8, '北京已读通知')
        self.add(9, '完成 100% 进度的说明')
        db.session.add_all([Subscription(user_id=1, school_id=1),
            UserAnnouncementState(user_id=1, announcement_id=7, archived=True), UserRead(user_id=1, announcement_id=8)])
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session: session['user_id'] = 1

    def add(self, ident, title, *, content='', school=1, dept=11, days=0):
        db.session.add(Announcement(id=ident, school_id=school, department_id=dept, title=title,
            url=f'https://example.test/{ident}', content_text=content, published_at=datetime.utcnow() - timedelta(days=days, minutes=ident)))

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.session.remove(); db.engine.dispose()
        self.ctx.pop(); self.temp.cleanup()

    def results(self, **params):
        response = self.client.get('/api/inbox/search-suggestions', query_string={'q': '北京', **params})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'private, no-store')
        return response.json

    def test_substring_title_and_deep_body_match_with_context(self):
        data = self.results()
        self.assertEqual({item['id'] for item in data['items']}, {1, 2, 5, 7, 8})
        body = next(item for item in data['items'] if item['id'] == 2)
        self.assertIn('北京', body['snippet'])
        self.assertLessEqual(len(body['snippet']), 162)
        self.assertEqual(UserRead.query.count(), 1)

    def test_scope_respects_school_department_time_read_and_group(self):
        self.assertEqual({item['id'] for item in self.results(school=1, dept=12)['items']}, {5})
        self.assertEqual({item['id'] for item in self.results(school=1, group='院系设置')['items']}, {5})
        self.assertEqual({item['id'] for item in self.results(read='read')['items']}, {8})
        self.assertNotIn(8, {item['id'] for item in self.results(read='unread')['items']})
        self.assertIn(6, {item['id'] for item in self.results(period='all')['items']})
        self.assertFalse(self.results(school=2)['items'])

    def test_saved_and_archived_are_private_even_after_unsubscribe(self):
        db.session.add(UserAnnouncementState(user_id=1, announcement_id=4, starred=True)); db.session.commit()
        self.assertEqual([item['id'] for item in self.results(view='saved')['items']], [4])
        Subscription.query.filter_by(user_id=1).delete(); db.session.commit()
        self.assertEqual([item['id'] for item in self.results()['items']], [7])
        self.assertEqual([item['id'] for item in self.results(view='archived')['items']], [7])
        with self.client.session_transaction() as session: session['user_id'] = 2
        self.assertFalse(self.results(view='saved')['items'])
        self.assertFalse(self.results(view='archived')['items'])

    def test_preview_limit_links_and_literal_wildcards(self):
        for ident in range(20, 32): self.add(ident, '北京额外通知')
        db.session.commit()
        data = self.results(school=1, dept=11, period='all')
        self.assertEqual(len(data['items']), 8); self.assertTrue(data['has_more'])
        link = parse_qs(urlsplit(data['items'][0]['url']).query)
        self.assertEqual(link['q'], ['北京']); self.assertEqual(link['dept'], ['11'])
        self.assertEqual(link['period'], ['all']); self.assertIn('selected', link)
        self.assertNotIn('page', link)
        self.assertEqual([item['id'] for item in self.results(q='%')['items']], [9])
        self.assertEqual([item['id'] for item in self.results(q='北京 学院')['items']], [5])

    def test_empty_search_and_anonymous_requests(self):
        self.assertEqual(self.results(q='  ')['items'], [])
        self.assertEqual(self.app.test_client().get('/api/inbox/search-suggestions?q=北京').status_code, 401)

    def test_focus_keeps_private_mailbox_scope_and_suggestion_links(self):
        db.session.add(UserAnnouncementState(user_id=1, announcement_id=4, starred=True))
        db.session.commit()
        for mailbox, ident in [('saved', 4), ('archived', 7)]:
            data = self.results(view='focus', mailbox=mailbox)
            expected = {4} if mailbox == 'saved' else {1, 2, 5, 6, 7, 8}
            self.assertEqual({item['id'] for item in data['items']}, expected)
            item = next(item for item in data['items'] if item['id'] == ident)
            params = parse_qs(urlsplit(item['url']).query)
            self.assertEqual(params['view'], ['focus'])
            if mailbox == 'saved':
                self.assertEqual(params['mailbox'], ['saved'])
            else:
                self.assertNotIn('mailbox', params)
                self.assertEqual(params['period'], ['all'])
            response = self.client.get(item['url'])
            self.assertEqual(response.status_code, 200)
            self.assertIn('北京未订阅通知' if mailbox == 'saved' else '北京已归档通知', response.text)
        with self.client.session_transaction() as session:
            session['user_id'] = 2
        self.assertFalse(self.results(view='focus', mailbox='saved')['items'])
        self.assertFalse(self.results(view='focus', mailbox='archived')['items'])

    def test_focus_default_and_invalid_mailbox_do_not_widen_scope(self):
        expected = {item['id'] for item in self.results()['items']}
        self.assertEqual({item['id'] for item in self.results(view='focus')['items']}, expected)
        self.assertEqual({item['id'] for item in self.results(view='focus', mailbox='other')['items']}, expected)
        # An unrelated mailbox argument cannot change the existing view contract.
        expected_all = {item['id'] for item in self.results(period='all')['items']}
        self.assertEqual({item['id'] for item in self.results(view='archived', mailbox='saved')['items']}, expected_all)

    def test_saved_article_links_keep_explicit_week_filter(self):
        from bs4 import BeautifulSoup
        db.session.add_all([UserAnnouncementState(user_id=1, announcement_id=i, starred=True)
                            for i in (1, 6)])
        db.session.commit()
        for view_params in ({'view': 'saved'}, {'view': 'focus', 'mailbox': 'saved'}):
            with self.subTest(view=view_params):
                UserRead.query.delete(); db.session.commit()
                response = self.client.get('/', query_string={**view_params,
                    'school': 1, 'period': 'week', 'read': 'unread', 'q': '北京'})
                soup = BeautifulSoup(response.text, 'html.parser')
                link = soup.select_one('[data-notice-link]')['href']
                params = parse_qs(urlsplit(link).query)
                self.assertEqual(params.get('period'), ['week'])
                self.assertEqual(params.get('read'), ['unread'])
                self.assertEqual(params.get('q'), ['北京'])
                opened = BeautifulSoup(self.client.get(link).text, 'html.parser')
                self.assertNotIn('北京历史通知', opened.select_one('#noticeList').get_text())


if __name__ == '__main__': unittest.main()
