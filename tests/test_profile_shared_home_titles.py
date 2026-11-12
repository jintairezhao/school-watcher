"""A closed profile route may explain two distinct, identically branded homes."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_material_assertion
from backend.services.profile_home_reference import home_return, verify_home_return
from backend.services.runtime_catalog import RuntimeCatalog

ROOT = 'https://www.example.edu.cn/'
HOME = 'http://materials.example.edu.cn/'
INDEX = HOME+'main.htm'
PROFILE = HOME+'intro/'
SOURCE = HOME+'notices/'
UNIT = '材料科学与工程学院'


class ProfileSharedHomeTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def case(self, mode='valid'):
        bodies = {ROOT:'<title>示例大学</title><main><h4><a href="'+HOME+'">'+UNIT+'</a></h4></main>',
            HOME:'<title>材料学院</title><nav><a href="'+INDEX+'">首页</a><a href="'+PROFILE+'">学院简介</a><a href="'+SOURCE+'">通知公告</a></nav>',
            INDEX:'<title>材料学院</title><nav><a href="'+PROFILE+'">学院简介</a></nav>',
            PROFILE:'<title>学院简介</title><nav><a href="'+INDEX+'">首页</a></nav><div class="wp_articlecontent"><p>示例大学'+UNIT+'成立于2015年，依托相关学科开展人才培养、科学研究和公共服务工作。</p></div>'}
        if mode == 'other_index_brand': bodies[INDEX] = bodies[INDEX].replace('材料学院','其他学院')
        if mode == 'other_entry_brand': bodies[HOME] = bodies[HOME].replace('材料学院','其他学院')
        if mode == 'other_subject': bodies[PROFILE] = bodies[PROFILE].replace(UNIT,'其他学院')
        if mode == 'missing_return_profile_route': bodies[INDEX] = bodies[INDEX].replace('学院简介','学院新闻')
        inv = Inventory(self.folder / (mode+'.sqlite3'))
        key = inv.ensure_site('示例大学',ROOT)
        with inv.connect() as conn:
            conn.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=? AND url=?",(key,ROOT))
        for url,label,kind in [(ROOT,'院系设置','directory'),(HOME,UNIT,'unit'),(INDEX,'首页','unit'),(PROFILE,'学院简介','unit')]:
            inv.enqueue(key,url,label,kind,1,[],'local_program_behavior')
            inspect_page(inv,inv.report(key)['site'],inv.get_page(key,url),ai_assist=False,
                         fetcher=lambda target:dict(url=target,status=200,html=bodies[target]))
        rel = SourceRelationships(inv.report(key),inv.structure(key),directory_navigation=True)
        paths = rel.paths_for(SOURCE)
        proofs = [_publisher_material_assertion(path,SimpleNamespace(list_url=SOURCE),lambda u:bodies.get(u,''),rel)for path in paths]
        returned = home_return(rel,rel.pages[HOME],rel.pages[PROFILE],UNIT)
        return inv,key,rel,bodies,proofs,returned

    def test_closed_profile_and_matching_titles_keep_page_identities_separate(self):
        _,_,rel,_,proofs,returned = self.case()
        self.assertTrue(any(proofs))
        self.assertIsNotNone(returned)
        self.assertFalse(rel._addresses(HOME) & rel._addresses(INDEX))
        witness = returned['profile_home_witness']
        self.assertEqual(witness['identity']['identity'],'材料学院')
        self.assertEqual(witness['profile_name_witness']['entity_name'],'示例大学'+UNIT)
        self.assertEqual(witness['shared_title_witness']['kind'],'same_literal_homepage_title')
        self.assertFalse(next(p for p in proofs if p)['profile_identity_witness']['identity_alias_asserted'])

    def test_brand_disagreement_wrong_subject_or_missing_leg_stays_unconfirmed(self):
        for mode in ('other_index_brand','other_entry_brand','other_subject','missing_return_profile_route'):
            with self.subTest(mode=mode):
                self.assertFalse(any(self.case(mode)[4]))

    def test_compact_projection_checks_both_current_titles_and_originals(self):
        inv,key,rel,bodies,_,returned = self.case()
        self.assertIsNotNone(returned)
        witness = returned['profile_home_witness'];back = returned['profile_home_locator']
        self.assertTrue(verify_home_return(HOME,PROFILE,back,witness,UNIT,rel,material=lambda u:bodies.get(u,'')))
        catalog = RuntimeCatalog(self.folder/'compact.sqlite3',versioned=False)
        catalog.publish(inv,key)
        pages = {p['url']:p for p in catalog.monitor_snapshot(key)['pages']}
        compact = SimpleNamespace(pages=pages,site=rel.site)
        self.assertTrue(verify_home_return(HOME,PROFILE,back,witness,UNIT,compact))
        forged = deepcopy(witness);forged['shared_title_witness']['content_hash']='a'*64
        self.assertFalse(verify_home_return(HOME,PROFILE,back,forged,UNIT,compact))
        forged = deepcopy(witness)
        forged['shared_title_witness']['identity']['identity']='其他学院'
        forged['identity']['identity']='其他学院'
        self.assertFalse(verify_home_return(HOME,PROFILE,back,forged,UNIT,compact))
        self.assertFalse(verify_home_return(HOME,PROFILE,back,witness,UNIT,rel,
            material=lambda u:'' if u==HOME else bodies.get(u,'')))


if __name__ == '__main__':
    unittest.main()
