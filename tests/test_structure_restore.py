"""Full offline restores invalidate cursors; additive imports preserve lineage."""
from copy import deepcopy
import importlib.util
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect

from backend import create_app
from backend.database.db import db
from backend.database.models import (Announcement, AnnouncementResource, AnnouncementSource,
    Department, School, Subscription, User, UserAnnouncementState, UserRead)
from backend.database.source_lifecycle_models import SourceLifecycle
from backend.services.backups import create_backup, restore_backup
from backend.services.data_transfer import export_data, read_backup, merge_data
from backend.services.saved_materials import resource_key
from backend.services.structure_snapshot import read_structure, graph_projection, transport_update
from backend.services.structure_versions import (current_version, changes_since,
    reset_restored_structure_epochs)
import test_storage_management as fixture


class StructureRestoreTests(unittest.TestCase):
    client_as = fixture.StorageManagementTests.client_as
    article = fixture.StorageManagementTests.article

    def setUp(self):
        fixture.StorageManagementTests.setUp(self)
        self.targets = []
        self.dept.list_selector = '.notices li'
        self.dept.title_selector = 'a'
        self.dept.link_selector = 'a'
        article = self.article()
        db.session.add_all([
            Subscription(user_id=self.reader.id, school_id=self.school.id,
                department_ids=[self.dept.id]),
            UserRead(user_id=self.reader.id, announcement_id=article.id),
            UserAnnouncementState(user_id=self.reader.id, announcement_id=article.id,
                starred=True, archived=True),
            AnnouncementSource(announcement_id=article.id, department_id=self.dept.id,
                list_url=self.dept.list_url, article_url=article.url),
            AnnouncementResource(announcement_id=article.id, url_key=resource_key(self.school.url + 'attachment.pdf'),
                url=self.school.url + 'attachment.pdf', filename='attachment.pdf', kind='attachment', state='ready',
                payload=b'preserved', byte_size=9, sha256=hashlib.sha256(b'preserved').hexdigest()),
            SourceLifecycle(source_id=self.dept.id, school_id=self.school.id, state='active')])
        db.session.commit()
        self.sid = self.school.id

    def tearDown(self):
        for app in self.targets:
            with app.app_context():
                db.session.remove()
                db.engine.dispose()
        fixture.StorageManagementTests.tearDown(self)

    def personal_rows(self):
        models = (User, Subscription, UserRead, UserAnnouncementState)
        return {model.__tablename__: [tuple(getattr(row, col.name) for col in model.__table__.columns)
            for row in model.query.order_by(*model.__table__.primary_key.columns).all()] for model in models}

    def business_rows(self):
        models = (School, Department, Announcement, AnnouncementSource, AnnouncementResource, SourceLifecycle)
        return dict(self.personal_rows(), **{model.__tablename__: [tuple(getattr(row, col.name)
            for col in model.__table__.columns) for row in model.query.order_by(*model.__table__.primary_key.columns).all()]
            for model in models})

    def graph(self, since=None):
        school = db.session.get(School, self.sid)
        capture = read_structure(school)
        return transport_update(graph_projection(capture, school), school_id=self.sid, since=since)

    def restored_app(self, destination):
        app = create_app({'TESTING': True, 'SECRET_KEY': 'restore-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(destination / 'main.db'),
            'SOURCE_CATALOG_PATH': str(destination / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(destination / 'discovery.db')})
        self.targets.append(app)
        return app

    @staticmethod
    def migration():
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/27d9e0345cb2_structure_transaction_versions.py'
        spec = importlib.util.spec_from_file_location('restore_structure_migration', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_full_restore_new_epoch_rejects_old_since_and_returns_snapshot_preserving_history(self):
        before = current_version(self.sid)
        graph = self.graph()
        expected = deepcopy(self.business_rows())
        backup = create_backup()
        self.school.name = 'Newer live name'
        db.session.commit()
        live = current_version(self.sid)
        destination = self.root / 'restored'
        restore_backup(self.root / 'backups' / backup['backup'], destination)
        app = self.restored_app(destination)
        with app.app_context():
            current = current_version(self.sid)
            self.assertNotEqual(current['epoch'], before['epoch'])
            self.assertEqual(current['revision'], 1)
            self.assertTrue(changes_since(self.sid, before['epoch'], before['revision'])['reset_required'])
            self.assertTrue(changes_since(self.sid, live['epoch'], live['revision'])['reset_required'])
            fresh = self.graph(graph['structure_token'])
            self.assertEqual(fresh['update_mode'], 'snapshot')
            self.assertEqual(fresh['structure_version'], current)
            self.assertNotEqual(fresh['structure_token'], graph['structure_token'])
            self.assertIn('nodes', fresh)
            self.assertEqual(self.business_rows(), expected)
            events = changes_since(self.sid, current['epoch'], 0)['changes']
            self.assertEqual(events[0]['changes'][0]['reason'], 'full_backup_restore')
        self.assertEqual(current_version(self.sid), live)
        self.assertEqual(db.session.get(School, self.sid).name, 'Newer live name')

    def test_additive_import_uses_same_epoch_one_revision_and_keeps_personal_state(self):
        before = current_version(self.sid)
        expected = deepcopy(self.personal_rows())
        with export_data() as package:
            data = read_backup(package)
        data['departments'].append(dict(id=9001, school_id=self.sid, name='Imported historical source',
            kind='column', list_url=self.school.url + 'imported/'))
        result = merge_data(data)
        self.assertEqual(result['departments_added'], 1)
        current = current_version(self.sid)
        self.assertEqual(current['epoch'], before['epoch'])
        self.assertEqual(current['revision'], before['revision'] + 1)
        delta = changes_since(self.sid, before['epoch'], before['revision'])
        self.assertFalse(delta['reset_required'])
        self.assertEqual(len(delta['changes']), 1)
        self.assertNotIn('epoch', [c['entity_type'] for c in delta['changes'][0]['changes']])
        self.assertEqual(self.personal_rows(), expected)
        merge_data(data)
        self.assertEqual(current_version(self.sid), current)
        self.assertEqual(self.personal_rows(), expected)

    def test_full_restore_preserves_independent_identity_ownership_and_evidence(self):
        from backend.database.source_governance_models import SourceValidation
        from backend.services import source_governance as governance
        from backend.services.runtime_catalog import RuntimeCatalog
        from backend.services.structure_snapshot import source_choices, source_paths
        from tests.test_independent_publisher_identity import (
            IndependentPublisherIdentityTests, NAME, ROOT, SCHOOL, SOURCE)
        helper = IndependentPublisherIdentityTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        self.school.name, self.school.url = SCHOOL, ROOT
        self.dept.name, self.dept.list_url = '管理制度', SOURCE
        # Fixture receipt and root navigation exercise restore behavior. They
        # are not independent validation of this real university's website.
        config_hash = governance._hash(governance.source_config(self.dept))
        validation = SourceValidation(school_id=self.sid, department_id=self.dept.id,
            config_hash=config_hash, validation_json=governance._json(dict(
                status='full', errors=[], articles={'passed': True, 'matched_count': 3},
                list_snapshot={'url': SOURCE, 'hash': 'f' * 64, 'outcome': 'usable'},
                region={'container': 'main', 'heading': 'h1', 'name': '管理制度'})))
        db.session.add(validation)
        db.session.commit()
        inventory, key, _, _, _, _ = helper.case()
        with patch('socket.socket.connect', side_effect=AssertionError('No network')):
            RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH']).publish(inventory, key)
        identity = Department.query.filter(Department.identity_json.is_not(None)).one()
        identity_id, source_id, validation_id = identity.id, self.dept.id, validation.id
        identity_json = deepcopy(identity.identity_json)
        expected = deepcopy(self.business_rows())
        before = read_structure(self.school)
        self.assertTrue(any(e['type'] == 'source_ownership' and e['target'] == f'source:{source_id}'
                            for e in before['facts']['relations']))
        old_graph = self.graph()
        backup = create_backup()
        identity.identity_json = None
        db.session.commit()
        live_version = current_version(self.sid)
        destination = self.root / 'independent-publisher-restored'
        restore_backup(self.root / 'backups' / backup['backup'], destination)
        app = self.restored_app(destination)
        with app.app_context(), patch('socket.socket.connect', side_effect=AssertionError('No network')):
            unit = db.session.get(Department, identity_id)
            self.assertEqual(unit.identity_json, identity_json)
            self.assertEqual(unit.identity_json['writer'], 'official_structure')
            self.assertFalse(unit.identity_json['administrative_parent_asserted'])
            self.assertEqual(db.session.get(SourceValidation, validation_id).config_hash, config_hash)
            restored = read_structure(db.session.get(School, self.sid))
            self.assertEqual(restored['facts']['nodes'], before['facts']['nodes'])
            self.assertEqual(restored['facts']['relations'], before['facts']['relations'])
            self.assertNotEqual(restored['version']['epoch'], before['version']['epoch'])
            self.assertEqual(restored['version']['revision'], 1)
            self.assertEqual(self.graph(old_graph['structure_token'])['update_mode'], 'snapshot')
            self.assertTrue(any(u['name'] == NAME and any(c['department'].id == source_id for c in u['own_columns'])
                for group in source_choices(restored).values() for u in group))
            self.assertTrue(any(p['relation_type'] == 'source_ownership' and p['nodes'] == [{'name': NAME}]
                for p in source_paths(restored)[source_id]))
            self.assertEqual(self.business_rows(), expected)
        self.assertEqual(current_version(self.sid), live_version)
        self.assertIsNone(db.session.get(Department, identity_id).identity_json)

    def test_pre_identity_schema_backup_restores_then_upgrades_without_losing_history(self):
        expected = deepcopy(self.business_rows())
        before = current_version(self.sid)
        db.session.remove()
        # Represent an actual older schema without exercising an unrelated
        # destructive downgrade of a table with personal-data foreign keys.
        with db.engine.begin() as connection:
            connection.exec_driver_sql('ALTER TABLE departments DROP COLUMN identity_json')
        backup = create_backup()
        destination = self.root / 'pre-identity-restored'
        restore_backup(self.root / 'backups' / backup['backup'], destination)
        app = self.restored_app(destination)
        with app.app_context():
            self.assertNotIn('identity_json', {c['name'] for c in inspect(db.engine).get_columns('departments')})
            path = Path(__file__).resolve().parents[1] / 'migrations/versions/5a0c32678fe5_organization_identity_evidence.py'
            spec = importlib.util.spec_from_file_location('restore_identity_migration', path)
            migration = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(migration)
            with db.engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    migration.upgrade()
            self.assertEqual(self.business_rows(), expected)
            self.assertIsNone(db.session.get(Department, self.dept.id).identity_json)
            self.assertNotEqual(current_version(self.sid)['epoch'], before['epoch'])
            self.assertTrue(changes_since(self.sid, before['epoch'], before['revision'])['reset_required'])

    def test_failed_merge_rolls_back_flushed_new_fact_and_version(self):
        before = current_version(self.sid)
        expected = deepcopy(self.business_rows())
        with export_data() as package:
            data = read_backup(package)
        data['departments'].extend([
            dict(id=9001, school_id=self.sid, name='Would be added', kind='unit'),
            dict(id=9002, school_id=self.sid, name='Rejected', kind='invalid-kind')])
        with self.assertRaisesRegex(ValueError, '部门类型'):
            merge_data(data)
        self.assertEqual(current_version(self.sid), before)
        self.assertEqual(self.business_rows(), expected)

    def test_connection_reset_rolls_back_and_covers_retained_deleted_school_cursor(self):
        before = current_version(self.sid)
        deleted = School(name='Deleted before backup', url='https://deleted.example.edu/')
        db.session.add(deleted)
        db.session.commit()
        deleted_id = deleted.id
        db.session.delete(deleted)
        db.session.commit()
        deleted_before = current_version(deleted_id)
        expected = deepcopy(self.business_rows())
        db.session.remove()
        with self.assertRaisesRegex(RuntimeError, 'abort restore'):
            with db.engine.begin() as connection:
                result = reset_restored_structure_epochs(connection, 'test_restore')
                self.assertEqual(result['schools_reset'], 2)
                raise RuntimeError('abort restore')
        self.assertEqual(current_version(self.sid), before)
        self.assertEqual(current_version(deleted_id), deleted_before)
        db.session.remove()
        with db.engine.begin() as connection:
            reset_restored_structure_epochs(connection, 'test_restore')
        self.assertTrue(changes_since(deleted_id, deleted_before['epoch'], deleted_before['revision'])['reset_required'])
        self.assertEqual(self.business_rows(), expected)

    def test_legacy_backup_defers_epoch_until_schema_upgrade_and_preserves_history(self):
        old = current_version(self.sid)
        expected = deepcopy(self.business_rows())
        db.session.remove()
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                self.migration().downgrade()
        backup = create_backup()
        destination = self.root / 'legacy-restored'
        restore_backup(self.root / 'backups' / backup['backup'], destination)
        app = self.restored_app(destination)
        with app.app_context():
            self.assertNotIn('structure_revisions', inspect(db.engine).get_table_names())
            with db.engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    self.migration().upgrade()
            current = current_version(self.sid)
            self.assertTrue(current['epoch'])
            self.assertNotEqual(current['epoch'], old['epoch'])
            self.assertEqual(current['revision'], 1)
            self.assertTrue(changes_since(self.sid, None, 0)['reset_required'])
            self.assertEqual(self.business_rows(), expected)


if __name__ == '__main__':
    unittest.main()
