"""Structure cursors describe committed facts, never worker activity or attempts."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
import tempfile
import unittest

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School, Subscription, User
from backend.database.structure_version_models import StructureRevision, StructureChange
from backend.database.source_governance_models import SourceValidation
from backend.services.structure_versions import current_version, changes_since, mark_structure_changed, reset_epoch


class StructureVersionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'structure-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(folder / 'app.db'),
            'SOURCE_CATALOG_PATH': str(folder / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(folder / 'discovery.db')})
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.school = School(name='Test school', url='https://example.edu/')
        db.session.add(self.school)
        db.session.commit()
        self.sid = self.school.id
        self.first = current_version(self.sid)

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.context.pop()
        self.temp.cleanup()

    def delta(self, version=None):
        version = version or self.first
        return changes_since(self.sid, version['epoch'], version['revision'])

    def add_department(self, name='Notices'):
        department = Department(school_id=self.sid, name=name, list_url='https://example.edu/notices')
        db.session.add(department)
        db.session.flush()
        return department

    def test_first_commit_has_epoch_and_school_change_read_is_side_effect_free(self):
        self.assertTrue(self.first['epoch'])
        self.assertEqual(self.first['revision'], 1)
        self.assertEqual(current_version(99999), {'epoch': None, 'revision': 0})
        self.assertEqual(StructureRevision.query.count(), 1)
        self.school.name = 'Unflushed'
        self.assertEqual(current_version(self.sid), self.first)
        self.assertIn(self.school, db.session.dirty)

    def test_many_flushes_one_revision_and_one_identity_per_change(self):
        department = self.add_department()
        department.name = 'Student notices'
        db.session.flush()
        department.group_name = 'Students'
        self.school.enabled = False
        db.session.flush()
        db.session.commit()
        self.assertEqual(current_version(self.sid)['revision'], 2)
        changes = self.delta()['changes']
        self.assertEqual(len(changes), 1)
        self.assertEqual([c['entity_type'] for c in changes[0]['changes']], ['department', 'school'])
        self.assertIn('group_name', changes[0]['changes'][0]['fields'])

    def test_reverted_and_same_value_and_ephemeral_entities_do_not_bump(self):
        self.school.name = 'Transient'
        db.session.flush()
        self.school.name = 'Test school'
        db.session.flush()
        transient = self.add_department()
        db.session.delete(transient)
        db.session.flush()
        db.session.commit()
        self.assertEqual(current_version(self.sid), self.first)
        db.session.expire(self.school, ['name'])
        self.school.name = 'Test school'
        db.session.commit()
        self.assertEqual(current_version(self.sid), self.first)

    def test_heartbeat_and_subscription_changes_do_not_bump(self):
        department = self.add_department()
        user = User(username='reader', password_hash='unused')
        db.session.add(user)
        db.session.commit()
        prior = current_version(self.sid)
        department.last_scraped_at = datetime.utcnow()
        self.school.subscriber_count = 1
        db.session.add(Subscription(user_id=user.id, school_id=self.sid, department_ids=[department.id]))
        db.session.commit()
        self.assertEqual(current_version(self.sid), prior)

    def test_rollback_and_failed_commit_publish_neither_facts_nor_version(self):
        self.add_department()
        mark_structure_changed(self.sid, 'catalog', 'site', fields=('generation',))
        db.session.rollback()
        self.assertEqual(current_version(self.sid), self.first)
        self.add_department()
        def reject(session):
            raise ValueError('rejected at final transaction fence')
        event.listen(Session, 'before_commit', reject)
        try:
            with self.assertRaisesRegex(ValueError, 'final transaction fence'):
                db.session.commit()
        finally:
            event.remove(Session, 'before_commit', reject)
            db.session.rollback()
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(current_version(self.sid), self.first)
        self.assertEqual(StructureChange.query.count(), 1)

    def test_nested_rollback_restores_outer_pending_change(self):
        self.school.name = 'Outer'
        db.session.flush()
        with self.assertRaisesRegex(ValueError, 'undo savepoint'):
            with db.session.begin_nested():
                self.school.name = 'Inner'
                self.add_department()
                mark_structure_changed(self.sid, 'catalog', 'discarded', fields=('generation',))
                reset_epoch(self.sid, 'discarded restore')
                raise ValueError('undo savepoint')
        db.session.commit()
        self.assertEqual(self.school.name, 'Outer')
        changes = self.delta()['changes'][0]['changes']
        self.assertEqual([(c['entity_type'], c['fields']) for c in changes], [('school', ['name'])])
        self.assertEqual(current_version(self.sid)['epoch'], self.first['epoch'])

    def test_nested_commit_is_not_publication_and_outer_rollback_cancels_it(self):
        with db.session.begin_nested():
            self.add_department()
        self.assertEqual(current_version(self.sid), self.first)
        db.session.rollback()
        self.assertEqual(current_version(self.sid), self.first)
        self.assertEqual(Department.query.count(), 0)
        with db.session.begin_nested():
            self.school.name = 'Retained nested change'
        db.session.commit()
        self.assertEqual(current_version(self.sid)['revision'], 2)

    def test_failed_flush_inside_savepoint_keeps_outer_change_only(self):
        department = self.add_department()
        department.structure_key = 'unique-structure'
        db.session.commit()
        prior = current_version(self.sid)
        self.school.name = 'Outer survives'
        with self.assertRaises(IntegrityError):
            with db.session.begin_nested():
                db.session.add(Department(school_id=self.sid, name='Rejected duplicate',
                    structure_key='unique-structure'))
                db.session.flush()
        db.session.commit()
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(self.delta(prior)['changes'][0]['changes'], [dict(
            entity_type='school', entity_id=str(self.sid), operation='upsert', fields=['name'])])

    def test_flushed_facts_and_cursor_are_unpublished_until_commit(self):
        self.add_department()
        with Session(db.engine) as observer:
            self.assertEqual(current_version(self.sid, session=observer), self.first)
            self.assertEqual(observer.query(Department).count(), 0)
        db.session.commit()
        with Session(db.engine) as observer:
            self.assertEqual(current_version(self.sid, session=observer)['revision'], 2)
            self.assertEqual(observer.query(Department).count(), 1)

    def test_explicit_marks_merge_and_reparent_invalidates_both_schools(self):
        other = School(name='Other', url='https://other.edu/')
        db.session.add(other)
        department = self.add_department()
        db.session.commit()
        prior = current_version(self.sid)
        other_prior = current_version(other.id)
        department.school_id = other.id
        mark_structure_changed(self.sid, 'catalog', 'site', fields=('generation',))
        mark_structure_changed(self.sid, 'catalog', 'site', fields=('path',))
        db.session.commit()
        changes = self.delta(prior)['changes'][0]['changes']
        self.assertEqual(changes[0]['fields'], ['generation', 'path'])
        self.assertEqual(changes[1]['operation'], 'delete')
        self.assertEqual(current_version(other.id)['revision'], other_prior['revision'] + 1)

    def test_directory_and_validation_share_transaction_without_private_material_in_journal(self):
        parent = self.add_department('Directory')
        child = self.add_department('Source')
        db.session.commit()
        prior = current_version(self.sid)
        db.session.add(DepartmentDirectoryEntry(parent_id=parent.id, department_id=child.id, position=1))
        db.session.add(SourceValidation(school_id=self.sid, department_id=child.id,
            config_hash='hash', validation_json='{"private_material":"not copied"}'))
        db.session.commit()
        changes = self.delta(prior)['changes'][0]['changes']
        self.assertEqual([c['entity_type'] for c in changes], ['directory_entry', 'validation'])
        self.assertNotIn('private_material', str(changes))
        prior = current_version(self.sid)
        entry = db.session.get(DepartmentDirectoryEntry, (parent.id, child.id))
        if hasattr(entry, 'facts_json'):
            entry.facts_json = [{'relation': 'directory_membership', 'evidence': ['snapshot:1']}]
            db.session.commit()
            self.assertIn('facts_json', self.delta(prior)['changes'][0]['changes'][0]['fields'])

    def test_epoch_reset_is_atomic_and_old_cursor_requires_snapshot(self):
        reset_epoch(self.sid, 'test restore')
        db.session.rollback()
        self.assertEqual(current_version(self.sid), self.first)
        reset_epoch(self.sid, 'test restore')
        self.school.name = 'Restored'
        db.session.commit()
        current = current_version(self.sid)
        self.assertNotEqual(current['epoch'], self.first['epoch'])
        self.assertEqual(current['revision'], 1)
        self.assertTrue(self.delta()['reset_required'])
        self.assertEqual(len(changes_since(self.sid, current['epoch'], 0)['changes']), 1)

    def test_journal_gap_and_overflow_require_snapshot(self):
        self.school.name = 'Second'
        db.session.commit()
        self.school.name = 'Third'
        db.session.commit()
        self.assertTrue(changes_since(self.sid, self.first['epoch'], 1, limit=1)['reset_required'])
        StructureChange.query.filter_by(school_id=self.sid, revision=2).delete()
        db.session.commit()
        self.assertTrue(self.delta()['reset_required'])
        self.assertTrue(changes_since(self.sid, self.first['epoch'], 99)['reset_required'])

    def test_concurrent_commits_have_contiguous_revisions(self):
        engine = db.engine
        sid = self.sid
        db.session.rollback()
        def write(index):
            with Session(engine) as session:
                session.add(Department(school_id=sid, name='Source ' + str(index)))
                session.commit()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(4)))
        self.assertEqual(current_version(sid)['revision'], 5)
        self.assertEqual([r['revision'] for r in self.delta()['changes']], [2, 3, 4, 5])

    def test_source_proposal_core_insert_marks_once_and_conflict_is_noop(self):
        from backend.services.source_governance import propose_source
        candidate = {'name': 'Notices', 'list_url': 'https://example.edu/notices/',
            'list_selector': '.list li', 'title_selector': 'a', 'link_selector': 'a'}
        proposal = propose_source(self.sid, candidate)
        prior = current_version(self.sid)
        self.assertEqual(prior['revision'], 2)
        again = propose_source(self.sid, candidate)
        self.assertEqual(again.id, proposal.id)
        self.assertEqual(current_version(self.sid), prior)
        proposal.state = 'validated'
        db.session.commit()
        self.assertEqual(self.delta(prior)['changes'][0]['changes'][0]['fields'], ['state'])

    def test_registry_binding_and_aliases_are_captured_without_empty_placeholder_changes(self):
        from backend.services.school_registry import ensure_school, rename_school
        school, created = ensure_school('Registered', 'https://registered.edu/')
        self.assertTrue(created)
        prior = current_version(school.id)
        self.assertEqual(prior['revision'], 1)
        _, created = ensure_school('Registered', 'https://registered.edu/')
        self.assertFalse(created)
        self.assertEqual(current_version(school.id), prior)
        rename_school(school, 'Renamed')
        db.session.commit()
        changed = changes_since(school.id, prior['epoch'], prior['revision'])
        self.assertEqual([c['entity_type'] for c in changed['changes'][0]['changes']], ['school', 'school_registry'])
        prior = current_version(school.id)
        from backend.database.school_registry_models import SchoolRegistryEntry
        SchoolRegistryEntry.query.filter_by(school_id=school.id).one().origin = 'selected'
        db.session.commit()
        self.assertEqual(current_version(school.id), prior)

    def test_school_disable_preserves_subscriptions_and_delete_retains_cursor(self):
        user = User(username='subscriber', password_hash='unused')
        db.session.add(user)
        db.session.flush()
        db.session.add(Subscription(user_id=user.id, school_id=self.sid, department_ids=[]))
        db.session.commit()
        self.school.enabled = False
        db.session.commit()
        self.assertEqual(Subscription.query.count(), 1)
        Subscription.query.delete()
        db.session.delete(self.school)
        db.session.commit()
        self.assertEqual(current_version(self.sid)['revision'], 3)
        self.assertEqual(self.delta()['changes'][-1]['changes'][0]['operation'], 'delete')

    def test_new_migration_preserves_existing_school_and_subscription(self):
        import importlib.util
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/27d9e0345cb2_structure_transaction_versions.py'
        spec = importlib.util.spec_from_file_location('test_structure_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        user = User(username='migration-reader', password_hash='unused')
        db.session.add(user)
        db.session.flush()
        db.session.add(Subscription(user_id=user.id, school_id=self.sid, department_ids=[]))
        db.session.commit()
        db.session.remove()
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.upgrade()
        self.assertEqual(db.session.get(School, self.sid).name, 'Test school')
        self.assertEqual(Subscription.query.count(), 1)
        initialized = current_version(self.sid)
        self.assertTrue(initialized['epoch'])
        self.assertNotEqual(initialized['epoch'], self.first['epoch'])
        self.assertEqual(initialized['revision'], 1)
        self.assertTrue(changes_since(self.sid, None, 0)['reset_required'])
        db.session.get(School, self.sid).enabled = False
        db.session.commit()
        self.assertEqual(current_version(self.sid)['revision'], 2)


if __name__ == '__main__':
    unittest.main()
