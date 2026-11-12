"""Evidence boundaries for the shared, offline structure projection."""
from copy import deepcopy
import json
import unittest

from backend.services.structure_facts import build_structure_facts


ROOT = 'https://campus.example.edu/'
DIRECTORY = ROOT + 'schools/'
SHARED = ROOT + 'notices/'
AT = '2026-10-21T01:00:00+00:00'


class StructureFactsTests(unittest.TestCase):
    def setUp(self):
        self.data = dict(site=dict(id=1, site_key='campus', root_url=ROOT),
            pages=[dict(url=url, label=label, state='fetched', content_hash='material-' + label,
                        checked_at=AT) for url, label in [(ROOT, 'home'), (DIRECTORY, 'directory'), (SHARED, 'notice')]],
            structure_nodes=[], navigation_edges=[], departments=[], directory_entries=[], validations=[])

    def build(self):
        return build_structure_facts(**self.data)

    def proof(self, reference=DIRECTORY, locator='main li:nth-child(1)'):
        material = next(p['content_hash'] for p in self.data['pages'] if p['url'] == reference)
        return dict(reference_url=reference, locator=locator, content_hash=material, observed_at=AT)

    def source(self, ident, **changes):
        return dict(dict(id=ident, school_id=1, name='Notice', kind='column',
                    list_url=SHARED, list_selector=f'#widget-{ident} li'), **changes)

    def unit(self, ident, **changes):
        return dict(dict(id=ident, school_id=1, name='Unit', kind='unit', list_url=ROOT + 'unit/'), **changes)

    def test_navigation_direction_uses_source_document_not_dom_parent(self):
        self.data['navigation_edges'] = [dict(parent_url=ROOT, target_url=DIRECTORY,
            **self.proof(ROOT, 'nav a'))]
        result = self.build()
        relation, = result['relations']
        by_id = {node['id']: node for node in result['nodes']}
        self.assertEqual(relation['type'], 'navigation_link')
        self.assertEqual(by_id[relation['source']]['url'], ROOT)
        self.assertEqual(by_id[relation['target']]['url'], DIRECTORY)
        self.assertTrue(relation['directed'])

    def test_shared_page_has_two_sources_and_two_configuration_edges(self):
        self.data['departments'] = [self.source(1), self.source(2)]
        result = self.build()
        sources = [n for n in result['nodes'] if n['entity_type'] == 'source']
        edges = [r for r in result['relations'] if r['type'] == 'source_on_page']
        self.assertEqual({n['id'] for n in sources}, {'source:1', 'source:2'})
        self.assertEqual(len({r['source'] for r in edges}), 1)
        self.assertEqual(len(edges), 2)
        self.assertEqual({r['evidence'][0]['kind'] for r in edges}, {'source_configuration'})
        self.assertTrue(all(n['verification'] == 'unknown' for n in sources))

    def redirected_source(self):
        configured=SHARED.replace('https://','http://')
        self.data['departments']=[self.source(1,list_url=configured),self.source(2)]
        digest=next(n['config_hash'] for n in self.build()['nodes'] if n['id']=='source:1')
        receipt=dict(source_id=1,config_hash=digest,receipt_id='proposal:7',
            url=configured,content_hash='material-notice',region=dict(container='main ul',heading='h2'),
            list_observation=dict(requested_url=configured,url=SHARED,status=200,outcome='usable',
                hash='material-notice',observed_at=AT))
        self.data['receipts']=[receipt]
        return receipt

    def same_page_receipt(self):
        self.data['departments'] = [self.source(1)]
        digest = next(n['config_hash'] for n in self.build()['nodes'] if n['id'] == 'source:1')
        receipt = dict(source_id=1, config_hash=digest, receipt_id='native:15',
            url=SHARED, content_hash='material-notice', region=dict(container='main ul', heading='main h2'),
            list_observation=dict(url=SHARED, outcome='usable', hash='material-notice', observed_at=AT))
        self.data['receipts'] = [receipt]
        return receipt

    def test_current_same_page_receipt_preserves_region_evidence_on_stable_binding(self):
        self.data['departments'] = [self.source(1)]
        before, = [edge for edge in self.build()['relations'] if edge['type'] == 'source_on_page']
        receipt = self.same_page_receipt()
        edge, = [edge for edge in self.build()['relations'] if edge['type'] == 'source_on_page']
        self.assertEqual(edge['id'], before['id'])
        self.assertEqual({p['locator'] for p in edge['evidence'] if p['kind'] == 'web_observation'},
                         {'main ul', 'main h2'})
        self.assertEqual(edge['assertions'][0]['receipt_id'], receipt['receipt_id'])
        self.assertEqual({p['content_hash'] for p in edge['evidence'] if p['kind'] == 'web_observation'},
                         {'material-notice'})
        self.assertFalse(any(r['type'] in ('source_ownership', 'organization_membership', 'navigation_link')
                             for r in self.build()['relations']))

    def test_same_page_region_does_not_use_stale_failed_or_missing_observations(self):
        for change in ('config', 'receipt_url', 'hash', 'time', 'region', 'outcome', 'status', 'missing', 'stale'):
            with self.subTest(change=change):
                self.setUp()
                receipt = self.same_page_receipt()
                if change == 'config': receipt['config_hash'] = 'old'
                elif change == 'receipt_url': receipt['url'] = ROOT + 'other/'
                elif change == 'hash': receipt['list_observation']['hash'] = 'other'
                elif change == 'time': receipt['list_observation'].pop('observed_at')
                elif change == 'region': receipt['region']['empty'] = True
                elif change == 'outcome': receipt['list_observation']['outcome'] = 'denied'
                elif change == 'status': receipt['list_observation']['status'] = 403
                elif change == 'missing': self.data['pages'][-1]['material_available'] = False
                elif change == 'stale': self.data['pages'][-1]['content_hash'] = 'changed'
                self.assertFalse(any(p['kind'] == 'web_observation' for edge in self.build()['relations']
                                     if edge['type'] == 'source_on_page' for p in edge['evidence']))

    def test_redirected_configuration_keeps_identity_and_adds_evidenced_response_page(self):
        receipt=self.redirected_source()
        result=self.build();nodes={n['id']:n for n in result['nodes']}
        bindings=[e for e in result['relations'] if e['target']=='source:1' and e['type']=='source_on_page']
        self.assertEqual(len(bindings),2)
        observed,= [e for e in bindings if e.get('observed_relation')=='validated_response_page']
        self.assertEqual(nodes[observed['source']]['url'],SHARED)
        self.assertEqual(nodes['source:1']['url'],receipt['url'])
        self.assertEqual(observed['evidence'][0]['locator'],'main ul')
        self.assertEqual(observed['evidence'][0]['observed_at'],AT)
        self.assertEqual(observed['assertions'][0]['receipt_id'],'proposal:7')
        self.assertEqual({n['id'] for n in nodes.values() if n['entity_type']=='source'},{'source:1','source:2'})
        self.assertFalse(any(e['type'] in ('navigation_link','source_ownership','organization_membership') for e in result['relations']))

    def test_redirect_binding_requires_current_receipt_and_complete_matching_material(self):
        for change in ('receipt','requested','status','hash','time','final','outcome','region','stale_page'):
            with self.subTest(change=change):
                receipt=self.redirected_source()
                if change=='receipt':receipt['config_hash']='old'
                elif change=='requested':receipt['list_observation']['requested_url']=ROOT+'other/'
                elif change=='status':receipt['list_observation']['status']=302
                elif change=='hash':receipt['list_observation']['hash']='old'
                elif change=='time':receipt['list_observation'].pop('observed_at')
                elif change=='final':receipt['list_observation']['url']=SHARED+'?token=private'
                elif change=='outcome':receipt['list_observation']['outcome']='needs_adapter'
                elif change=='region':receipt['region']={}
                elif change=='stale_page':
                    next(p for p in self.data['pages'] if p['url']==SHARED)['state']='failed'
                result=self.build()
                self.assertFalse(any(e.get('observed_relation')=='validated_response_page' for e in result['relations']))
                # Restore the raw material state for the next independent case.
                next(p for p in self.data['pages'] if p['url']==SHARED)['state']='fetched'

    def test_same_name_and_page_do_not_merge_entities(self):
        self.data['departments'] = [self.unit(1), self.unit(2)]
        result = self.build()
        self.assertEqual(len([n for n in result['nodes'] if n['entity_type'] == 'organization']), 2)
        self.assertFalse(result['relations'])

    def test_legacy_membership_is_a_gap_not_confirmed_organization(self):
        self.data['departments'] = [self.unit(1), self.source(2)]
        self.data['directory_entries'] = [dict(parent_id=1, department_id=2, position=0)]
        result = self.build()
        self.assertEqual([r['type'] for r in result['relations']], ['source_on_page'])
        self.assertIn('legacy_membership_without_evidence', [g['code'] for g in result['gaps']])

    def test_multiple_evidenced_owners_are_retained(self):
        self.data['departments'] = [self.unit(1), self.unit(2), self.source(3)]
        self.data['directory_entries'] = [dict(parent_id=ident, department_id=3,
            relation_type='source_ownership', assertion=dict(kind='explicit_publisher_identity',
                predicate='publication_owner', direction='publisher_to_source', identity_text=f'Owner {ident}',
                identity_reference_url=DIRECTORY, identity_locator=f'main #owner-{ident}',
                entry_reference_url=DIRECTORY, entry_locator=f'main #column-{ident}', entry_kind='column_link', entry_url=SHARED),
            evidence=[self.proof(locator=f'main #owner-{ident}'), self.proof(locator=f'main #column-{ident}')]) for ident in (1, 2)]
        result = self.build()
        owners = [r for r in result['relations'] if r['type'] == 'source_ownership']
        self.assertEqual(len(owners), 2)
        self.assertEqual({r['target'] for r in owners}, {'source:3'})
        self.assertFalse(result['gaps'])
        self.assertEqual({r['assertions'][0]['identity_text'] for r in owners}, {'Owner 1', 'Owner 2'})

    def test_collection_receipt_and_url_do_not_replace_publisher_identity_assertion(self):
        self.data['departments'] = [self.unit(1), self.source(2)]
        digest = next(n['config_hash'] for n in self.build()['nodes'] if n['entity_type'] == 'source')
        self.data['receipts'] = [dict(source_id=2, config_hash=digest, receipt_id='native:7')]
        self.data['directory_entries'] = [dict(parent_id=1, department_id=2, facts_json=[
            dict(type='source_ownership', writer='official_structure', config_hash=digest, receipt_id='native:7',
                evidence=[self.proof(locator='title')])])]
        result = self.build()
        self.assertFalse(any(r['type'] == 'source_ownership' for r in result['relations']))
        self.assertIn('publisher_identity_assertion_unverified', {g['code'] for g in result['gaps']})

    def test_publisher_assertion_requires_current_identity_and_source_entrance_material(self):
        self.data['departments'] = [self.unit(1), self.source(2)]
        assertion = dict(kind='explicit_publisher_identity', predicate='publication_owner', direction='publisher_to_source',
            identity_text='Unit', identity_reference_url=DIRECTORY, identity_locator='title',
            entry_reference_url=SHARED, entry_locator='main li', entry_kind='publication_region', entry_url=SHARED)
        self.data['directory_entries'] = [dict(parent_id=1, department_id=2, type='source_ownership', assertion=assertion,
            evidence=[self.proof(locator='title'), self.proof(SHARED, 'main li')])]
        self.assertEqual(len([r for r in self.build()['relations'] if r['type'] == 'source_ownership']), 1)
        self.data['directory_entries'][0]['evidence'][0]['content_hash'] = 'stale'
        self.assertFalse(any(r['type'] == 'source_ownership' for r in self.build()['relations']))

    def test_stale_and_unlocated_evidence_cannot_create_links(self):
        self.data['navigation_edges'] = [dict(parent_url=ROOT, target_url=DIRECTORY,
            **dict(self.proof(ROOT), content_hash='old')), dict(parent_url=ROOT, target_url=SHARED,
            **dict(self.proof(ROOT), locator=''))]
        result = self.build()
        self.assertFalse(result['relations'])
        self.assertEqual({g['code'] for g in result['gaps']}, {'stale_or_unavailable_material', 'incomplete_web_evidence'})

    def test_duplicate_navigation_evidence_aggregates_without_discarding_locator(self):
        self.data['navigation_edges'] = [dict(parent_url=ROOT, target_url=DIRECTORY,
            **self.proof(ROOT, locator)) for locator in ('nav a', 'footer a', 'nav a')]
        result = self.build()
        relation, = result['relations']
        self.assertEqual({p['locator'] for p in relation['evidence']}, {'nav a', 'footer a'})
        self.data['navigation_edges'].reverse()
        self.data['pages'].reverse()
        self.assertEqual(result, self.build())

    def test_directory_listing_is_not_administrative_parent(self):
        self.data['departments'] = [self.unit(1)]
        self.data['structure_nodes'] = [dict(node_key='page', parent_key='', name='Directory', kind='group',
            relation='page_identity', url=DIRECTORY, **self.proof()), dict(node_key='unit', parent_key='page',
            department_id=1, name='Unit', kind='unit', relation='directory_entry', url=ROOT + 'unit/', **self.proof())]
        relation, = [r for r in self.build()['relations'] if r['type'] == 'directory_listing']
        self.assertEqual(relation['type'], 'directory_listing')
        self.assertEqual(relation['target'], 'entity:1:1')

    def test_unmapped_subunit_observation_cannot_create_real_organization_edge(self):
        self.data['structure_nodes'] = [dict(node_key='parent', parent_key='', name='Parent', kind='unit',
            relation='directory_entry', url=ROOT + 'unit/', **self.proof()), dict(node_key='child', parent_key='parent',
            name='Child', kind='unit', relation='sub_unit', url=ROOT + 'unit/child/', **self.proof())]
        result = self.build()
        self.assertNotIn('organization_membership', [r['type'] for r in result['relations']])
        self.assertIn('organization_identity_not_bound', [g['code'] for g in result['gaps']])
        self.assertFalse(any(n['entity_type'] == 'organization' for n in result['nodes']))

    def test_bound_entities_and_parser_subunit_alone_do_not_prove_organization(self):
        self.data['departments'] = [self.unit(1), self.unit(2)]
        self.data['structure_nodes'] = [dict(node_key='parent', parent_key='', department_id=1, name='Parent',
            kind='unit', relation='directory_entry', **self.proof()), dict(node_key='child', parent_key='parent',
            department_id=2, name='Child', kind='unit', relation='sub_unit', **self.proof())]
        self.assertFalse(any(r['type'] == 'organization_membership' for r in self.build()['relations']))
        self.data['directory_entries'] = [dict(parent_id=1, department_id=2, relation_type='organization_membership',
            observed_relation='sub_unit', assertion=dict(kind='explicit_table_header', predicate='subordinate_unit',
                direction='parent_to_child', text='下属单位', locator='thead th', parent_locator='tbody .parent', child_locator='tbody .child'),
            evidence=[self.proof(locator='thead th'), self.proof(locator='tbody .child')])]
        relation, = [r for r in self.build()['relations'] if r['type'] == 'organization_membership']
        self.assertEqual((relation['source'], relation['target']), ('entity:1:1', 'entity:1:2'))
        self.assertEqual(relation['observed_relation'], 'sub_unit')
        self.assertEqual(relation['assertions'][0]['text'], '下属单位')
        self.assertFalse(any(g['code'] == 'organization_assertion_unverified' for g in self.build()['gaps']))
        self.assertTrue(any(r['type'] == 'directory_listing' and r['source'] == relation['source']
                            and r['target'] == relation['target'] for r in self.build()['relations']))

    def test_verified_organization_does_not_resolve_other_material_or_invalid_typed_claim(self):
        self.test_bound_entities_and_parser_subunit_alone_do_not_prove_organization()
        self.data['structure_nodes'].extend([
            dict(node_key='other-parent', parent_key='', department_id=1, name='Parent',
                 kind='unit', relation='directory_entry', **self.proof(ROOT)),
            dict(node_key='other-child', parent_key='other-parent', department_id=2, name='Child',
                 kind='unit', relation='sub_unit', **self.proof(ROOT))])
        invalid = deepcopy(self.data['directory_entries'][0])
        invalid['assertion']['text'] = ''
        self.data['directory_entries'].append(invalid)
        gaps = [g for g in self.build()['gaps'] if g['code'] == 'organization_assertion_unverified']
        self.assertEqual(len(gaps), 2)
        self.assertEqual({g.get('evidence', {}).get('reference_url') for g in gaps}, {ROOT, None})

    def test_current_child_cannot_confirm_organization_when_header_material_expired(self):
        self.test_bound_entities_and_parser_subunit_alone_do_not_prove_organization()
        self.data['directory_entries'][0]['evidence'][0]['content_hash'] = 'expired-header'
        result = self.build()
        self.assertFalse(any(r['type'] == 'organization_membership' for r in result['relations']))
        self.assertTrue(any(g['code'] == 'organization_assertion_unverified' and 'source' in g
                            for g in result['gaps']))

    def test_latest_failed_validation_never_falls_back_to_old_success(self):
        self.data['departments'] = [self.source(1)]
        config_hash = next(n['config_hash'] for n in self.build()['nodes'] if n['entity_type'] == 'source')
        good = dict(status='full', articles={'passed': True}, region={'container': '#widget-1', 'heading': 'h2'},
            list_snapshot={'url': SHARED, 'hash': 'verified-original-html'})
        self.data['validations'] = [dict(id=1, department_id=1, config_hash=config_hash, created_at=AT,
            validation_json=json.dumps(good)), dict(id=2, department_id=1, config_hash=config_hash,
            created_at='2026-10-21T02:00:00+00:00', validation_json=json.dumps({'status': 'failed'}))]
        node = next(n for n in self.build()['nodes'] if n['entity_type'] == 'source')
        self.assertEqual(node['verification'], 'not_verified')
        self.assertEqual(node['validation_id'], 2)

    def test_valid_execution_does_not_prove_owner_or_current_access(self):
        self.data['departments'] = [self.source(1)]
        config_hash = next(n['config_hash'] for n in self.build()['nodes'] if n['entity_type'] == 'source')
        self.data['validations'] = [dict(id=1, department_id=1, config_hash=config_hash, created_at=AT,
            validation_json=dict(status='full', articles={'passed': True}, region={'container': '#widget-1', 'heading': 'h2'},
                list_snapshot={'url': SHARED, 'hash': 'verified-original-html'}))]
        result = self.build()
        node = next(n for n in result['nodes'] if n['entity_type'] == 'source')
        self.assertEqual(node['verification'], 'verified')
        self.assertEqual(node['access'], 'unknown')
        self.assertEqual([r['type'] for r in result['relations']], ['source_on_page'])

    def test_input_is_unchanged_and_no_transaction_version_is_invented(self):
        self.data['departments'] = [self.source(1)]
        original = deepcopy(self.data)
        result = self.build()
        self.assertEqual(self.data, original)
        self.assertIsNone(result['structure_revision'])
        self.assertEqual(result['sync_contract'], 'unversioned')
        proof, = result['relations'][0]['evidence']
        self.assertIsNone(proof['observed_at'])
        self.assertEqual(proof['timestamp_status'], 'unknown')

    def test_validation_payload_cannot_claim_another_source(self):
        self.data['departments'] = [self.source(1)]
        config_hash = next(n['config_hash'] for n in self.build()['nodes'] if n['entity_type'] == 'source')
        self.data['validations'] = [dict(id=1, department_id=1, config_hash=config_hash, created_at=AT,
            validation_json=dict(department_id=2, status='full', articles={'passed': True},
                region={'container': '#widget-1', 'heading': 'h2'},
                list_snapshot={'url': SHARED, 'hash': 'verified-original-html'}))]
        result = self.build()
        node = next(n for n in result['nodes'] if n['entity_type'] == 'source')
        self.assertEqual(node['verification'], 'unknown')
        self.assertIn('validation_identity_mismatch', [g['code'] for g in result['gaps']])

    def test_membership_adapter_cannot_promote_navigation_to_organization(self):
        self.data['departments'] = [self.unit(1), self.unit(2)]
        self.data['directory_entries'] = [dict(parent_id=1, department_id=2,
            relation_type='organization_membership', observed_relation='navigation_entry',
            evidence=[self.proof()])]
        result = self.build()
        self.assertFalse(result['relations'])
        self.assertIn('organization_claim_not_explicit', [g['code'] for g in result['gaps']])


if __name__ == '__main__':
    unittest.main()
