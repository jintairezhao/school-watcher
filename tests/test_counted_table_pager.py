"""A real counted table pager binds to its column without global label aliases."""
from pathlib import Path
import gzip
import hashlib
import unittest

from bs4 import BeautifulSoup
from backend.scraper.engine import _counted_pager, _next_page_url

URL='https://www.swpu.edu.cn/gs/pygl/tzgg.htm'
NEXT='https://www.swpu.edu.cn/gs/pygl/tzgg/9.htm'
SELECTOR='.rightcolmn > .body > table.winstyle200922 > tr:has(> td > a[title][href])'
SCOPE=dict(container_selector='.rightcolmn',heading_selector='.rightcolmn > div:first-child')


def original():
    path=Path(__file__).parent/'fixtures/structure/swpu-gs-cultivation-a79a1e8d-20261103.html.gz'
    raw=gzip.decompress(path.read_bytes())
    assert hashlib.sha256(raw).hexdigest()=='a79a1e8d04b4e468d551fa3973587e3890ad94a44a0805fb4d162b889b5f8a59'
    return BeautifulSoup(raw.decode(),'lxml')


def pager(soup):
    return next(a for a in soup.select('a[href]') if a.get_text(' ',strip=True)=='下页').find_parent('table')


class CountedTablePagerTests(unittest.TestCase):
    def next(self,soup):
        return _next_page_url(str(soup),URL,1,list_selector=SELECTOR,scope=SCOPE)

    def test_retained_count_current_total_and_jump_control_bind_actual_next_page(self):
        soup=original()
        self.assertEqual(len(soup.select(SELECTOR)),20)
        self.assertTrue(_counted_pager(pager(soup)))
        self.assertEqual(self.next(soup),NEXT)

    def test_last_page_label_without_count_evidence_keeps_unknown_ownership(self):
        soup=original();pager(soup).select_one('[id^="fanye"]').clear()
        self.assertFalse(_counted_pager(pager(soup)))
        self.assertIsNone(self.next(soup))

    def test_invalid_current_total_does_not_supply_page_ownership(self):
        soup=original();counter=pager(soup).select_one('[id^="fanye"]')
        counter.string=counter.get_text().replace('1/10','11/10')
        self.assertFalse(_counted_pager(pager(soup)))
        self.assertIsNone(self.next(soup))

    def test_page_suffix_requires_the_real_jump_control_and_page_input(self):
        for selector in ('input[type="button"]','input:not([type])'):
            with self.subTest(selector=selector):
                soup=original();pager(soup).select_one(selector).decompose()
                self.assertFalse(_counted_pager(pager(soup)))
                self.assertIsNone(self.next(soup))

    def test_extra_article_in_pager_wrapper_is_not_hidden_by_counts(self):
        soup=original();link=soup.new_tag('a',href='other-article.htm');link.string='另一个栏目的真实通知'
        pager(soup).select_one('div').append(link)
        self.assertFalse(_counted_pager(pager(soup)))
        self.assertIsNone(self.next(soup))

    def test_visible_page_suffix_without_page_input_remains_unknown(self):
        soup=original();pager(soup).select_one('input:not([type])')['type']='hidden'
        self.assertFalse(_counted_pager(pager(soup)))
        self.assertIsNone(self.next(soup))

    def test_valid_table_controls_under_neighbor_heading_do_not_rebind(self):
        soup=original();block=pager(soup).find_parent('tr').extract()
        neighbor=soup.new_tag('section');heading=soup.new_tag('h2');heading.string='其他栏目'
        item=soup.new_tag('a',href='neighbor-article.htm');item.string='相邻栏目中的真实通知'
        for node in (heading,item,block):neighbor.append(node)
        soup.select_one('.rightcolmn > .body').append(neighbor)
        self.assertIsNone(self.next(soup))


if __name__=='__main__':unittest.main()
