"""The acquisition gate must recognize the article layout already used by parsing."""
import unittest

from backend.scraper.acquisition import FetchRequest, FetchResult, classify_result

URL = 'https://news.example.edu.cn/kydt/article.htm'
BODY = '学校科研团队公布新的研究成果，介绍了研究方法、实验数据与结论，供师生阅读和参考。'


class GPArticleAcquisitionTests(unittest.TestCase):
    def classify(self, html, *, selector=None, status=200):
        return classify_result(FetchRequest(URL, purpose='article', readiness_selector=selector),
                               FetchResult(URL, status=status, html=html))

    def test_existing_gp_article_layout_is_readable_without_a_new_model_rule(self):
        html = f'<h3 id="shareTitle">科研成果发布</h3><p>发布时间：2026年10月14日</p><div class="gp-article gp-f18"><div id="articleDiv"><p>{BODY}</p></div></div>'
        result = self.classify(html)
        self.assertEqual(result.outcome, 'usable')
        self.assertIn('title_bound_article_region', result.evidence)

    def test_navigation_in_the_same_named_container_is_not_a_body(self):
        result = self.classify('<div class="gp-article"><nav><a href="/">学校首页</a><a href="/news/">新闻通知</a></nav></div>')
        self.assertNotEqual(result.outcome, 'usable')

    def test_heading_and_menu_alone_are_not_a_body(self):
        html = '<h3>科研成果发布</h3><div class="gp-article"><nav><a href="/">首页</a><a href="/news/">其他通知</a></nav></div>'
        self.assertNotEqual(self.classify(html).outcome, 'usable')

    def test_empty_or_hidden_article_region_does_not_prove_a_body(self):
        for html in ('<div class="gp-article"></div>', f'<div class="gp-article" hidden><p>{BODY}</p></div>'):
            with self.subTest(html=html):
                self.assertNotEqual(self.classify(html).outcome, 'usable')

    def test_explicit_content_contract_is_not_bypassed_by_a_neighbouring_article(self):
        result = self.classify(f'<div class="gp-article"><p>{BODY}</p></div>', selector='#required-body')
        self.assertNotEqual(result.outcome, 'usable')

    def test_access_failure_is_not_overridden_by_article_markup(self):
        result = self.classify(f'<div class="gp-article"><p>{BODY}</p></div>', status=403)
        self.assertNotEqual(result.outcome, 'usable')


if __name__ == '__main__':
    unittest.main()
