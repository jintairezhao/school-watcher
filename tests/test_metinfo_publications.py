"""Alternating row decorations must not hide an official MetInfo news list."""
import unittest

from backend.scraper.discovery.publication_lists import publication_lists


URL = 'http://www.dgieie.cn/d_xwzx/d_xwzx_248_1.html'


def listing(heading='通知公告', crumb='通知公告'):
    rows = ''.join(
        f'<li class="news_center_li news_center_li{i % 2 + 1}">'
        f'<a href="d_xwzx{i}.html">关于第{i}期项目申报工作的通知</a>'
        f'<span>2025-04-{i:02d}</span></li>' for i in range(1, 5))
    return (f'<section id="news_right"><h2>{heading}<span>'
            '<a href="/">首页</a>&gt;<a href="/d_xwzx/">新闻资讯</a>&gt;'
            f'<a href="d_xwzx_248_1.html">{crumb}</a></span></h2>'
            f'<div class="news_center"><ul>{rows}</ul></div></section>')


class MetInfoPublicationsTests(unittest.TestCase):
    def test_alternating_rows_keep_all_dates_and_current_column(self):
        feeds = publication_lists(listing(), URL)
        self.assertEqual(len(feeds), 1)
        feed = feeds[0]
        self.assertEqual(feed['name'], '通知公告')
        self.assertEqual(feed['column_url'], URL)
        self.assertEqual(feed['item_count'], 4)
        self.assertTrue(feed['publication_dates_complete'])
        self.assertEqual(feed['latest_publication'], '2025-04-04')
        self.assertEqual(feed['samples'][0]['url'], 'http://www.dgieie.cn/d_xwzx/d_xwzx1.html')

    def test_conflicting_breadcrumb_cannot_supply_a_false_category(self):
        feeds = publication_lists(listing(crumb='学院新闻'), URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '')
        self.assertTrue(feeds[0]['heading_ambiguous'])

    def test_navigation_does_not_become_a_publication_list(self):
        html = listing().replace('<section', '<nav').replace('</section>', '</nav>')
        self.assertEqual(publication_lists(html, URL), [])


if __name__ == '__main__':
    unittest.main()
