"""Column observations distinguish publication headings from linked card titles."""
import unittest

from bs4 import BeautifulSoup
from backend.scraper.discovery.column_regions import materials

URL = 'https://example.edu.cn/announcements/'


class ColumnObservationTests(unittest.TestCase):
    def rows(self, html):
        return [row for part in materials(1, URL, html)
                for proof in part['evidence'] for row in proof.get('heading_locations', [])]

    def test_card_article_headings_are_not_column_heading_observations(self):
        html = '<main><h2>通知公告</h2><div class="cards">' + ''.join(
            f'<div class="card"><a href="/info/10/{10000+i}.htm"><h3>关于组织学术活动的通知{i}</h3></a>'
            '<span>2026-10-15</span></div>' for i in range(3)) + '</div></main>'
        self.assertEqual([row['heading'] for row in self.rows(html)], ['通知公告'])

    def test_current_page_links_supply_literal_heading_and_original_position(self):
        html = ('<main id="content"><div class="crumb"><a href="/">首页</a> / '
                '<a href="/announcements/">通知公告</a></div><div class="cards">'
                '<div class="card"><a href="/info/10/10001.htm"><h3>关于组织学术交流的通知</h3></a>'
                '<span>2026-10-15</span></div></div></main>')
        rows = self.rows(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['heading'], '通知公告')
        self.assertEqual(rows[0]['basis'], 'observed_current_page_link')
        soup = BeautifulSoup(html, 'lxml')
        self.assertEqual(soup.select_one(rows[0]['selector'])['href'], '/announcements/')
        self.assertEqual(soup.select_one(rows[0]['containing_region_selector'])['id'], 'content')

    def test_current_page_role_does_not_promote_other_pages_or_hash_placeholders(self):
        html = ('<main><div class="crumb"><a href="/other/">邻近栏目</a><a href="#">当前</a></div>'
                '<h2>真实栏目标题</h2><a href="/info/10/10001.htm">关于组织本学期学术交流的通知</a>'
                '<span>2026-10-15</span></main>')
        self.assertEqual([row['heading'] for row in self.rows(html)], ['真实栏目标题'])

    def test_header_self_links_are_not_reintroduced_after_navigation_is_cleared(self):
        html = ('<header><nav><a href="/announcements/">通知公告</a></nav></header>'
                '<main><h2>本栏目</h2><a href="/info/10/10001.htm">关于组织本学期学术交流的通知</a>'
                '<span>2026-10-15</span></main>')
        self.assertEqual([row['heading'] for row in self.rows(html)], ['本栏目'])

    def test_local_column_navigation_is_retained_with_its_observed_target(self):
        html = ('<header><nav><a href="/about/">学校概况</a></nav></header>'
                '<main id="content"><div class="section-nav"><ul><li>'
                '<a href="/announcements/index.html">通知公告</a></li></ul></div>'
                '<div class="cards"><div class="card"><a href="/info/10/10001.htm">'
                '<h3>关于组织学术交流的通知</h3></a><span>2026-10-15</span></div></div></main>')
        parts = materials(1, URL, html)
        rows = [row for part in parts for proof in part['evidence']
                for row in proof.get('navigation_locations', [])]
        self.assertEqual([row['text'] for row in rows], ['通知公告'])
        self.assertEqual(rows[0]['url'], URL + 'index.html')
        soup = BeautifulSoup(html, 'lxml')
        self.assertEqual(soup.select_one(rows[0]['selector']).get_text(), '通知公告')
        self.assertTrue(any('通知公告' in proof['html'] for part in parts for proof in part['evidence']))
        self.assertFalse(any('学校概况' in proof['html'] for part in parts for proof in part['evidence']))

    def test_malformed_navigation_does_not_hide_valid_column_material(self):
        html = ('<main><nav><a href="http://[bad">无效入口</a>'
                '<a href="/announcements/">通知公告</a></nav><section>'
                '<a href="/info/10/10001.htm">关于组织本学期学术交流的通知</a>'
                '<span>2026-10-15</span></section></main>')
        self.assertEqual([row['heading'] for row in self.rows(html)], ['通知公告'])
