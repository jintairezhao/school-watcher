"""Different retained homepage URLs can complete identity proof without aliasing pages."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip
import tempfile
import unittest

from bs4 import BeautifulSoup
from backend.services.source_inventory import Inventory,resolve_page_link
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_assertion
from tests.test_profile_navigation_paths import SCHOOL,ROOT,HOME,MIDDLE,PROFILE,SOURCE,NAME

INDEX=HOME+'Home/Index'


class ProfileHomeReferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)

    def case(self,roster=False,*,homepage_identity=None,homepage_profile=True,entry_home=True,homepage_url=INDEX):
        raw=gzip.decompress((Path(__file__).parent/'fixtures/structure/cupk-chem-profile-20261024.html.gz').read_bytes()).decode('utf8')
        soup=BeautifulSoup(raw,'lxml')
        for anchor in soup.select('a[href]'):
            if anchor.get_text(' ',strip=True)=='首页' and resolve_page_link(PROFILE,anchor['href'])==HOME:
                anchor['href']=homepage_url
        if roster:
            soup=BeautifulSoup('<nav><a href="'+homepage_url+'">首页</a></nav><div class="sub-right"><p>'+
                NAME+'成立于2000年，承担学校教学实验、科学研究、学生培养与公共服务等多项工作。</p></div>','lxml')
        school_entrance=('<main><h4><a href="'+HOME+'">'+NAME+'</a></h4></main>'if roster else
            '<nav><a href="'+HOME+'">示范中心</a></nav>')
        bodies={ROOT:'<title>'+SCHOOL+'</title>'+school_entrance,
            HOME:'<title>'+SCHOOL+(''if roster else NAME)+'</title><nav>'+('<a href="'+homepage_url+'">首页</a>'if entry_home else'')+
                '<a href="'+MIDDLE+'">机构设置</a><a href="'+SOURCE+'">管理制度</a></nav>',
            MIDDLE:'<nav><a href="'+PROFILE+'">中心概况</a></nav>',PROFILE:str(soup),
            homepage_url:'<title>'+SCHOOL+(homepage_identity or NAME)+'</title><nav>'+
                ('<a href="'+MIDDLE+'">机构设置</a>'if homepage_profile else'')+'</nav>'}
        inv=Inventory(self.root/('inventory-'+str(len(list(self.root.iterdir())))+'.sqlite3'))
        key=inv.ensure_site(SCHOOL,ROOT)
        if roster:
            with inv.connect()as connection:
                connection.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?AND url=?",(key,ROOT))
        for url,label,kind in [(ROOT,'院系设置'if roster else SCHOOL,'directory'if roster else'root'),
                (HOME,NAME,'unit'),(MIDDLE,'机构设置','unit'),(PROFILE,'中心概况','unit'),(homepage_url,NAME,'unit')]:
            inv.enqueue(key,url,label,kind,1,[],'local_behavior_only')
            inspect_page(inv,inv.report(key)['site'],inv.get_page(key,url),
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]),ai_assist=False)
        rel=SourceRelationships(inv.report(key),inv.structure(key),directory_navigation=True)
        return inv,key,rel,rel.paths_for(SOURCE),bodies

    def proofs(self,rel,paths,bodies):
        return [_publisher_assertion(p,SimpleNamespace(list_url=SOURCE),None,{'receipt_id':'local-only'},
            lambda u:bodies.get(u,''),rel) for p in paths]

    def test_roster_identity_accepts_complete_home_reference_without_page_alias(self):
        _,_,rel,paths,bodies=self.case(roster=True)
        self.assertTrue(any(self.proofs(rel,paths,bodies)))
        chosen=next(p for p in paths if p.get('website_identity',{}).get('profile_home_witness'))
        self.assertEqual(chosen['unit_name'],NAME)
        self.assertFalse(rel._addresses(HOME)&rel._addresses(INDEX))
        self.assertIn(INDEX,{r['url']for r in chosen['references']})

    def test_independent_declaration_uses_the_same_home_reference_proof(self):
        _,_,rel,paths,bodies=self.case()
        self.assertTrue(any(self.proofs(rel,paths,bodies)))
        self.assertTrue(any(p.get('publisher_declaration',{}).get('profile_home_witness')for p in paths))
        self.assertFalse(rel._addresses(HOME)&rel._addresses(INDEX))

    def test_same_brand_without_entry_home_link_or_return_route_is_insufficient(self):
        for kwargs in [dict(entry_home=False),dict(homepage_profile=False),dict(homepage_identity='另一个中心'),
                dict(homepage_url=ROOT+'other/Home/Index')]:
            with self.subTest(kwargs=kwargs):
                _,_,rel,paths,bodies=self.case(roster=True,**kwargs)
                self.assertFalse(any(self.proofs(rel,paths,bodies)))

    def test_missing_or_mutated_home_original_invalidates_the_proof(self):
        _,_,rel,paths,bodies=self.case(roster=True)
        self.assertTrue(any(self.proofs(rel,paths,bodies)))
        for raw in ['',bodies[INDEX].replace(MIDDLE,HOME+'other/')]:
            changed=dict(bodies);changed[INDEX]=raw
            self.assertFalse(any(self.proofs(rel,paths,changed)))

    def test_capture_keeps_the_distinct_homepage_and_its_return_route(self):
        from backend.services.source_governance import _identity_observations
        _,_,rel,paths,bodies=self.case()
        self.assertTrue(any(self.proofs(rel,paths,bodies)))
        captured=_identity_observations({'site':rel.site,'pages':list(rel.pages.values())},paths)
        self.assertIn(INDEX,{p['url']for p in captured['identity_pages']})
        restored=SourceRelationships({'site':captured['identity_site'],'pages':captured['identity_pages']},[])
        from backend.services.publisher_identity import verify_declaration
        self.assertTrue(any(verify_declaration(p,lambda u:bodies.get(u,''),restored)
            for p in paths if p.get('publisher_declaration')))

    def test_forged_homepage_identity_route_and_locator_are_rejected(self):
        _,_,rel,paths,bodies=self.case(roster=True)
        self.assertTrue(any(self.proofs(rel,paths,bodies)))
        for field,value in [('homepage_url',ROOT+'other/'),('entry_locator','a.missing'),('profile_route',[])]:
            changed=deepcopy(paths)
            for path in changed:
                identity=path.get('website_identity')or{}
                if identity.get('profile_home_witness'):
                    identity['profile_home_witness'][field]=value
            self.assertFalse(any(self.proofs(rel,changed,bodies)))

    def test_projection_checks_current_homepage_hash_without_parser_notes(self):
        from backend.services.profile_home_reference import verify_home_return
        _,_,rel,paths,_=self.case(roster=True)
        identity=next(p['website_identity']for p in paths if p.get('website_identity',{}).get('profile_home_witness'))
        pages={u:{k:p[k]for k in ('url','state','content_hash','checked_at')}for u,p in rel.pages.items()}
        view=SimpleNamespace(pages=pages,site=rel.site)
        def check(witness):return verify_home_return(HOME,PROFILE,identity['profile_home_locator'],witness,NAME,view)
        self.assertTrue(check(identity['profile_home_witness']))
        for value in ({},{'homepage_url':INDEX},dict(identity['profile_home_witness'],identity=7),
                dict(identity['profile_home_witness'],identity={'identity':7}),
                dict(identity['profile_home_witness'],profile_route=[{}])):
            self.assertIsNone(check(value))
        pages[INDEX]['content_hash']='changed-current-material'
        self.assertIsNone(check(identity['profile_home_witness']))

    def test_published_projection_requires_homepage_evidence_for_both_identity_forms(self):
        from unittest.mock import patch
        from backend.database.db import db
        from backend.database.models import Department,DepartmentDirectoryEntry
        from backend.services import source_governance as g
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.structure_snapshot import read_structure
        from tests.test_source_governance import SourceGovernanceTests
        for roster in (False,True):
            with self.subTest(roster=roster):
                fixture=SourceGovernanceTests();fixture.setUp()
                try:
                    fixture.school.name,fixture.school.url=SCHOOL,ROOT
                    source=fixture.dept;source.name,source.list_url,source.group_name='管理制度',SOURCE,''
                    db.session.commit()
                    inv,key,_,_,_=self.case(roster=roster)
                    receipt=dict(source_id=source.id,receipt_id='local:only',config_hash=g._hash(g.source_config(source)),url=SOURCE,content_hash='fixture',region={})
                    with patch('backend.services.source_path_receipts.load_source_receipts',return_value=[receipt]):
                        RuntimeCatalog(fixture.app.config['SOURCE_CATALOG_PATH']).publish(inv,key)
                        def owners():return [r for r in read_structure(fixture.school)['facts']['relations']if r['type']=='source_ownership'and r['target']=='source:'+str(source.id)]
                        self.assertTrue(owners())
                        self.assertFalse(any(r['type']=='organization_membership'for r in read_structure(fixture.school)['facts']['relations']))
                        if not roster:
                            unit=Department.query.filter(Department.identity_json.is_not(None)).one()
                            original=deepcopy(unit.identity_json)
                            unit.identity_json=dict(original,evidence=[e for e in original['evidence']if e['reference_url']!=INDEX])
                            db.session.commit()
                            self.assertFalse(owners())
                        else:
                            row=DepartmentDirectoryEntry.query.filter_by(department_id=source.id).one()
                            facts=deepcopy(row.facts_json)
                            for fact in facts:
                                if fact.get('type')=='source_ownership':
                                    fact['evidence']=[e for e in fact['evidence']if e['reference_url']!=INDEX]
                            row.facts_json=facts;db.session.commit()
                            self.assertFalse(owners())
                finally:fixture.tearDown();fixture.doCleanups()


if __name__=='__main__':unittest.main()
