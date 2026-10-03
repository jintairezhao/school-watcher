"""A reader can find whole publishing channels without widening their inbox."""
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from flask import g

import test_lightweight as fixture
from backend.database.db import db
from backend.database.models import BackgroundTask, Department, Subscription, UserRead, UserAnnouncementState
from backend.services.announcement_sources import record_source
from backend.services.channel_views import channel_overview


class ChannelNavigationTests(unittest.TestCase):
    setUp = fixture.LightweightTests.setUp
    tearDown = fixture.LightweightTests.tearDown
    client_as = fixture.LightweightTests.client_as
    write = fixture.LightweightTests.write
    state = fixture.LightweightTests.state

    def overview(self, **kwargs):
        with self.app.test_request_context('/channels'):
            g.user = self.user
            return channel_overview(self.user.id, **kwargs)

    @staticmethod
    def rows(result):
        return {row['source_id']: row for school in result['channel_schools']
                for publisher in school['publishers'] for row in publisher['channels']}

    def test_counts_each_observed_channel_but_deduplicates_overall_total(self):
        record_source(self.notice, self.news, self.school.url + '/news/also-published')
        db.session.add(UserRead(user_id=self.user.id, announcement_id=self.notice.id))
        db.session.commit()
        before = BackgroundTask.query.count()
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No AI during channel browsing')):
            result = self.overview()
        rows = self.rows(result)
        self.assertEqual(result['total_notices'], 3)
        self.assertEqual(result['total_channels'], 2)
        self.assertEqual(rows[self.teaching.id]['count'], 2)
        self.assertEqual(rows[self.news.id]['count'], 2)
        self.assertEqual(rows[self.news.id]['unread_count'], 1)
        self.assertEqual(BackgroundTask.query.count(), before)
        address = parse_qs(urlsplit(rows[self.teaching.id]['filter_url']).query)
        self.assertEqual(address['period'], ['all'])
        self.assertEqual(address['dept'], [str(self.teaching.id)])

    def test_selected_subscription_hides_unselected_and_foreign_channels(self):
        record_source(self.notice, self.news)
        Subscription.query.one().department_ids = [self.news.id]
        db.session.commit()
        result = self.overview()
        self.assertEqual(set(self.rows(result)), {self.news.id})
        self.assertEqual(result['total_notices'], 2)
        self.assertEqual(self.overview(school_id=self.foreign.id)['total_channels'], 0)
        with self.app.test_request_context('/channels'):
            g.user = self.other
            self.assertEqual(channel_overview(self.other.id)['total_channels'], 0)

    def test_saved_channel_survives_unsubscribe_and_keeps_saved_scope(self):
        self.state(self.notice, starred=True)
        record_source(self.notice, self.news)
        db.session.delete(Subscription.query.one())
        db.session.commit()
        result = self.overview(view='saved')
        self.assertEqual(result['total_notices'], 1)
        self.assertEqual(set(self.rows(result)), {self.teaching.id, self.news.id})
        for row in self.rows(result).values():
            self.assertEqual(row['count'], 1)
            self.assertEqual(parse_qs(urlsplit(row['filter_url']).query)['view'], ['saved'])
        self.assertEqual(self.overview()['total_channels'], 0)

    def test_retained_channel_remains_findable_without_resubscribing(self):
        # Archive is retained legacy data, not an operation offered by today's UI.
        db.session.add(UserAnnouncementState(user_id=self.user.id, announcement_id=self.old.id, archived=True))
        db.session.delete(Subscription.query.one())
        db.session.commit()
        result = self.overview()
        self.assertEqual(result['total_notices'], 1)
        self.assertEqual(set(self.rows(result)), {self.teaching.id})

    def test_search_and_disabled_school_cannot_widen_the_directory(self):
        result = self.overview(q='校园要闻')
        self.assertEqual(set(self.rows(result)), {self.news.id})
        self.assertEqual(result['total_notices'], 1)
        self.assertEqual(self.overview(q='不存在的渠道')['total_channels'], 0)
        self.school.enabled = False
        db.session.commit()
        self.assertEqual(self.overview()['total_channels'], 0)

    def test_verified_empty_channel_is_listed_without_empty_organisation_nodes(self):
        channel = Department(school_id=self.school.id, name='实习机会',
            list_url='https://career.tsinghua.edu.cn/internships/', list_selector='li.notice')
        unit = Department(school_id=self.school.id, name='空学院', kind='unit',
            list_url='https://college.tsinghua.edu.cn/')
        db.session.add_all([channel, unit]); db.session.commit()
        rows = self.rows(self.overview())
        self.assertIn(channel.id, rows)
        self.assertEqual(rows[channel.id]['count'], 0)
        self.assertNotIn(unit.id, rows)

    def test_collection_time_is_not_presented_as_a_publication_date(self):
        self.notice.published_at = self.old.published_at = None
        db.session.commit()
        row = self.rows(self.overview())[self.teaching.id]
        self.assertEqual(row['count'], 2)
        self.assertIsNone(row['latest_at'])
        page = BeautifulSoup(self.client.get('/channels').text, 'lxml')
        self.assertIn('发布日期未注明', page.select_one(f'[data-channel-id="{self.teaching.id}"]').get_text())

    def test_channel_page_links_to_complete_history_and_preserves_actual_publication_urls(self):
        record_source(self.notice, self.news, self.school.url + '/news/also-published')
        db.session.commit()
        response = self.client.get('/channels')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        page = BeautifulSoup(response.text, 'lxml')
        link = next(a for a in page.select('a[href]') if parse_qs(urlsplit(a['href']).query).get('dept') == [str(self.teaching.id)])
        history = self.client.get(link['href'])
        self.assertIn('历史资料通知', history.text)
        article = BeautifulSoup(self.client.get(f'/announcement/{self.notice.id}').text, 'lxml')
        self.assertIsNotNone(article.select_one('a[href="' + self.school.url + '/news/also-published"]'))
        self.assertEqual(self.app.test_client().get('/channels').status_code, 302)


if __name__ == '__main__':
    unittest.main()
