"""Browser-resolvable cross-scheme same-host references retain their articles."""
import unittest
from backend.services.source_inventory import resolve_page_link
from backend.scraper.engine import _resolve_url
from backend.services.source_governance import _column_scope

BASE='https://www.example.edu.cn/news/index_17.shtml'


class SameHostSchemeLinkTests(unittest.TestCase):
    def test_observed_cross_scheme_missing_authority_slashes_matches_browser_target(self):
        link='http:www.example.edu.cn/college/c/2025-06-12/528820.shtml'
        expected='http://www.example.edu.cn/college/c/2025-06-12/528820.shtml'
        self.assertEqual(resolve_page_link(BASE,link),expected)
        self.assertEqual(_resolve_url(BASE,link),expected)

    def test_complete_column_keeps_the_notice_with_the_observed_spelling(self):
        config=dict(name='通知公告',group_name='',list_url=BASE,list_selector='#notices li',
            title_selector='a',link_selector='a',date_selector='span',content_selector='article')
        rows=''.join('<li><a href="'+href+'">关于本学期学生教学事项安排的通知'+str(i)+'</a><span>2025-06-13</span></li>'
            for i,href in enumerate(('/news/c/2025-06-12/1.shtml','http:www.example.edu.cn/college/c/2025-06-12/2.shtml','/news/c/2025-06-12/3.shtml')))
        html='<section id="notices"><h2>通知公告</h2><ul>'+rows+'</ul></section>'
        records,errors,region=_column_scope(config,{'url':BASE},html=html)
        self.assertEqual(errors,[])
        self.assertEqual(len(records),3)
        self.assertIn('http://www.example.edu.cn/college/c/2025-06-12/2.shtml',{r['url'] for r in records})

    def test_cross_scheme_foreign_or_disguised_authorities_are_not_repaired(self):
        for link in ('http:other.example.edu.cn/news','http:www.example.edu.cn.evil.invalid/news',
            'http:www.example.edu.cn@evil.invalid/news','http:user@www.example.edu.cn/news',
            'http:www.example.edu.cn:bad/news','http:www.example.edu.cn:8080/news',
            'http:www.example.edu.cn\\evil/news','http:www.example.edu.cn/line\nbreak'):
            with self.subTest(link=link):self.assertEqual(resolve_page_link(BASE,link),'')

    def test_same_scheme_reference_retains_existing_relative_resolution(self):
        self.assertEqual(resolve_page_link(BASE,'https:www.example.edu.cn/item'),
            'https://www.example.edu.cn/news/www.example.edu.cn/item')

    def test_query_route_and_ordinary_links_keep_existing_meaning(self):
        self.assertEqual(resolve_page_link(BASE,'http:www.example.edu.cn/items?b=2&a=1#/detail/1'),
            'http://www.example.edu.cn/items?a=1&b=2#/detail/1')
        self.assertEqual(resolve_page_link(BASE,'https://other.example.edu.cn/news'),
            'https://other.example.edu.cn/news')
        self.assertEqual(resolve_page_link(BASE,'../notice.htm'),'https://www.example.edu.cn/notice.htm')
        self.assertEqual(resolve_page_link(BASE,'#expired'),'')


if __name__=='__main__':unittest.main()
