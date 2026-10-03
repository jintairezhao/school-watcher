"""A school with group-level columns must stay navigable, including empty/error states."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup

from backend import create_app
from backend.database.db import db
from backend.database.models import Department, DepartmentDirectoryEntry, School, Subscription, User


class SchoolNavigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'PROPAGATE_EXCEPTIONS': False,
            'SECRET_KEY': 'navigation-test', 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
            'SOURCE_CATALOG_PATH': str(folder / 'catalog.sqlite3'),
            'SOURCE_INVENTORY_PATH': str(folder / 'inventory.sqlite3')})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.user = User(username='reader', password_hash='unused')
        self.school = School(name='东南大学', url='https://www.seu.edu.cn', subscriber_count=1)
        db.session.add_all([self.user, self.school]); db.session.flush()
        self.group = Department(school_id=self.school.id, name='招生就业', kind='group', group_name='招生就业')
        self.column = Department(school_id=self.school.id, name='研究生招生办公室', kind='column',
            list_url='https://yzb.seu.edu.cn', group_name='招生就业')
        self.unit = Department(school_id=self.school.id, name='机械工程学院', kind='unit',
            group_name='院系设置', list_url='https://me.seu.edu.cn')
        db.session.add_all([self.group, self.column, self.unit]); db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=self.group.id, department_id=self.column.id, position=1))
        db.session.add(Subscription(user_id=self.user.id, school_id=self.school.id))
        db.session.commit()
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id'] = self.user.id

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def test_empty_school_can_open_sources_notices_and_reload(self):
        paths = (f'/subscriptions/{self.school.id}', f'/school/{self.school.id}',
                 f'/?school={self.school.id}', f'/?school={self.school.id}')
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200, response.text[:300])
                self.assertIn('机械工程学院', response.text)
                self.assertIn('研究生招生办公室', response.text)
                self.assertIn(f'/subscriptions/{self.school.id}', response.text)
        fragment = self.client.get(f'/?school={self.school.id}', headers={'X-Inbox-Fragment': '1'})
        self.assertEqual(fragment.status_code, 200)
        self.assertIn('inbox-workspace', fragment.text)
        self.assertNotIn('<html', fragment.text)

    def test_server_error_has_visible_explanation_and_working_exit(self):
        with patch('backend.services.inbox.source_hierarchy', side_effect=RuntimeError('directory unavailable')):
            response = self.client.get(f'/?school={self.school.id}')
        self.assertEqual(response.status_code, 500)
        main = BeautifulSoup(response.text, 'html.parser').select_one('main')
        self.assertIn('页面暂时无法打开', main.get_text())
        directory = main.select_one('a[href="/explore"]')
        self.assertIsNotNone(directory)
        self.assertEqual(self.client.get(directory['href']).status_code, 200)
        self.assertNotIn('directory unavailable', response.text)

    def test_missing_page_has_visible_recovery_and_api_keeps_json(self):
        response = self.client.get('/no-such-school-page')
        self.assertEqual(response.status_code, 404)
        main = BeautifulSoup(response.text, 'html.parser').select_one('main')
        self.assertIn('页面不存在', main.get_text())
        self.assertIsNotNone(main.select_one('a[href="/explore"]'))
        self.assertEqual(self.client.get('/api/no-such-school-page').status_code, 404)
        self.assertTrue(self.client.get('/api/no-such-school-page').is_json)


if __name__ == '__main__':
    unittest.main()
