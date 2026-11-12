"""Publication uses actual observation time, without leaking later packaging time."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from backend.scraper.discovery.inventory_crawler import PARSER_REVISION
from backend.services.source_inventory import site_key
from backend.services.source_material_publication import _materials

ROOT='https://school.example.edu.cn/'
URL=ROOT+'notices/'
OLD='2026-10-01T06:00:00+00:00'
NOW='2026-10-20T06:00:00+00:00'
PACKED='2026-10-25T06:00:00+00:00'


class MaterialObservationTimeTests(unittest.TestCase):
    def setUp(self):
        self.school=SimpleNamespace(url=ROOT,name='测试大学')
        self.page=dict(url=URL,kind='channel',label='通知公告',state='fetched',content_hash='a'*64,checked_at=OLD,parser_revision=PARSER_REVISION)
        self.bundle=dict(identity_site=dict(site_key=site_key(ROOT),root_url=ROOT),identity_pages=[dict(self.page)],
            identity_snapshots=[dict(url=URL,hash='a'*64,role='identity',captured_at=PACKED)],
            list=dict(url=URL,hash='a'*64,role='list',captured_at=OLD),
            independent=dict(url=URL,hash='a'*64,role='independent_list',captured_at=PACKED,observed_at=NOW,outcome='usable'))
        self.published=SimpleNamespace(get_page=lambda key,url:deepcopy(self.page),snapshot=lambda key,url:'old HTML')

    def material(self):
        with patch('backend.services.source_governance.read_snapshot',return_value='checked original HTML'):
            return _materials(self.school,self.bundle,dict(list_url=URL,name='通知公告'),self.published)

    def test_identical_bytes_still_record_a_new_real_independent_observation(self):
        result=self.material()
        self.assertEqual(len(result),1)
        self.assertEqual(result[0]['observed_at'],NOW)

    def test_changed_bytes_use_observation_time_instead_of_later_packaging(self):
        self.bundle['independent']['hash']='b'*64
        self.assertEqual(self.material()[0]['observed_at'],NOW)

    def test_late_packaging_cannot_overwrite_newer_official_material(self):
        self.page.update(checked_at='2026-10-21T06:00:00+00:00',content_hash='c'*64)
        self.bundle['independent']['hash']='b'*64
        with self.assertRaisesRegex(ValueError,'更新的材料'):self.material()

    def test_reparsing_retained_dom_keeps_its_original_time(self):
        self.bundle.pop('independent')
        self.page['parser_revision']='older-parser'
        self.assertEqual(self.material()[0]['observed_at'],OLD)

    def test_reparsing_same_observation_preserves_known_http_status(self):
        self.bundle.pop('independent')
        self.page.update(parser_revision='older-parser',status_code=200)
        original=deepcopy(self.bundle)
        material=self.material()[0]
        self.assertEqual(material['reference'].get('status'),200)
        self.assertEqual(material['reference']['status_evidence']['observed_at'],OLD)
        self.assertEqual(self.bundle,original)

    def test_later_identical_bytes_do_not_inherit_old_http_success(self):
        self.page['status_code']=200
        material=self.material()[0]
        self.assertEqual(material['observed_at'],NOW)
        self.assertIsNone(material['reference'].get('status'))
        self.assertNotIn('status_evidence',material['reference'])

    def test_explicit_http_failure_is_not_replaced_with_retained_success(self):
        self.bundle.pop('independent')
        self.bundle['list']['status']=403
        self.page.update(parser_revision='older-parser',status_code=200)
        material=self.material()[0]
        self.assertEqual(material['reference']['status'],403)
        self.assertNotIn('status_evidence',material['reference'])

    def test_changed_body_or_unknown_prior_status_remains_unknown(self):
        self.page['status_code']=200
        self.bundle['independent']['hash']='b'*64
        self.assertIsNone(self.material()[0]['reference'].get('status'))
        self.bundle.pop('independent')
        self.page.update(parser_revision='older-parser',status_code=None)
        self.assertIsNone(self.material()[0]['reference'].get('status'))

    def test_invalid_explicit_observation_time_is_not_replaced_by_packaging(self):
        self.bundle['independent']['observed_at']='unknown'
        with self.assertRaisesRegex(ValueError,'观察时间'):self.material()


if __name__=='__main__':unittest.main()
