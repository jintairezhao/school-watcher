import json
from pathlib import Path
import sys
import unittest

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.structure import publication_evidence
from backend.services.source_catalog import publication_candidates

URL = 'https://www.example.edu.cn/'


def section(name, start, count=4, self_anchors=False):
    items = []
    for i in range(count):
        anchor = '<a href="/info/1001/' + str(start + i) + '.htm">' + name + '关于开展新学期工作的通知' + str(i) + '</a>'
        items.append(anchor if self_anchors else '<li>' + anchor + '<span>2026-09-18</span></li>')
    return '<section><h2>' + name + '</h2><ul class="news-list">' + ''.join(items) + '</ul></section>'


class PublicationListTests(unittest.TestCase):
    def test_table_rows_ignore_empty_decoration_links_before_actual_titles(self):
        html = '<html><head><title>教务通知</title></head><body><div id="notices"><table class="main">' + ''.join(
            f'<tr><td><img src="bullet.gif"></td><td class="main"><a href="/2026/0901/a{i}/page.htm"></a>'
            f'<a href="/2026/0901/a{i}/page.htm" title="关于学生选课安排的通知{i}"><font>关于学生选课安排的通知{i}</font></a></td>'
            '<td class="main"><div align="right">2026-09-01</div></td></tr>' for i in range(4)) + '</table></div></body></html>'
        feeds = publication_lists(html, URL + 'teaching/list.htm')
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '教务通知')
        self.assertEqual(feeds[0]['item_count'], 4)
        self.assertTrue(all(s['date'] == '2026-09-01' for s in feeds[0]['samples']))
        # The published selector must work in the collector too, not just in
        # the detector's temporary sample records.
        soup = BeautifulSoup(html, 'lxml')
        for item in soup.select(feeds[0]['list_selector']):
            self.assertTrue(item.select_one(feeds[0]['title_selector']).get('title'))

    def test_current_page_breadcrumb_names_only_its_adjacent_article_list(self):
        u = URL + 'notices/'
        def listing(crumb):
            return ('<div class="c-main__right"><div class="middleArticle__position">'
                    '<div class="middleArticle__position--label"><a href="/">首页</a><a href="' + crumb +
                    '">通知公告</a></div></div><div class="middleArticle__art"><ul class="middleArticle__articleList">' + ''.join(
                    '<li class="middleArticle--articleList"><a class="middleArticle__articleList--article" href="/info/1/' + str(i) +
                    '.htm">新学期奖学金评审通知' + str(i) + '</a><span class="middleArticle__articleList--date">2026-09-19</span></li>'
                    for i in range(3)) + '</ul></div></div>')
        feed = publication_lists(listing(u), u)[0]
        self.assertEqual((feed['name'], feed['column_url']), ('通知公告', u))
        self.assertEqual(feed['heading_method'], 'current_page_breadcrumb')
        for other in (URL + 'other/', '#', 'javascript:void(0)'):
            self.assertEqual(publication_lists(listing(other), u)[0]['name'], '')
        html = listing(u) + section('校友活动', 20000)
        self.assertEqual({f['name'] for f in publication_lists(html, u)}, {'通知公告', '校友活动'})

    def test_nested_zcms_modules_keep_titles_more_links_and_split_calendar_dates(self):
        from backend.scraper.change_detector import parse_date
        from backend.scraper.discovery.structure import extract_structure
        prefix = 'c-shiyouxueyuan-new-module__'
        def heading(name, path):
            return ('<div class="' + prefix + 'head-box"><div><div class="' + prefix +
                    'head-title-words">' + name + '</div></div><div class="' + prefix +
                    'head-more"><a href="/' + path + '/">more</a></div></div>')
        news = '<div class="' + prefix + 'half-width">' + heading('学院新闻', 'news') + '<div><ul>' + ''.join(
            '<li class="' + prefix + 'half-li"><a href="/info/1/' + str(i) + '.htm"><span class="' + prefix +
            'detail-content-words">学院科学研究取得新进展' + str(i) + '</span><span class="' + prefix +
            'detail-content-date">2026-08-29</span></a></li>' for i in range(3)) + '</ul></div></div>'
        notices = '<div class="' + prefix + 'half-width">' + heading('通知公告', 'notices') + ''.join(
            '<div class="' + prefix + 'detail"><ul class="' + prefix + 'notice-ul"><li><div class="' + prefix +
            'half-li-box-left"><div>19</div><div>2026-09</div></div><div class="' + prefix +
            'half-li-box-right-title"><a href="/info/2/' + str(i) + '.htm">学院奖学金评选结果公示' + str(i) +
            '</a></div></li></ul></div>' for i in range(3)) + '</div>'
        html = '<p>header.template.html 第48行发生错误: 未指定栏目或指定的栏目不存在</p>' + news + notices
        feeds = publication_lists(html, URL)
        self.assertEqual({f['name']: (f['column_url'], f['latest_publication']) for f in feeds},
                         {'学院新闻': (URL + 'news/', '2026-08-29'), '通知公告': (URL + 'notices/', '2026-09-19')})
        self.assertTrue(all(f['dated_item_count'] == 3 for f in feeds))
        self.assertTrue(all('2026-' not in s['title'] for f in feeds for s in f['samples']))
        soup = BeautifulSoup(html, 'lxml')
        # Subscription extraction strips text without separators; it must agree with discovery.
        for feed in feeds:
            item = soup.select(feed['list_selector'])[0]
            self.assertEqual(parse_date(item.select_one(feed['date_selector']).get_text(strip=True)).date().isoformat(),
                             feed['latest_publication'])
        links = extract_structure(html, URL, URL)['links']
        self.assertEqual({(l['label'], l['url']) for l in links if l.get('raw_label') == 'more'},
                         {('学院新闻', URL + 'news/'), ('通知公告', URL + 'notices/')})

    def test_zcms_module_with_missing_or_multiple_headings_cannot_borrow_a_neighbor_title(self):
        for title in ('', '<div class="c-shiyouxueyuan-new-module__head-title-words">学院新闻</div>'
                      '<div class="c-shiyouxueyuan-new-module__head-title-words">通知公告</div>'):
            html = '<h2>其他学院新闻</h2><div class="c-shiyouxueyuan-new-module__half-width">' + title + section('通知公告', 10000).replace('<h2>通知公告</h2>', '') + '</div>'
            feed = publication_lists(html, URL)[0]
            self.assertEqual(feed['name'], '')
            self.assertTrue(feed['heading_ambiguous'])

    def test_explicit_tabs_keep_their_own_titles_and_urls_in_reversed_dom_order(self):
        tabs = '<div class="title"><button role="tab" aria-controls="events">学术活动</button><button role="tab" aria-controls="notices">通知公告</button></div>'
        html = tabs + '<div>' + ''.join(section(label, start).replace('<section>', '<section id="' + name + '">').replace(
            '<h2>' + label + '</h2>', '<a href="/' + name + '/">更多</a>')
            for name, label, start in [('notices', '通知公告', 10000), ('events', '学术活动', 20000)]) + '</div>'
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 2)
        self.assertEqual({f['name']: f['column_url'] for f in feeds}, {'通知公告': URL + 'notices/', '学术活动': URL + 'events/'})
        self.assertTrue(all(f['heading_method'] == 'explicit_tab_control' for f in feeds))
        self.assertTrue(all(f['item_count'] == 4 for f in feeds))

    def test_conflicting_tab_controls_and_unbound_multiple_headings_are_unresolved(self):
        html = '<div class="title"><a href="/notices/">通知公告</a><a href="/events/">学术活动</a></div>' + section('通知公告', 10000).replace('<h2>通知公告</h2>', '')
        feeds = publication_lists('<main>' + html + '</main>', URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '')
        self.assertTrue(feeds[0]['heading_ambiguous'])
        html = '<button role="tab" aria-controls="panel">通知公告</button><button role="tab" aria-controls="panel">学术活动</button>' + section('通知公告', 10000).replace('<section>', '<section id="panel">')
        feed = publication_lists(html, URL)[0]
        self.assertEqual(feed['name'], '')
        self.assertTrue(feed['heading_ambiguous'])

    def test_gzhu_literal_switch_maps_panels_despite_repeated_link_ids(self):
        html = "<script>function selectSwtich(parentNodeID,selfObj,showContentPrefix,showContentIndex,showClassName){document.getElementById(showContentPrefix + showContentIndex).style.display='block';}</script>"
        for prefix in ('bbb', 'ccc'):
            html += '<div class="title"><ul>' + ''.join(
                '<li onmouseover="selectSwtich(\'\',this,\'' + prefix + '\',' + str(i) + ',\'current\')"><a id="duplicate' + str(i) + '" href="/' + path + '/">' + name + '</a></li>'
                for i, (name, path) in enumerate([('教育教学', 'teach'), ('科学研究', 'research')])) + '</ul></div><div>'
            for i, name in enumerate(('教育教学', '科学研究')):
                html += section(name, 10000 + i * 100).replace('<section>', '<section id="' + prefix + str(i) + '">').replace('<h2>' + name + '</h2>', '<a href="/more' + str(i) + '/">更多</a>')
            html += '</div>'
        feeds = publication_lists(html, 'https://www.gzhu.edu.cn/')
        self.assertEqual({f['name']: f['column_url'] for f in feeds}, {'教育教学': 'https://www.gzhu.edu.cn/teach/', '科学研究': 'https://www.gzhu.edu.cn/research/'})
        self.assertEqual(len(feeds), 2)
        self.assertTrue(all(f.get('alternative_selectors') for f in feeds))
        from backend.scraper.discovery.structure import extract_structure
        structure = extract_structure(html, 'https://www.gzhu.edu.cn/', 'https://www.gzhu.edu.cn/')
        more_links = [link for link in structure['links'] if link.get('raw_label') == '更多']
        self.assertEqual({(link['label'], link['url']) for link in more_links},
                         {('教育教学', 'https://www.gzhu.edu.cn/more0/'), ('科学研究', 'https://www.gzhu.edu.cn/more1/')})

    def test_undated_cms_and_wechat_articles_remain_in_publication_lists(self):
        for pattern in ('/info.jsp?wbnewsid={}', '/news-show-{}.html', '/2026/0901/c1a{}/page.htm', 'https://mp.weixin.qq.com/s/article{}'):
            html = '<section><h2>科学研究</h2><ul>' + ''.join(
                '<li><a href="' + pattern.format(i) + '">学院团队研究成果取得进展' + str(i) + '</a></li>' for i in range(3)) + '</ul></section>'
            with self.subTest(pattern=pattern):
                feeds = publication_lists(html, URL)
                self.assertEqual(len(feeds), 1)
                self.assertEqual(feeds[0]['item_count'], 3)
                self.assertIsNone(feeds[0]['latest_publication'])

    def test_dates_quoted_in_article_summaries_are_not_publication_times(self):
        html = '<section><h2>学校新闻</h2><ul>' + ''.join(
            '<li><a href="/info/1/' + str(i) + '.htm"><h3>学校研究团队取得新成果' + str(i) +
            '</h3><p>项目根据2020年3月13日发布的数据开展研究。</p></a></li>' for i in range(3)) + '</ul></section>'
        feed = publication_lists(html, URL)[0]
        self.assertEqual(feed['item_count'], 3)
        self.assertEqual(feed['dated_item_count'], 0)
        self.assertFalse(feed['publication_dates_complete'])
        self.assertIsNone(feed['latest_publication'])

    def test_same_css_on_one_page_retains_distinct_named_scopes(self):
        html = section('通知公告', 10000) + section('学术活动', 20000)
        feeds = publication_lists(html, URL)
        self.assertEqual({f['name'] for f in feeds}, {'通知公告', '学术活动'})
        self.assertEqual(len(feeds), 2)
        soup = BeautifulSoup(html, 'lxml')
        for feed in feeds:
            rows = soup.select(feed['list_selector'])
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(feed['name'] in row.get_text() for row in rows))
            self.assertFalse(feed['verified'])

    def test_responsive_copies_keep_one_channel_and_original_selectors(self):
        html = section('通知公告', 10000) * 2
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        self.assertGreater(len(feeds[0]['alternative_selectors']), 0)

    def test_two_undated_article_links_are_not_discarded(self):
        feeds = publication_lists(section('通知公告', 10000, count=2, self_anchors=True), URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['item_count'], 2)
        self.assertIsNone(feeds[0]['latest_publication'])

    def test_navigation_lists_do_not_become_publication_sources(self):
        html = '<section><h2>院系设置</h2><ul class="news-list">' + ''.join(
            '<li><a href="/college/' + str(i) + '/">第' + str(i) + '信息与科学工程学院</a></li>' for i in range(8)) + '</ul></section>'
        self.assertEqual(publication_lists(html, URL), [])

    def test_candidates_keep_official_column_names_without_inventing_unit_groups(self):
        evidence = publication_evidence(section('通知公告', 10000) + section('学术活动', 20000), URL)
        report = {'site': {'root_url': URL}, 'pages': [{'feed_json': json.dumps(evidence), 'state': 'fetched', 'path_json': '["院系设置"]',
                             'kind': 'unit', 'label': '工程学院', 'url': URL, 'final_url': URL, 'health': 'date_unknown'}]}
        sources = publication_candidates(report)
        self.assertEqual({s['name'] for s in sources}, {'通知公告', '学术活动'})
        self.assertEqual({s['group_name'] for s in sources}, {''})
        self.assertTrue(all(s['discovery_path'] == ['院系设置'] for s in sources))
        self.assertEqual(len({s['list_selector'] for s in sources}), 2)

    def test_official_full_column_link_and_heading_evidence_are_retained(self):
        html = section('通知公告', 10000).replace('<h2>通知公告</h2>', '<h2><a href="/notices/">通知公告</a><a href="/notices/">更多</a></h2>')
        feed = publication_lists(html, URL)[0]
        self.assertEqual(feed['name'], '通知公告')
        self.assertEqual(feed['column_url'], URL + 'notices/')
        self.assertTrue(feed['column_link_locator'])

    def test_news_cards_with_image_link_before_title_preserve_articles(self):
        html = '<section><h2>校园新闻</h2><div>' + ''.join(
            '<article><a href="/info/1/' + str(i) + '.htm"><img src="/image.png"></a>'
            '<div class="name"><a href="/info/1/' + str(i) + '.htm">学校召开新学期教学工作会议' + str(i) + '</a></div></article>'
            for i in range(10000, 10003)) + '</div></section>'
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['item_count'], 3)

    def test_more_link_outside_heading_stays_in_its_own_widget(self):
        html = (section('通知公告', 10000).replace('</h2>', '</h2><a href="/notices/">更多</a>') +
                section('学术活动', 20000).replace('</h2>', '</h2><a href="/events/">更多</a>'))
        feeds = publication_lists(html, URL)
        self.assertEqual({f['name']: f['column_url'] for f in feeds},
                         {'通知公告': URL + 'notices/', '学术活动': URL + 'events/'})


if __name__ == '__main__':
    unittest.main()
