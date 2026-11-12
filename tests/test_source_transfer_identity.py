"""Portable source identity cannot be replaced by a shared label and URL."""
import unittest
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from backend.database.db import db
from backend.database.models import Announcement, Department, DepartmentDirectoryEntry, School
from backend.database.source_governance_models import SourceProposal, SourceTransferIdentity
from backend.services.data_transfer import export_data, read_backup, merge_data
from tests import test_storage_management as fixture


class SourceTransferIdentityTests(unittest.TestCase):
    setUp = fixture.StorageManagementTests.setUp
    tearDown = fixture.StorageManagementTests.tearDown
    client_as = fixture.StorageManagementTests.client_as
    article = fixture.StorageManagementTests.article

    def two_columns(self):
        self.dept.name = '通知公告'
        self.dept.list_selector = '#ug li'
        other = Department(school_id=self.school.id, name=self.dept.name,
            list_url=self.dept.list_url, list_selector='#pg li')
        db.session.add(other); db.session.commit()
        self.article('ug')
        self.article('pg', department_id=other.id)
        with export_data() as package:
            return read_backup(package)

    def test_same_name_same_page_columns_keep_separate_history_and_repeat_import(self):
        data = self.two_columns()
        db.session.remove(); db.drop_all(); db.create_all()
        result = merge_data(data)
        sources = Department.query.filter_by(kind='column', name='通知公告').all()
        self.assertEqual(len(sources), 2)
        self.assertTrue(all(not source.list_selector for source in sources))
        notices = {row.url: row.department_id for row in Announcement.query.all()}
        self.assertNotEqual(notices['https://example.edu.cn/ug'], notices['https://example.edu.cn/pg'])
        self.assertEqual({row.department_id for row in SourceProposal.query.all()}, {s.id for s in sources})
        self.assertEqual(result['sources_pending_review'], 2)
        ids = {source.id for source in sources}
        merge_data(data)
        self.assertEqual({source.id for source in Department.query.filter_by(kind='column')}, ids)
        self.assertEqual(Announcement.query.count(), 2)
        self.assertEqual(SourceProposal.query.count(), 2)

    def test_keys_survive_rename_relocation_and_export_does_not_write(self):
        from backend.services.structure_versions import current_version
        data = self.two_columns()
        keys = {row['id']: row['transfer_key'] for row in data['departments']}
        self.assertEqual(len(set(keys.values())), 2)
        self.dept.name, self.dept.list_url, self.dept.list_selector = '新的栏目名', self.school.url + 'moved/', '#new li'
        db.session.commit()
        before = current_version(self.school.id)
        identities = [(r.transfer_key, r.source_id) for r in SourceTransferIdentity.query.order_by(SourceTransferIdentity.transfer_key)]
        with export_data() as package:
            after = read_backup(package)
        self.assertEqual({row['id']: row['transfer_key'] for row in after['departments']}, keys)
        self.assertEqual(current_version(self.school.id), before)
        self.assertEqual([(r.transfer_key, r.source_id) for r in SourceTransferIdentity.query.order_by(SourceTransferIdentity.transfer_key)], identities)

    def test_even_equal_configurations_do_not_merge_distinct_record_keys(self):
        data = self.two_columns()
        data['departments'][1]['list_selector'] = data['departments'][0]['list_selector']
        db.session.remove(); db.drop_all(); db.create_all()
        merge_data(data)
        self.assertEqual(Department.query.filter_by(kind='column').count(), 2)
        self.assertEqual(SourceProposal.query.count(), 2)

    def test_foreign_record_key_does_not_borrow_an_existing_same_page_source(self):
        data = self.two_columns()
        old_ids = {d.id for d in Department.query.all()}
        data['departments'][0]['transfer_key'] = 'source:' + 'f' * 32
        result = merge_data(data)
        self.assertEqual(result['departments_added'], 1)
        new_id = SourceTransferIdentity.query.filter_by(transfer_key='source:' + 'f' * 32).one().source_id
        self.assertNotIn(new_id, old_ids)
        self.assertFalse(db.session.get(Department, new_id).list_selector)
        self.assertEqual(db.session.get(Department, self.dept.id).list_selector, '#ug li')

    def test_legacy_two_scopes_repeat_without_collapsing_or_adding_sources(self):
        data = self.two_columns()
        for row in data['departments']:
            row.pop('transfer_key')
        db.session.remove(); db.drop_all(); db.create_all()
        merge_data(data)
        ids = {d.id for d in Department.query.all()}
        self.assertEqual(len(ids), 2)
        merge_data(data)
        self.assertEqual({d.id for d in Department.query.all()}, ids)
        self.assertEqual(SourceProposal.query.count(), 2)

    def test_duplicate_or_malformed_keys_are_rejected_before_import(self):
        data = self.two_columns()
        for value in ('bad-key', data['departments'][1]['transfer_key']):
            changed = deepcopy(data)
            changed['departments'][0]['transfer_key'] = value
            changed = json.loads(json.dumps(changed, default=lambda value: value.isoformat()))
            with self.subTest(value=value), self.assertRaises(ValueError):
                read_backup(fixture.packed(changed))
        self.assertEqual(Department.query.count(), 2)

    def test_key_from_another_school_cannot_reassign_a_source(self):
        data = self.two_columns()
        data['schools'][0]['name'], data['schools'][0]['url'] = '另一所学校', 'https://other.example.edu.cn/'
        before = [(d.id, d.school_id, d.name, d.list_url) for d in Department.query.order_by(Department.id)]
        with self.assertRaisesRegex(ValueError, '身份.*冲突'):
            merge_data(data)
        self.assertEqual([(d.id, d.school_id, d.name, d.list_url) for d in Department.query.order_by(Department.id)], before)
        self.assertEqual(School.query.count(), 1)

    def test_failed_import_rolls_back_identity_mappings_together_with_records(self):
        data = self.two_columns()
        db.session.remove(); db.drop_all(); db.create_all()
        def fail(phase, *args):
            if phase == 'committing':
                raise RuntimeError('injected failure')
        with self.assertRaises(RuntimeError):
            merge_data(data, progress=fail)
        self.assertEqual(SourceTransferIdentity.query.count(), 0)
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(Announcement.query.count(), 0)

    def test_old_database_requires_explicit_migration_and_backfill_preserves_records(self):
        from backend.services.structure_versions import current_version
        self.two_columns()
        before = [(d.id, d.name, d.list_selector) for d in Department.query.order_by(Department.id)]
        version = current_version(self.school.id)
        SourceTransferIdentity.__table__.drop(db.engine)
        with self.assertRaisesRegex(ValueError, '升级'):
            export_data()
        self.assertFalse(inspect(db.engine).has_table('source_transfer_identities'))
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/7c2e5489ab07_source_transfer_identity.py'
        spec = importlib.util.spec_from_file_location('source_transfer_migration', path)
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        with db.engine.begin() as connection, patch.object(migration, 'op', Operations(MigrationContext.configure(connection))):
            migration.upgrade()
        self.assertEqual(SourceTransferIdentity.query.count(), 2)
        self.assertEqual([(d.id, d.name, d.list_selector) for d in Department.query.order_by(Department.id)], before)
        self.assertEqual(current_version(self.school.id), version)
        with db.engine.begin() as connection, patch.object(migration, 'op', Operations(MigrationContext.configure(connection))):
            migration.downgrade()
        self.assertEqual([(d.id, d.name, d.list_selector) for d in Department.query.order_by(Department.id)], before)

    def test_runtime_backup_import_preserves_the_same_portable_keys(self):
        from backend.services.backups import create_backup
        data = self.two_columns()
        keys = {r['id']: r['transfer_key'] for r in data['departments']}
        result = create_backup()
        with (self.root / 'backups' / result['backup']).open('rb') as stream:
            restored = read_backup(stream)
        self.assertEqual({r['id']: r['transfer_key'] for r in restored['departments']}, keys)

    def test_deletion_and_recreation_never_reuses_the_old_record_key(self):
        old = SourceTransferIdentity.query.filter_by(source_id=self.dept.id, kind='record').one().transfer_key
        db.session.delete(self.dept); db.session.commit()
        self.assertIsNone(db.session.get(SourceTransferIdentity, old))
        source = Department(school_id=self.school.id, name='教务处', list_url='https://example.edu.cn/list')
        db.session.add(source); db.session.commit()
        new = SourceTransferIdentity.query.filter_by(source_id=source.id, kind='record').one().transfer_key
        self.assertNotEqual(old, new)

    def test_same_page_columns_keep_their_own_directory_memberships(self):
        self.two_columns()
        sources = Department.query.filter_by(kind='column').order_by(Department.id).all()
        keys = {r.source_id: r.transfer_key for r in SourceTransferIdentity.query.filter_by(kind='record')}
        for index, source in enumerate(sources):
            parent = Department(school_id=self.school.id, name=f'学院{index}', kind='unit',
                list_url=self.school.url + f'college-{index}/')
            db.session.add(parent); db.session.flush()
            db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=source.id,
                position=0, facts_json=[dict(type='directory_listing', evidence=[])]))
        db.session.commit()
        with export_data() as package:
            data = read_backup(package)
        original = [keys[s.id] for s in sources]
        db.session.remove(); db.drop_all(); db.create_all()
        merge_data(data)
        for index, key in enumerate(original):
            target = db.session.get(SourceTransferIdentity, key).source_id
            parents = [db.session.get(Department, row.parent_id).name
                for row in DepartmentDirectoryEntry.query.filter_by(department_id=target)]
            self.assertEqual(parents, [f'学院{index}'])

    def test_updated_name_and_rules_propose_changes_for_the_same_record_key(self):
        data = self.two_columns()
        source_id = data['departments'][0]['id']
        data['departments'][0].update(name='改名后的通知', list_url=self.school.url + 'moved/', list_selector='#updated li')
        result = merge_data(data)
        self.assertEqual(result['departments_added'], 0)
        proposal = SourceProposal.query.filter_by(department_id=source_id).one()
        self.assertEqual(json.loads(proposal.candidate_json)['list_selector'], '#updated li')
        self.assertEqual(db.session.get(Department, source_id).list_selector, '#ug li')

    def test_explicit_migration_transaction_rolls_back_schema_and_keys_on_failure(self):
        self.two_columns()
        before = [(d.id, d.name, d.list_selector) for d in Department.query.order_by(Department.id)]
        SourceTransferIdentity.__table__.drop(db.engine)
        db.session.rollback()
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/7c2e5489ab07_source_transfer_identity.py'
        spec = importlib.util.spec_from_file_location('source_transfer_failed_migration', path)
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        with db.engine.connect() as connection:
            connection.exec_driver_sql('BEGIN IMMEDIATE')
            try:
                with patch.object(migration, 'op', Operations(MigrationContext.configure(connection))), \
                     patch.object(migration, 'uuid4', side_effect=RuntimeError('injected failure')):
                    with self.assertRaises(RuntimeError):
                        migration.upgrade()
            finally:
                connection.rollback()
        self.assertFalse(inspect(db.engine).has_table('source_transfer_identities'))
        self.assertEqual([(d.id, d.name, d.list_selector) for d in Department.query.order_by(Department.id)], before)

    def test_imported_ids_agree_in_shared_facts_labels_and_exploration_graph(self):
        from backend.services.source_channels import channels_for
        from backend.services.structure_snapshot import read_structure, graph_projection
        data = self.two_columns()
        db.session.remove(); db.drop_all(); db.create_all()
        merge_data(data)
        sources = Department.query.filter_by(kind='column').all()
        school = db.session.get(School, sources[0].school_id)
        with self.app.test_request_context('/'):
            capture = read_structure(school)
            labels = channels_for(sources)
            exploration = graph_projection(capture, school, scope='exploration')
            connected = graph_projection(capture, school)
        ids = {f'source:{source.id}' for source in sources}
        self.assertEqual({node['id'] for node in exploration['nodes'] if node['entity_type'] == 'source'}, ids)
        self.assertFalse(any(node['id'] in ids for node in connected['nodes']))
        self.assertFalse(any(r['type'] == 'source_ownership' for r in capture['facts']['relations']))
        self.assertEqual(set(labels), {source.id for source in sources})
        for source in sources:
            node = next(n for n in capture['facts']['nodes'] if n['id'] == f'source:{source.id}')
            self.assertEqual(labels[source.id]['source_id'], source.id)
            self.assertEqual(labels[source.id]['verification'], node['verification'])
