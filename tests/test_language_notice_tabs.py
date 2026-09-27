"""Literal official tab mappings must not mix student notice categories."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.structure import extract_structure, publication_evidence, add_publication_structure
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_inventory import Inventory
from backend.services.source_relationships import SourceRelationships

URL = 'http://sfl.swjtu.edu.cn/'
HTML = (Path(__file__).parent / 'fixtures/swjtu_language_notices.html').read_text(encoding='utf-8')
NAMES = ['通知公告', '党政办公', '本科生教育', '研究生教育', '学生工作', '国际交流', '学术研究']
TARGETS = ['tzgg.htm', 'tzgg/dzbg.htm', 'tzgg/bksjy.htm', 'tzgg/yjsjy.htm', 'tzgg/xsgz.htm', 'tzgg/gjjl.htm', 'tzgg/xsyj.htm']


class LanguageNoticeTabTests(unittest.TestCase):
    def test_seven_categories_keep_their_literal_urls_dates_and_script_evidence(self):
        feeds = publication_evidence(HTML, URL)['lists']
        self.assertEqual([f['name'] for f in feeds], NAMES)
        self.assertEqual([f['column_url'] for f in feeds], [URL + t for t in TARGETS])
        self.assertTrue(all(f['item_count'] == f['dated_item_count'] == 6 for f in feeds))
        self.assertEqual(feeds[2]['samples'][0]['date'], '2026-09-08')
        self.assertTrue(all(f['column_script_locator'] and f['column_link_method'] == 'official_tab_handler' for f in feeds))
        self.assertEqual([f['column_tab_index'] for f in feeds], list(range(7)))

    def test_news_and_notices_with_identical_category_names_have_separate_parents(self):
        news = HTML.replace('title-list2', 'title-list').replace('product-wrap2', 'product-wrap').replace(
            'noticemore1', 'newsmore').replace('tzgg', 'xyxw').replace('通知公告</li>', '学院新闻</li>')
        html = news + HTML
        parsed = extract_structure(html, URL, 'https://www.swjtu.edu.cn/', 'unit', '外国语学院')
        add_publication_structure(parsed, publication_evidence(html, URL))
        groups = {n['key']: n['name'] for n in parsed['nodes'] if n['relation'] == 'publication_group'}
        self.assertEqual(set(groups.values()), {'学院新闻', '通知公告'})
        undergraduate = [n for n in parsed['nodes'] if n['relation'] == 'publication_column' and n['name'] == '本科生教育']
        self.assertEqual({(groups[n['parent']], n['url']) for n in undergraduate},
                         {('学院新闻', URL + 'xyxw/bksjy.htm'), ('通知公告', URL + 'tzgg/bksjy.htm')})

    def test_changed_index_expression_does_not_guess_the_old_order(self):
        feeds = publication_evidence(HTML.replace('.eq(liindex)', '.eq(liindex+1)'), URL)['lists']
        self.assertTrue(feeds)
        self.assertTrue(all(not f['name'] and not f['column_url'] for f in feeds))

    def test_duplicate_handler_or_commented_handler_is_not_unique_evidence(self):
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(HTML, 'lxml')
        handler = str(soup.find('script'))
        for html in [HTML + handler, HTML.replace(handler, '<!--' + handler + '-->')]:
            with self.subTest(html=html[-30:]):
                feeds = publication_evidence(html, URL)['lists']
                self.assertTrue(all(not f['name'] and not f['column_url'] for f in feeds))

    def test_missing_control_or_changed_branch_requires_review(self):
        for html in [HTML.replace('<li>本科生教育</li>', ''), HTML.replace('tzgg/bksjy.htm', 'different.htm')]:
            feeds = publication_evidence(html, URL)['lists']
            self.assertTrue(all(not f['name'] and not f['column_url'] for f in feeds))

    def test_college_source_paths_retain_notice_parent_and_script_locator(self):
        with tempfile.TemporaryDirectory() as directory:
            inv = Inventory(Path(directory) / 'sources.db')
            key = inv.ensure_site('西南交通大学', 'https://www.swjtu.edu.cn/')
            def read(url, label, kind, html):
                inv.enqueue(key, url, label, kind, 1, [], 'school_domain')
                report = inv.report(key)
                page = next(p for p in report['pages'] if p['url'] == url)
                inspect_page(inv, report['site'], page, fetcher=lambda _: {'html': html, 'status': 200, 'url': url})
            read('https://www.swjtu.edu.cn/units/', '院系设置', 'directory', '<a href="' + URL + '">外国语学院</a>')
            read(URL, '外国语学院', 'unit', '<title>外国语学院</title>' + HTML)
            paths = SourceRelationships(inv.report(key), inv.structure(key)).paths_for(URL + 'tzgg/bksjy.htm')
            self.assertEqual(len(paths), 1)
            self.assertEqual(paths[0]['unit_name'], '外国语学院')
            self.assertEqual([n['name'] for n in paths[0]['entry_nodes']], ['通知公告'])
            self.assertTrue(any('script:' in loc for r in paths[0]['references'] for loc in r['locators']))
            from backend.services.source_catalog import publication_candidates
            candidates = publication_candidates(inv.report(key), inv.structure(key), focus='student')
            undergraduate = next(c for c in candidates if c['column_url'] == URL + 'tzgg/bksjy.htm')
            self.assertEqual(undergraduate['column_group_name'], '通知公告')
            self.assertEqual([n['name'] for n in undergraduate['source_structure'][0]['entry_nodes']], ['通知公告'])

    def test_listing_requires_agreeing_heading_and_current_breadcrumb(self):
        listing_url = URL + 'tzgg/bksjy.htm'
        html = ('<div class="xxright ntext"><div class="righttop"><span>本科生教育</span>'
                '<span class="ben"><a href="../index.htm">首页</a><a href="../tzgg.htm">通知公告</a>'
                '<a href="bksjy.htm">本科生教育</a></span></div><div class="xnewlist"><ul class="rightnew">' +
                ''.join('<li><a href="../info/1270/' + str(i) + '.htm">本科课程安排通知' + str(i) +
                        '</a><span>2026-09-08</span></li>' for i in range(10000,10003)) + '</ul></div></div>')
        feed = publication_evidence(html, listing_url)['lists'][0]
        self.assertEqual((feed['name'], feed['column_url'], feed['column_group_name']),
                         ('本科生教育', listing_url, '通知公告'))
        for broken in [html.replace('href="bksjy.htm"', 'href="yjsjy.htm"'),
                       html.replace('<span>本科生教育</span>', '<span>研究生教育</span>')]:
            self.assertTrue(all(not f['name'] and not f['column_url'] for f in publication_evidence(broken, listing_url)['lists']))


if __name__ == '__main__':
    unittest.main()
