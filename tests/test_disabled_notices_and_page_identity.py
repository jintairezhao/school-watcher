"""Actual disabled notice links and the canonical crumb on a fetched next page."""
from pathlib import Path
import gzip
import unittest

from bs4 import BeautifulSoup
from backend.services.source_governance import _column_scope
from backend.services.source_onboarding import CONFIG_FIELDS
from backend.scraper.discovery.publication_lists import publication_lists
from backend.scraper.engine import _resolve_url
from tests.test_local_notice_breadcrumb import original, URL

NEXT = 'http://www.swpu.edu.cn/dky/tzgg/29.htm'


def following():
    return gzip.decompress((Path(__file__).parent / 'fixtures/structure/swpu-dky-notices-page2-76a3eae5-20261102.html.gz').read_bytes()).decode('utf-8')


def config():
    feed = publication_lists(original(),URL)[0]
    return dict({key:feed.get(key,'') for key in CONFIG_FIELDS},list_url=URL,group_name='地球科学与技术学院')


class DisabledNoticeAndPageIdentityTests(unittest.TestCase):
    def test_disabled_first_page_row_is_not_a_notification_for_the_list_url(self):
        rows,errors,_ = _column_scope(config(),{'url':URL},html=original())
        self.assertEqual(errors,[])
        self.assertEqual(len(rows),19)
        self.assertNotIn(URL,{row['url'] for row in rows})

    def test_real_next_page_keeps_canonical_column_identity_and_fourteen_live_rows(self):
        rows,errors,region = _column_scope(config(),{'url':NEXT},html=following())
        self.assertEqual(errors,[])
        self.assertEqual(len(rows),14)
        self.assertEqual(region['name'],'通知公告')
        self.assertNotIn(NEXT,{row['url'] for row in rows})
        self.assertEqual(len({row['url'] for row in rows}),len(rows))

    def test_other_canonical_crumb_or_partial_list_does_not_pass(self):
        soup = BeautifulSoup(following(),'lxml')
        soup.select_one('.right > .local').select('a')[-1]['href'] = '../other.htm'
        _,errors,_ = _column_scope(config(),{'url':NEXT},html=str(soup))
        self.assertIn('column_identity_or_scope_unconfirmed',errors)
        cfg = config()
        cfg['list_selector'] += ':nth-child(-n+3)'
        _,errors,_ = _column_scope(cfg,{'url':NEXT},html=following())
        self.assertIn('column_identity_or_scope_unconfirmed',errors)

    def test_collection_resolver_rejects_disabled_fragments_but_preserves_routes(self):
        for href in ('#','#expired',' # '):
            with self.subTest(href=href):
                self.assertFalse(_resolve_url(URL,href))
        self.assertEqual(_resolve_url(URL,'#/article/1'),URL+'#/article/1')
        self.assertEqual(_resolve_url(URL,'info/1012/43921.htm'),URL.rsplit('/',1)[0]+'/info/1012/43921.htm')


if __name__ == '__main__':
    unittest.main()
