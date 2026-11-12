"""Publisher links must resolve as the captured page actually directs them."""
import hashlib
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from bs4 import BeautifulSoup
from backend.services.source_workflow import publisher_proven
from backend.services.source_relationships import SourceRelationships
from backend.services.school_structure import _publisher_material_assertion
from backend.services.source_inventory import site_key
from backend.scraper.discovery.structure import locator

ROOT = 'https://example.edu.cn/'
HOME = ROOT + 'office/'


class PublisherLinkBasesTests(unittest.TestCase):
    def chain(self, *, root_base='', directory_base='', home_base='', directory=ROOT+'units/',
              home=HOME, root_link='units/', directory_link=None, home_link='forms/', target=None, extra=''):
        target = target or home + 'forms/'
        documents = dict(root=root_base + extra + f'<a href="{root_link}">机构设置</a>',
            directory=directory_base + f'<main><a href="{directory_link or home}">教务部</a></main>',
            home=home_base + f'<title>示例大学教务部</title><a href="{home_link}">教师用表</a>')
        refs = {key: dict(url=url, hash=key) for key, url in [('root', ROOT), ('directory', directory), ('home', home)]}
        bundle = dict(root_url=ROOT, school_name='示例大学', publisher_material=refs,
            publisher_evidence=dict(name='教务部', directory_evidence_id='directory', homepage_evidence_id='home'))
        with patch('backend.services.source_governance.read_snapshot', side_effect=lambda ref: documents[ref['hash']]):
            return publisher_proven(bundle, dict(name='教师用表', group_name='教务部', list_url=target))

    def test_home_base_cannot_turn_an_outgoing_link_into_a_local_entry(self):
        html = '<base href="https://other.example.edu.cn/"><title>示例大学教务部</title><a href="forms/">教师用表</a>'
        soup = BeautifulSoup(html, 'lxml')
        digest = hashlib.sha256(html.encode()).hexdigest()
        page = dict(url=HOME, final_url=HOME, title='示例大学教务部', state='fetched', health='',
                    notes_json='[]', content_hash=digest)
        relations = SourceRelationships(dict(site=dict(name='示例大学', root_url=ROOT, site_key=site_key(ROOT)), pages=[page]), [])
        path = dict(basis='official_website_entry', unit_name='教务部', nodes=[dict(kind='unit', name='教务部', url=HOME)],
            website_identity=dict(locator=locator(soup.title), identity='示例大学教务部'),
            references=[dict(url=HOME, content_hash=digest, locators=[locator(soup.title), locator(soup.a)])])
        self.assertIsNone(_publisher_material_assertion(path, SimpleNamespace(list_url=HOME+'forms/'), lambda _: html, relations))
        self.assertFalse(self.chain(home_base='<base href="https://other.example.edu.cn/">'))

    def test_a_valid_relative_base_resolves_to_its_actual_nested_column(self):
        self.assertTrue(self.chain(home_base='<base href="nested/">', target=HOME+'nested/forms/'))

    def test_directory_base_uses_the_real_department_address(self):
        base = '<base href="/different/">'
        self.assertFalse(self.chain(directory_base=base, directory_link='office/', home=ROOT+'units/office/'))
        self.assertTrue(self.chain(directory_base=base, directory_link='office/', home=ROOT+'different/office/'))

    def test_root_base_uses_the_real_directory_address(self):
        base = '<base href="/campus/">'
        self.assertFalse(self.chain(root_base=base))
        self.assertTrue(self.chain(root_base=base, directory=ROOT+'campus/units/'))

    def test_one_malformed_unrelated_href_does_not_cancel_valid_material(self):
        self.assertTrue(self.chain(extra='<a href="https://broken.example.edu.cn:bad/">其他</a>'))

    def test_absolute_links_remain_absolute_in_a_page_with_base(self):
        self.assertTrue(self.chain(home_base='<base href="https://other.example.edu.cn/">', home_link=HOME+'forms/'))


if __name__ == '__main__':
    unittest.main()
