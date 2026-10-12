"""An article card is not its headline; all readers share field extraction."""
import unittest
from bs4 import BeautifulSoup

from backend.scraper.detectors.title_quality import extract_title_text, extract_best_title


CARD = '''<li><a class="item" href="/xsjz/20261014/226487.html">
<div class="time"><span>2026-10-14 14:00—15:30</span></div>
<p class="dot title">如何在大学阶段学好英语</p>
<div class="tag dot">学习能力提升营</div>
<div class="address">闵行校区东中院1-106</div><div class="person">陶庆</div>
</a></li>'''


class NoticeTitleFieldTests(unittest.TestCase):
    def test_real_sjtu_card_excludes_time_venue_speaker_and_tag(self):
        item = BeautifulSoup(CARD, 'lxml').li
        self.assertEqual(extract_title_text(item.a, anchor=item.a), '如何在大学阶段学好英语')
        self.assertEqual(extract_best_title(item, article_url=item.a['href']), '如何在大学阶段学好英语')

    def test_metadata_fields_are_removed_without_stripping_title_numbers(self):
        for title in ('2026-2027学年课程教学质量通知', '5G实验室开放通知', '50—13:20实验编号说明'):
            item = BeautifulSoup(f'<a href="/info/1.htm"><time>2026-10-12 14:00</time>'
                f'<span>{title}</span><span class="author">张老师</span></a>', 'lxml').a
            self.assertEqual(extract_title_text(item, anchor=item), title)

    def test_neighbouring_article_cannot_supply_its_title(self):
        item = BeautifulSoup('<li><a href="/info/1.htm">2026-10-12</a>'
            '<a href="/info/2.htm"><h3>关于另一个项目申报的通知</h3></a></li>', 'lxml').li
        self.assertEqual(extract_best_title(item, article_url='/info/1.htm'), '')

    def test_attributes_and_wrapped_title_preserve_legitimate_years(self):
        item = BeautifulSoup('<a title="2026-2027学年课程安排通知" href="/info/1.htm">'
            '<h3>显示截断…</h3></a>', 'lxml').a
        self.assertEqual(extract_title_text(item, anchor=item), '2026-2027学年课程安排通知')
        item = BeautifulSoup('<a href="/info/1.htm"><span class="date">06-15 2026</span>'
            '<div class="des">2026年本科生招生专业目录</div></a>', 'lxml').a
        self.assertEqual(extract_title_text(item, anchor=item), '2026年本科生招生专业目录')

    def test_ambiguous_fields_are_not_concatenated_into_a_fake_headline(self):
        item = BeautifulSoup('<a href="/info/1.htm"><h3>项目一报名通知</h3>'
            '<h3>项目二报名通知</h3></a>', 'lxml').a
        self.assertEqual(extract_title_text(item, anchor=item), '')
        self.assertEqual(extract_best_title(item, article_url=item['href']), '')

    def test_collection_repairs_only_a_reobserved_primary_title_without_losing_state(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import (Department, Announcement, AnnouncementSource,
            Subscription, UserRead, UserAnnouncementState, User)
        from backend.scraper.engine import _process_announcement_item
        from backend.services.announcement_sources import record_source
        fixture = DirectOnboardingTests(); fixture.setUp()
        try:
            source = Department(school_id=fixture.school_id, name='讲座列表',
                list_url='https://example.edu.cn/xsjz/index.html', list_selector='li',
                title_selector='a', link_selector='a', date_selector='.time')
            other = Department(school_id=fixture.school_id, name='其他栏目', list_url='https://example.edu.cn/other/')
            db.session.add_all([source, other]); db.session.flush()
            notice = Announcement(school_id=fixture.school_id, department_id=source.id,
                title='00—15:30如何在大学阶段学好英语学习能力提升营闵行校区东中院1-106陶庆',
                url='https://example.edu.cn/xsjz/20261014/226487.html',
                content_text='保留正文', summary='保留历史摘要')
            db.session.add(notice); db.session.flush()
            user = User.query.one()
            db.session.add_all([UserRead(user_id=user.id, announcement_id=notice.id),
                UserAnnouncementState(user_id=user.id, announcement_id=notice.id, starred=True, archived=True)])
            record_source(notice, source); record_source(notice, other); db.session.commit()
            before = (notice.id, notice.created_at, notice.url, notice.department_id, notice.content_text,
                notice.summary, list(Subscription.query.one().department_ids or []))
            item = BeautifulSoup(CARD, 'lxml').li
            self.assertFalse(_process_announcement_item(item, source, 'https://example.edu.cn/'))
            db.session.commit()
            self.assertEqual(notice.title, '如何在大学阶段学好英语')
            self.assertEqual(before, (notice.id, notice.created_at, notice.url, notice.department_id,
                notice.content_text, notice.summary, list(Subscription.query.one().department_ids or [])))
            self.assertEqual(Announcement.query.count(), 1)
            self.assertEqual(AnnouncementSource.query.count(), 2)
            self.assertEqual(UserRead.query.count(), 1)
            self.assertTrue(UserAnnouncementState.query.one().starred)
            self.assertTrue(UserAnnouncementState.query.one().archived)
            # Valid older wording is not rewritten simply because a list changes.
            notice.title = '英语学习课程安排通知'; db.session.commit()
            self.assertFalse(_process_announcement_item(item, source, 'https://example.edu.cn/'))
            self.assertEqual(notice.title, '英语学习课程安排通知')
        finally:
            fixture.tearDown()


if __name__ == '__main__':
    unittest.main()
