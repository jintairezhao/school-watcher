"""A standalone footer signature is distinct from partner and navigation links."""
import unittest
from bs4 import BeautifulSoup
from backend.services.source_ownership import branding_candidates

SIGNATURE='中国石油大学（北京）克拉玛依校区就业指导中心'

class FooterPublisherIdentityTests(unittest.TestCase):
    def candidates(self,html):
        return [p for p in branding_candidates(BeautifulSoup(html,'lxml')) if p['source']=='footer_signature']

    def test_standalone_signature_is_separate_from_the_footer_friend_links(self):
        html=('<div class="footer"><div class="footer-top"><a href="https://other.edu.cn">其他大学学院</a></div>'
              f'<div class="school">{SIGNATURE}</div><div>学校地址：安定路355号</div></div>')
        found=self.candidates(html)
        self.assertEqual([p['identity'] for p in found],[SIGNATURE])
        self.assertIn('div:nth-of-type(2)',found[0]['locator'])

    def test_body_mentions_links_and_partner_groups_are_not_signatures(self):
        for html in (f'<div class="school">{SIGNATURE}</div>',
                f'<div class="footer"><a href="/partner">{SIGNATURE}</a></div>',
                f'<div class="footer"><div class="friend-links"><div>{SIGNATURE}</div></div></div>',
                f'<div class="footer"><div class="partners">{SIGNATURE}</div></div>',
                f'<div class="footer"><div>合作单位：{SIGNATURE}</div></div>'):
            with self.subTest(html=html):self.assertEqual(self.candidates(html),[])

    def test_hidden_or_loading_signatures_are_not_current_visible_evidence(self):
        for attribute in ('hidden','style="display:none"','aria-busy="true"'):
            with self.subTest(attribute=attribute):
                self.assertEqual(self.candidates(f'<div class="footer" {attribute}><div>{SIGNATURE}</div></div>'),[])

    def test_footer_signature_can_complete_a_real_official_publisher_chain(self):
        from tests.test_source_governance import SourceGovernanceTests
        from backend.services import source_governance as g
        from backend.services.source_workflow import publisher_proven
        f=SourceGovernanceTests();f.setUp()
        try:
            root=f.school.url;home='https://career.example.edu.cn/';column=home+'guidance/'
            directory=root+'departments/'
            def observe(html,url):return g._snapshot(html,url,role='identity')
            refs=dict(root=observe('<a href="/departments/">机构设置</a>',root),
                directory=observe(f'<a href="{home}">就业指导中心</a>',directory),
                home=observe('<title>示例大学</title><div class="footer"><div class="school">示例大学就业指导中心</div></div>'
                    '<nav><a href="guidance/">就业指导</a></nav>',home))
            bundle=dict(root_url=root,school_name='示例大学',publisher_material=refs,
                publisher_evidence=dict(name='就业指导中心',directory_evidence_id='directory',homepage_evidence_id='home'))
            config=dict(group_name='就业指导中心',list_url=column)
            self.assertTrue(publisher_proven(bundle,config))
            self.assertFalse(publisher_proven(bundle,dict(config,group_name='学生工作处')))
        finally:f.tearDown()
