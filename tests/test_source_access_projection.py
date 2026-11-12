"""Entry response observations never grant identity, admission or permanent loss."""
from copy import deepcopy
import unittest

from tests import test_structure_facts as fixture


class SourceAccessProjectionTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.StructureFactsTests(); self.f.setUp()
        self.f.data['departments'] = [self.f.source(1, lifecycle='active')]

    def source(self):
        return next(row for row in self.f.build()['nodes'] if row['id'] == 'source:1')

    def receipt(self, status=200, **changes):
        row = self.f.same_page_receipt()
        self.f.data['departments'][0]['lifecycle'] = 'active'
        row['list_observation'].update(status=status, **changes)
        return row

    def validation(self, status=403, **changes):
        digest = self.source()['config_hash']
        reference = dict(url=fixture.SHARED, status=status, outcome='denied', observed_at=fixture.AT)
        reference.update(changes)
        row = dict(id=99, school_id=1, department_id=1, config_hash=digest, created_at=fixture.AT,
            validation_json=dict(school_id=1, department_id=1, config_hash=digest,
                list_url=fixture.SHARED, status='rejected', list_snapshot=reference, errors=['read_failed']))
        self.f.data['validations'] = [row]
        return row

    def test_current_receipt_records_response_time_and_identity_without_new_relations(self):
        receipt = self.receipt()
        node = self.source()
        self.assertEqual(node['access'], 'responded')
        observation = node['access_observation']
        self.assertEqual((observation['status_code'], observation['record_id']), (200, receipt['receipt_id']))
        self.assertEqual(observation['reference_url'], fixture.SHARED)
        self.assertIn('2026-10-21', node['access_observed_at'])
        self.assertTrue(node['configured'])
        self.assertEqual(node['lifecycle'], 'active')
        self.assertFalse(any(row['type'] in ('source_ownership', 'organization_membership', 'navigation_link')
                             for row in self.f.build()['relations']))

    def test_attributed_native_failure_is_independent_of_admission_and_not_permanent_loss(self):
        for status, expected in ((401, 'restricted'), (403, 'restricted'), (451, 'restricted'),
                                 (404, 'not_found'), (410, 'not_found'), (429, 'failed'), (503, 'failed')):
            with self.subTest(status=status):
                self.validation(status)
                node = self.source()
                self.assertEqual(node['access'], expected)
                self.assertEqual(node['verification'], 'not_verified')
                self.assertEqual(node['lifecycle'], 'active')
                self.assertEqual(node['access_observation']['record_id'], 'native:99')
                self.assertNotIn('失效', str(node.get('access_label', '')))

    def test_missing_response_time_or_identity_cannot_borrow_old_page_success(self):
        receipt = self.receipt(status=None)
        self.assertEqual(self.source()['access'], 'unknown')
        for changes in ({'observed_at': ''}, {'observed_at': 'not-a-time'},
                        {'url': 'https://other.example/notices/'}, {'status': True}):
            with self.subTest(changes=changes):
                receipt['list_observation'] = dict(url=fixture.SHARED, status=200, outcome='usable',
                    hash=receipt['content_hash'], observed_at=fixture.AT) | changes
                self.assertEqual(self.source()['access'], 'unknown')
        receipt['config_hash'] = 'other-config'
        self.assertEqual(self.source()['access'], 'unknown')

    def test_latest_record_is_not_replaced_by_older_success_and_equal_time_conflicts_stay_explicit(self):
        self.receipt()
        self.validation(403, observed_at='2026-10-22T01:00:00Z')
        self.assertEqual(self.source()['access'], 'restricted')
        self.validation(403, observed_at=fixture.AT)
        self.assertEqual(self.source()['access'], 'conflicting')
        self.assertIsNone(self.source()['access_observation'])
        self.assertEqual(len(self.source()['access_observations']), 2)

    def test_foreign_validation_and_stale_config_do_not_supply_source_access(self):
        row = self.validation()
        for key, value in (('school_id', 2), ('config_hash', 'old'), ('department_id', 2)):
            original = deepcopy(row)
            row[key] = value
            self.assertEqual(self.source()['access'], 'unknown')
            row.clear(); row.update(original)
        row['validation_json']['list_url'] = fixture.ROOT + 'other/'
        self.assertEqual(self.source()['access'], 'unknown')

    def test_redirect_response_requires_the_current_observed_response_page_binding(self):
        receipt = self.f.redirected_source()
        self.assertEqual(self.source()['access'], 'responded')
        observation = self.source()['access_observation']
        self.assertEqual(observation['reference_url'], fixture.SHARED)
        self.assertEqual(observation['requested_url'], receipt['url'])
        receipt['list_observation']['requested_url'] = fixture.ROOT + 'different/'
        self.assertEqual(self.source()['access'], 'unknown')

    def test_redirected_login_observation_is_not_discarded_as_unbound_success(self):
        self.receipt()
        self.validation(200,url=fixture.ROOT+'login.jsp',requested_url=fixture.SHARED,
            outcome='needs_manual',error_code='source_login_required',observed_at='2026-10-22T01:00:00Z')
        node=self.source()
        self.assertEqual(node['access'],'restricted')
        self.assertEqual(node['verification'],'verified')
        self.assertEqual(node['access_observation']['reference_url'],fixture.ROOT+'login.jsp')

    def test_runtime_observation_requires_stable_source_school_and_current_config(self):
        self.receipt()
        row=dict(source_id=1,school_id=1,config_hash=self.source()['config_hash'],kind='collection_entry_response',
            record_id='collection:test',reference=dict(url=fixture.SHARED,requested_url=fixture.SHARED,status=403,
            outcome='denied',observed_at='2026-10-22T01:00:00Z'))
        self.f.data['entry_observations']=[row]
        self.assertEqual(self.source()['access'],'restricted')
        self.assertEqual(self.source()['verification'],'verified')
        for key,value in (('source_id',2),('school_id',2),('config_hash','old-config')):
            original=row[key];row[key]=value
            self.assertEqual(self.source()['access'],'responded')
            row[key]=original


if __name__ == '__main__':
    unittest.main()
