"""Each real publication channel retains its own safe, observed address."""
import unittest
from unittest.mock import patch

from sqlalchemy import event
from werkzeug.datastructures import MultiDict

from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, Department, School, Subscription
from backend.services.announcement_sources import publication_links_for, record_source, sources_for


class PublicationLinksTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.first = Department(school_id=self.fixture.school_id, name='教务通知',
            list_url='https://example.edu.cn/teaching/')
        self.second = Department(school_id=self.fixture.school_id, name='学院公告',
            list_url='https://college.example.edu.cn/notices/')
        db.session.add_all([self.first, self.second])
        db.session.flush()
        self.ann = Announcement(school_id=self.fixture.school_id, department_id=self.first.id,
            title='本科生申请安排', url='https://example.edu.cn/info/1.htm')
        self.same_title = Announcement(school_id=self.fixture.school_id, department_id=self.first.id,
            title=self.ann.title, url='https://example.edu.cn/info/2.htm')
        db.session.add_all([self.ann, self.same_title])
        db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_each_channel_keeps_its_actual_publication_url_without_rewriting_original(self):
        first_observed = 'https://example.edu.cn/article/view?id=1&tab=notice'
        second_observed = 'https://college.example.edu.cn/#/news?id=728'
        record_source(self.ann, self.first, first_observed)
        record_source(self.ann, self.second, second_observed)
        db.session.commit()
        links = publication_links_for([self.ann.id])[self.ann.id]
        self.assertEqual([row['source'].id for row in links], [self.first.id, self.second.id])
        self.assertEqual([row['article_url'] for row in links], [first_observed, second_observed])
        self.assertEqual([row['list_url'] for row in links], [self.first.list_url, self.second.list_url])
        self.assertTrue(all(row['observed'] for row in links))
        self.assertEqual(self.ann.url, 'https://example.edu.cn/info/1.htm')
        self.assertEqual(sources_for([self.ann.id])[self.ann.id], [self.first, self.second])

    def test_legacy_first_source_is_retained_without_faking_observation(self):
        record_source(self.ann, self.second, 'https://college.example.edu.cn/notices/728.html')
        db.session.commit()
        links = publication_links_for([self.ann.id])[self.ann.id]
        self.assertFalse(links[0]['observed'])
        self.assertTrue(links[1]['observed'])
        self.assertEqual(links[0]['article_url'], self.ann.url)
        self.assertEqual(links[0]['list_url'], self.first.list_url)
        self.assertEqual(AnnouncementSource.query.count(), 1)

    def test_snapshot_list_url_is_kept_when_current_channel_moves(self):
        record_source(self.ann, self.second, 'https://college.example.edu.cn/notices/728.html')
        db.session.commit()
        observed_list = self.second.list_url
        self.second.list_url = 'https://college.example.edu.cn/renamed/'
        db.session.commit()
        links = publication_links_for([self.ann.id])[self.ann.id]
        self.assertEqual(links[1]['list_url'], observed_list)
        self.assertEqual(links[1]['source'].list_url, self.second.list_url)

    def test_missing_secondary_publication_url_does_not_borrow_another_channels_url(self):
        db.session.add_all([
            AnnouncementSource(announcement_id=self.ann.id, department_id=self.first.id, article_url='', list_url=''),
            AnnouncementSource(announcement_id=self.ann.id, department_id=self.second.id, article_url='', list_url='')])
        db.session.commit()
        links = publication_links_for([self.ann.id])[self.ann.id]
        self.assertEqual(links[0]['article_url'], self.ann.url)
        self.assertEqual(links[1]['article_url'], '')
        self.assertEqual(links[1]['list_url'], self.second.list_url)
        self.assertTrue(all(row['observed'] for row in links))

    def test_enabled_channel_survives_disabled_original_school(self):
        other = School(name='另一高校', url='https://other.edu.cn/', enabled=True)
        db.session.add(other)
        db.session.flush()
        other_source = Department(school_id=other.id, name='交流信息', list_url=other.url + 'exchange/')
        db.session.add(other_source)
        db.session.flush()
        record_source(self.ann, other_source, other.url + 'notice/9')
        self.first.school.enabled = False
        db.session.commit()
        links = publication_links_for([self.ann.id])[self.ann.id]
        self.assertEqual([row['source'] for row in links], [other_source])
        self.assertEqual([row['source'] for row in links], sources_for([self.ann.id])[self.ann.id])
        other.enabled = False
        db.session.commit()
        self.assertEqual(publication_links_for([self.ann.id]), {})

    def test_untrusted_urls_are_unlinked_and_never_resolved_over_network(self):
        record_source(self.ann, self.second)
        db.session.commit()
        edge = db.session.get(AnnouncementSource, (self.ann.id, self.second.id))
        unsafe = ['javascript:alert(1)', 'data:text/html,<script>alert(1)</script>',
                  '//attacker.example/page', '/relative/page', 'https://user:password@example.edu.cn/page',
                  'https://example.edu.cn/\\evil', '\x00https://example.edu.cn/page',
                  'https://example.edu.cn/a\nb', 'https://[::1]/page', 'file:///C:/secret']
        with patch('socket.getaddrinfo', side_effect=AssertionError('Display must not resolve URLs')):
            for address in unsafe:
                with self.subTest(url=address):
                    edge.article_url = edge.list_url = address
                    db.session.commit()
                    links = publication_links_for([self.ann.id])[self.ann.id]
                    self.assertEqual(len(links), 2)
                    self.assertEqual(links[1]['article_url'], '')
                    self.assertEqual(links[1]['list_url'], '')
                    self.assertTrue(links[1]['observed'])
            self.ann.url = 'javascript:alert(2)'
            self.first.list_url = 'data:text/html,bad'
            db.session.commit()
            first = publication_links_for([self.ann.id])[self.ann.id][0]
            self.assertEqual((first['article_url'], first['list_url']), ('', ''))

    def test_only_requested_articles_are_returned_and_equal_titles_are_not_merged(self):
        self.assertEqual(set(publication_links_for([self.ann.id, self.ann.id, 999999])), {self.ann.id})
        all_links = publication_links_for([self.ann.id, self.same_title.id])
        self.assertEqual(set(all_links), {self.ann.id, self.same_title.id})
        self.assertNotEqual(all_links[self.ann.id][0]['article_url'], all_links[self.same_title.id][0]['article_url'])
        self.assertEqual(publication_links_for([]), {})

    def test_publication_read_does_not_widen_selected_subscription(self):
        from backend.services.inbox import filtered_inbox
        record_source(self.ann, self.second)
        sub = Subscription.query.one()
        sub.department_ids = [self.second.id]
        db.session.commit()
        ids = [a.id for a in filtered_inbox(sub.user_id, MultiDict({'period': 'all'}))]
        self.assertEqual(ids, [self.ann.id])
        self.assertEqual(set(publication_links_for(ids)), {self.ann.id})
        self.assertEqual([a.id for a in filtered_inbox(sub.user_id, MultiDict({'period': 'all'}))], ids)
        self.assertEqual(sub.department_ids, [self.second.id])

    def test_bulk_read_uses_one_query_and_does_not_load_article_bodies(self):
        record_source(self.ann, self.second)
        self.ann.content_text = '正文' * 10000
        db.session.commit()
        ids = [self.ann.id, self.same_title.id]
        statements = []
        def record(_connection, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)
        event.listen(db.engine, 'before_cursor_execute', record)
        try:
            links = publication_links_for(ids)
            self.assertTrue(all(row['source'].school.name for rows in links.values() for row in rows))
        finally:
            event.remove(db.engine, 'before_cursor_execute', record)
        self.assertEqual(len(statements), 1)
        self.assertNotIn('content_text', statements[0])
        self.assertNotIn('content_html', statements[0])


if __name__ == '__main__':
    unittest.main()
