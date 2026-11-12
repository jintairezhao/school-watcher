"""A roster body in a positional layout panel is not navigation chrome."""
from pathlib import Path
import gzip
import hashlib
import unittest

from bs4 import BeautifulSoup
from backend.scraper.discovery.structure import auxiliary_navigation,extract_structure

ROOT='https://www.swpu.edu.cn/'
DIRECTORY=ROOT+'jgsz/xysz.htm'
COLLEGE='石油与天然气工程学院'
HOME='http://www.swpu.edu.cn/sgy/'


class DirectoryContentPanelTests(unittest.TestCase):
    def is_auxiliary(self,html):
        return auxiliary_navigation(BeautifulSoup(html,'lxml').select_one('a'))

    def test_actual_college_roster_retains_its_body_membership(self):
        path=Path(__file__).parent/'fixtures/structure/swpu-colleges-10127aec-20261102.html.gz'
        raw=gzip.decompress(path.read_bytes())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),'10127aec13ce5d41a2ad462595d9c4198299488cf7e731e37bc2fd7b4374c67f')
        parsed=extract_structure(raw.decode(),DIRECTORY,ROOT,'directory','机构设置',[])
        members=[n for n in parsed['nodes'] if n['name']==COLLEGE and n['url']==HOME]
        self.assertEqual(len(members),1)
        self.assertEqual(members[0]['kind'],'unit')
        self.assertEqual(members[0]['relation'],'directory_entry')

    def test_static_cms_body_disambiguates_positional_navigation_name(self):
        for panel in ('navright','nav-right','nav_right'):
            with self.subTest(panel=panel):
                self.assertFalse(self.is_auxiliary(f'<div id="{panel}"><div id="vsb_content"><table>'
                    '<tr><td><a href="/unit/">计算机学院</a></td></tr></table></div></div>'))

    def test_position_name_without_a_body_witness_stays_navigation(self):
        self.assertTrue(self.is_auxiliary('<div id="navright"><a href="/unit/">计算机学院</a></div>'))

    def test_explicit_navigation_inside_the_body_stays_navigation(self):
        self.assertTrue(self.is_auxiliary('<div id="navright"><div id="vsb_content">'
            '<div class="menu"><a href="/unit/">计算机学院</a></div></div></div>'))

    def test_header_footer_and_semantic_navigation_are_not_overridden(self):
        for wrapper in ('header','footer','nav'):
            with self.subTest(wrapper=wrapper):
                self.assertTrue(self.is_auxiliary(f'<{wrapper}><div id="navright"><div id="vsb_content">'
                    f'<a href="/unit/">计算机学院</a></div></div></{wrapper}>'))
        for label in ('header','footer','breadcrumb','menu'):
            with self.subTest(label=label):
                self.assertTrue(self.is_auxiliary(f'<div class="{label}"><div id="navright"><div id="vsb_content">'
                    '<a href="/unit/">计算机学院</a></div></div></div>'))

    def test_a_layout_panel_with_an_explicit_menu_role_is_not_exempted(self):
        self.assertTrue(self.is_auxiliary('<div id="navright" class="menu"><div id="vsb_content">'
            '<a href="/unit/">计算机学院</a></div></div>'))
        self.assertTrue(self.is_auxiliary('<div id="navright" role="navigation"><div id="vsb_content">'
            '<a href="/unit/">计算机学院</a></div></div>'))


if __name__=='__main__':unittest.main()
