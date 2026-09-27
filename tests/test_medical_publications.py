"""Official module labels, date semantics and single video publications."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.publication_lists import publication_lists

URL = 'http://www.cmm.zju.edu.cn/'


def module(classes, name, address, widget, rows):
    return '<div class="' + classes + '"><div class="title-block"><div class="rt-text"><h3>' + name + '</h3></div></div><div id="' + widget + '">' + rows + '</div><a href="' + address + '">查看更多</a></div>'


class MedicalPublicationTests(unittest.TestCase):
    def test_news_uses_module_heading_and_row_date_not_summary_date(self):
        rows = ''.join('<li class="items"><div class="texts"><div class="news-time">2025.09.20</div><h3><a href="/2025/0910/c38670a' + str(i) + '/page.htm">医学教育研究成果发布' + str(i) + '</a></h3><p>会议于2025年09月10日举办</p></div></li>' for i in (100, 101))
        html = module('first-news-box', '学院新闻', '/38670/list.htm', 'wp_news_w4', '<ul>' + rows + '</ul>')
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '学院新闻')
        self.assertEqual(feeds[0]['column_url'], URL + '38670/list.htm')
        self.assertEqual(feeds[0]['dated_item_count'], 2)
        self.assertEqual({s['date'] for s in feeds[0]['samples']}, {'2025-09-20'})

    def test_event_time_and_incomplete_month_day_remain_without_publication_date(self):
        lectures = ''.join('<li class="items"><div class="rt"><h3><a href="/2025/0901/c38677a' + str(i) + '/page.htm">医学学者论坛系列讲座' + str(i) + '</a></h3><div class="infos"><span>时间：2025-09-24 15:00:00</span></div></div></li>' for i in (110, 111))
        notices = ''.join('<li class="items"><div class="texts"><h3><a href="/2025/0901/c38678a' + str(i) + '/page.htm">医学教育教学工作通知' + str(i) + '</a></h3></div><div class="time"><span>18</span><span>09</span></div></li>' for i in (120, 121))
        html = '<div class="second-notice-box">' + module('rt-block', '学术讲座', '/38677/list.htm', 'wp_news_w6', '<ul>' + lectures + '</ul>')
        html += module('ft-block', '通知公告', '/38678/list.htm', 'wp_news_w5', '<ul>' + notices + '</ul>') + '</div>'
        feeds = publication_lists(html, URL)
        self.assertEqual({f['name'] for f in feeds}, {'学术讲座', '通知公告'})
        self.assertTrue(all(f['dated_item_count'] == 0 and f['latest_publication'] is None for f in feeds))

    def test_single_video_with_article_link_is_retained_without_invented_date(self):
        html = module('ft-audio-visual', '视听浙医', '/stzy/list.htm', 'wp_news_w11',
                      '<video src="/movie.mp4"></video><h3><a href="/2025/0625/c55113a3064477/page.htm">《为医的答案》</a></h3>')
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '视听浙医')
        self.assertEqual(feeds[0]['samples'][0]['title'], '《为医的答案》')
        self.assertIsNone(feeds[0]['samples'][0]['date'])
        self.assertFalse(publication_lists(html.replace('<video src="/movie.mp4"></video>', ''), URL))

    def test_research_external_articles_keep_links_and_title_not_image(self):
        rows = ''.join('<li class="items"><div class="picimg"><a href="https://mp.weixin.qq.com/s/paper' + str(i) + '"><img src="/image.jpg"></a></div><div class="texts"><a href="https://mp.weixin.qq.com/s/paper' + str(i) + '">医学科研最新研究进展' + str(i) + '</a></div></li>' for i in (1, 2))
        feeds = publication_lists(module('add-research-box', '科研进展', '/kycx/list.htm', 'wp_news_w7', '<ul>' + rows + '</ul>'), URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual(feeds[0]['name'], '科研进展')
        self.assertEqual(feeds[0]['article_urls'], ['https://mp.weixin.qq.com/s/paper1', 'https://mp.weixin.qq.com/s/paper2'])

    def test_unlinked_media_titles_do_not_become_homepage_articles(self):
        rows = '<ul><li><h2><a href="">健康报医学报道文章标题</a></h2><span class="time">2025.08.08</span></li><li><h2><a href="">人民日报医学报道文章标题</a></h2><span class="time">2025.03.11</span></li></ul>'
        feeds = publication_lists(module('ft-media-look', '媒体看浙医', '/mtkzy/list.htm', 'wp_news_w12', rows), URL)
        self.assertFalse(feeds)

    def test_module_cannot_borrow_neighboring_heading_or_apply_to_other_site(self):
        rows = '<ul><li class="items"><div class="texts"><a href="/2025/0901/c1a1/page.htm">医学科研最新研究进展一</a></div></li><li class="items"><div class="texts"><a href="/2025/0901/c1a2/page.htm">医学科研最新研究进展二</a></div></li></ul>'
        html = module('add-research-box', '科研进展', '/kycx/list.htm', 'wp_news_w7', rows)
        ambiguous = html.replace('</h3></div>', '</h3><h3>其他栏目</h3></div>')
        for source, url in [(ambiguous, URL), (html, 'https://other.edu.cn/')]:
            self.assertFalse(any(f['method'] == 'official_medical_module' for f in publication_lists(source, url)))


if __name__ == '__main__':
    unittest.main()
