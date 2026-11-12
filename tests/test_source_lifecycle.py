"""Lifecycle actions stop future collection while preserving reader history."""
import unittest
from unittest.mock import patch
from sqlalchemy.exc import OperationalError

from tests import test_lightweight as fixture
from backend.database.db import db
from backend.database.models import (Department, Announcement, AnnouncementSource, AnnouncementResource,
    Subscription, UserRead, UserAnnouncementState, BackgroundTask, RuntimeLease)
from backend.database.source_lifecycle_models import SourceLifecycle
from backend.database.source_governance_models import SourceProposal, SourceConfigVersion, SourceValidation
from backend.services.source_lifecycle import (set_source_state, states_for, SourceInactiveError)
from backend.services.structure_versions import current_version, changes_since
from backend.services import tasks


class SourceLifecycleTests(unittest.TestCase):
    tearDown = fixture.LightweightTests.tearDown
    client_as = fixture.LightweightTests.client_as
    write = fixture.LightweightTests.write

    def setUp(self):
        fixture.LightweightTests.setUp(self)
        self.user.role = 'admin'
        self.teaching.list_selector = '.notices li'
        self.teaching.list_url = self.school.url + '/teaching/'
        self.teaching.title_selector = 'a'
        self.teaching.link_selector = 'a'
        sub = Subscription.query.filter_by(user_id=self.user.id, school_id=self.school.id).one()
        sub.department_ids = [self.teaching.id]
        db.session.add(UserRead(user_id=self.user.id, announcement_id=self.notice.id))
        db.session.add(UserAnnouncementState(user_id=self.user.id, announcement_id=self.notice.id, starred=True))
        db.session.add(AnnouncementSource(announcement_id=self.notice.id, department_id=self.teaching.id,
            list_url=self.teaching.list_url, article_url=self.notice.url))
        db.session.add(AnnouncementResource(announcement_id=self.notice.id, url_key='attachment',
            url=self.school.url + '/attachment.pdf', state='saved', byte_size=7, payload=b'payload', sha256='same'))
        db.session.add(SourceConfigVersion(department_id=self.teaching.id, version=1,
            config_json='{}', config_hash='same', previous_json='{}'))
        db.session.add(SourceValidation(school_id=self.school.id, department_id=self.teaching.id,
            config_hash='same', validation_json='{}'))
        db.session.commit()
        self.sid, self.did = self.school.id, self.teaching.id

    def saved_rows(self):
        models = [Announcement, AnnouncementSource, AnnouncementResource, Subscription,
                  UserRead, UserAnnouncementState, SourceConfigVersion, SourceValidation]
        return {model.__tablename__: [tuple(getattr(row, col.name) for col in model.__table__.columns)
            for row in model.query.order_by(*model.__table__.primary_key.columns).all()] for model in models}

    def change(self, state):
        return self.write(f'/api/departments/{self.did}/lifecycle', {'state': state})

    def test_admin_list_distinguishes_source_controls_from_organization_rows(self):
        unit = Department(school_id=self.sid, name='教学单位', kind='unit')
        db.session.add(unit)
        db.session.commit()
        self.assertEqual(self.change('disabled').status_code, 200)
        prior = current_version(self.sid)
        response = self.client.get(f'/api/schools/{self.sid}/departments')
        self.assertEqual(response.status_code, 200)
        rows = {row['id']: row for row in response.json}
        self.assertEqual(rows[self.did]['kind'], 'column')
        self.assertEqual(rows[self.did]['lifecycle_state'], 'disabled')
        self.assertEqual(rows[unit.id]['kind'], 'unit')
        self.assertEqual(current_version(self.sid), prior)

    def test_disable_soft_remove_and_restore_keep_identity_and_all_history(self):
        saved = self.saved_rows()
        prior = current_version(self.sid)
        for index, state in enumerate(('disabled', 'removed', 'active')):
            with self.subTest(state=state):
                result = self.change(state)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json['state'], state)
                self.assertEqual(result.json['source_id'], self.did)
                self.assertEqual(result.json['structure_version']['revision'], prior['revision'] + index + 1)
                self.assertEqual(self.saved_rows(), saved)
                self.assertIsNotNone(db.session.get(Department, self.did))
        job = BackgroundTask.query.filter_by(identity=f'collect:{self.did}').one()
        self.assertEqual(job.state, 'pending')
        changes = changes_since(self.sid, prior['epoch'], prior['revision'])['changes']
        self.assertTrue(all([c['entity_type'] for c in revision['changes']] == ['source_lifecycle'] for revision in changes))

    def test_noop_and_failed_lifecycle_commit_do_not_publish_revision(self):
        prior = current_version(self.sid)
        self.assertFalse(self.change('active').json['changed'])
        self.assertEqual(current_version(self.sid), prior)
        job = tasks.enqueue('collect', self.did, {'school_id': self.sid, 'department_id': self.did})
        with patch.object(db.session, 'commit', side_effect=OperationalError('COMMIT', {}, Exception('disk busy'))):
            response = self.change('disabled')
        self.assertEqual(response.status_code, 500)
        self.assertEqual(states_for([self.did])[self.did], 'active')
        self.assertEqual(db.session.get(BackgroundTask, job.id).state, 'pending')
        self.assertEqual(current_version(self.sid), prior)

    def test_reverted_default_lifecycle_does_not_publish_semantic_change(self):
        prior = current_version(self.sid)
        set_source_state(self.teaching, 'disabled')
        db.session.flush()
        set_source_state(self.teaching, 'active')
        db.session.commit()
        self.assertEqual(states_for([self.did])[self.did], 'active')
        self.assertEqual(current_version(self.sid), prior)

    def test_revoked_worker_cannot_commit_new_notices_or_versions(self):
        tasks.enqueue('collect', self.did, {'school_id': self.sid, 'department_id': self.did})
        handle = tasks.claim(capabilities=['http'])
        self.assertIsNotNone(handle)
        self.assertEqual(self.change('disabled').status_code, 200)
        disabled = current_version(self.sid)
        with tasks.execution_scope(handle), self.assertRaises(tasks.LeaseLost):
            db.session.add(Announcement(school_id=self.sid, department_id=self.did, title='Late result'))
            db.session.commit()
        db.session.rollback()
        self.assertEqual(Announcement.query.filter_by(title='Late result').count(), 0)
        self.assertEqual(current_version(self.sid), disabled)
        self.assertIsNone(db.session.get(RuntimeLease, handle['source_key']))

    def test_direct_collection_new_queue_and_old_claims_exclude_inactive(self):
        from backend.services.inbox_refresh import subscribed_sources, queue_sources
        from backend.services.source_collection import collect_source
        tasks.enqueue('collect', self.did, {'school_id': self.sid, 'department_id': self.did})
        # Simulate an imported legacy queue that did not receive revocation.
        db.session.add(SourceLifecycle(source_id=self.did, school_id=self.sid, state='disabled'))
        db.session.commit()
        self.assertEqual(subscribed_sources(self.school, [self.did]), [])
        self.assertEqual(queue_sources([self.teaching], manual=True), [])
        with self.assertRaises(SourceInactiveError):
            tasks.enqueue('collect', self.did, {'school_id': self.sid, 'department_id': self.did})
        with patch('backend.scraper.engine._fetch_html', side_effect=AssertionError('No network')):
            with self.assertRaises(SourceInactiveError):
                collect_source(self.teaching)
        self.assertIsNone(tasks.claim(capabilities=['http']))
        self.assertEqual(BackgroundTask.query.filter_by(identity=f'collect:{self.did}').one().error_code, 'source_inactive')

    def test_existing_source_cannot_be_reinstalled_while_inactive(self):
        from backend.services.source_governance import source_config
        from backend.services.source_onboarding import _install
        self.change('removed')
        prior = current_version(self.sid)
        with patch('backend.services.source_validation.same_column', return_value=True):
            with self.assertRaises(SourceInactiveError):
                _install(self.sid, source_config(self.teaching), [], repair=True,
                         repair_department_id=self.did, validation={})
        db.session.rollback()
        self.assertEqual(states_for([self.did])[self.did], 'removed')
        self.assertEqual(current_version(self.sid), prior)

    def test_reused_activated_proposal_does_not_override_lifecycle(self):
        from backend.services.source_governance import activate_proposal
        proposal = SourceProposal(school_id=self.sid, department_id=self.did,
            identity_key='identity', proposal_key='proposal', origin='configuration', state='activated',
            candidate_json='{}', expected_config_hash='same', evidence_hash='same', evidence_json='{}')
        db.session.add(proposal)
        db.session.commit()
        self.change('disabled')
        prior = current_version(self.sid)
        with self.assertRaises(SourceInactiveError):
            activate_proposal(proposal.id)
        db.session.rollback()
        self.assertEqual(current_version(self.sid), prior)

    def test_non_admin_and_invalid_states_are_rejected(self):
        self.user.role = 'user'
        db.session.commit()
        self.assertEqual(self.change('disabled').status_code, 403)
        self.user.role = 'admin'
        db.session.commit()
        self.assertEqual(self.change('invented').status_code, 400)
        self.assertEqual(states_for([self.did])[self.did], 'active')

    def test_lifecycle_migration_preserves_existing_history(self):
        import importlib.util
        from pathlib import Path
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        saved = self.saved_rows()
        path = Path(__file__).resolve().parents[1] / 'migrations/versions/38ea10456dc3_source_lifecycle.py'
        spec = importlib.util.spec_from_file_location('test_lifecycle_migration', path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        db.session.remove()
        with db.engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.downgrade()
                migration.upgrade()
        self.assertEqual(states_for([self.did])[self.did], 'active')
        self.assertEqual(self.saved_rows(), saved)
        set_source_state(db.session.get(Department, self.did), 'removed')
        db.session.commit()
        self.assertEqual(states_for([self.did])[self.did], 'removed')
        self.assertEqual(self.saved_rows(), saved)


if __name__ == '__main__':
    unittest.main()
