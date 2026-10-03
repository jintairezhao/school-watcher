"""Deletion is atomic, revokes background work and preserves other schools."""
import unittest
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from tests import test_lightweight as fixture
from backend.database.db import db
from backend.database.models import (School, Department, Announcement, ScrapeLog,
    BackgroundTask, AppConfig, RuntimeLease, Subscription)
from backend.services import tasks


class SchoolDeletionTests(unittest.TestCase):
    setUp = fixture.LightweightTests.setUp
    tearDown = fixture.LightweightTests.tearDown
    client_as = fixture.LightweightTests.client_as
    write = fixture.LightweightTests.write

    def prepare(self):
        self.user.role = 'admin'
        db.session.add_all([
            ScrapeLog(school_id=self.school.id, source_name='通知公告', status='success'),
            ScrapeLog(school_id=self.foreign.id, source_name='其他学校', status='success')])
        db.session.commit()
        return self.school.id, self.foreign.id

    def test_school_with_scrape_history_deletes_without_affecting_other_school(self):
        school_id, other_id = self.prepare()
        tasks.enqueue('discover', school_id, {'school_id': school_id})
        running = tasks.claim(capabilities=['directory'])
        own = tasks.enqueue('collect', self.teaching.id, {'school_id': school_id, 'department_id': self.teaching.id})
        other = tasks.enqueue('collect', self.foreign_dept.id, {'school_id': other_id, 'department_id': self.foreign_dept.id})
        own_id, other_task_id = own.id, other.id
        AppConfig.set(f'discovery_started_{school_id}', '1')
        result = self.write(f'/api/schools/{school_id}', method='delete')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIsNone(db.session.get(School, school_id))
        self.assertEqual(ScrapeLog.query.filter_by(school_id=school_id).count(), 0)
        self.assertEqual(Subscription.query.filter_by(school_id=school_id).count(), 0)
        self.assertEqual(Department.query.filter_by(school_id=school_id).count(), 0)
        self.assertEqual(Announcement.query.filter_by(school_id=school_id).count(), 0)
        self.assertIsNone(db.session.get(BackgroundTask, own_id))
        self.assertIsNone(AppConfig.get(f'discovery_started_{school_id}'))
        with self.assertRaises(tasks.LeaseLost):
            tasks.assert_owned(running)
        self.assertIsNone(db.session.get(RuntimeLease, running['source_key']))
        self.assertIsNotNone(db.session.get(BackgroundTask, other_task_id))
        self.assertEqual(ScrapeLog.query.filter_by(school_id=other_id).count(), 1)
        self.assertIsNotNone(db.session.get(School, other_id))

    def test_failed_delete_rolls_back_and_returns_readable_json(self):
        school_id, _ = self.prepare()
        task = tasks.enqueue('discover', school_id, {'school_id': school_id})
        task_id = task.id
        with patch.object(db.session, 'commit', side_effect=OperationalError('DELETE', {}, Exception('disk busy'))):
            result = self.write(f'/api/schools/{school_id}', method='delete')
        self.assertEqual(result.status_code, 500)
        self.assertTrue(result.is_json)
        self.assertIn('未删除', result.json['error'])
        self.assertIsNotNone(db.session.get(School, school_id))
        self.assertEqual(ScrapeLog.query.filter_by(school_id=school_id).count(), 1)
        self.assertEqual(db.session.get(BackgroundTask, task_id).state, 'pending')
        self.assertEqual(self.client.get('/api/schools').status_code, 200)

    def test_department_delete_revokes_its_work_only(self):
        self.prepare()
        ident = self.teaching.id
        own = tasks.enqueue('collect', ident, {'department_id': ident})
        own_id = own.id
        other = tasks.enqueue('collect', self.news.id, {'department_id': self.news.id})
        other_id = other.id
        response = self.write(f'/api/departments/{ident}', method='delete')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(db.session.get(BackgroundTask, own_id))
        self.assertIsNotNone(db.session.get(BackgroundTask, other_id))

    def test_unhandled_database_failure_returns_json_and_releases_session(self):
        from backend.database.models import User
        self.app.config['PROPAGATE_EXCEPTIONS'] = False

        @self.app.route('/api/test-database-error')
        def fail():
            db.session.add(User(username=self.user.username, password_hash='unused'))
            db.session.flush()

        response = self.client.get('/api/test-database-error')
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.is_json)
        self.assertEqual(self.client.get('/api/schools').status_code, 200)


if __name__ == '__main__':
    unittest.main()
