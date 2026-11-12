"""A current sidebar self-link can identify the sole adjacent school list."""
from pathlib import Path
import gzip
import unittest
from bs4 import BeautifulSoup
from backend.scraper.discovery.publication_lists import publication_lists
from backend.services.source_governance import _column_scope

URL = 'https://www.cupk.edu.cn/tzgg/'
HTML = gzip.decompress((Path(__file__).parent / 'fixtures/structure/cupk-school-notices-43dec74cbc625f10.html.gz').read_bytes()).decode('utf8')


class ZcmsActiveColumnTests(unittest.TestCase):
    def test_original_current_sidebar_names_only_the_complete_article_list(self):
        feeds = publication_lists(HTML, URL)
        self.assertEqual(len(feeds), 1)
        self.assertEqual((feeds[0]['name'], feeds[0]['column_url']), ('通知公告', URL))
        self.assertEqual(feeds[0]['heading_method'], 'current_sidebar_self_link')
        soup = BeautifulSoup(HTML, 'lxml')
        self.assertEqual(soup.select_one(feeds[0]['heading_locator']).get_text(strip=True), '通知公告')
        config = dict(name='通知公告', list_url=URL, list_selector='div.SecondArticle__art > div.SecondArticle__art--art',
            title_selector='h3.SecondArticle__art--titles', link_selector='a.SecondArticle__art--link',
            date_selector='.SecondArticle__dates', content_selector='', group_name='')
        records, errors, _ = _column_scope(config, {'url': URL}, html=HTML)
        self.assertEqual((len(records), errors), (10, []))

    def test_conflicting_active_links_mobile_label_or_neighbor_list_stays_unknown(self):
        for mutation in ('href', 'duplicate', 'mobile', 'neighbor'):
            with self.subTest(mutation=mutation):
                soup = BeautifulSoup(HTML, 'lxml')
                active = soup.select_one('.SecondLeft-nav__left--navClick')
                if mutation == 'href':
                    active.a['href'] = '/other/'
                elif mutation == 'duplicate':
                    active.parent.append(BeautifulSoup(str(active), 'lxml').li)
                elif mutation == 'mobile':
                    soup.select_one('.SecondTop-nav__top--navClick a').string = '校区新闻'
                else:
                    soup.select_one('.c-main__right').append(BeautifulSoup(
                        '<section><h2>学院新闻</h2><a href="/c/2026-01-01/100000.shtml">另一个独立栏目的通知</a></section>', 'lxml'))
                self.assertTrue(all(not feed['name'] for feed in publication_lists(str(soup), URL)))

    def test_second_page_needs_literal_pager_evidence(self):
        feeds = publication_lists(HTML, URL+'index_2.shtml')
        self.assertEqual((feeds[0]['name'], feeds[0]['column_url']), ('通知公告', URL))
        soup = BeautifulSoup(HTML, 'lxml')
        for script in soup.select('.SecondArticle__art script'):
            script.decompose()
        self.assertTrue(all(not feed['name'] for feed in publication_lists(str(soup), URL+'index_2.shtml')))

    def test_active_article_address_or_missing_current_marker_cannot_name_list(self):
        soup = BeautifulSoup(HTML, 'lxml')
        soup.select_one('.SecondLeft-nav__left--navClick a')['href'] = '/c/2026-01-01/100000.shtml'
        self.assertTrue(all(not f['name'] for f in publication_lists(str(soup), 'https://www.cupk.edu.cn/c/2026-01-01/100000.shtml')))
        soup = BeautifulSoup(HTML, 'lxml')
        soup.select_one('.SecondLeft-nav__left--navClick')['class'] = []
        self.assertTrue(all(not f['name'] for f in publication_lists(str(soup), URL)))


if __name__ == '__main__':
    unittest.main()
