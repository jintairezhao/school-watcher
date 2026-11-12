"""Explicit department responsibilities are evidence, never automatic ownership."""
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import tempfile
import unittest

from bs4 import BeautifulSoup
from backend.scraper.discovery.structure import locator_element
from backend.services.source_ownership import profile_identity_candidates, normalized_name
from backend.services.publisher_identity import observations

ROOT='https://www.swpu.edu.cn/'
HOME=ROOT+'gs/index.htm'
PROFILE=ROOT+'gs/jgsz/bmzz.htm'
SOURCE=ROOT+'gs/zsgz/tzgg.htm'
UNIT='研究生院(一流学科建设办公室）'
FIXTURES=Path(__file__).parent/'fixtures/structure'


class DepartmentResponsibilityIdentityTests(unittest.TestCase):
    def setUp(self):
        self.gs=gzip.decompress((FIXTURES/'swpu-gs-responsibilities-98548e80-20261103.html.gz').read_bytes()).decode('utf-8')
        self.dean=gzip.decompress((FIXTURES/'swpu-dean-responsibilities-ca616190-20261103.html.gz').read_bytes()).decode('utf-8')
        self.assertEqual(hashlib.sha256(self.gs.encode()).hexdigest(), '98548e80edbcf597d90d6eb68f2fdfd2198115aa0b9be06a9a6f8cc248bd9f67')
        self.assertEqual(hashlib.sha256(self.dean.encode()).hexdigest(), 'ca616190f6bdbd3138aa9a82baf7c4bba0edad87b60bc5f4485ea3c111d60f67')

    def test_retained_responsibility_openings_keep_full_subject_and_original_locator(self):
        for html,name in ((self.gs,UNIT),(self.dean,'教务处')):
            with self.subTest(name=name):
                soup=BeautifulSoup(html,'lxml')
                candidate,=profile_identity_candidates(soup)
                self.assertEqual(normalized_name(candidate['entity_name']),normalized_name(name))
                self.assertEqual(candidate['source'],'unit_profile_self_description')
                self.assertEqual(locator_element(soup,candidate['locator']).get_text(' ',strip=True),candidate['identity'])
                self.assertNotIn('organization_membership',candidate.values())
                self.assertNotIn('parent_id',candidate)

    def test_headings_and_later_mentions_do_not_replace_an_opening_statement(self):
        for mode in ('heading_only','unrelated_opening','conflicting_heading','news_title'):
            with self.subTest(mode=mode):
                soup=BeautifulSoup(self.gs,'lxml');body=soup.select_one('.WordSection1')
                statement=body.select('p')[1]
                if mode=='heading_only':statement.decompose()
                elif mode=='unrelated_opening':
                    lead=soup.new_tag('p');lead.string='本页记录部门工作交流的一般情况。';statement.insert_before(lead)
                elif mode=='conflicting_heading':body.select_one('p').string='其他学院工作职责'
                else:soup.title.string='研究生院召开工作交流会议'
                self.assertEqual(profile_identity_candidates(soup),[])

    def test_quotes_hidden_text_and_noncurrent_claims_are_not_identity(self):
        for mode in ('report','quote','blockquote','hidden','past','negative'):
            with self.subTest(mode=mode):
                soup=BeautifulSoup(self.gs,'lxml');node=soup.select_one('.WordSection1').select('p')[1]
                text=node.get_text(' ',strip=True)
                if mode=='report':node.string='据报道，'+text
                elif mode=='quote':node.string='“'+text+'”'
                elif mode=='blockquote':node.wrap(soup.new_tag('blockquote'))
                elif mode=='hidden':node['hidden']=''
                elif mode=='past':node.string=text.replace('是负责','曾是负责')
                else:node.string=text.replace('是负责','不是负责')
                self.assertEqual(profile_identity_candidates(soup),[])

    def test_department_responsibility_navigation_is_a_recorded_profile_route(self):
        html='<title>研究生院</title><nav><a href="jgsz/bmzz.htm">部门职责</a></nav>'
        data=observations(BeautifulSoup(html,'lxml'),'西南石油大学',ROOT+'gs/')
        route,=data['routes']
        self.assertEqual((route['label'],route['target_url']),('部门职责',PROFILE))

    def case(self,*,label='部门职责',profile=None):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import inspect_page
        from backend.services.source_relationships import SourceRelationships
        from backend.services.school_structure import _publisher_material_assertion
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        inv=Inventory(Path(tmp.name)/'inventory.sqlite3');self.inventory=inv
        key=inv.ensure_site('西南石油大学',ROOT)
        with inv.connect() as c:c.execute("UPDATE pages SET kind='directory',label='机构设置' WHERE site_key=?",(key,))
        bodies={ROOT:'<main><h4><a href="/gs/index.htm">'+UNIT+'</a></h4></main>',
            HOME:'<title>西南石油大学研究生院</title><nav><a href="/gs/jgsz/bmzz.htm">'+label+'</a>'
                 '<a href="/gs/zsgz/tzgg.htm">通知公告</a></nav>',PROFILE:self.gs if profile is None else profile}
        for url,name,kind in ((ROOT,'机构设置','directory'),(HOME,UNIT,'unit'),(PROFILE,'部门职责','unit')):
            inv.enqueue(key,url,name,kind,1,[],'local-contract')
            inspect_page(inv,inv.report(key)['site'],inv.get_page(key,url),
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]),ai_assist=False)
        relations=SourceRelationships(inv.report(key),inv.structure(key),directory_navigation=True)
        paths=relations.paths_for(SOURCE)
        proofs=[_publisher_material_assertion(path,SimpleNamespace(list_url=SOURCE),
                lambda url:bodies.get(url,''),relations) for path in paths]
        return relations,paths,proofs,bodies

    def test_full_roster_name_uses_responsibility_evidence_without_site_title_alias(self):
        relations,paths,proofs,_=self.case()
        proof=next((p for p in proofs if p),None)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['profile_identity_witness']['unit_name'],UNIT)
        self.assertFalse(proof['profile_identity_witness']['identity_alias_asserted'])
        self.assertTrue(any(p.get('website_identity',{}).get('reference_url')==PROFILE for p in paths))

    def test_structure_bridge_is_profile_endpoint_only_with_explicit_self_description(self):
        _,_,proofs,_=self.case(label='机构设置')
        self.assertTrue(any(proofs))
        invalid=self.gs.replace('是负责学校研究生教育','曾是负责学校研究生教育')
        _,_,proofs,_=self.case(label='机构设置',profile=invalid)
        self.assertFalse(any(proofs))

    def test_compact_projection_keeps_only_current_profile_endpoint_evidence(self):
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.publisher_identity import checked_profile_route,saved_observation
        relations,paths,proofs,_=self.case(label='机构设置')
        proof=next(proof for proof in proofs if proof)
        witness=proof['profile_identity_witness']
        catalog=RuntimeCatalog(self.inventory.path.with_name('projection.sqlite3'),versioned=False)
        catalog.publish(self.inventory,relations.site['site_key'])
        pages={page['url']:page for page in catalog.monitor_snapshot(relations.site['site_key'])['pages']}
        self.assertTrue(checked_profile_route(witness['website_url'],witness['reference_url'],
            witness['profile_entry_locator'],witness['profile_route'],pages,ROOT))
        observed=saved_observation(pages[PROFILE],relations.site['site_key'])
        self.assertTrue(observed['profile_names'])
        self.assertNotIn('routes',observed)
        self.assertNotIn('nodes',observed)
        # A stale parser annotation cannot establish a terminal profile route.
        pages[PROFILE]['content_hash']='a'*64
        self.assertFalse(checked_profile_route(witness['website_url'],witness['reference_url'],
            witness['profile_entry_locator'],witness['profile_route'],pages,ROOT))


if __name__=='__main__':unittest.main()
