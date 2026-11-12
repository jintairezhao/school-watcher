"""CMS indentation must not count as publication metadata's visible length."""
import unittest
from bs4 import BeautifulSoup
from backend.scraper.acquisition import FetchRequest,FetchResult
from backend.scraper.acquisition.classifier import classify_result
from backend.scraper.article_observation import title_bound_body

TITLE='校区举办第二期“湖畔·论学”学生工作论坛'
URL='https://www.cupk.edu.cn/c/2026-11-07/539634.shtml'
PROSE='本段为受控正文夹具，用于验证页面排版空白不会阻断文章正文识别。'


class ArticleMetadataWhitespaceTests(unittest.TestCase):
    def page(self,metadata,body=None):
        # Header arrangement is reduced from the approved 2026-11-08 response;
        # people and body text here are synthetic. The raw response stays in
        # the scoped evidence directory with its original content hash.
        return ('<div class="c-right-detail"><div class="c-right-detail__header"><div class="c-right-detail__title">'
                f'<div class="c-right-detail__title-name1">{TITLE}</div></div></div>'
                f'<div class="SecondList__msg">{metadata}</div><div class="c-right-detail__sub-title"></div>'
                f'<div class="c-right-detail__content">{body or "<p>"+PROSE+"</p>"}</div></div>')

    def metadata(self,date='20261107',gap=' '):
        return gap.join([f'发布日期：{date}','作者：示例甲','摄影：示例乙','来源：学生工作与安全保卫部','编辑：示例丙','浏览：65'])

    def test_indented_header_recovers_article_despite_stale_selector(self):
        html=self.page(self.metadata(gap='\xa0\xa0\n'+' '*48))+'<script src="/counter.js"></script>'
        self.assertGreater(len(BeautifulSoup(html,'lxml').select_one('.SecondList__msg').get_text()),220)
        request=FetchRequest(URL,purpose='article',readiness_selector='article',
            policy={'expected_article_title':TITLE,'article_selector_hint':True})
        result=classify_result(request,FetchResult(URL,status=200,html=html))
        self.assertEqual(result.outcome,'usable',result.message)
        self.assertIn('title_bound_article',result.evidence)

    def test_line_breaks_tabs_and_spaces_preserve_the_same_body(self):
        for date in ('20261107','2026-11-07'):
            for gap in (' ','\n'+' '*60,'\xa0\n\t'*30):
                with self.subTest(date=date,gap=repr(gap)):
                    body=title_bound_body(BeautifulSoup(self.page(self.metadata(date,gap)),'lxml'),URL,TITLE)
                    self.assertIsNotNone(body)
                    self.assertEqual(body.get_text(' ',strip=True),PROSE)

    def test_actual_long_metadata_still_does_not_supply_a_custom_body(self):
        text=self.metadata()+' 本段是较长的其他说明文字，不能当作短发布信息使用。'*20
        self.assertIsNone(title_bound_body(BeautifulSoup(self.page(text),'lxml'),URL,TITLE))

    def test_sidebar_publication_field_is_not_article_metadata(self):
        html=self.page('')+f'<aside>{self.metadata(gap=" "*240)}</aside>'
        self.assertIsNone(title_bound_body(BeautifulSoup(html,'lxml'),URL,TITLE))


if __name__=='__main__':unittest.main()
