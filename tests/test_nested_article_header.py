"""A title-only wrapper cannot turn real adjacent body text into a missing page."""
import unittest
from bs4 import BeautifulSoup
from backend.services.source_validation import _remove_article_noise, article_samples
from tests.test_direct_onboarding import DirectOnboardingTests

TITLE='关于组织申报国际科技创新合作项目的通知'
HTML='<main><div class="headline-wrapper"><div class="article__header">' \
    '<div class="article-title">'+TITLE+'</div></div></div>' \
    '<div class="publication">发布日期：2026-11-06</div><div class="article-body">' \
    '<h1>根据上级部门关于发布项目申报指南的要求，现将有关事项通知如下：</h1>' \
    '<p>各学院请按照相关申报要求准备材料，并于规定时间提交申报系统，具体安排和联系人详见本通知。</p></div></main>'


class NestedArticleHeaderTests(unittest.TestCase):
    def test_title_only_wrapper_keeps_the_real_headline_and_prose_h1(self):
        soup=BeautifulSoup(HTML,'lxml')
        _remove_article_noise(soup,{'content_selector':'.article-body'},title=TITLE,page_url='https://example.edu.cn/info/1.htm')
        self.assertIsNotNone(soup.select_one('.article-title'))
        self.assertIsNotNone(soup.select_one('.article-body h1'))

    def test_native_article_validation_matches_the_same_retained_title_and_body(self):
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            config=dict(content_selector='.article-body',list_url='https://example.edu.cn/notices/')
            result=article_samples(config,[dict(url='https://example.edu.cn/info/1.htm',title=TITLE)],lambda *args:HTML)
            self.assertTrue(result['passed'],result)
        finally:fixture.tearDown()

    def test_executed_article_column_can_include_the_title_wrapper(self):
        # Malformed CMS closing tags can leave the prose outside its advertised
        # body div. The configured column still bounds this one article.
        html=HTML.replace('<main>', '<main><div class="article-column">').replace('</main>', '</div></main>')
        fixture=DirectOnboardingTests();fixture.setUp()
        try:
            result=article_samples(dict(content_selector='.article-column',list_url='https://example.edu.cn/notices/'),
                [dict(url='https://example.edu.cn/info/1.htm',title=TITLE)],lambda *args:html)
            self.assertTrue(result['passed'],result)
        finally:fixture.tearDown()

    def test_unrelated_wrapper_text_and_site_brand_do_not_borrow_body_evidence(self):
        for changed in (HTML.replace('<div class="headline-wrapper">','<div class="headline-wrapper"><p>另一个网站导航</p>'),
                        HTML.replace('headline-wrapper','site-brand')):
            soup=BeautifulSoup(changed,'lxml')
            _remove_article_noise(soup,{'content_selector':'.article-body'},title=TITLE,page_url='https://example.edu.cn/info/1.htm')
            self.assertIsNone(soup.select_one('.article-title'))


if __name__=='__main__':unittest.main()
