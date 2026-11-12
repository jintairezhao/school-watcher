"""Utility shortcuts are access evidence, including on genuine directory pages."""
from pathlib import Path
from types import SimpleNamespace
import gzip
import hashlib
import unittest
from unittest.mock import patch
from bs4 import BeautifulSoup

from backend.scraper.discovery.structure import extract_structure,locator_element
from backend.services.source_relationships import ROSTER_RELATIONS
from backend.services.school_structure import _publisher_material_assertion

ROOT='https://www.cupk.edu.cn/'
DIRECTORY=ROOT+'c/2017-04-21/485097.shtml'
HOME=ROOT+'library/'
DRAWER='''<div class="title_link"><div class="title_link_content">
<div class="title_link_content_part"><a class="title_link_content_part_a" href="/English/">English</a></div>
<div class="title_link_content_part"><a class="title_link_content_part_a" href="https://www.cup.edu.cn/">校本部</a></div>
<div class="title_link_content_part"><a class="title_link_content_part_a" href="/library/">图书馆</a></div>
<div class="title_link_content_part"><a class="title_link_content_part_a" href="https://portal.cupk.edu.cn/">融合门户</a></div>
</div></div>'''

class UtilityNavigationRosterTests(unittest.TestCase):
    def test_ingestion_rejects_shortcuts_but_keeps_a_genuine_body_member(self):
        from backend.services.source_workflow import publisher_proven
        for body_entry in (False, True):
            with self.subTest(body_entry=body_entry):
                docs={ROOT:'<a href="'+DIRECTORY+'">组织机构</a>',
                    DIRECTORY:DRAWER+('<main><a href="/library/">图书馆</a></main>' if body_entry else ''),
                    HOME:'<title>中国石油大学（北京）克拉玛依校区图书馆</title>'}
                refs={role:dict(url=url,hash=hashlib.sha256(docs[url].encode()).hexdigest())
                    for role,url in [('root',ROOT),('directory',DIRECTORY),('home',HOME)]}
                bundle=dict(root_url=ROOT,school_name='中国石油大学（北京）克拉玛依校区',publisher_material=refs,
                    publisher_evidence=dict(name='图书馆',directory_evidence_id='directory',homepage_evidence_id='home'))
                with patch('backend.services.source_governance.read_snapshot',side_effect=lambda r:docs[r['url']]):
                    self.assertEqual(publisher_proven(bundle,dict(group_name='图书馆',list_url=HOME)),body_entry)

    def test_ingestion_does_not_promote_repeated_header_navigation_to_roster(self):
        from backend.services.source_workflow import publisher_proven
        for tag in ('header','nav','footer'):
            with self.subTest(container=tag):
                docs={ROOT:'<a href="'+DIRECTORY+'">组织机构</a>',
                    DIRECTORY:'<'+tag+'><a href="/library/">图书馆</a></'+tag+'><main><h1>院系设置</h1></main>',
                    HOME:'<title>中国石油大学（北京）克拉玛依校区图书馆</title>'}
                refs={role:dict(url=url,hash=hashlib.sha256(docs[url].encode()).hexdigest())
                    for role,url in [('root',ROOT),('directory',DIRECTORY),('home',HOME)]}
                bundle=dict(root_url=ROOT,school_name='中国石油大学（北京）克拉玛依校区',publisher_material=refs,
                    publisher_evidence=dict(name='图书馆',directory_evidence_id='directory',homepage_evidence_id='home'))
                with patch('backend.services.source_governance.read_snapshot',side_effect=lambda r:docs[r['url']]):
                    self.assertFalse(publisher_proven(bundle,dict(group_name='图书馆',list_url=HOME)))

    def test_real_directory_keeps_library_roster_entry_but_not_utility_copy(self):
        raw=gzip.decompress((Path(__file__).parent/'fixtures/structure/cupk-service-binding-03a8cc2ea0bb5a68-20261023.html.gz').read_bytes())
        html=raw.decode('utf8');soup=BeautifulSoup(html,'lxml')
        parsed=extract_structure(html,DIRECTORY,ROOT,'directory','组织机构')
        library=[n for n in parsed['nodes'] if n['name']=='图书馆']
        utility=[n for n in library if any('title_link' in p.get('class',[]) for p in locator_element(soup,n['locator']).parents)]
        self.assertTrue(utility)
        self.assertTrue(all(n['relation']=='navigation_entry' for n in utility))
        self.assertTrue(any(n['relation'] in ROSTER_RELATIONS for n in library))
        self.assertTrue(any(link['url']==HOME for link in parsed['links']))

    def test_similar_named_body_cards_remain_real_directory_members(self):
        html='<main><h1>直属单位</h1>'+DRAWER.replace('English','信息中心').replace('校本部','教务部').replace('融合门户','财务部')+'</main>'
        parsed=extract_structure(html,DIRECTORY,ROOT,'directory','组织机构')
        self.assertTrue(any(n['name']=='图书馆' and n['relation'] in ROSTER_RELATIONS for n in parsed['nodes']))

    def test_legacy_navigation_mislabel_cannot_regain_publisher_authority(self):
        home='<title>中国石油大学（北京）克拉玛依校区图书馆</title><section id="news"><h2 id="heading">最新消息</h2><a href="/library/c/2026-01-01/1.shtml">通知正文</a></section>'
        for body_entry in (False,True):
            with self.subTest(body_entry=body_entry):
                directory=DRAWER+('<main><a id="actual-unit" href="/library/">图书馆</a></main>' if body_entry else '')
                slot='#actual-unit' if body_entry else '.title_link a[href="/library/"]'
                docs={DIRECTORY:directory,HOME:home};hashes={u:hashlib.sha256(t.encode()).hexdigest() for u,t in docs.items()}
                path=dict(unit_key='library-unit',unit_name='图书馆',basis='official_website_entry',
                    nodes=[dict(node_key='library-unit',name='图书馆',kind='unit',url=HOME,relation='directory_entry')],
                    website_identity=dict(locator='title',identity='中国石油大学（北京）克拉玛依校区图书馆'),
                    references=[dict(url=DIRECTORY,content_hash=hashes[DIRECTORY],locators=[slot]),
                        dict(url=HOME,content_hash=hashes[HOME],locators=['title','#news','#heading'])])
                relationships=SimpleNamespace(site=dict(root_url=ROOT),website_addresses=lambda p:[HOME],
                    _addresses=lambda u:{u},pages={HOME:dict(state='fetched',content_hash=hashes[HOME])},
                    _unit_website_identity=lambda p,o:None)
                proof=_publisher_material_assertion(path,SimpleNamespace(list_url=HOME),docs.get,relationships,
                    publication=dict(content_hash=hashes[HOME],region=dict(container='#news',heading='#heading')))
                self.assertEqual(proof is not None,body_entry)
