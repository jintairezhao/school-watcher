"""Keep literal logo links and home labels; they do not establish ownership alone."""
from pathlib import Path
from types import SimpleNamespace
from copy import deepcopy
import gzip
import hashlib
import tempfile
import unittest

from bs4 import BeautifulSoup
from backend.services.publisher_identity import observations
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_material_assertion

ROOT='https://www.swpu.edu.cn/'
HOME=ROOT+'dean/'
PROFILE=HOME+'jgsz/xrldjzn.htm'
SOURCE=HOME+'tzgg.htm'


class ProfileHomeAnchorTests(unittest.TestCase):
    def setUp(self):
        fixture=Path(__file__).parent/'fixtures/structure/swpu-dean-responsibilities-ca616190-20261103.html.gz'
        self.html=gzip.decompress(fixture.read_bytes()).decode('utf-8')
        self.assertEqual(hashlib.sha256(self.html.encode()).hexdigest(),
                         'ca616190f6bdbd3138aa9a82baf7c4bba0edad87b60bc5f4485ea3c111d60f67')

    def test_retained_header_logo_keeps_its_empty_text_and_exact_target(self):
        data=observations(BeautifulSoup(self.html,'lxml'),'西南石油大学',PROFILE)
        logo,=[r for r in data['routes'] if r.get('home_link_kind')=='header_logo']
        self.assertEqual((logo['label'],logo['target_url']),('',HOME))
        self.assertTrue(logo['locator'])

    def test_actual_site_home_wording_is_recorded_without_replacing_it(self):
        data=observations(BeautifulSoup(self.html,'lxml'),'西南石油大学',PROFILE)
        homes=[r for r in data['routes'] if r['label']=='本站首页']
        self.assertTrue(homes)
        self.assertTrue(all(r['target_url']==HOME+'index.htm' for r in homes))

    def test_article_images_and_hidden_header_logos_are_not_home_routes(self):
        for html in (
            '<article><header><div class="logo"><a href="'+HOME+'"><img src="logo.png"></a></div></header></article>',
            '<header><a href="'+HOME+'"><img src="news.jpg"></a></header>',
            '<header hidden><div class="logo"><a href="'+HOME+'"><img src="logo.png"></a></div></header>',
            '<header><div class="logo"><a href="'+HOME+'">校外广告<img src="logo.png"></a></div></header>',
        ):
            with self.subTest(html=html):
                data=observations(BeautifulSoup(html,'lxml'),'西南石油大学',PROFILE)
                self.assertFalse(any(r.get('home_link_kind')=='header_logo' for r in data['routes']))

    def case(self,*,independent=False):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        inv=Inventory(Path(tmp.name)/'inventory.sqlite3');key=inv.ensure_site('西南石油大学',ROOT)
        if independent:
            name='教学服务中心'
            profile=('<header><div class="logo"><a href="'+HOME+'"><img src="logo.png"></a></div></header>'
                '<div class="sub-right"><p>'+name+'成立于2000年，承担学校教学实验、科学研究、学生培养与公共服务等多项工作。</p></div>')
            root='<title>西南石油大学</title><nav><a href="'+HOME+'">教学服务中心</a></nav>'
            home='<title>西南石油大学教学服务中心</title>'
            label='中心概况'
        else:
            name='教务处';profile=self.html
            root='<main><h4><a href="'+HOME+'">教务处</a></h4></main>'
            home="<title>西南石油大学教务处 - Dean's Office of Southwest Petroleum University</title>"
            label='机构设置'
            with inv.connect() as c:c.execute("UPDATE pages SET kind='directory',label='机构设置' WHERE site_key=?",(key,))
        home+='<nav><a href="'+PROFILE+'">'+label+'</a><a href="'+SOURCE+'">通知公告</a></nav>'
        bodies={ROOT:root,HOME:home,PROFILE:profile}
        for url,title,kind in ((ROOT,'机构设置' if not independent else '西南石油大学','directory' if not independent else 'root'),
                               (HOME,name,'unit'),(PROFILE,label,'unit')):
            inv.enqueue(key,url,title,kind,1,[],'local-home-anchor-contract')
            inspect_page(inv,inv.report(key)['site'],inv.get_page(key,url),
                fetcher=lambda target:dict(url=target,status=200,html=bodies[target]),ai_assist=False)
        rel=SourceRelationships(inv.report(key),inv.structure(key),directory_navigation=True)
        return rel,rel.paths_for(SOURCE),bodies

    def proofs(self,rel,paths,bodies):
        return [_publisher_material_assertion(p,SimpleNamespace(list_url=SOURCE),lambda url:bodies.get(url,''),rel)
                for p in paths]

    def test_original_logo_is_consumed_by_roster_and_independent_writers(self):
        for independent in (False,True):
            with self.subTest(independent=independent):
                rel,paths,bodies=self.case(independent=independent)
                self.assertTrue(any(self.proofs(rel,paths,bodies)))
                self.assertTrue(any((p.get('publisher_declaration') if independent else p.get('website_identity') or {})
                                    .get('profile_home_locator') for p in paths))

    def test_cached_metadata_cannot_turn_a_removed_or_retargeted_logo_into_proof(self):
        for independent in (False,True):
            rel,paths,bodies=self.case(independent=independent)
            self.assertTrue(any(self.proofs(rel,paths,bodies)))
            for mode in ('retarget','remove_image'):
                with self.subTest(independent=independent,mode=mode):
                    changed=dict(bodies);soup=BeautifulSoup(changed[PROFILE],'lxml')
                    anchor=soup.select_one('header .logo a')
                    if mode=='retarget':anchor['href']=ROOT+'other/'
                    else:anchor.img.decompose()
                    changed[PROFILE]=str(soup)
                    local=deepcopy(paths)
                    prior=rel.pages[PROFILE]['content_hash']
                    digest=hashlib.sha256(changed[PROFILE].encode()).hexdigest()
                    rel.pages[PROFILE]['content_hash']=digest
                    for path in local:
                        for reference in path.get('references',[]):
                            if reference['url']==PROFILE:reference['content_hash']=digest
                    self.assertFalse(any(self.proofs(rel,local,changed)))
                    rel.pages[PROFILE]['content_hash']=prior


if __name__=='__main__':unittest.main()
