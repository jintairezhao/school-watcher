"""Publisher identity must come from site branding, not mentioned departments."""
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.services.source_workflow import _publisher_branding, publisher_proven


class PublisherBrandingScopeTests(unittest.TestCase):
    def check_chain(self, home, column=None):
        root = 'https://www.example.edu.cn/'
        unit = 'https://materials.example.edu.cn/'
        target = unit + 'notices/' if column is None else 'https://notice.example.edu.cn/notices/'
        texts = dict(root='<a href="/units/">组织机构</a>',
                     directory=f'<main><a href="{unit}">材料科学与工程学院</a></main>',
                     home=home+f'<a href="{target}">通知公告</a>')
        refs = {key:dict(url=url, hash=key) for key,url in
                [('root',root),('directory',root+'units/'),('home',unit)]}
        bundle = dict(root_url=root,school_name='东北大学',publisher_material=refs,
                      publisher_evidence=dict(name='材料科学与工程学院',
                          directory_evidence_id='directory',homepage_evidence_id='home'))
        if column is not None:
            texts['column'] = column
            bundle['list'] = dict(url=target,hash='column')
        with patch('backend.services.source_governance.read_snapshot',side_effect=lambda ref:texts[ref['hash']]):
            return publisher_proven(bundle,dict(group_name='材料科学与工程学院',list_url=target))

    def test_news_thumbnail_cannot_resolve_homepage_short_name(self):
        # Reduced from the retained NEU materials-college home: its own title
        # is short, while news thumbnail descriptions contain the full name.
        home = ('<title>材料学院</title><main><a href="/article/">'
                '<img alt="东北大学材料科学与工程学院本科生推免公示"></a></main>')
        self.assertFalse(self.check_chain(home))

    def test_navigation_and_article_mentions_are_not_site_branding(self):
        for body in ('<header><nav><a href="/friend/">材料科学与工程学院</a></nav></header>',
                     '<footer><a href="/friend/">材料科学与工程学院</a></footer>',
                     '<article><h1>材料科学与工程学院获奖</h1></article>'):
            with self.subTest(body=body):
                self.assertFalse(_publisher_branding(BeautifulSoup('<title>学校新闻</title>'+body,'lxml'),
                                                    '材料科学与工程学院'))

    def test_cross_site_column_cannot_use_article_thumbnail_as_publisher(self):
        target = '<title>通知公告</title><main><img alt="材料科学与工程学院活动报道"></main>'
        self.assertFalse(self.check_chain('<title>东北大学材料科学与工程学院</title>',target))

    def test_explicit_site_identity_forms_still_prove_branding(self):
        for html in ('<title>东北大学材料科学与工程学院</title>',
                     '<header><div class="logo"><img alt="东北大学材料科学与工程学院"></div></header>',
                     '<footer><p>东北大学材料科学与工程学院</p></footer>'):
            with self.subTest(html=html):
                self.assertTrue(self.check_chain(html))


if __name__ == '__main__':
    unittest.main()
