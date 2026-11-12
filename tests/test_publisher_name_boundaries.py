"""A department name is an identity, not a substring of another publisher."""
import hashlib
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_workflow import publisher_proven
from backend.services.source_relationships import SourceRelationships
from backend.services.source_inventory import site_key

ROOT = 'https://www.example.edu.cn/'
HOME = 'https://science.example.edu.cn/'
COLUMN = HOME + 'notices/'
NAME = '理学院'
SCHOOL = '示例大学'


class PublisherNameBoundariesTests(unittest.TestCase):
    def proven(self, home_title, *, target_title=None):
        target = COLUMN if target_title is None else 'https://notice.example.edu.cn/notices/'
        texts = dict(root='<a href="/units/">组织机构</a>',
            directory=f'<main><a href="{HOME}">{NAME}</a></main>',
            home=f'<title>{home_title}</title><a href="{target}">通知公告</a>')
        refs = {key: dict(url=url, hash=key) for key, url in
                [('root', ROOT), ('directory', ROOT + 'units/'), ('home', HOME)]}
        bundle = dict(root_url=ROOT, school_name=SCHOOL, publisher_material=refs,
            publisher_evidence=dict(name=NAME, directory_evidence_id='directory', homepage_evidence_id='home'))
        if target_title is not None:
            texts['column'] = f'<title>{target_title}</title>'
            bundle['list'] = dict(url=target, hash='column')
        with patch('backend.services.source_governance.read_snapshot', side_effect=lambda ref: texts[ref['hash']]):
            return publisher_proven(bundle, dict(name='通知公告', group_name=NAME, list_url=target))

    def test_overlapping_department_names_do_not_pass_while_graph_identity_rejects_them(self):
        title = SCHOOL + '物理学院'
        page = dict(url=HOME, final_url=HOME, title=title, state='fetched', health='', notes_json='[]',
                    content_hash=hashlib.sha256(title.encode()).hexdigest())
        relations = SourceRelationships(dict(site=dict(name=SCHOOL, root_url=ROOT, site_key=site_key(ROOT)), pages=[page]), [])
        self.assertIsNone(relations._unit_website_identity(page, dict(unit_name=NAME)))
        self.assertFalse(self.proven(title))

    def test_longer_named_subunit_is_not_the_department_it_mentions(self):
        self.assertFalse(self.proven(SCHOOL + NAME + '教学研究中心'))

    def test_another_school_with_the_same_department_name_is_not_the_known_school(self):
        self.assertFalse(self.proven('另一大学' + NAME))

    def test_cross_website_column_checks_its_own_complete_publisher_name(self):
        self.assertFalse(self.proven(SCHOOL + NAME, target_title=SCHOOL + '物理学院通知公告'))
        self.assertTrue(self.proven(SCHOOL + NAME, target_title=SCHOOL + NAME + '通知公告'))

    def test_exact_known_school_name_and_explicit_home_forms_remain_valid(self):
        for title in (NAME, SCHOOL + NAME, '首页 - ' + SCHOOL + NAME,
                      SCHOOL + NAME + ' | ' + SCHOOL + NAME):
            with self.subTest(title=title):
                self.assertTrue(self.proven(title))


if __name__ == '__main__':
    unittest.main()
