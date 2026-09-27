"""Medical listing pages: current title, split dates, recruitment and media."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.discovery.structure import extract_structure
from backend.scraper.article_urls import ARTICLE
from backend.services.source_inventory import resolve_page_link

BASE = 'http://www.cmm.zju.edu.cn/'


def listing(title, rows, ul='news-ul'):
    return '<title>' + title + '</title><section class="banner-box"><h1>综合服务</h1></section><section class="news-content-box"><div id="wp_news_w222"><ul class="' + ul + '">' + rows + '</ul></div></section>'


class MedicalColumnPageTests(unittest.TestCase):
    def test_current_title_and_split_year_month_day_not_parent_banner(self):
        rows = ''.join('<li><div class="fttime"><span class="day">18</span><span class="mouth">2025.09</span></div><div class="rttext"><h3><a href="/2025/0901/c1a' + str(i) + '/page.htm">医学教学工作安排通知</a></h3><p>活动2025年09月01日举办</p></div></li>' for i in (1, 2))
        for path in ('38678/list.htm', '38678/list2.htm'):
            feeds = publication_lists(listing('通知公告', rows), BASE + path)
            self.assertEqual(len(feeds), 1)
            self.assertEqual(feeds[0]['name'], '通知公告')
            self.assertEqual(feeds[0]['column_url'], BASE + path)
            self.assertEqual(feeds[0]['column_link_locator'], 'document')
            self.assertEqual({s['date'] for s in feeds[0]['samples']}, {'2025-09-18'})

    def test_lecture_schedule_stays_separate_from_publication_date(self):
        rows = ''.join('<li><div class="top-block"><div class="rttext"><a href="/2025/0901/c38677a' + str(i) + '/page.htm">医学系列学术论坛报告会</a></div></div><div class="bottom-block"><div class="item"><span>日期：2025-09-24 15:00:00</span></div></div></li>' for i in (1, 2))
        feeds = publication_lists(listing('学术讲座', rows, 'academic-ul'), BASE + '38677/list.htm')
        self.assertEqual(feeds[0]['dated_item_count'], 0)
        self.assertIsNone(feeds[0]['latest_publication'])

    def test_video_uses_titled_anchor_after_empty_duplicate(self):
        row = '<li><div class="video"><video src="/movie.mp4"></video></div><div class="rttext"><h3><a href="/2025/0625/c55113a1/page.htm"></a><a href="/2025/0625/c55113a1/page.htm" title="《为医的答案》">《为医的答案》</a></h3></div></li>'
        feeds = publication_lists(listing('视听浙医', row), BASE + 'stzy/list.htm')
        self.assertEqual(feeds[0]['item_count'], 1)
        self.assertEqual(feeds[0]['samples'][0]['title'], '《为医的答案》')
        self.assertIsNone(feeds[0]['samples'][0]['date'])

    def test_recruitment_groups_keep_all_categories_and_detail_with_short_title(self):
        groups = ''
        for name, path in [('教师', 'js_84610'), ('行政', 'xz_84611'), ('博后', 'bh_84612'), ('其它', 'qt_84613')]:
            groups += '<ul class="wp_subcolumn_list"><li class="wp_sublist"><h3 class="sublist_title"><a childcolumnid="1" href="/' + path + '/list.htm">' + name + '</a></h3><div class="top"><a href="{栏目URL}">更多</a></div><ul class="bottom"><li><span class="jt">图标</span><span class="title"><a href="https://ehr.zju.edu.cn/vuejs/recruitment/position-detail.htm?id=N123">劳务派遣人员招聘</a></span><span class="time">2024-09-29</span></li></ul></li></ul>'
        html = '<title>人才招聘</title><section class="news-content-box">' + groups + '</section>'
        feeds = publication_lists(html, BASE + 'rczp/list.htm')
        self.assertEqual({f['name'] for f in feeds}, {'教师', '行政', '博后', '其它'})
        self.assertTrue(all(f['item_count'] == 1 and f['dated_item_count'] == 1 for f in feeds))
        self.assertEqual(resolve_page_link(BASE, '{栏目URL}'), '')
        parsed = extract_structure(html, BASE + 'rczp/list.htm', 'https://www.zju.edu.cn/', 'channel', '人才招聘')
        self.assertFalse(any('{栏目URL}' in link['url'] for link in parsed['links']))
        self.assertFalse(any(n['name'] == '劳务派遣人员招聘' for n in parsed['nodes']))
        template_html = html.replace('href="{栏目URL}">更多', 'href="{栏目URL}">教师')
        template_structure = extract_structure(template_html, BASE + 'rczp/list.htm', 'https://www.zju.edu.cn/', 'channel', '人才招聘')
        self.assertEqual(template_structure['nodes'], parsed['nodes'])
        changed = html.replace('劳务派遣人员招聘', '派遣至医学研究院')
        parsed = extract_structure(changed, BASE + 'rczp/list.htm', 'https://www.zju.edu.cn/', 'channel', '人才招聘')
        self.assertFalse(any(n['name'] == '派遣至医学研究院' for n in parsed['nodes']))
        self.assertFalse(ARTICLE.search('https://ehr.zju.edu.cn/vuejs/recruitment/position-detail.htm'))
        self.assertFalse(ARTICLE.search('https://ehr.zju.edu.cn/vuejs/recruitment/position-detail.htm?id='))

    def test_dated_external_media_detail_is_valid_without_known_cms_url(self):
        row = '<li><div class="fttime"><span class="day">12</span><span class="mouth">2025.05</span></div><div class="rttext"><h3><a href="https://www.peopleapp.com/column/30049039124-500006253224">长三角医学教育联盟教育创新论坛在杭州召开</a></h3></div></li>'
        feeds = publication_lists(listing('媒体看浙医', row), BASE + 'mtkzy/list.htm')
        self.assertEqual(feeds[0]['name'], '媒体看浙医')
        self.assertEqual(feeds[0]['samples'][0]['date'], '2025-05-12')
        self.assertTrue(feeds[0]['samples'][0]['url'].startswith('https://www.peopleapp.com/'))

    def test_unreviewed_page_does_not_bind_document_title(self):
        row = '<li><div class="fttime"><span class="day">12</span><span class="mouth">2025.05</span></div><div class="rttext"><h3><a href="/2025/0512/c1a1/page.htm">医学教学安排工作通知</a></h3></div></li>'
        feeds = publication_lists(listing('不相关网页标题', row), BASE + 'other/list.htm')
        self.assertFalse(any(f.get('heading_method') == 'current_medical_list_document' for f in feeds))


if __name__ == '__main__':
    unittest.main()
