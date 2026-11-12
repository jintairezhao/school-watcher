"""Terminal breadcrumbs name one complete list; earlier same-URL crumbs are context."""
import unittest
from bs4 import BeautifulSoup

from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_review_recovery import observed_scope_revision

URL = 'https://www.example.edu.cn/jwb/jxyx/tzgg/'


def document(leaf=URL, extra='', crumb=True, count=3):
    heading = ('<div class="c-main__title-container"><div class="c-main__cur">'
               '<a href="/jwb/">首页</a> / <a href="' + URL + '">教学运行</a> / '
               '<a href="' + leaf + '">通知公告</a></div></div>') if crumb else ''
    rows = ''.join('<li class="middleNotice__notice--list"><a href="/jwb/c/2026-09-17/'
                   + str(538585 + index) + '.shtml">关于本学期教学安排的通知' + str(index)
                   + '</a><span class="middleNotice--dates">2026-09-17</span></li>' for index in range(count))
    return ('<div class="c-main__right">' + heading + '<div class="middleNotice">'
            '<ul class="middleNotice__notice">' + rows + '</ul></div>' + extra + '</div>')


class ZcmsNoticeBreadcrumbTests(unittest.TestCase):
    def test_terminal_self_link_names_list_despite_same_url_parent(self):
        html = document()
        feeds = publication_lists(html, URL)
        self.assertEqual(len(feeds), 1)
        feed = feeds[0]
        self.assertEqual((feed['name'], feed['column_url']), ('通知公告', URL))
        self.assertEqual(feed['heading_method'], 'current_page_breadcrumb')
        self.assertNotIn('column_group_name', feed)
        soup = BeautifulSoup(html, 'lxml')
        self.assertEqual(soup.select_one(feed['heading_locator']).get_text(strip=True), '通知公告')
        self.assertEqual(len(soup.select(feed['list_selector'])), 3)

    def test_wrong_terminal_link_cannot_borrow_the_earlier_matching_link(self):
        feeds = publication_lists(document(leaf=URL + 'other/'), URL)
        self.assertTrue(feeds)
        self.assertTrue(all(not feed['name'] and feed.get('heading_ambiguous') for feed in feeds))

    def test_neighbour_list_makes_shared_breadcrumb_insufficient(self):
        extra = '<section><h2>学院新闻</h2><ul><li><a href="/jwb/c/2026-09-17/539000.shtml">学院科研取得新进展</a></li></ul></section>'
        feeds = publication_lists(document(extra=extra), URL)
        notice = next(feed for feed in feeds if any('/538585.' in item['url'] for item in feed['samples']))
        self.assertFalse(notice['name'])
        self.assertTrue(notice.get('heading_ambiguous'))

    def test_header_or_sidebar_breadcrumb_cannot_name_the_main_list(self):
        html = document(crumb=False).replace('<div class="c-main__right">',
            '<header><div class="c-main__cur"><a href="' + URL + '">通知公告</a></div></header>'
            '<div class="c-main__right">')
        self.assertTrue(all(not feed['name'] for feed in publication_lists(html, URL)))

    def test_two_breadcrumb_blocks_are_not_arbitrarily_chosen(self):
        extra = '<div class="c-main__title-container"><div class="c-main__cur"><a href="' + URL + '">学院新闻</a></div></div>'
        feeds = publication_lists(document(extra=extra), URL)
        self.assertTrue(all(not feed['name'] and feed.get('heading_ambiguous') for feed in feeds))

    def test_current_article_address_cannot_become_a_column_self_link(self):
        article_url = 'https://www.example.edu.cn/jwb/c/2026-09-17/538585.shtml'
        feeds = publication_lists(document(leaf=article_url), article_url)
        self.assertTrue(all(not feed['name'] for feed in feeds))

    def test_second_page_requires_its_observed_pager_template(self):
        page = URL + 'index_2.shtml'
        script = '<script>function page(n){return "index_${PageIndex}.shtml".replace("${PageIndex}",n);}</script>'
        feed = publication_lists(document(extra=script), page)[0]
        self.assertEqual((feed['name'], feed['column_url']), ('通知公告', URL))
        self.assertEqual(feed['heading_method'], 'pagination_page_breadcrumb')
        self.assertEqual(feed['listing_alias']['url'], page)
        self.assertTrue(all(not feed['name'] for feed in publication_lists(document(), page)))

    def test_existing_review_recognizes_exact_scope_without_changing_identity_fields(self):
        config = dict(name='教务部-教学运行', list_url=URL, list_selector='li.middleNotice__notice--list',
            title_selector='a', link_selector='a', date_selector='.middleNotice--dates',
            content_selector='.article-body', group_name='组织机构')
        revision = observed_scope_revision(config, document())
        self.assertIsNotNone(revision)
        self.assertEqual(revision['config']['name'], '通知公告')
        self.assertEqual({**revision['config'], 'name': config['name']}, config)

    def test_one_entry_last_page_uses_its_actual_breadcrumb_and_pager_proof(self):
        from backend.services.source_governance import _column_scope
        page = URL + 'index_2.shtml'
        script = '<script>function page(n){return "index_${PageIndex}.shtml".replace("${PageIndex}",n);}</script>'
        config = dict(name='通知公告', list_url=URL, list_selector='li.middleNotice__notice--list',
            title_selector='a', link_selector='a', date_selector='.middleNotice--dates',
            content_selector='main', group_name='')
        records, errors, region = _column_scope(config, {'url': page}, html=document(extra=script, count=1))
        self.assertEqual(len(records), 1)
        self.assertEqual(errors, [])
        self.assertEqual((region['name'], region['basis']), ('通知公告', 'single_entry_breadcrumb'))
        self.assertTrue(_column_scope(config, {'url': page}, html=document(count=1))[1])

    def test_selecting_one_entry_from_a_larger_list_is_not_the_complete_column(self):
        from backend.services.source_governance import _column_scope
        config = dict(name='通知公告', list_url=URL, list_selector='li.middleNotice__notice--list:first-child',
            title_selector='a', link_selector='a', date_selector='.middleNotice--dates',
            content_selector='main', group_name='')
        self.assertTrue(_column_scope(config, {'url': URL}, html=document())[1])

    def test_one_entry_page_still_rejects_neighbor_widget_or_wrong_name(self):
        from backend.services.source_governance import _column_scope
        config = dict(name='其他栏目', list_url=URL, list_selector='li.middleNotice__notice--list',
            title_selector='a', link_selector='a', date_selector='.middleNotice--dates',
            content_selector='main', group_name='')
        self.assertTrue(_column_scope(config, {'url': URL}, html=document(count=1))[1])
        config['name'] = '通知公告'
        extra = '<section><h2>其他栏目</h2><a href="/other/">其他入口</a></section>'
        self.assertTrue(_column_scope(config, {'url': URL}, html=document(count=1, extra=extra))[1])


if __name__ == '__main__':
    unittest.main()
