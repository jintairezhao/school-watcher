"""Concurrent catalogue updates are a retryable inbox read, never a false success."""
import unittest
from unittest.mock import patch
from backend.database.db import db
from backend.database.models import Department, Subscription, UserRead
from backend.services.structure_snapshot import StructureReadConflict


class InboxStructureRetryTests(unittest.TestCase):
    def setUp(self):
        from test_structure_snapshot import StructureSnapshotTests
        self.fixture = StructureSnapshotTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.school = self.fixture.school
        self.source = self.fixture.source('通知公告')
        user = self.fixture.fixture.fixture.admin
        db.session.add(Subscription(user_id=user.id, school_id=self.school.id))
        db.session.commit()

    def test_fragment_conflict_returns_explicit_bounded_retry_signal_without_writes(self):
        before = (Department.query.count(), Subscription.query.count(), UserRead.query.count())
        with patch('backend.services.structure_snapshot.read_structure', side_effect=StructureReadConflict('changed')):
            response = self.fixture.client.get('/?school='+str(self.school.id), headers={'X-Inbox-Fragment':'1'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json['error_code'],'structure_updating')
        self.assertEqual(response.headers['X-Structure-Updating'],'1')
        self.assertEqual(response.headers['Retry-After'],'1')
        self.assertEqual(before,(Department.query.count(),Subscription.query.count(),UserRead.query.count()))

    def test_full_page_conflict_has_html_recovery_instead_of_internal_error(self):
        with patch('backend.services.structure_snapshot.read_structure', side_effect=StructureReadConflict('changed')):
            response = self.fixture.client.get('/?school='+str(self.school.id))
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.mimetype,'text/html')
        self.assertNotIn('Structure changed during reading',response.get_data(as_text=True))

    def test_later_publication_label_read_uses_same_fragment_retry_contract(self):
        before = (Department.query.count(), Subscription.query.count(), UserRead.query.count())
        with patch('backend.services.channel_views.prime_publications', side_effect=StructureReadConflict('later read changed')):
            response = self.fixture.client.get('/?school='+str(self.school.id), headers={'X-Inbox-Fragment':'1'})
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.json['error_code'],'structure_updating')
        self.assertEqual(response.headers['X-Structure-Updating'],'1')
        self.assertEqual(before,(Department.query.count(),Subscription.query.count(),UserRead.query.count()))

    def test_later_publication_label_read_has_full_page_recovery(self):
        with patch('backend.services.channel_views.prime_publications', side_effect=StructureReadConflict('later read changed')):
            response = self.fixture.client.get('/?school='+str(self.school.id))
        self.assertEqual(response.status_code,503)
        self.assertEqual(response.mimetype,'text/html')
        self.assertNotIn('later read changed',response.get_data(as_text=True))

    def test_unrelated_program_errors_are_not_relabelled_as_transient_updates(self):
        with patch('backend.services.structure_snapshot.read_structure', side_effect=RuntimeError('another defect')):
            with self.assertRaisesRegex(RuntimeError,'another defect'):
                self.fixture.client.get('/?school='+str(self.school.id), headers={'X-Inbox-Fragment':'1'})


if __name__ == '__main__':
    unittest.main()
