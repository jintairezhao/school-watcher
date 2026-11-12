"""Portable evidence round trips preserve identities without activating sources."""
from copy import deepcopy
import json
import unittest

from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry
from backend.database.source_governance_models import SourceProposal
from backend.services.data_transfer import export_data, read_backup, merge_data
import test_storage_management as fixture


class StructureTransferTests(unittest.TestCase):
    setUp = fixture.StorageManagementTests.setUp
    tearDown = fixture.StorageManagementTests.tearDown
    client_as = fixture.StorageManagementTests.client_as

    def test_identity_evidence_roundtrip_keeps_import_as_history_not_authority(self):
        unit = Department(school_id=self.school.id, name='独立中心', kind='unit',
            list_url=self.school.url + 'center/', structure_key='d' * 64,
            identity_json={'schema': 1, 'kind': 'website_publisher_declaration',
                           'writer': 'official_structure', 'name': '独立中心', 'evidence': []})
        db.session.add(unit)
        db.session.commit()
        with export_data() as package:
            data = read_backup(package)
        row = next(row for row in data['departments'] if row.get('structure_key') == 'd' * 64)
        self.assertEqual(row['identity_json'], unit.identity_json)
        db.session.delete(unit)
        db.session.commit()
        merge_data(data)
        restored = Department.query.filter_by(structure_key='d' * 64).one()
        self.assertEqual(restored.identity_json['name'], '独立中心')
        self.assertEqual(restored.identity_json['writer'], 'imported_history')

    def seed(self):
        parent = Department(school_id=self.school.id, name='学院目录', kind='group',
            list_url=self.school.url + 'colleges/', structure_key='a' * 64)
        unit = Department(school_id=self.school.id, name='计算机学院', kind='unit',
            list_url=self.school.url + 'cs/', structure_key='b' * 64)
        other = Department(school_id=self.school.id, name=unit.name, kind='unit',
            list_url=unit.list_url, structure_key='c' * 64)
        self.dept.list_selector = 'ul.news li'
        db.session.add_all([parent, unit, other]); db.session.flush()
        self.fact = {'writer': 'official_structure', 'type': 'directory_listing',
            'observed_relation': 'directory_entry', 'node_key': 'roster-unit',
            'evidence': [{'reference_url': parent.list_url, 'locator': 'main ul li a',
                'content_hash': 'd' * 64, 'observed_at': '2026-10-21T10:00:00', 'parser_revision': '1'}]}
        db.session.add_all([DepartmentDirectoryEntry(parent_id=parent.id, department_id=unit.id,
            position=0, facts_json=[deepcopy(self.fact)]), DepartmentDirectoryEntry(parent_id=parent.id,
                department_id=other.id, position=1, facts_json=[])])
        db.session.commit()

    def test_roundtrip_preserves_material_and_distinct_identity_without_activating_sources(self):
        self.seed()
        with export_data() as package:
            data = read_backup(package)
        self.assertEqual(data['department_directory_entries'][0]['facts_json'], [self.fact])
        db.session.remove(); db.drop_all(); db.create_all()
        result = merge_data(data)
        self.assertEqual(Department.query.filter_by(name='计算机学院').count(), 2)
        self.assertEqual({r.structure_key for r in Department.query.filter_by(name='计算机学院')}, {'b' * 64, 'c' * 64})
        rows = DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).all()
        self.assertEqual(rows[0].facts_json, [self.fact])
        self.assertEqual(rows[1].facts_json, [])
        self.assertEqual(Department.query.filter(Department.list_selector != '').count(), 0)
        self.assertEqual(result['sources_pending_review'], 1)
        self.assertEqual(SourceProposal.query.count(), 1)
        self.assertNotEqual(SourceProposal.query.one().state, 'active')
        before = [(r.parent_id, r.department_id, r.facts_json) for r in rows]
        merge_data(data)
        self.assertEqual(before, [(r.parent_id, r.department_id, r.facts_json)
            for r in DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position)])

    def test_disk_backed_sqlite_json_and_old_absent_field_are_supported(self):
        self.seed()
        with export_data() as package:
            data = read_backup(package)
        data['department_directory_entries'][0]['facts_json'] = json.dumps([self.fact])
        data['department_directory_entries'][1].pop('facts_json')
        encoded = json.loads(json.dumps(data, default=lambda stamp: stamp.isoformat()))
        data = read_backup(fixture.packed(encoded), disk_backed=True)
        try:
            merge_data(data)
            self.assertEqual(DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).first().facts_json, [self.fact])
        finally:
            data.close()

    def test_backup_cannot_override_membership_endpoints_inside_fact(self):
        self.seed()
        with export_data() as package:
            data = read_backup(package)
        data['department_directory_entries'][0]['facts_json'][0]['parent_id'] = 999
        data = json.loads(json.dumps(data, default=lambda stamp: stamp.isoformat()))
        with self.assertRaisesRegex(ValueError, '关系类型'):
            read_backup(fixture.packed(data))

    def test_import_adds_missing_proof_without_replacing_current_relationship(self):
        self.seed()
        with export_data() as package:
            data = read_backup(package)
        row = DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).first()
        current = deepcopy(self.fact)
        current['writer'] = 'manual'
        row.facts_json = [current]
        db.session.commit()
        merge_data(data)
        db.session.refresh(row)
        self.assertEqual(row.facts_json, [current, self.fact])

    def test_current_website_binding_roundtrip_preserves_the_typed_fact(self):
        from tests.test_service_website_bindings import ServiceWebsiteBindingTests
        helper = ServiceWebsiteBindingTests(); helper.setUp()
        data = helper.projection_data()
        fact = next(deepcopy(fact) for row in data['directory_entries'] for fact in row['facts_json']
                    if fact['type'] == 'entity_page')
        self.seed()
        row = DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).first()
        row.facts_json = [fact]
        db.session.commit()
        with export_data() as package:
            exported = read_backup(package)
        self.assertEqual(exported['department_directory_entries'][0]['facts_json'], [fact])
        row.facts_json = []
        db.session.commit()
        merge_data(exported)
        self.assertEqual(db.session.get(DepartmentDirectoryEntry, (row.parent_id, row.department_id)).facts_json, [fact])

    def test_current_nested_publisher_assertion_survives_streamed_import(self):
        from tests.test_service_website_bindings import ServiceWebsiteBindingTests
        helper = ServiceWebsiteBindingTests(); helper.setUp()
        assertion = next(a for a in helper.assertions(1441) if a)
        self.assertIsInstance(assertion['website_binding'], dict)
        self.seed()
        row = DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).first()
        fact = dict(deepcopy(self.fact), type='source_ownership', assertion=assertion)
        row.facts_json = [fact]
        db.session.commit()
        with export_data() as package:
            exported = read_backup(package, disk_backed=True)
        try:
            imported = list(exported['department_directory_entries'])
            self.assertEqual(imported[0]['facts_json'], [fact])
            merge_data(exported)
            self.assertEqual(row.facts_json, [fact])
        finally:
            exported.close()

    def test_explicit_missing_evidence_is_kept_as_a_gap_without_activating_source(self):
        from backend.services.structure_facts import build_structure_facts
        self.seed()
        row = DepartmentDirectoryEntry.query.order_by(DepartmentDirectoryEntry.position).first()
        fact = dict(deepcopy(self.fact), evidence=[])
        row.facts_json = [fact]
        db.session.commit()
        with export_data() as package:
            exported = read_backup(package)
        self.assertEqual(exported['department_directory_entries'][0]['facts_json'], [fact])
        departments = [dict(id=d.id, school_id=d.school_id, name=d.name, kind=d.kind, list_url=d.list_url)
                       for d in Department.query.all()]
        graph = build_structure_facts(site=dict(id=self.school.id, name=self.school.name, root_url=self.school.url),
            pages=[], structure_nodes=[], navigation_edges=[], departments=departments, directory_entries=[dict(parent_id=row.parent_id,
                department_id=row.department_id, facts_json=[fact])])
        self.assertFalse(graph['relations'])
        self.assertTrue(graph['gaps'])

    def test_nested_assertion_limits_and_endpoint_fields_remain_rejected(self):
        from backend.services.data_transfer import _directory_facts
        self.seed()
        nested = 'value'
        for _ in range(12):
            nested = {'nested': nested}
        for bad in (dict(self.fact, parent_id=999),
                    dict(self.fact, assertion={'nested': nested}),
                    dict(self.fact, assertion={'value': float('nan')}),
                    dict(self.fact, page_url='javascript:alert(1)', type='entity_page'),
                    dict(self.fact, evidence='not-a-list')):
            with self.subTest(fields=sorted(bad)), self.assertRaises(ValueError):
                _directory_facts([bad])

    def test_more_than_128_retained_proofs_roundtrip_under_the_existing_byte_limit(self):
        from backend.services.data_transfer import _directory_facts
        self.seed()
        fact = deepcopy(self.fact)
        fact['evidence'] = [dict(fact['evidence'][0], locator=f'main a:nth-of-type({i + 1})')
                            for i in range(169)]
        self.assertEqual(_directory_facts([fact]), [fact])
        oversized = dict(fact, evidence=[dict(fact['evidence'][0], locator='a' * 8192)] * 65)
        with self.assertRaises(ValueError):
            _directory_facts([oversized])


if __name__ == '__main__':
    unittest.main()
