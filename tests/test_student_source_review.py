"""Regressions from the student-facing source review, without live websites."""
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.acquisition import FetchRequest, FetchResult, fetch
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.discovery.publication_lists import heading_evidence
from backend.services.student_sources import student_priority
from bs4 import BeautifulSoup


class StudentSourceReviewTests(unittest.TestCase):
    def test_required_directory_navigation_cannot_be_replaced_by_news(self):
        url = 'https://example.edu.cn/'
        partial = '<script src="header.js"></script><a href="/news/1">校园新闻</a>'
        ready = partial + '<nav><a href="/schools/">院系设置</a></nav>'
        calls = []
        def browser(request):
            calls.append(request)
            return FetchResult(url, status=200, html=ready, transport='browser')
        result = fetch(FetchRequest(url, purpose='directory', readiness_selector='nav a[href="/schools/"]'),
                       http_transport=lambda _: FetchResult(url, status=200, html=partial), browser_transport=browser)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.html, ready)
        result = fetch(FetchRequest(url, purpose='directory', readiness_selector='nav a'),
                       http_transport=lambda _: FetchResult(url, status=200, html=partial),
                       browser_transport=lambda _: FetchResult(url, status=200, html=partial, transport='browser'))
        self.assertFalse(result.ok)

    def test_sjtu_grouped_roster_is_content_despite_headerpage_wrapper(self):
        html = '''<div class="headerpage"><div class="Article org-content">
        <h3 class="org-title">工科</h3><ul class="org-list">
        <li><a href="https://www.cs.sjtu.edu.cn/">计算机学院（网络空间安全学院、密码学院）</a></li></ul>
        <h3 class="org-title">交叉学科</h3><ul class="org-list">
        <li><a href="#">量子科技学院</a></li></ul></div></div>'''
        result = extract_structure(html, 'https://www.sjtu.edu.cn/yxsz/',
                                   'https://www.sjtu.edu.cn/', 'directory', '院系设置')
        nodes = {n['name']: n for n in result['nodes']}
        self.assertEqual(nodes['工科']['relation'], 'academic_group')
        self.assertEqual(nodes['计算机学院（网络空间安全学院、密码学院）']['parent'], nodes['工科']['key'])
        self.assertEqual(nodes['计算机学院（网络空间安全学院、密码学院）']['relation'], 'directory_entry')
        self.assertEqual(nodes['量子科技学院']['url'], '')
        self.assertEqual(nodes['量子科技学院']['relation'], 'directory_entry_no_link')

    def test_read_button_does_not_replace_publication_heading(self):
        soup = BeautifulSoup('<section><h2>教学发展基金<a href="/fund/">Read</a></h2>'
                             '<ul><li><a href="/post/1">项目申报通知</a></li></ul></section>', 'lxml')
        self.assertEqual(heading_evidence(soup.li, 'https://example.edu.cn/')['name'], '教学发展基金')

    def test_admissions_and_summer_camp_are_student_priorities(self):
        for label in ('研究生招生', '本科招生', '夏令营', '预推免', '保研通知'):
            self.assertEqual(student_priority('channel', label), 2, label)
        self.assertIsNotNone(student_priority('channel', '学院动态', json.dumps(['计算机学院'])))

    def test_exact_homepage_branding_does_not_require_bare_title(self):
        from backend.services.source_ownership import website_identity_forms, normalized_name
        identity = normalized_name('上海交通大学材料科学与工程学院')
        self.assertIn(identity, website_identity_forms('首页 - 上海交通大学材料科学与工程学院'))
        self.assertNotIn(identity, website_identity_forms('上海交通大学材料科学与工程学院代表团来访'))

    def test_student_order_preserves_news_and_administration(self):
        from backend.services.inbox import source_hierarchy
        from types import SimpleNamespace
        rows = [SimpleNamespace(id=i, school_id=1, name=name, group_name=group)
                for i, (name, group) in enumerate([('人事通知', '组织机构'), ('学校新闻', '校园新闻'),
                    ('计算机学院', '院系设置'), ('研究生招生', '招生就业')], 1)]
        tree = source_hierarchy(rows)
        self.assertEqual(list(tree)[:2], ['院系设置', '招生就业'])
        self.assertEqual({c['department'].id for units in tree.values() for u in units for c in u['columns']}, {1, 2, 3, 4})
        from backend.services.student_sources import display_priority
        self.assertLess(display_priority('教务处'), display_priority('教学发展中心'))
        self.assertLess(display_priority('本科生'), display_priority('相关新闻'))

    def test_calendar_list_keeps_title_date_and_column_separate(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<title>博士招生 - 示例大学研究生招生网</title><div class="announcement-list">' + ''.join(
            f'<a class="item" href="/post/{i}"><div class="calendar"><div class="day">06</div>'
            '<div class="month">2026.09</div></div><div class="text-box"><div class="title">'
            f'博士研究生招生简章{i}</div></div></a>' for i in range(3)) + '</div>'
        feeds = publication_lists(html, 'https://example.edu.cn/zkxx/bszs')
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '博士招生')
        self.assertEqual(feeds[0]['samples'][0]['title'], '博士研究生招生简章0')
        self.assertEqual(feeds[0]['samples'][0]['date'], '2026-09-06')

    def test_specific_title_survives_pinned_short_navigation_row(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<h2>招生信息</h2><ul>' + ''.join(
            f'<li><a href="/zs/{i}.html"><div class="time"><p>20</p><span>2026-09</span></div>'
            f'<div class="txt">{title}</div></a></li>' for i, title in enumerate(
                ['本科招生网', '推免生综合考核实施细则', '推免生拟录取名单公示', '夏令营入营名单公示'])) + '</ul>'
        feed = publication_lists(html, 'https://example.edu.cn/zs.html')[0]
        self.assertEqual(feed['title_selector'], '.txt')
        self.assertEqual(feed['samples'][0]['title'], '推免生综合考核实施细则')

    def test_navigation_div_is_not_a_publication_list(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<div class="header"><ul>' + ''.join(
            f'<li><a href="/Web/Content/{i}">示例大学第{i}研究中心</a></li>' for i in range(5)) + '</ul></div>'
        self.assertEqual(publication_lists(html, 'https://example.edu.cn/'), [])

    def test_body_header_theme_does_not_hide_real_publications(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<body class="header-white"><main><h2>通知公告</h2><ul>' + ''.join(
            f'<li><a href="/info/1/{i}.htm">教学发展项目申报通知{i}</a>'
            '<time>2026-09-20</time></li>' for i in range(3)) + '</ul></main></body>'
        self.assertTrue(publication_lists(html, 'https://example.edu.cn/'))

    def test_college_split_calendar_exposes_recommendation_notices(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<title>通知公告-示例大学电气工程学院</title><div class="ny_bt">通知公告</div><ul>' + ''.join(
            f'<li><a href="/admission/{i}.html"><div class="time"><p>20</p><span>2026-09</span></div>'
            f'<div class="txt">2027年招收推免生综合考核实施细则{i}</div></a></li>' for i in range(3)) + '</ul>'
        feeds = publication_lists(html, 'https://example.edu.cn/admission.html')
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '通知公告')
        self.assertEqual(feeds[0]['samples'][0]['date'], '2026-09-20')
        self.assertEqual(feeds[0]['samples'][0]['title'], '2027年招收推免生综合考核实施细则0')

    def test_card_excerpt_does_not_become_notice_title(self):
        from backend.scraper.discovery.publication_lists import publication_lists
        html = '<title>招生信息-示例大学心理学院</title><ul>' + ''.join(
            f'<li><a href="/zsxx/{i}.html"><div class="time"><p>18</p><span>2026-09</span></div>'
            f'<div class="info"><div class="tit">推免生综合考核实施细则{i}</div>'
            '<div class="txt">' + '申请条件与材料要求。' * 100 + '</div></div></a></li>' for i in range(3)) + '</ul>'
        feeds = publication_lists(html, 'https://example.edu.cn/zsxx.html')
        self.assertTrue(feeds)
        self.assertEqual(feeds[0]['samples'][0]['title'], '推免生综合考核实施细则0')


if __name__ == '__main__':
    unittest.main()
