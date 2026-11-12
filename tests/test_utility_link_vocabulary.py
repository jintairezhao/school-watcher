"""Named link widgets prove navigation without depending on topic vocabulary."""
import unittest

from backend.scraper.discovery.structure import classify, extract_structure
from backend.services.source_relationships import ROSTER_RELATIONS

ROOT='https://campus.example.edu/'
HOME=ROOT+'teaching/'
WIDGET='''<div class="c-main-home-right__block"><div class="c-links">
<div class="c-verticalTitle">下载专区</div>
<a class="c-iconItem js-iconItem" href="/teaching/forms/teachers/"><div class="icon icon-link"></div>教师用表</a>
<a class="c-iconItem js-iconItem" href="/teaching/forms/students/">学生用表</a>
</div></div>'''


class UtilityLinkVocabularyTests(unittest.TestCase):
    def test_actual_widget_shape_keeps_unclassified_form_entrances(self):
        self.assertIsNone(classify('教师用表'))
        for page_kind in ('unit','directory'):
            with self.subTest(page_kind=page_kind):
                parsed=extract_structure(WIDGET,HOME,ROOT,page_kind,'教务部')
                for label,target in [('教师用表',HOME+'forms/teachers/'),('学生用表',HOME+'forms/students/')]:
                    links=[e for e in parsed['links'] if e['url']==target]
                    self.assertEqual(len(links),1)
                    self.assertEqual((links[0]['label'],links[0]['kind'],links[0]['decision']),(label,'navigation','follow'))
                    node=next(n for n in parsed['nodes'] if n['url']==target)
                    self.assertEqual(node['relation'],'navigation_entry')

    def test_ordinary_body_text_and_arbitrary_css_are_not_a_link_widget(self):
        for markup in ('<main><a href="/explore/">交叉领域</a></main>',
                       '<div class="links-article-text"><a href="/explore/">交叉领域</a></div>'):
            self.assertFalse(any(e['url']==ROOT+'explore/' for e in extract_structure(markup,HOME,ROOT,'unit','教务部')['links']))

    def test_link_widget_on_a_directory_never_supplies_roster_membership(self):
        html='<main><a href="/college/">计算机学院</a></main><aside class="quick-links"><a href="/college/">计算机学院</a></aside>'
        parsed=extract_structure(html,ROOT+'units/',ROOT,'directory','院系设置')
        nodes=[n for n in parsed['nodes'] if n['name']=='计算机学院']
        self.assertEqual({n['relation'] for n in nodes},{'directory_entry','navigation_entry'})
        self.assertFalse(any(n['relation'] in ROSTER_RELATIONS for n in nodes if 'aside:' in n['locator']))

    def test_service_and_attachment_decisions_are_preserved(self):
        html='<div class="useful_links"><a href="/sso/">身份认证</a><a href="/forms.docx">样表</a></div>'
        links={e['label']:e for e in extract_structure(html,HOME,ROOT,'unit','教务部')['links']}
        self.assertEqual(links['身份认证']['decision'],'restricted_or_service')
        self.assertEqual(links['样表']['decision'],'document_reference')


if __name__=='__main__':unittest.main()
