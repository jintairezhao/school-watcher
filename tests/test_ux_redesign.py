"""Notification inbox and administrator-boundary regression tests."""

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import create_app
from backend.database.db import db
from backend.database.models import (Announcement, Department, School,
                                     Subscription, User, UserRead)


class UXRedesignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app({
            'TESTING': True,
            'SECRET_KEY': 'ux-redesign-tests',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://',
        })
        with cls.app.app_context():
            db.create_all()

            cls.reader = User(username='reader', password_hash='unused', role='user')
            cls.admin = User(username='operator', password_hash='unused', role='admin')
            school_a = School(name='甲大学', url='https://a.example', enabled=True)
            school_b = School(name='乙大学', url='https://b.example', enabled=True)
            db.session.add_all([cls.reader, cls.admin, school_a, school_b])
            db.session.flush()

            dept_a1 = Department(school_id=school_a.id, name='教务处')
            dept_a2 = Department(school_id=school_a.id, name='学生工作部')
            dept_b = Department(school_id=school_b.id, name='科研处')
            db.session.add_all([dept_a1, dept_a2, dept_b])
            db.session.flush()

            recent = datetime.utcnow() - timedelta(days=2)
            cls.ann_recent = Announcement(
                school_id=school_a.id, department_id=dept_a1.id,
                title='甲校近期通知', url='https://a.example/recent',
                content_text='近期内容', published_at=recent,
            )
            cls.ann_other_dept = Announcement(
                school_id=school_a.id, department_id=dept_a2.id,
                title='甲校另一部门通知', url='https://a.example/other',
                content_text='另一部门内容', published_at=recent - timedelta(hours=1),
            )
            cls.ann_archive = Announcement(
                school_id=school_a.id, department_id=dept_a1.id,
                title='甲校归档通知', url='https://a.example/archive',
                content_text='归档内容', published_at=datetime(2025, 4, 15, 9, 0),
            )
            cls.ann_foreign = Announcement(
                school_id=school_b.id, department_id=dept_b.id,
                title='乙校不应出现', url='https://b.example/recent',
                content_text='越权内容', published_at=recent,
            )
            db.session.add_all([
                cls.ann_recent, cls.ann_other_dept,
                cls.ann_archive, cls.ann_foreign,
            ])
            db.session.add(Subscription(user_id=cls.reader.id, school_id=school_a.id))
            db.session.commit()

            cls.reader_id = cls.reader.id
            cls.admin_id = cls.admin.id
            cls.school_a_id = school_a.id
            cls.dept_a1_id = dept_a1.id
            cls.dept_b_id = dept_b.id
            cls.ann_recent_id = cls.ann_recent.id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.drop_all()

    def setUp(self):
        with self.app.app_context():
            UserRead.query.delete()
            db.session.commit()

    def client_as(self, user_id, csrf=False):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = user_id
            if csrf:
                session['_csrf_token'] = 'test-token'
        return client

    def test_inbox_only_contains_subscribed_schools(self):
        response = self.client_as(self.reader_id).get('/?period=all')
        self.assertEqual(response.status_code, 200)
        self.assertIn('甲校近期通知', response.text)
        self.assertNotIn('乙校不应出现', response.text)
        self.assertNotIn('科研处', response.text)

    def test_school_department_filter_rejects_cross_school_department(self):
        client = self.client_as(self.reader_id)
        response = client.get(
            f'/?school={self.school_a_id}&dept={self.dept_a1_id}&period=all')
        self.assertIn('甲校近期通知', response.text)
        self.assertIn('甲校归档通知', response.text)
        self.assertNotIn('甲校另一部门通知', response.text)

        cross_school = client.get(
            f'/?school={self.school_a_id}&dept={self.dept_b_id}&period=all')
        self.assertNotIn('乙校不应出现', cross_school.text)
        self.assertNotIn('科研处', cross_school.text)
        self.assertIn('学生工作部', cross_school.text)

    def test_year_and_month_can_combine_with_department(self):
        response = self.client_as(self.reader_id).get(
            f'/?school={self.school_a_id}&dept={self.dept_a1_id}'
            '&period=archive&year=2025&month=4')
        self.assertEqual(response.status_code, 200)
        self.assertIn('甲校归档通知', response.text)
        self.assertNotIn('甲校近期通知', response.text)

    def test_read_filter_and_unread_undo_are_per_user(self):
        with self.app.app_context():
            db.session.add(UserRead(
                user_id=self.reader_id,
                announcement_id=self.ann_recent_id,
            ))
            db.session.commit()

        unread = self.client_as(self.reader_id).get('/?period=all&read=unread')
        self.assertNotIn('甲校近期通知', unread.text)

        client = self.client_as(self.reader_id, csrf=True)
        undone = client.delete(
            f'/api/announcements/{self.ann_recent_id}/read',
            headers={'X-CSRF-Token': 'test-token'},
        )
        self.assertEqual(undone.status_code, 200)
        with self.app.app_context():
            self.assertIsNone(UserRead.query.filter_by(
                user_id=self.reader_id,
                announcement_id=self.ann_recent_id,
            ).first())

    def test_system_management_is_admin_only_and_separate(self):
        regular = self.client_as(self.reader_id)
        self.assertEqual(regular.get('/admin').status_code, 403)
        personal = regular.get('/me')
        self.assertEqual(personal.status_code, 200)
        self.assertNotIn('进入系统管理', personal.text)

        admin = self.client_as(self.admin_id).get('/admin')
        self.assertEqual(admin.status_code, 200)
        self.assertIn('class="admin-shell"', admin.text)
        self.assertIn('仅管理员', admin.text)
        self.assertIn('返回通知', admin.text)
        legacy = self.client_as(self.admin_id).get('/settings/legacy')
        self.assertEqual(legacy.status_code, 302)
        self.assertEqual(legacy.location, '/admin#platform')


if __name__ == '__main__':
    unittest.main()
