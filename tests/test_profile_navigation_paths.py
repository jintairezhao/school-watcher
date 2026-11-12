"""A profile route may contain retained navigation pages, never invented parents."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import tempfile
import unittest

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_assertion

SCHOOL = '中国石油大学（北京）克拉玛依校区'
ROOT = 'https://www.cupk.edu.cn/'
HOME = ROOT+'chem/'
MIDDLE = HOME+'organization/'
PROFILE = HOME+'c/2023-06-28/517808.shtml'
SOURCE = HOME+'glzd/'
NAME = '化学与化工实验教学中心'


class ProfileNavigationPathTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.profile = gzip.decompress((Path(__file__).parent/
            'fixtures/structure/cupk-chem-profile-20261024.html.gz').read_bytes()).decode('utf8')

    def case(self, *, bridge_label='机构设置', direct=False, middle_url=MIDDLE):
        inventory = Inventory(Path(self.tmp.name)/('inventory-'+str(len(list(Path(self.tmp.name).iterdir())))+'.sqlite3'))
        key = inventory.ensure_site(SCHOOL, ROOT)
        bodies = {ROOT:'<title>'+SCHOOL+'</title><nav><a href="'+HOME+'">示范中心</a></nav>',
            HOME:'<title>'+SCHOOL+NAME+'</title><nav><a href="'+middle_url+'">'+bridge_label+'</a>'+
                ('<a href="'+PROFILE+'">中心概况</a>' if direct else '')+
                '<a href="'+SOURCE+'">管理制度</a></nav>',
            middle_url:'<nav><a href="'+HOME+'">首页</a><a href="'+PROFILE+'">中心概况</a></nav>',
            PROFILE:self.profile}
        for url,label,kind in ((ROOT,SCHOOL,'root'),(HOME,NAME,'unit'),
                (middle_url,'机构设置','unit'),(PROFILE,'中心概况','unit')):
            inventory.enqueue(key,url,label,kind,1,[],'fixture')
            inspect_page(inventory,inventory.report(key)['site'],inventory.get_page(key,url),
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]),ai_assist=False)
        relations = SourceRelationships(inventory.report(key),inventory.structure(key),directory_navigation=True)
        paths = relations.paths_for(SOURCE)
        return inventory,key,relations,paths,bodies

    def assertions(self,relations,paths,bodies,receipt=True):
        return [_publisher_assertion(path,SimpleNamespace(list_url=SOURCE),None,
            {'receipt_id':'fixture'} if receipt else None,lambda url:bodies.get(url,''),relations) for path in paths]

    def test_two_hop_profile_has_original_evidence_for_every_forward_link(self):
        _,_,relations,paths,bodies = self.case()
        self.assertTrue(any(self.assertions(relations,paths,bodies)))
        chosen = next(p for p in paths if p.get('publisher_declaration'))
        chain = chosen['publisher_declaration']['profile_route']
        self.assertEqual([(h['reference_url'],h['target_url']) for h in chain],[(HOME,MIDDLE),(MIDDLE,PROFILE)])
        self.assertTrue(all(h['content_hash'] and h['observed_at'] and h['locator'] for h in chain))
        from backend.services.publisher_identity import verify_declaration
        proof = verify_declaration(chosen,lambda url:bodies.get(url,''),relations)
        self.assertFalse(proof['administrative_parent_asserted'])
        self.assertIn(MIDDLE,{e['reference_url'] for e in proof['evidence']})

    def test_capture_and_restore_preserve_the_intermediate_material(self):
        from backend.services.source_governance import _identity_observations
        from backend.services.publisher_identity import verify_declaration
        _,_,relations,paths,bodies = self.case()
        self.assertTrue(any(self.assertions(relations,paths,bodies)))
        captured = _identity_observations({'site':relations.site,'pages':list(relations.pages.values())},paths)
        self.assertIn(MIDDLE,{p['url'] for p in captured['identity_pages']})
        restored = SourceRelationships({'site':captured['identity_site'],'pages':captured['identity_pages']},[])
        chosen = next(p for p in paths if p.get('publisher_declaration'))
        self.assertIsNotNone(verify_declaration(chosen,lambda url:bodies.get(url,''),restored))

    def test_missing_original_and_changed_intermediate_link_are_not_bypassed(self):
        _,_,relations,paths,bodies = self.case()
        self.assertTrue(any(self.assertions(relations,paths,bodies)))
        for replacement in ('',bodies[MIDDLE].replace(PROFILE,HOME+'other/')):
            with self.subTest(replacement=replacement):
                changed=dict(bodies);changed[MIDDLE]=replacement
                self.assertFalse(any(self.assertions(relations,paths,changed)))

    def test_forged_route_hash_locator_direction_and_missing_receipt_are_rejected(self):
        _,_,relations,paths,bodies = self.case()
        self.assertTrue(any(self.assertions(relations,paths,bodies)))
        for field,value in (('content_hash','0'*64),('locator','a.nonexistent'),('target_url',HOME),
                ('target_url',[]),('observed_at',None)):
            with self.subTest(field=field):
                forged=deepcopy(paths)
                for path in forged:
                    if path.get('publisher_declaration'):
                        path['publisher_declaration']['profile_route'][1][field]=value
                self.assertFalse(any(self.assertions(relations,forged,bodies)))
        self.assertFalse(any(self.assertions(relations,paths,bodies,receipt=False)))

    def test_news_detours_and_other_website_scopes_are_not_profile_bridges(self):
        for kwargs in (dict(bridge_label='新闻动态'),dict(middle_url=ROOT+'other/organization/')):
            with self.subTest(kwargs=kwargs):
                _,_,relations,paths,bodies=self.case(**kwargs)
                self.assertFalse(any(self.assertions(relations,paths,bodies)))

    def test_direct_profile_route_is_preferred_and_cycles_are_not_replayed(self):
        _,_,relations,paths,bodies=self.case(direct=True)
        self.assertTrue(any(self.assertions(relations,paths,bodies)))
        chosen=next(p for p in paths if p.get('publisher_declaration'))
        chain=chosen['publisher_declaration'].get('profile_route',[])
        self.assertEqual(len(chain),1)
        self.assertEqual(chain[0]['reference_url'],HOME)

    def test_roster_profile_uses_the_same_route_and_original_material_checks(self):
        from backend.services.source_governance import _identity_observations
        college='化工与环境学院';home=ROOT+'gxy/';middle=home+'organization/'
        profile=home+'xyjj/';source=home+'bkjy/jxtz/'
        raw=gzip.decompress((Path(__file__).parent/
            'fixtures/structure/cupk-chemical-college-profile-20261024.html.gz').read_bytes()).decode('utf8')
        bodies={ROOT:'<main><h4><a href="'+home+'">'+college+'</a></h4></main>',
            home:'<title>中国石油大学(北京)克拉玛依校区化学与环境学院</title><nav>'
                '<a href="'+middle+'">机构设置</a><a href="'+source+'">教学通知</a></nav>',
            middle:'<nav><a href="'+profile+'">学院概况</a></nav>',profile:raw}
        inventory=Inventory(Path(self.tmp.name)/'roster.sqlite3');key=inventory.ensure_site(SCHOOL,ROOT)
        with inventory.connect() as conn:
            conn.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?",(key,))
        for url,label,kind in ((ROOT,'院系设置','directory'),(home,college,'unit'),
                (middle,'机构设置','unit'),(profile,'学院概况','unit')):
            inventory.enqueue(key,url,label,kind,1,[],'fixture')
            inspect_page(inventory,inventory.report(key)['site'],inventory.get_page(key,url),
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]),ai_assist=False)
        relations=SourceRelationships(inventory.report(key),inventory.structure(key),directory_navigation=True)
        paths=relations.paths_for(source)
        def proofs():
            return [_publisher_assertion(p,SimpleNamespace(list_url=source),None,{'receipt_id':'fixture'},
                lambda url:bodies.get(url,''),relations) for p in paths]
        self.assertTrue(any(proofs()))
        accepted=next(p for p,proof in zip(paths,proofs()) if proof)
        self.assertEqual(len(accepted['website_identity']['profile_route']),2)
        captured=_identity_observations({'site':relations.site,'pages':list(relations.pages.values())},paths)
        self.assertIn(middle,{p['url'] for p in captured['identity_pages']})
        bodies[middle]=''
        self.assertFalse(any(proofs()))

    def test_published_projection_requires_the_intermediate_page_proof(self):
        from unittest.mock import patch
        from backend.database.db import db
        from backend.database.models import Department
        from backend.services import source_governance as g
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.structure_snapshot import read_structure
        from tests.test_source_governance import SourceGovernanceTests
        fixture=SourceGovernanceTests();fixture.setUp()
        try:
            fixture.school.name,fixture.school.url=SCHOOL,ROOT
            source=fixture.dept;source.name,source.list_url,source.group_name='管理制度',SOURCE,''
            db.session.commit()
            inventory,key,_,_,_=self.case()
            receipt=dict(source_id=source.id,receipt_id='native:fixture',config_hash=g._hash(g.source_config(source)),
                url=SOURCE,content_hash='fixture',region={})
            with patch('backend.services.source_path_receipts.load_source_receipts',return_value=[receipt]):
                RuntimeCatalog(fixture.app.config['SOURCE_CATALOG_PATH']).publish(inventory,key)
                identity=Department.query.filter(Department.identity_json.is_not(None)).one()
                original=deepcopy(identity.identity_json)
                facts=read_structure(fixture.school)['facts']
                self.assertTrue(any(r['type']=='source_ownership' and r['target']=='source:'+str(source.id) for r in facts['relations']))
                self.assertFalse(any(r['type']=='organization_membership' for r in facts['relations']))
                identity.identity_json=dict(original,evidence=[e for e in original['evidence'] if e['reference_url']!=MIDDLE])
                db.session.commit()
                changed=read_structure(fixture.school)['facts']
                self.assertFalse(any(r['type']=='source_ownership' and r['target']=='source:'+str(source.id) for r in changed['relations']))
        finally:
            fixture.tearDown();fixture.doCleanups()

    def test_roster_ownership_projection_requires_each_route_witness(self):
        from tests.test_structure_facts import StructureFactsTests,AT,SHARED,ROOT as EXAMPLE_ROOT
        fixture=StructureFactsTests();fixture.setUp()
        home=EXAMPLE_ROOT+'unit/';middle=home+'organization/';profile=home+'profile/'
        fixture.data['departments']=[fixture.unit(1),fixture.source(2)]
        fixture.data['pages'] += [dict(url=url,state='fetched',content_hash='material-'+label,checked_at=AT)
            for url,label in ((home,'unit'),(middle,'bridge'),(profile,'profile'))]
        route=[dict(reference_url=url,target_url=target,label=label,locator=position,
            content_hash=fixture.proof(url)['content_hash'],observed_at=AT) for url,target,label,position in
            ((home,middle,'机构设置','nav #organization'),(middle,profile,'中心介绍','nav #intro'))]
        evidence=[fixture.proof(hop['reference_url'],hop['locator']) for hop in route]+[
            fixture.proof(profile,'main p'),fixture.proof(profile,'nav #home'),fixture.proof(SHARED,'main li')]
        digest=next(n['config_hash'] for n in fixture.build()['nodes'] if n['entity_type']=='source')
        fixture.data['receipts']=[dict(source_id=2,config_hash=digest,receipt_id='native:7')]
        fixture.data['directory_entries']=[dict(parent_id=1,department_id=2,type='source_ownership',
            writer='official_structure',config_hash=digest,receipt_id='native:7',evidence=evidence,
            assertion=dict(kind='explicit_publisher_identity',predicate='publication_owner',direction='publisher_to_source',
                identity_text='Unit',identity_reference_url=profile,identity_locator='main p',entry_reference_url=SHARED,
                entry_locator='main li',entry_kind='column_link',entry_url=SHARED,
                profile_identity_witness=dict(kind='explicit_unit_self_description',unit_name='Unit',website_url=home,
                    reference_url=profile,locator='main p',profile_entry_locator='nav #organization',
                    profile_home_locator='nav #home',profile_route=route)))]
        def owners():return [r for r in fixture.build()['relations'] if r['type']=='source_ownership']
        self.assertEqual(len(owners()),1)
        fixture.data['directory_entries'][0]['evidence']=[e for e in evidence if e['reference_url']!=middle]
        self.assertFalse(owners())
        fixture.data['directory_entries'][0]['evidence']=evidence
        page=next(p for p in fixture.data['pages'] if p['url']==middle)
        page['content_hash']='changed'
        self.assertFalse(owners())
        page.update(content_hash='material-bridge',checked_at='2026-10-31T12:00:00+00:00')
        self.assertEqual(len(owners()),1)


if __name__=='__main__':
    unittest.main()
