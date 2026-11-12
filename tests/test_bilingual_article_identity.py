"""Literal multilingual titles can span styled lines without becoming prose matches."""
import unittest

from bs4 import BeautifulSoup
from backend.scraper.detectors.title_quality import extract_title_text, observed_title_variants

EN = '2026 Admission Brochure for International Students’ Master’s Programs at Example University (EU)'
CN1 = '2026年示例大学'
CN2 = '来华留学生硕士项目招生简章'
FULL = EN + CN1 + CN2
SHORT = EN[:72]
URL = 'https://example.edu.cn/info/1.htm'


class BilingualListingTitleTests(unittest.TestCase):
    def element(self, attribute=FULL):
        return BeautifulSoup(f'<a href="/info/1.htm" title="{attribute}"><h3>{SHORT}</h3></a>', 'lxml')

    def test_same_anchor_supplies_its_literal_complete_bilingual_title(self):
        soup = self.element()
        self.assertEqual(extract_title_text(soup.h3, anchor=soup.a, page_url=URL), FULL)
        self.assertIn(SHORT, observed_title_variants(soup.h3, FULL, anchor=soup.a, page_url=URL))

    def test_unrelated_attribute_or_foreign_anchor_is_not_a_title_expansion(self):
        soup = self.element(FULL.replace('2026', '2027'))
        self.assertEqual(extract_title_text(soup.h3, anchor=soup.a, page_url=URL), SHORT)
        soup = self.element()
        other = BeautifulSoup('<a href="/info/2.htm">其他通知</a>', 'lxml').a
        self.assertEqual(extract_title_text(soup.h3, anchor=other, page_url=URL), '')
        unlinked = BeautifulSoup(f'<div><h3>{SHORT}</h3><a href="/info/1.htm" title="{FULL}">原文</a></div>', 'lxml')
        self.assertEqual(extract_title_text(unlinked.h3, anchor=unlinked.a, page_url=URL), SHORT)

    def test_chinese_headlines_and_short_generic_prefixes_are_not_expanded(self):
        for visible, attribute in [('通知公告', '通知公告 News 2026年示例大学'), ('2026 Admission', FULL)]:
            soup = BeautifulSoup(f'<a href="/info/1.htm" title="{attribute}"><h3>{visible}</h3></a>', 'lxml')
            self.assertEqual(extract_title_text(soup.h3, anchor=soup.a, page_url=URL), visible)


class BilingualArticleIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def heading(self, value):
        return f'<p style="text-align:center"><strong>{value}</strong></p>'

    def document(self, fragments=None, before='', after='', extra=''):
        fragments = [CN1, CN2, EN[:50], EN[50:]] if fragments is None else fragments
        return ('<title>University website</title>' + extra + '<div class="article-content">' + before
                + ''.join(self.heading(v) for v in fragments) + after
                + '<p>请申请人按学校公布的招生条件提交申请材料，认真核对申请时间、培养要求和办理流程。</p></div>')

    def sample(self, html):
        from backend.services.source_validation import article_samples
        return article_samples(dict(list_url='https://example.edu.cn/notices/', content_selector='.article-content'),
                               [dict(url=URL, title=FULL)], lambda *_: html)['samples'][0]

    def test_exact_bilingual_title_can_rotate_its_complete_heading_lines(self):
        sample = self.sample(self.document())
        self.assertTrue(sample['matched'], sample)
        self.assertEqual(sample['title_identity']['basis'], 'literal_bilingual_heading_block')
        self.assertEqual(len(sample['title_identity']['locators']), 4)

    def test_wrong_year_or_missing_language_cannot_prove_identity(self):
        for fragments in ([CN1.replace('2026', '2027'), CN2, EN], [EN], [CN1, CN2, EN[:50]]):
            with self.subTest(fragments=fragments):
                self.assertFalse(self.sample(self.document(fragments))['matched'])

    def test_title_quoted_later_in_prose_or_sidebar_is_not_a_heading(self):
        for html in (self.document(before='<p>正文首先讨论另一个事项，以下引用其他学校的招生简章作为参考。</p>'),
                     '<div class="article-content"><p>请同学按另一个通知的要求提交相关申请材料，注意确认办理时间与责任单位。</p></div>'
                     + '<aside>' + self.document() + '</aside>'):
            with self.subTest(html=html): self.assertFalse(self.sample(html)['matched'])

    def test_conflicting_actual_headline_still_overrules_matching_body_title_block(self):
        sample = self.sample(self.document(extra='<article><h1>另一项明确的学生事务通知</h1></article>'))
        self.assertFalse(sample['matched'])
        self.assertEqual(sample['reason'], 'article_identity_conflict')

    def test_hidden_or_linked_heading_fragments_do_not_count(self):
        for marker in ('hidden', 'aria-hidden="true"'):
            with self.subTest(marker=marker):
                html = self.document().replace('<strong>' + CN2, '<strong ' + marker + '>' + CN2)
                self.assertFalse(self.sample(html)['matched'])
        self.assertFalse(self.sample(self.document().replace(CN2, '<a href="/other.htm">' + CN2 + '</a>'))['matched'])

    def test_complete_literal_title_updates_the_same_notice_and_keeps_personal_state(self):
        from backend.database.db import db
        from backend.database.models import Department, Announcement, UserRead, UserAnnouncementState
        from backend.services.announcement_identity import upsert_listing
        from backend.scraper.engine import _process_announcement_item
        source = Department(school_id=self.fixture.school_id, name='News and Information',
            list_url='https://example.edu.cn/notices/', list_selector='li', title_selector='h3',
            link_selector='a', date_selector='span', content_selector='.article-content')
        db.session.add(source); db.session.flush()
        article, _ = upsert_listing(source, SHORT, URL)
        db.session.flush()
        ident = article.id
        db.session.add(UserRead(user_id=1, announcement_id=ident))
        db.session.add(UserAnnouncementState(user_id=1, announcement_id=ident, starred=True))
        db.session.commit()
        item = BeautifulSoup(f'<li><a href="{URL}" title="{FULL}"><h3>{SHORT}</h3></a><span>2026-10-01</span></li>', 'lxml').li
        self.assertFalse(_process_announcement_item(item, source, 'https://example.edu.cn/'))
        db.session.commit()
        self.assertEqual(Announcement.query.count(), 1)
        self.assertEqual(db.session.get(Announcement, ident).title, FULL)
        self.assertEqual(UserRead.query.filter_by(user_id=1, announcement_id=ident).count(), 1)
        self.assertTrue(UserAnnouncementState.query.filter_by(user_id=1, announcement_id=ident).one().starred)


if __name__ == '__main__':
    unittest.main()
