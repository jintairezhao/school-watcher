"""Source trial validation must distinguish layout names from navigation."""
import unittest

from backend.services.source_validation import article_samples


class SourceArticleLayoutTests(unittest.TestCase):
    title = '探索生命奥秘，点亮生活美学：我校公选实验课《生命的艺术与创作》圆满收官'
    url = 'http://ugs.hust.edu.cn/info/1181/7228.htm'

    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def page(self, wrapper='div', marker='nav_right', body=None):
        # Reduced actual HUST structure and literal title/date from retained
        # HTML (its full hash is recorded by the retained-material acceptance
        # replay). Body text below is a controlled fixture, not a live result.
        prose = body if body is not None else '<p>' + '本课程结合生命科学与艺术创作开展实验教学，学生完成课程实践并展示自己的学习成果。' * 2 + '</p>'
        return (f'<html><head><title>{self.title}</title></head><body>'
                f'<{wrapper} class="{marker}"><div class="right_inner"><div class="content">'
                f'<div class="content_title"><h1>{self.title}</h1>'
                '<p>作者：　　时间：2025-12-09　　浏览：</p></div>'
                f'<div class="v_news_content">{prose}</div></div></div></{wrapper}></body></html>')

    def observe(self, html, selector=''):
        return article_samples({'list_url':'http://ugs.hust.edu.cn/','content_selector':selector},
            [{'title':self.title,'url':self.url}],lambda *_:html)['samples'][0]

    def test_local_article_proof_preserves_navigation_named_layout(self):
        for marker in ('nav_right','navigation-main','content_nav_wrapper'):
            for selector in ('','.v_news_content'):
                with self.subTest(marker=marker,selector=selector):
                    self.assertTrue(self.observe(self.page(marker=marker),selector)['matched'])

    def test_actual_navigation_and_sidebar_stay_excluded(self):
        for wrapper,marker in (('nav','nav_right'),('aside','sidebar'),('div','sidebar nav_right'),
                               ('div','related navigation-main'),('div','menu nav_right')):
            with self.subTest(wrapper=wrapper,marker=marker):
                self.assertFalse(self.observe(self.page(wrapper,marker),'.v_news_content')['matched'])

    def test_metadata_or_menu_text_cannot_supply_article_body(self):
        bodies = ('<p>时间：2025-12-09</p>',
                  '<nav><p>'+'本段为导航菜单说明文字，不属于通知正文。'*5+'</p></nav>',
                  '<div class="sidebar"><p>'+'本段为侧边栏说明文字，不属于通知正文。'*5+'</p></div>',
                  '<p hidden>'+'本段为尚不可见的材料，不属于可验证正文。'*5+'</p>',
                  '<p aria-busy="true">'+'本段仍在异步加载，不属于已经就绪的正文。'*5+'</p>')
        for body in bodies:
            with self.subTest(body=body[:40]):
                self.assertFalse(self.observe(self.page(body=body))['matched'])

    def test_activity_time_cannot_validate_the_layout_exception(self):
        html=self.page().replace('作者：　　时间：2025-12-09　　浏览：','活动时间：2025-12-09')
        self.assertFalse(self.observe(html)['matched'])


if __name__ == '__main__':
    unittest.main()
