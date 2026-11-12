"""A reciprocal current profile can bind full roster identity to shorter branding."""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import gzip
import tempfile
import unittest

from bs4 import BeautifulSoup
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.profile_home_reference import home_return,verify_home_return
from backend.services.school_structure import _profile_identity_assertion
from backend.services.runtime_catalog import RuntimeCatalog

ROOT='https://www.swpu.edu.cn/'
ENTRY='http://www.swpu.edu.cn/gs/'
INDEX=ENTRY+'index.htm'
PROFILE=ENTRY+'jgsz/bmzz.htm'
SOURCE=ENTRY+'zsgz/tzgg.htm'
UNIT='研究生院(一流学科建设办公室）'


class ProfileHomeFullNameTests(unittest.TestCase):
    def setUp(self):
        folder=Path(__file__).parent/'fixtures/structure'
        self.home=gzip.decompress((folder/'swpu-gs-home-37b0db2d-20261103.html.gz').read_bytes()).decode('utf-8')
        self.profile=gzip.decompress((folder/'swpu-gs-responsibilities-98548e80-20261103.html.gz').read_bytes()).decode('utf-8')
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)

    def case(self,*,mode=None):
        index=self.home;profile=self.profile
        if mode=='other_brand':
            soup=BeautifulSoup(index,'lxml');soup.title.string='西南石油大学其他学院';index=str(soup)
        elif mode=='other_subject':profile=profile.replace('研究生院（一流学科建设办公室）是负责','其他学院是负责')
        elif mode=='retargeted_return':profile=profile.replace('href="../index.htm"','href="../../other/index.htm"')
        inventory=Inventory(Path(self.temp.name)/(str(mode or'valid')+'.sqlite3'))
        key=inventory.ensure_site('西南石油大学',ROOT)
        with inventory.connect()as c:
            c.execute("UPDATE pages SET kind='directory',label='机构设置' WHERE site_key=?AND url=?",(key,ROOT))
        bodies={ROOT:'<main><h4><a href="'+ENTRY+'">'+UNIT+'</a></h4></main>',ENTRY:self.home,INDEX:index,PROFILE:profile}
        for url,kind in ((ROOT,'directory'),(ENTRY,'unit'),(INDEX,'unit'),(PROFILE,'unit')):
            inventory.enqueue(key,url,url,kind,1,[],'local-profile-name-contract')
            inspect_page(inventory,inventory.report(key)['site'],inventory.get_page(key,url),ai_assist=False,
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]))
        rel=SourceRelationships(inventory.report(key),inventory.structure(key),directory_navigation=True)
        return inventory,rel,bodies

    def test_full_current_profile_and_closed_routes_keep_literal_brand_and_roster_name(self):
        _,rel,bodies=self.case()
        returned=home_return(rel,rel.pages[ENTRY],rel.pages[PROFILE],UNIT)
        self.assertIsNotNone(returned)
        self.assertEqual(returned['profile_home_witness']['identity']['identity'],'西南石油大学研究生院')
        self.assertEqual(returned['profile_home_witness']['profile_name_witness']['entity_name'],'研究生院（一流学科建设办公室）')
        proofs=[_profile_identity_assertion(path.get('website_identity')or{},path,lambda url:bodies.get(url,''),rel)
            for path in rel.paths_for(SOURCE)if(path.get('website_identity')or{}).get('source')=='unit_profile_self_description']
        proof=next((p for p in proofs if p),None)
        self.assertIsNotNone(proof)
        self.assertEqual(proof['unit_name'],UNIT)
        self.assertFalse(proof['identity_alias_asserted'])

    def test_wrong_brand_wrong_subject_and_retargeted_return_are_rejected(self):
        for mode in ('other_brand','other_subject','retargeted_return'):
            with self.subTest(mode=mode):
                _,rel,bodies=self.case(mode=mode)
                self.assertIsNone(home_return(rel,rel.pages[ENTRY],rel.pages[PROFILE],UNIT))

    def test_compact_projection_rechecks_profile_material_and_rejects_expired_name_witness(self):
        inventory,rel,bodies=self.case()
        returned=home_return(rel,rel.pages[ENTRY],rel.pages[PROFILE],UNIT)
        self.assertIsNotNone(returned)
        witness=returned['profile_home_witness'];back=returned['profile_home_locator']
        original=verify_home_return(ENTRY,PROFILE,back,witness,UNIT,rel,material=lambda url:bodies.get(url,''))
        self.assertTrue(original)
        catalog=RuntimeCatalog(Path(self.temp.name)/'catalog.sqlite3',versioned=False)
        catalog.publish(inventory,rel.site['site_key'])
        pages={page['url']:page for page in catalog.monitor_snapshot(rel.site['site_key'])['pages']}
        compact=SimpleNamespace(pages=pages,site=rel.site)
        self.assertTrue(verify_home_return(ENTRY,PROFILE,back,witness,UNIT,compact))
        invalid=deepcopy(witness);invalid['profile_name_witness']['content_hash']='a'*64
        self.assertFalse(verify_home_return(ENTRY,PROFILE,back,invalid,UNIT,compact))


if __name__=='__main__':unittest.main()
