"""Publication dates must not come from lecture schedules, deadlines or address paths."""
from datetime import datetime
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import Announcement, Department, UserAnnouncementState, UserRead

TITLE='从知识传授到创新孵化：课堂教学的模式重构'
URL='https://fixture.example.edu/lecture/20261007/123.html'
ARTICLE=f'<article><h2>{TITLE}</h2><div><span>2026年09月30日</span><span>责任编辑：张老师</span></div><div><p>讲座时间：2026-10-16 11:50—13:20</p><p>这是课堂教学活动的完整说明。</p></div></article>'
ITEM=f'<li><a href="{URL}"><div class="time"><span>2026-10-16 11:50—13:20</span></div><p class="title">{TITLE}</p></a></li>'


class PublicationDateTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture=DirectOnboardingTests();self.fixture.setUp()
        self.source=Department(school_id=self.fixture.school_id,name='通知公告',list_url='https://fixture.example.edu/lecture/',
            title_selector='p.title',link_selector='a',date_selector='span')
        db.session.add(self.source);db.session.flush()
        self.ann=Announcement(school_id=self.source.school_id,department_id=self.source.id,title=TITLE,url=URL,
            published_at=datetime(2026,10,16),content_html=ARTICLE,content_text='已保存的正文',content_cached_at=datetime.utcnow())
        db.session.add(self.ann);db.session.commit()
    def tearDown(self):self.fixture.tearDown()

    def test_event_time_is_not_a_publication_date(self):
        from backend.scraper.date_elements import publication_date_text
        for text in ['2026-10-16 11:50—13:20','讲座时间：2026-10-16','报名截止：2026-10-16','更新时间：2026-10-16']:
            self.assertEqual(publication_date_text(BeautifulSoup(f'<span>{text}</span>','lxml').span),'')
        self.assertEqual(publication_date_text(BeautifulSoup('<span>发布日期：2026-09-30</span>','lxml').span),'发布日期：2026-09-30')

    def test_title_bound_visible_metadata_overrides_event_and_url_dates(self):
        from backend.services.publication_dates import inspect_article_date
        evidence=inspect_article_date(ARTICLE,URL,TITLE)
        self.assertEqual(evidence['state'],'verified')
        self.assertEqual(evidence['date'],'2026-09-30')
        self.assertNotIn('2026-10-16',evidence['text'])
        self.assertEqual(inspect_article_date(ARTICLE,URL,'另一篇完全不同的通知')['state'],'unknown')

    def test_event_only_or_quoted_publication_cannot_supply_a_publish_date(self):
        from backend.services.publication_dates import inspect_article_date
        for body in ['<p>讲座时间：2026-10-16</p>',
                     '<blockquote><p>发布日期：2026-09-30</p></blockquote>',
                     '<p>请参考其他公告，发布日期：2026-09-30。</p>']:
            evidence=inspect_article_date(f'<article><h1>{TITLE}</h1>{body}</article>',URL,TITLE)
            self.assertEqual(evidence['state'],'unknown')

    def test_cached_body_corrects_existing_date_and_preserves_personal_records(self):
        from backend.services.publication_dates import reconcile_cached_date
        from backend.database.models import User
        user=User.query.one()
        db.session.add_all([UserRead(user_id=user.id,announcement_id=self.ann.id),
            UserAnnouncementState(user_id=user.id,announcement_id=self.ann.id,starred=True)])
        db.session.commit()
        before=(self.ann.id,self.ann.content_html,UserRead.query.one().read_at)
        self.assertTrue(reconcile_cached_date(self.ann))
        db.session.commit()
        self.assertEqual(self.ann.published_at,datetime(2026,9,30))
        self.assertEqual((self.ann.id,self.ann.content_html,UserRead.query.one().read_at),before)
        self.assertTrue(UserAnnouncementState.query.one().starred)
        self.assertFalse(reconcile_cached_date(self.ann))

    def test_event_list_does_not_install_an_address_date_and_reuses_correct_body(self):
        from backend.scraper.engine import _process_announcement_item
        item=BeautifulSoup(ITEM,'lxml').li
        _process_announcement_item(item,self.source,self.source.school.url)
        db.session.commit()
        self.assertEqual(self.ann.published_at,datetime(2026,9,30))
        self.assertEqual(Announcement.query.count(),1)

    def test_event_list_without_body_keeps_publication_unknown_and_queues_a_bounded_read(self):
        from backend.scraper.engine import _process_announcement_item
        from backend.database.models import BackgroundTask
        self.ann.content_html='';self.ann.content_cached_at=None;db.session.commit()
        _process_announcement_item(BeautifulSoup(ITEM,'lxml').li,self.source,self.source.school.url)
        db.session.commit()
        self.assertIsNone(self.ann.published_at)
        jobs=BackgroundTask.query.filter_by(kind='publication_date').all()
        self.assertEqual(len(jobs),1)
        _process_announcement_item(BeautifulSoup(ITEM,'lxml').li,self.source,self.source.school.url)
        self.assertEqual(BackgroundTask.query.filter_by(kind='publication_date').count(),1)

    def test_body_fetch_corrects_date_even_when_old_list_date_is_already_present(self):
        from backend.services.content_cache import fetch_content
        self.ann.content_html='';self.ann.content_cached_at=None;db.session.commit()
        with patch('backend.scraper.engine._fetch_html',return_value=ARTICLE):
            fetch_content(self.ann.id)
        self.assertEqual(self.ann.published_at,datetime(2026,9,30))

    def test_verified_date_does_not_revert_when_event_day_matches_the_publication_day(self):
        from backend.services.publication_dates import reconcile_cached_date
        self.ann.content_html=ARTICLE.replace('2026年09月30日','2026年10月16日')
        reconcile_cached_date(self.ann);db.session.commit()
        from backend.scraper.engine import _process_announcement_item
        _process_announcement_item(BeautifulSoup(ITEM,'lxml').li,self.source,self.source.school.url)
        db.session.commit()
        self.assertEqual(self.ann.published_at,datetime(2026,10,16))

    def test_incomplete_body_read_cannot_erase_a_verified_publication_date(self):
        from backend.services.publication_dates import reconcile_cached_date, apply_article_date
        from backend.scraper.engine import _process_announcement_item
        self.ann.content_html=ARTICLE.replace('2026年09月30日','2026年10月16日')
        reconcile_cached_date(self.ann);db.session.commit()
        incomplete=f'<article><h1>{TITLE}</h1><p>讲座时间：2026-10-16</p></article>'
        apply_article_date(self.ann,incomplete,URL);db.session.commit()
        _process_announcement_item(BeautifulSoup(ITEM,'lxml').li,self.source,self.source.school.url)
        db.session.commit()
        self.assertEqual(self.ann.published_at,datetime(2026,10,16),
                         'a later incomplete observation must not erase previously verified publication metadata')

    def test_unrelated_deadline_in_list_row_does_not_change_the_date_field_role(self):
        from backend.scraper.date_elements import publication_date_text
        node=BeautifulSoup('<li><a href="/notice">申请通知</a><span class="date">2026-09-30</span><p>报名截止10月16日。</p></li>','lxml').span
        self.assertEqual(publication_date_text(node),'2026-09-30')

    def test_missing_year_is_not_filled_from_the_current_year(self):
        from backend.scraper.date_elements import publication_date_text
        self.assertEqual(publication_date_text(BeautifulSoup('<span>09-30</span>','lxml').span),'')
        self.assertEqual(publication_date_text(BeautifulSoup('<span>Jan 19, 2026</span>','lxml').span),'Jan 19, 2026')

    def test_unrecognized_header_does_not_repeat_paid_or_network_work_each_collection(self):
        from backend.services.publication_dates import check_date
        from backend.scraper.engine import _process_announcement_item
        self.ann.content_html='';self.ann.content_cached_at=None;db.session.commit()
        item=BeautifulSoup(ITEM,'lxml').li
        _process_announcement_item(item,self.source,self.source.school.url);db.session.commit()
        with patch('backend.scraper.engine._fetch_html',return_value=f'<article><h1>{TITLE}</h1><p>讲座时间：2026-10-16</p></article>') as fetch:
            self.assertEqual(check_date(self.ann.id)['state'],'unknown')
            for _ in range(5):
                _process_announcement_item(item,self.source,self.source.school.url);db.session.commit()
            self.assertEqual(fetch.call_count,1)
        self.assertIsNone(self.ann.published_at)

    def test_scheduler_corrects_saved_dates_without_reading_websites(self):
        from backend.services.publication_dates import resume_date_checks
        with patch('backend.scraper.engine._fetch_html') as fetch:
            resume_date_checks();resume_date_checks()
            fetch.assert_not_called()
        self.assertEqual(self.ann.published_at,datetime(2026,9,30))

    def test_parser_upgrade_rechecks_unknown_saved_header_without_touching_body_or_user_state(self):
        from backend.database.models import BackgroundTask, User
        from backend.services.publication_dates import resume_date_checks, PARSER_VERSION
        self.ann.published_at=None
        self.ann.content_html=f'<article><h6>{TITLE}</h6><span>2026/09/30</span><p>已永久保存的正文。</p></article>'
        old_body=self.ann.content_html
        user=User.query.one()
        db.session.add(UserAnnouncementState(user_id=user.id,announcement_id=self.ann.id,starred=True))
        db.session.add(BackgroundTask(identity=f'publication_date:{self.ann.id}',kind='publication_date',
            state='done',payload={'announcement_id':self.ann.id},result={'state':'unknown',
            'parser_version':'title-bound-3','checked_full':True},checkpoint={}))
        db.session.commit()
        with patch('backend.scraper.engine._fetch_html') as fetch:
            resume_date_checks();resume_date_checks()
            fetch.assert_not_called()
        job=BackgroundTask.query.filter_by(kind='publication_date').one()
        self.assertEqual(self.ann.published_at,datetime(2026,9,30))
        self.assertEqual(self.ann.content_html,old_body)
        self.assertTrue(UserAnnouncementState.query.one().starred)
        self.assertEqual(job.result['parser_version'],PARSER_VERSION)
        self.assertEqual(job.result['state'],'verified')

    def test_date_correction_does_not_grant_another_user_access(self):
        from backend.database.models import User
        from backend.services.publication_dates import reconcile_cached_date
        other=User(username='unsubscribed',password_hash='unused');db.session.add(other)
        reconcile_cached_date(self.ann);db.session.commit()
        client=self.fixture.app.test_client()
        with client.session_transaction() as s:s['user_id']=other.id
        response=client.get(f'/?school={self.source.school_id}&period=week&selected={self.ann.id}')
        self.assertEqual(response.status_code,200)
        self.assertNotIn(TITLE,response.text)

    def test_date_correction_keeps_selected_reader_and_personal_state_outside_current_page(self):
        from backend.database.models import User
        user=User.query.one()
        db.session.add(UserAnnouncementState(user_id=user.id,announcement_id=self.ann.id,starred=True));db.session.commit()
        self.source.school.subscriber_count=1
        for i in range(45):
            db.session.add(Announcement(school_id=self.source.school_id,department_id=self.source.id,title=f'其他通知{i}',
                url=f'https://fixture.example.edu/info/{i}',published_at=datetime(2026,10,10)))
        db.session.commit()
        client=self.fixture.app.test_client()
        with client.session_transaction() as s:s['user_id']=user.id
        response=client.get(f'/?school={self.source.school_id}&period=all&selected={self.ann.id}')
        self.assertEqual(response.status_code,200)
        soup=BeautifulSoup(response.text,'lxml')
        self.assertIn(TITLE,soup.select_one('.reader-document').get_text())
        self.assertIn('2026年09月30日',soup.select_one('.reading-meta time').get_text())
        self.assertEqual(UserRead.query.one().announcement_id,self.ann.id)
        self.assertTrue(UserAnnouncementState.query.one().starred)


if __name__=='__main__':unittest.main()
