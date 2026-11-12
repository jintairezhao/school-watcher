"""A retained numbered pager has current-page proof but no total-count text."""
from copy import deepcopy
import unittest

from bs4 import BeautifulSoup
from backend.scraper.engine import _counted_pager, _next_page_url
from backend.scraper.discovery.publication_lists import publication_lists
from tests.test_local_notice_breadcrumb import original, URL


class IndexedPagerTests(unittest.TestCase):
    def next(self, soup):
        html = str(soup)
        feed = publication_lists(html, URL)[0]
        return _next_page_url(html, URL, 1, list_selector=feed['list_selector'])

    def test_retained_current_page_and_jump_controls_bind_actual_next_link(self):
        self.assertEqual(self.next(BeautifulSoup(original(),'lxml')),URL.rsplit('/',1)[0]+'/tzgg/29.htm')

    def test_numbered_group_and_outer_jump_wrapper_are_bounded_controls(self):
        soup = BeautifulSoup(original(),'lxml')
        self.assertTrue(_counted_pager(soup.select_one('.p_pages')))
        self.assertTrue(_counted_pager(soup.select_one('.pb_sys_common')))

    def test_pager_classes_cannot_hide_foreign_article(self):
        soup = BeautifulSoup(original(),'lxml')
        link = soup.new_tag('a',href='info/1012/other.htm')
        link.string = '其他发布区域的重要通知'
        soup.select_one('.pb_sys_common').append(link)
        self.assertFalse(_counted_pager(soup.select_one('.pb_sys_common')))
        self.assertIsNone(self.next(soup))

    def test_unmarked_or_duplicate_current_page_is_not_proof(self):
        for duplicate in (False,True):
            with self.subTest(duplicate=duplicate):
                soup = BeautifulSoup(original(),'lxml')
                current = soup.select_one('.p_no_d')
                if duplicate:
                    current.parent.append(deepcopy(current))
                else:
                    current['class'] = ['plain']
                self.assertFalse(_counted_pager(soup.select_one('.p_pages')))
                self.assertFalse(_counted_pager(soup.select_one('.pb_sys_common')))
                self.assertIsNone(self.next(soup))

    def test_multiple_page_groups_do_not_name_one_wrapper(self):
        soup = BeautifulSoup(original(),'lxml')
        pager = soup.select_one('.p_pages')
        pager.parent.append(deepcopy(pager))
        self.assertFalse(_counted_pager(soup.select_one('.pb_sys_common')))
        self.assertIsNone(self.next(soup))

    def test_valid_controls_under_neighbor_heading_belong_to_neighbor(self):
        soup = BeautifulSoup(original(),'lxml')
        pager = soup.select_one('.pb_sys_common').extract()
        neighbor = soup.new_tag('section')
        heading = soup.new_tag('h2'); heading.string = '其他栏目'
        entry = soup.new_tag('div')
        link = soup.new_tag('a',href='other.htm'); link.string = '相邻栏目的真实条目'
        entry.append(link)
        for node in (heading,entry,pager): neighbor.append(node)
        soup.select_one('.news-text').append(neighbor)
        self.assertIsNone(self.next(soup))


if __name__ == '__main__':
    unittest.main()
