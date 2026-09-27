import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
from werkzeug.datastructures import MultiDict

import test_lightweight as fixture
from backend.database.db import db
from backend.database.models import Announcement, AnnouncementSource, Subscription, UserRead
from backend.services.announcement_sources import record_source, source_counts
from backend.services.inbox import filtered_inbox
from backend.scraper.engine import _process_announcement_item


class MembershipTests(unittest.TestCase):
    setUp = fixture.LightweightTests.setUp
    tearDown = fixture.LightweightTests.tearDown
    client_as = fixture.LightweightTests.client_as
    write = fixture.LightweightTests.write
    state = fixture.LightweightTests.state

    def test_reader_keeps_original_signature_separate_from_current_column(self):
        from test_article_provenance import article
        self.teaching.name = '院内新闻'
        self.teaching.group_name = '地球科学与工程学院'
        self.notice.url = 'https://www.cupk.edu.cn/syxy/c/2026-06-10/534685.shtml'
        self.notice.content_html = article('作者：管理员 来源：351-石油学院 浏览：767')
        db.session.commit()
        for path in (f'/?period=all&selected={self.notice.id}', f'/announcement/{self.notice.id}'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            document = BeautifulSoup(response.text, 'lxml')
            self.assertEqual(document.select_one('.article-origin').get_text(' ', strip=True), '原文署名来源： 351-石油学院')
            self.assertIn('地球科学与工程学院', response.text)
            self.assertIn('院内新闻', response.text)
        self.assertEqual(self.notice.department_id, self.teaching.id)
        self.notice.content_html = article('来源：&lt;img src=x onerror=alert(1)&gt; 浏览：2')
        db.session.commit()
        document = BeautifulSoup(self.client.get(f'/announcement/{self.notice.id}').text, 'lxml')
        self.assertIsNone(document.select_one('.article-origin img'))
        self.assertIn('<img src=x onerror=alert(1)>', document.select_one('.article-origin').get_text())

    def add_secondary(self):
        record_source(self.notice, self.news)
        db.session.commit()

    def test_short_real_guide_is_ingested_with_its_original_column(self):
        self.news.link_selector = self.news.title_selector = 'a'
        self.news.date_selector = 'span'
        self.news.content_selector = 'article'
        item = BeautifulSoup('<li><a href="/info/1/90001.htm">会议扫码签到说明</a>'
                             '<span>2026-09-16</span></li>', 'lxml').li
        with patch('backend.scraper.engine._fetch_html', return_value='<article>会议签到的具体操作步骤。</article>'):
            self.assertTrue(_process_announcement_item(item, self.news, self.school.url))
        db.session.commit()
        created = Announcement.query.filter_by(title='会议扫码签到说明').one()
        self.assertFalse(created.content_text)
        from backend.services.content_cache import fetch_content
        with patch('backend.scraper.engine._fetch_html', return_value='<article>会议签到的具体操作步骤。</article>'):
            fetch_content(created.id)
        self.assertEqual(created.content_text, '会议签到的具体操作步骤。')
        self.assertIsNotNone(db.session.get(AnnouncementSource, (created.id, self.news.id)))

    def test_carousel_date_on_the_list_item_survives_actual_ingestion(self):
        self.news.link_selector = self.news.title_selector = 'h3 a'
        self.news.date_selector = 'li[data-title]'
        self.news.content_selector = 'article'
        item = BeautifulSoup('<li data-title="&lt;strong&gt;12&lt;/strong&gt;&lt;i&gt;2025.09&lt;/i&gt;">'
                             '<h3><a href="/news/opaque-date.htm">本科新生第一堂思政课开讲</a></h3></li>', 'lxml').li
        with patch('backend.scraper.engine._fetch_html', return_value='<article>迎新课程报道。</article>'):
            self.assertTrue(_process_announcement_item(item, self.news, self.school.url))
        db.session.commit()
        created = Announcement.query.filter_by(title='本科新生第一堂思政课开讲').one()
        self.assertEqual(created.published_at.date().isoformat(), '2025-09-12')

    def test_repeat_ingestion_preserves_original_and_records_each_real_column(self):
        self.news.link_selector = self.news.title_selector = 'a'
        item = BeautifulSoup('<a href="' + self.notice.url + '">' + self.notice.title + '</a>', 'lxml').a
        original = (self.notice.id, self.notice.department_id, self.notice.content_text, self.notice.created_at)
        with patch('backend.scraper.engine._fetch_html') as fetch:
            for _ in range(2):
                self.assertFalse(_process_announcement_item(item, self.news, self.school.url))
            db.session.commit()
            fetch.assert_not_called()
        self.assertEqual((self.notice.id, self.notice.department_id, self.notice.content_text, self.notice.created_at), original)
        self.assertEqual(Announcement.query.count(), 4)
        self.assertEqual(AnnouncementSource.query.count(), 1)
        self.assertEqual(source_counts(self.school.id), {self.teaching.id: 2, self.news.id: 2})

    def test_replaying_old_evidence_does_not_pretend_it_is_a_new_observation(self):
        from datetime import datetime
        record_source(self.notice, self.news, observed_at=datetime(2026, 9, 18))
        db.session.commit()
        record_source(self.notice, self.news, observed_at=datetime(2026, 9, 15))
        db.session.commit()
        source = db.session.get(AnnouncementSource, (self.notice.id, self.news.id))
        self.assertEqual(source.first_seen_at, datetime(2026, 9, 18))
        self.assertEqual(source.last_seen_at, datetime(2026, 9, 18))

    def test_subscribing_to_secondary_column_includes_article_exactly_once(self):
        self.add_secondary()
        Subscription.query.one().department_ids = [self.news.id]
        db.session.commit()
        result = filtered_inbox(self.user.id, MultiDict({'period': 'all'})).all()
        self.assertEqual({a.id for a in result}, {self.notice.id, self.other_notice.id})
        self.assertEqual(len(result), 2)
        response = self.client.get('/?period=all&school=' + str(self.school.id) + '&dept=' + str(self.news.id))
        self.assertEqual(response.status_code, 200)
        self.assertIn('选课通知', response.text)
        self.assertIn('2 个官网栏目', response.text)

    def test_group_and_column_filters_must_match_the_same_membership(self):
        self.add_secondary()
        args = MultiDict({'school': str(self.school.id), 'dept': str(self.teaching.id), 'group': self.news.group_name, 'period': 'all'})
        self.assertEqual(filtered_inbox(self.user.id, args).count(), 0)

    def test_bulk_read_secondary_column_does_not_duplicate_or_widen_scope(self):
        self.add_secondary()
        response = self.write('/api/inbox/read?period=all&school=' + str(self.school.id) + '&dept=' + str(self.news.id))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['count'], 2)
        self.assertEqual({r.announcement_id for r in UserRead.query.all()}, {self.notice.id, self.other_notice.id})

    def test_cross_school_official_appearance_respects_enabled_source_and_subscription(self):
        record_source(self.notice, self.foreign_dept)
        db.session.add(Subscription(user_id=self.other.id, school_id=self.foreign.id, department_ids=[self.foreign_dept.id]))
        self.school.enabled = False
        db.session.commit()
        ids = {a.id for a in filtered_inbox(self.other.id, MultiDict({'period': 'all'}))}
        self.assertEqual(ids, {self.notice.id, self.foreign_notice.id})
        self.assertEqual(self.client_as(self.other.id).get('/?period=all').status_code, 200)
        public = self.app.test_client()
        for path in ('/search?q=选课', '/school/' + str(self.foreign.id),
                     '/announcement/' + str(self.notice.id)):
            response = public.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn('北大新闻', response.text)
            self.assertNotIn('本科教育', response.text)
        self.foreign.enabled = False
        db.session.commit()
        self.assertEqual(filtered_inbox(self.other.id, MultiDict({'period': 'all'})).count(), 0)
        self.assertEqual(self.write('/api/announcements/' + str(self.notice.id) + '/read').status_code, 404)

    def test_saved_and_read_state_survive_adding_membership_and_unsubscribing(self):
        self.state(self.notice, starred=True)
        db.session.add(UserRead(user_id=self.user.id, announcement_id=self.notice.id))
        self.add_secondary()
        db.session.delete(Subscription.query.one())
        db.session.commit()
        response = self.client.get('/?view=saved&period=all&school=' + str(self.school.id) + '&dept=' + str(self.news.id))
        self.assertEqual(response.status_code, 200)
        self.assertIn('选课通知', response.text)
        self.assertEqual(UserRead.query.filter_by(announcement_id=self.notice.id).count(), 1)

    def test_distinct_official_urls_with_same_text_are_not_discarded_by_content_hash(self):
        self.news.link_selector = self.news.title_selector = 'a'
        self.news.content_selector = ''
        for i in (101, 102):
            item = BeautifulSoup('<li><a href="/notice/' + str(i) + '">关于开展新学期校园安全教育活动的通知</a></li>', 'lxml').li
            self.assertTrue(_process_announcement_item(item, self.news, self.school.url))
        db.session.commit()
        self.assertEqual(Announcement.query.count(), 6)
        self.assertEqual(AnnouncementSource.query.count(), 2)

    def test_placeholder_without_article_url_cannot_create_a_fake_original(self):
        self.news.link_selector = self.news.title_selector = 'a'
        item = BeautifulSoup('<li><a href="#">关于开展新学期校园安全教育活动的通知</a></li>', 'lxml').li
        self.assertFalse(_process_announcement_item(item, self.news, self.school.url))
        self.assertEqual(Announcement.query.count(), 4)

    def test_school_counts_count_shared_articles_once_per_school(self):
        from backend.services.read_state import total_count_by_school, unread_count_by_school
        self.add_secondary()
        record_source(self.notice, self.foreign_dept)
        db.session.add(UserRead(user_id=self.user.id, announcement_id=self.notice.id))
        db.session.commit()
        schools = [self.school.id, self.foreign.id]
        self.assertEqual(total_count_by_school(schools), {self.school.id: 3, self.foreign.id: 2})
        self.assertEqual(unread_count_by_school(self.user.id, schools), {self.school.id: 1, self.foreign.id: 1})
        self.assertEqual(self.foreign.to_dict()['announcement_count'], 2)

    def test_legacy_bulk_read_respects_selected_column_subscription(self):
        self.add_secondary()
        Subscription.query.one().department_ids = [self.news.id]
        db.session.commit()
        response = self.write('/api/announcements/read-all', {'school_id': self.school.id})
        self.assertEqual(response.json['count'], 2)
        self.assertEqual({r.announcement_id for r in UserRead.query.all()}, {self.notice.id, self.other_notice.id})

    def test_invalid_column_filter_cannot_widen_scope(self):
        for value in (str(self.foreign_dept.id), '9999', 'invalid'):
            args = MultiDict({'school': str(self.school.id), 'dept': value, 'period': 'all'})
            self.assertEqual(filtered_inbox(self.user.id, args).count(), 0)

    def test_deleting_original_column_keeps_other_official_appearance_and_personal_state(self):
        self.add_secondary()
        self.user.role = 'admin'
        self.state(self.notice, starred=True)
        db.session.add(UserRead(user_id=self.user.id, announcement_id=self.notice.id))
        db.session.commit()
        notice_id, news_id = self.notice.id, self.news.id
        response = self.write('/api/departments/' + str(self.teaching.id), method='delete')
        self.assertEqual(response.status_code, 200)
        retained = db.session.get(Announcement, notice_id)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.department_id, news_id)
        self.assertEqual(retained.content_text, '选课办理方式')
        self.assertEqual(UserRead.query.filter_by(announcement_id=notice_id).count(), 1)
        self.assertIn('选课通知', self.client.get('/?view=saved&period=all').text)

    def test_deleting_original_school_preserves_cross_school_appearance(self):
        record_source(self.notice, self.foreign_dept)
        self.user.role = 'admin'
        self.state(self.notice, starred=True)
        db.session.commit()
        notice_id, foreign_id = self.notice.id, self.foreign.id
        response = self.write('/api/schools/' + str(self.school.id), method='delete')
        self.assertEqual(response.status_code, 200)
        retained = db.session.get(Announcement, notice_id)
        self.assertIsNotNone(retained)
        self.assertEqual(retained.school_id, foreign_id)
        self.assertIn('选课通知', self.client.get('/?view=saved&period=all').text)


if __name__ == '__main__':
    unittest.main()
