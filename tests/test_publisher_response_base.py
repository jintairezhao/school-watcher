"""Relative publisher entrances use the witnessed response address."""
import hashlib
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from backend.scraper.discovery.structure import locator
from backend.services.school_structure import _publisher_material_assertion
from backend.services.source_relationships import SourceRelationships
from backend.services.source_inventory import site_key


class PublisherResponseBaseTests(unittest.TestCase):
    def test_explicit_response_page_takes_precedence_over_an_older_redirect(self):
        from backend.services import source_governance as g
        root='https://example.edu.cn/'
        requested,final='http://example.edu.cn/office/',root+'office/'
        old=dict(url=requested,hash='a'*64,outcome='usable')
        redirect=dict(url=requested,final_url=final,state='fetched',health='recent_publication',
            content_hash='a'*64,checked_at='2026-10-15T10:21:10+00:00')
        current=dict(url=final,final_url=final,state='fetched',health='recent_publication',
            content_hash='b'*64,checked_at='2026-10-23T11:40:39+00:00')
        report=dict(site=dict(root_url=root),pages=[redirect,current])
        paths=[dict(references=[dict(url=requested)])]
        observed=g._identity_observations(report,paths)
        self.assertEqual({p['url'] for p in observed['identity_pages']},{requested,final})
        self.assertNotIn(final,g._captured_identity_responses([old],observed['identity_pages'],root))
        newer=dict(url=final,hash='b'*64,outcome='usable')
        self.assertEqual(g._captured_identity_responses([old,newer],observed['identity_pages'],root)[final],newer)
        observed['identity_pages'][-1]['state']='failed'
        self.assertNotIn(final,g._captured_identity_responses([old],observed['identity_pages'],root))

    def test_retained_response_home_can_complete_the_original_directory_proof(self):
        from backend.services import source_governance as g
        from backend.services.source_workflow import publisher_proven
        root = 'https://example.edu.cn/'
        requested, final = 'http://example.edu.cn/office/', root+'office/'
        raw = {'root':'<a href="/units/">机构设置</a>',
               'directory':f'<a href="{final}">教务部</a>',
               'home':'<title>示例大学教务部</title><a href="forms/">教师用表</a>'}
        refs = {name:dict(hash=hashlib.sha256(text.encode()).hexdigest(),outcome='usable',url=url)
                for (name,text),url in zip(raw.items(),(root,root+'units/',requested))}
        observed = dict(url=requested,final_url=final,state='fetched',health='recent_publication',
                        content_hash=refs['home']['hash'],checked_at='2026-10-15T10:21:10+00:00')
        for changes, expected in (({},True),(dict(state='failed'),False),(dict(health='dynamic_content'),False),
                                  (dict(content_hash='b'*64),False),(dict(final_url='https://other.edu.cn/office/'),False)):
            with self.subTest(changes=changes):
                captured=g._captured_identity_responses(list(refs.values()),[dict(observed,**changes)],root)
                bundle=dict(root_url=root,school_name='示例大学',publisher_evidence=dict(name='教务部',directory_evidence_id='directory',
                    homepage_evidence_id='home'),publisher_material=dict(root=refs['root'],directory=refs['directory'],
                    **({'home':captured[final]} if final in captured else {})))
                texts={ref['hash']:raw[name] for name,ref in refs.items()}
                with patch.object(g,'read_snapshot',side_effect=lambda ref:texts[ref['hash']]):
                    self.assertEqual(publisher_proven(bundle,dict(group_name='教务部',list_url=final+'forms/')),expected)
                if expected:
                    self.assertEqual(captured[final]['requested_url'],requested)
                    self.assertEqual(captured[final]['captured_at'],observed['checked_at'])

    def assertion(self, **changes):
        requested = 'http://example.edu.cn/office/'
        final = 'https://example.edu.cn/office/'
        html = '<title>示例大学教务部</title><a href="forms/">教师用表</a>'
        soup = BeautifulSoup(html, 'lxml')
        digest = hashlib.sha256(html.encode()).hexdigest()
        page = dict(url=requested, final_url=final, title='示例大学教务部', state='fetched',
                    health='recent_publication', content_hash=digest, notes_json='')
        page.update(changes)
        relations = SourceRelationships(dict(site=dict(name='示例大学',root_url='https://example.edu.cn/',
            site_key=site_key('https://example.edu.cn/')),pages=[page]), [])
        path = dict(basis='official_website_entry', unit_name='教务部',
            nodes=[dict(kind='unit',name='教务部',url=final)],
            website_identity=dict(locator=locator(soup.title),identity='示例大学教务部'),
            references=[dict(url=requested,content_hash=digest,locators=[locator(soup.title),locator(soup.a)])])
        return _publisher_material_assertion(path, SimpleNamespace(list_url=final+'forms/'),
                                             lambda url: html, relations)

    def test_observed_response_resolves_relative_publisher_entrance(self):
        self.assertIsNotNone(self.assertion())

    def test_missing_failed_or_unrelated_observation_cannot_supply_response_base(self):
        for changes in (dict(final_url=None), dict(state='failed'), dict(health='dynamic_content'),
                        dict(content_hash='a'*64), dict(final_url='https://other.edu.cn/office/')):
            with self.subTest(changes=changes):
                self.assertIsNone(self.assertion(**changes))
