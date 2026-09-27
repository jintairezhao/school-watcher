import json
import unittest
from types import SimpleNamespace

from backend.scraper.detectors.title_quality import is_junk_title
from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_catalog import publication_candidates

URL = 'https://www.example.edu.cn/'


class ShortArticleTests(unittest.TestCase):
    def test_explicit_article_address_can_support_a_short_title(self):
        for title in ('会议扫码签到说明', '工作简报', '用印流程'):
            self.assertTrue(is_junk_title(title))
            self.assertFalse(is_junk_title(title, article_url=URL + 'content.jsp?wbnewsid=3761'))
            self.assertTrue(is_junk_title(title, article_url=URL + 'services/'))
        for junk in ('2026-09-21', '通知公告', '查看更多', '首页'):
            self.assertTrue(is_junk_title(junk, article_url=URL + 'info/1/10001.htm'))

    def test_short_articles_survive_without_turning_navigation_into_news(self):
        html = '<section><h2>办事指南</h2><ul>' + ''.join(
            f'<li><a href="/info/1/{10000+i}.htm">{title}</a><span>2026-09-16</span></li>'
            for i, title in enumerate(('会议扫码签到说明', '工作简报', '用印流程'))) + '</ul></section>'
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['item_count'], 3)
        self.assertEqual(publication_lists(html.replace('/info/1/', '/services/').replace('.htm', '/'), URL), [])
        roster = '<div class="main"><h2>院系介绍</h2><ul>' + ''.join(
            f'<li><a href="/info/1/{10000+i}.htm">{name}</a></li>'
            for i, name in enumerate(('物理学院', '数学学院', '艺术学院'))) + '</ul></div>'
        self.assertEqual(publication_lists(roster, URL), [])

    def test_incomplete_dates_do_not_support_long_inactivity(self):
        feeds = [dict(name=name, latest_publication=date, publication_dates_complete=complete)
                 for name, date, complete in [('缺少日期', '2020-01-01', False),
                                               ('旧缓存', '2020-01-01', None),
                                               ('明确旧列表', '2020-01-01', True),
                                               ('已见近期文章', '2026-09-20', False)]]
        report = {'site': {'root_url': URL}, 'pages': [{'feed_json': json.dumps({'lists': feeds}),
                  'state': 'fetched', 'path_json': '[]', 'label': '学院网站', 'url': URL,
                  'final_url': URL, 'health': 'recent_publication'}]}
        self.assertEqual({c['name']: c['source_health'] for c in publication_candidates(report)},
                         {'缺少日期': 'date_unknown', '旧缓存': 'date_unknown',
                          '明确旧列表': 'stale', '已见近期文章': 'recent_publication'})

    def test_detection_and_selector_health_agree_on_short_articles(self):
        from backend.scraper.detectors.list_detector import detect_notice_list
        from backend.scraper.selector_monitor import _quick_stats
        html = '<section><ul class="guides">' + ''.join(
            f'<li><a href="/info/1/{10000+i}.htm">{title}</a><span>2026-09-16</span></li>'
            for i, title in enumerate(('会议扫码签到说明', '工作简报', '用印流程'))) + '</ul></section>'
        profile = dict(list_selector='ul.guides > li', title_selector='a', link_selector='a', date_selector='span')
        detected = detect_notice_list(html, URL, existing_profiles=[profile])
        self.assertIsNotNone(detected)
        self.assertEqual(set(detected['sample_titles']), {'会议扫码签到说明', '工作简报', '用印流程'})
        self.assertEqual(_quick_stats(html, SimpleNamespace(list_url=URL, **profile)), {'matched': 3, 'junk': 0})


if __name__ == '__main__':
    unittest.main()
