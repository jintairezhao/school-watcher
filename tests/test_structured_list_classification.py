"""A parsed visible list must pass acquisition without weakening access contracts."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from backend.scraper.acquisition import FetchRequest,FetchResult,classify_result
from backend.scraper.discovery.publication_lists import publication_lists

URL='https://example.edu.cn/notices/list.htm'


def split_rows(prefix='2026/1104/c10a',suffix='/page.htm'):
    return '<h2>通知公告</h2><ul class="news_list">'+''.join(
        '<li class="news"><div class="news_title"><a href="'+prefix+str(index)+suffix+'">'
        '关于本科生实践项目申报安排的通知'+str(index)+'</a></div>'
        '<span class="news_meta">2026-11-04</span></li>' for index in range(3))+'</ul>'


class StructuredListClassificationTests(unittest.TestCase):
    def setUp(self):
        forbidden=AssertionError('List recognition checks forbid HTTP, models and databases')
        for target in ('socket.getaddrinfo','socket.socket.connect','sqlite3.connect'):
            guard=patch(target,side_effect=forbidden);guard.start();self.addCleanup(guard.stop)

    def raw(self,html):return FetchResult(URL,status=200,html=html)

    def test_visible_parsed_rows_with_dates_outside_anchor_parent_are_usable(self):
        for html in (split_rows(),split_rows('detail.jsp?notice=','')):
            with self.subTest(html=html):
                self.assertEqual(publication_lists(html,URL)[0]['item_count'],3)
                result=classify_result(FetchRequest(URL,purpose='list'),self.raw(html))
                self.assertEqual(result.outcome,'usable')
                self.assertIn('publication_list_parser',result.evidence)

    def test_hidden_parsed_rows_do_not_override_visible_empty_message(self):
        html='<p>暂无通知</p><section hidden>'+split_rows()+'</section>'
        self.assertEqual(classify_result(FetchRequest(URL,purpose='list'),self.raw(html)).outcome,'empty')

    def test_navigation_links_are_not_a_publication_list(self):
        html='<nav><ul>'+''.join('<li><a href="2026/1104/c10a'+str(n)+'/page.htm">'
            '学院机构职责介绍'+str(n)+'</a></li>' for n in range(3))+'</ul></nav>'
        self.assertEqual(classify_result(FetchRequest(URL,purpose='list'),self.raw(html)).outcome,'needs_adapter')

    def test_access_failure_and_explicit_readiness_contract_remain_authoritative(self):
        raw=self.raw(split_rows())
        self.assertEqual(classify_result(FetchRequest(URL,purpose='list'),replace(raw,status=403)).outcome,'denied')
        request=FetchRequest(URL,purpose='list',readiness_selector='#missing-target')
        result=classify_result(request,raw)
        self.assertEqual((result.outcome,result.error_code),('needs_adapter','readiness_missing'))


if __name__=='__main__':unittest.main()
