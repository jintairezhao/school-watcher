"""A list and its banner may share a title; a neighboring widget may not borrow it."""
import unittest
from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_review_recovery import observed_scope_revision

URL = 'https://www.example.edu.cn/zhb/'


def module(title='工作动态', extra='', more='/zhb/gzdt/', head_extra=''):
    rows = ''.join('<li class="c-zonghe-module__half-li"><a href="/zhb/c/2026-10-20/'
        + str(539000 + index) + '.shtml">学校召开新学期工作会议' + str(index)
        + '</a><span>2026-10-20</span></li>' for index in range(3))
    banner = '<div class="c-zonghe-module__banner-box"><div class="gong-banner"><div class="banner-move_J"><div class="position-relative"><a href="/zhb/c/2026-10-20/538999.shtml">校区开展交流活动</a></div></div></div></div>'
    return ('<div class="c-zonghe-module__half-width"><div class="c-zonghe-module__head">'
        '<div class="c-zonghe-module__head-title-container"><span class="c-zonghe-module__head-title-color">'
        + title + '</span>' + head_extra + '</div><div class="c-zonghe-module__head-more">'
        '<a href="' + more + '">更多 &gt;&gt;</a></div></div><div class="c-zonghe-module__detail">'
        + banner + '<ul>' + rows + '</ul>' + extra + '</div></div>')


def config():
    return dict(name='综合办公室', list_url=URL, list_selector='li.c-zonghe-module__half-li',
        title_selector='a', link_selector='a', date_selector='span', content_selector='main', group_name='')


class ZongheModuleHeadingTests(unittest.TestCase):
    def test_existing_list_and_banner_use_their_exact_module_title_and_entrance(self):
        html = module()
        feed = next(feed for feed in publication_lists(html, URL) if feed['item_count'] == 3)
        self.assertEqual((feed['name'], feed['column_url']), ('工作动态', URL + 'gzdt/'))
        heading = BeautifulSoup(html, 'lxml').select_one(feed['heading_locator'])
        self.assertEqual(heading.get_text(strip=True), '工作动态')
        revision = observed_scope_revision(config(), html)
        self.assertEqual(revision['config']['name'], '工作动态')
        self.assertEqual({**revision['config'], 'name': config()['name']}, config())

    def test_neighbor_module_keeps_its_own_heading(self):
        html = module() + module(title='规章制度', more='/zhb/rules/')
        feeds = [feed for feed in publication_lists(html, URL) if feed['item_count'] == 3]
        self.assertEqual({(feed['name'], feed['column_url']) for feed in feeds},
                         {('工作动态', URL + 'gzdt/'), ('规章制度', URL + 'rules/')})
        self.assertIsNone(observed_scope_revision(config(), html), 'A joint selector is still not one source')

    def test_second_widget_cannot_borrow_the_module_title(self):
        feeds = publication_lists(module(extra='<section><h2>其他栏目</h2><a href="/other/">其他入口</a></section>'), URL)
        listed = next(feed for feed in feeds if feed['item_count'] == 3)
        self.assertFalse(listed['name'])
        self.assertTrue(listed.get('heading_ambiguous'))

    def test_duplicate_titles_are_not_arbitrarily_chosen(self):
        html = module(head_extra='<span class="c-zonghe-module__head-title-color">通知公告</span>')
        self.assertIsNone(observed_scope_revision(config(), html))

    def test_article_more_link_cannot_be_used_as_a_column_entrance(self):
        self.assertIsNone(observed_scope_revision(config(), module(more='/zhb/c/2026-10-20/539000.shtml')))

    def test_independent_tabs_are_not_concatenated_into_one_title(self):
        html = module(title='<button>工作动态</button><button>通知公告</button>')
        self.assertIsNone(observed_scope_revision(config(), html))

    def test_title_outside_the_module_is_not_donated(self):
        html = module().replace('c-zonghe-module__head-title-color', 'unrelated')
        html = '<header><span class="c-zonghe-module__head-title-color">工作动态</span></header>' + html
        self.assertIsNone(observed_scope_revision(config(), html))


if __name__ == '__main__':
    unittest.main()
