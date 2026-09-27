from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from backend.scraper.date_elements import publication_date_text
from backend.scraper.discovery.publication_lists import publication_lists

URL = 'https://www.cup.edu.cn/news/'


def cards(start=0):
    return '<ul>' + ''.join('<li><a href="sx/opaque-' + str(i) + '.htm"><span class="newsImg"><img src="/x.jpg"></span>'
                            '<span class="newsText"><strong><i class="iconshijian"></i>2025-09-18</strong>'
                            '<h3>研究生教育教学工作会议召开</h3></span></a></li>' for i in range(start, start + 2)) + '</ul>'


def slides(attribute='&lt;strong&gt;12&lt;/strong&gt;&lt;i&gt;2025.09&lt;/i&gt;'):
    return '<div class="flex"><ul class="slides">' + ''.join('<li data-title="' + attribute + '"><a href="sx/slide-' + str(i) + '.htm"><img src="/x.jpg"></a>'
           '<div class="slideTitle"><h3><a href="sx/slide-' + str(i) + '.htm">本科新生第一堂思政课开讲</a></h3></div></li>' for i in range(2)) + '</ul></div>'


class NewsCardDatesTests(unittest.TestCase):
    def test_card_title_and_date_ignore_image_and_decorative_icon(self):
        feeds = publication_lists('<div class="articleList03"><div class="articleTitle01"><span class="title">时讯</span>'
                                  '<span class="more"><a href="sx/index.htm"><i></i></a></span></div>' + cards() + '</div>', URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual((feeds[0]['name'], feeds[0]['column_url']), ('时讯', URL + 'sx/index.htm'))
        self.assertEqual(feeds[0]['dated_item_count'], 2)
        self.assertEqual(feeds[0]['samples'][0]['title'], '研究生教育教学工作会议召开')
        self.assertEqual(feeds[0]['latest_publication'], '2025-09-18')

    def test_carousel_attribute_date_and_shared_top_heading(self):
        html = '<div class="Wrapmode01"><div class="listTitle01"><h2><a href="syxw/index.htm">要闻</a></h2></div>' + slides() + '<div class="articleList02">' + cards() + '</div></div>'
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 2)
        self.assertEqual({f['name'] for f in feeds}, {'要闻'})
        self.assertEqual({f['column_url'] for f in feeds}, {URL + 'syxw/index.htm'})
        self.assertEqual({f['latest_publication'] for f in feeds}, {'2025-09-12', '2025-09-18'})

    def test_title_attribute_is_not_arbitrary_html_or_an_event_date(self):
        for attribute in ['讲座于2025-09-12举行', '&lt;strong onclick=x&gt;12&lt;/strong&gt;&lt;i&gt;2025.09&lt;/i&gt;',
                          '&lt;strong&gt;12&lt;/strong&gt;&lt;i&gt;09&lt;/i&gt;',
                          '&lt;strong&gt;31&lt;/strong&gt;&lt;i&gt;2025.02&lt;/i&gt;']:
            self.assertEqual(publication_lists(slides(attribute), URL), [])

    def test_neighbor_or_ambiguous_titles_cannot_supply_column_ownership(self):
        html = '<div class="articleList03"><div class="articleTitle01"><span class="title">时讯</span><span class="title">学习</span>'
        html += '<span class="more"><a href="sx/index.htm"></a></span></div>' + cards() + '</div>'
        feeds = publication_lists(html, URL)
        self.assertEqual(feeds[0]['name'], '')
        self.assertTrue(feeds[0]['heading_ambiguous'])
        feeds = publication_lists(html.replace('<span class="title">学习</span>', ''), 'https://other.edu.cn/news/')
        self.assertFalse(any(f.get('heading_method') == 'official_cup_news_module' for f in feeds))

    def test_datetime_and_split_visible_dates_keep_spaces(self):
        node = BeautifulSoup('<time datetime="2025-09-12">昨天</time><span><b>12</b><i>2025.09</i></span>', 'lxml')
        self.assertEqual(publication_date_text(node.time), '2025-09-12')
        self.assertEqual(publication_date_text(node.span), '2025-09-12')


if __name__ == '__main__':
    unittest.main()
