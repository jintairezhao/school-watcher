"""Navigation evidence keeps its source document, direction and material version."""
import unittest
from backend.services.discovery_monitor import _page_navigation_relations, _navigation_relations


class StructureNavigationProjectionTests(unittest.TestCase):
    def test_primary_header_link_survives_without_a_category_keyword(self):
        from bs4 import BeautifulSoup
        from backend.scraper.discovery.structure import extract_structure, classify
        from backend.services.source_directory_reconciliation import navigation_evidence
        root='https://example.edu/'
        label='交叉领域'
        self.assertIsNone(classify(label))
        for opening, closing in (('<header>', '</header>'),
                                 ('<div class="c-header_title">', '</div>'),
                                 ('<div id="site-header">', '</div>')):
            with self.subTest(container=opening):
                html=opening+'<a href="/explore/"><span>'+label+'</span></a>'+closing
                primary=navigation_evidence(BeautifulSoup(html,'lxml'),root)
                self.assertEqual([p['name'] for p in primary],[label])
                parsed=extract_structure(html,root,root,'root','Example University')
                links=[link for link in parsed['links'] if link['label']==label]
                self.assertEqual(len(links),1)
                self.assertEqual((links[0]['url'],links[0]['kind'],links[0]['decision']),
                                 (root+'explore/','navigation','follow'))
                entry=next(n for n in parsed['nodes'] if n['name']==label)
                self.assertEqual((entry['kind'],entry['relation']),('group','navigation_entry'))

    def test_unclassified_body_link_does_not_become_primary_navigation(self):
        from backend.scraper.discovery.structure import extract_structure
        root='https://example.edu/'
        parsed=extract_structure('<main><a href="/explore/">交叉领域</a></main>',
                                 root,root,'root','Example University')
        self.assertFalse(any(link['url']==root+'explore/' for link in parsed['links']))

    def test_current_hyperlink_retains_complete_provenance(self):
        pages={'https://example.edu/':{'content_hash':'material-1'}}
        edge=dict(parent_url='https://example.edu/',target_url='https://example.edu/units',
                  label='院系设置',locator='nav a.units',content_hash='material-1',observed_at='2026-10-21T00:00:00Z')
        links=_page_navigation_relations([edge],pages)
        self.assertEqual(len(links),1)
        self.assertEqual(links[0]['source_url'],edge['parent_url'])
        self.assertEqual(links[0]['type'],'navigation')
        self.assertTrue(links[0]['access'])
        for key in ('content_hash','observed_at','locator'):
            self.assertEqual(links[0][key],edge[key])
        for field in ('content_hash','observed_at','locator'):
            self.assertEqual(_page_navigation_relations([{**edge,field:''}],pages),[])
        self.assertEqual(_page_navigation_relations([{**edge,'content_hash':'stale'}],pages),[])

    def test_nested_menu_parent_url_does_not_change_click_origin(self):
        root='https://example.edu/'
        pages={root:{'kind':'root','content_hash':'m1'},root+'overview':{'kind':'navigation'}}
        records=[dict(node_key='overview',url=root+'overview',reference_url=root,relation='menu_group'),
                 dict(node_key='units',parent_key='overview',url=root+'units',reference_url=root,
                      name='组织机构',relation='navigation_entry',locator='nav li ul a',
                      content_hash='m1',observed_at='2026-10-21T00:00:00Z')]
        links=_navigation_relations(records,pages)
        self.assertEqual([(r['source_url'],r['target_url'],r['type']) for r in links],
                         [(root,root+'units','navigation')])


if __name__=='__main__':unittest.main()
