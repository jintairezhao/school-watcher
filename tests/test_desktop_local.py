"""Desktop ownership, clean catalogue installation and optional browser recovery."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import create_app
from backend.auth.desktop import ensure_local_owner
from backend.database.db import db
from backend.database.models import (Announcement, AppConfig, Department, DepartmentDirectoryEntry,
                                     School, Subscription, User, UserRead, UserAnnouncementState)


class DesktopLocalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'isolated-local-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://', 'DESKTOP_MODE': True, 'DESKTOP_TOKEN': 'a' * 64,
            'DESKTOP_ORIGIN': 'http://localhost', 'SESSION_COOKIE_SECURE': False,
            'SOURCE_CATALOG_PATH': str(Path(self.temp.name) / 'catalog.sqlite3')})
        self.ctx = self.app.app_context()
        self.ctx.push()
        db.create_all()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.engine.dispose()
        self.ctx.pop()
        self.temp.cleanup()

    def open(self):
        ensure_local_owner()
        return self.client.get('/_desktop/open?token=' + 'a' * 64, follow_redirects=True)

    def test_no_registration_and_private_native_session(self):
        for path in ('/', '/admin', '/api/admin/stats', '/login', '/register'):
            self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.get('/_desktop/open?token=invalid').status_code, 403)
        response = self.open()
        self.assertEqual(response.status_code, 200)
        self.assertIn('系统管理', response.text)
        self.assertNotIn('退出登录', response.text)
        self.assertNotIn('个人中心', response.text)
        self.assertEqual(response.headers['Referrer-Policy'], 'no-referrer')
        self.assertEqual(self.client.get('/admin').status_code, 200)
        self.assertNotIn('id="panel-users"', self.client.get('/admin').text)
        self.assertNotIn('id="tgReg"', self.client.get('/admin').text)
        self.assertEqual(self.client.get('/api/admin/users').status_code, 404)
        self.assertEqual(self.client.post('/register').status_code, 404)
        self.assertEqual(User.query.count(), 1)
        # Neither a different Host nor a previous launch's cookie grants access.
        self.assertEqual(self.client.get('/admin', headers={'Host': 'attacker.example'}).status_code, 403)
        self.app.config['DESKTOP_TOKEN'] = 'b' * 64
        self.assertEqual(self.client.get('/admin').status_code, 403)

    def test_direct_management_still_requires_csrf_for_writes(self):
        self.open()
        self.assertEqual(self.client.post('/api/settings', json={'interval': 40}).status_code, 403)
        with self.client.session_transaction() as session:
            token = session['_csrf_token']
        self.assertEqual(self.client.post('/api/settings', json={'interval': 40},
            headers={'X-CSRF-Token': token}).status_code, 200)
        self.assertEqual(AppConfig.get('scrape_interval'), '40')

    def test_upgrade_keeps_owner_subscriptions_and_reading_states(self):
        user = User(username='old-owner', password_hash='old-password-hash', role='admin')
        school = School(name='Existing school', url='https://example.edu')
        db.session.add_all([user, school]); db.session.flush()
        department = Department(school_id=school.id, name='Existing notices')
        db.session.add(department); db.session.flush()
        notice = Announcement(school_id=school.id, department_id=department.id, title='Saved notice')
        db.session.add(notice); db.session.flush()
        db.session.add_all([Subscription(user_id=user.id, school_id=school.id),
            UserRead(user_id=user.id, announcement_id=notice.id),
            UserAnnouncementState(user_id=user.id, announcement_id=notice.id, starred=True)])
        db.session.commit()
        owner = ensure_local_owner()
        self.assertEqual(owner.id, user.id)
        self.assertEqual(owner.password_hash, 'old-password-hash')
        self.assertEqual(ensure_local_owner().id, owner.id)
        self.assertEqual(Subscription.query.filter_by(user_id=owner.id).count(), 1)
        self.assertEqual(UserRead.query.filter_by(user_id=owner.id).count(), 1)
        self.assertTrue(UserAnnouncementState.query.one().starred)

    def test_starter_configuration_contains_no_user_activity_and_preserves_edits(self):
        from backend.services.starter_catalog import catalog, install, FIELDS
        from backend.services.inbox import source_hierarchy
        from backend.services.directory_options import directory_entries_for
        self.open()
        source = catalog()
        for school in source['schools']:
            self.assertEqual(set(school), {'name', 'url', 'departments', 'directory_entries'})
            for entry in school['departments']:
                self.assertEqual(set(entry), set(FIELDS) | {'key', 'placements'})
        install()
        self.assertEqual(School.query.count(), 5)
        self.assertEqual(Department.query.count(), 397)
        self.assertEqual(Department.query.filter_by(name='专题专栏', list_url='https://news.sjtu.edu.cn/index.html').count(), 2)
        self.assertEqual(DepartmentDirectoryEntry.query.count(), 55)
        for model in (Subscription, Announcement, UserRead, UserAnnouncementState):
            self.assertEqual(model.query.count(), 0)
        self.assertTrue(all(s.subscriber_count == 0 for s in School.query.all()))
        self.assertTrue(all(d.last_scraped_at is None for d in Department.query.all()))
        for school in School.query.all():
            depts = list(school.departments)
            tree = source_hierarchy(depts, directory_entries=directory_entries_for(school.id))
            self.assertTrue(tree)
        first = Department.query.first()
        first.list_selector = '.my-custom-rule'
        db.session.commit()
        install()
        self.assertEqual(db.session.get(Department, first.id).list_selector, '.my-custom-rule')
        self.assertEqual(Department.query.count(), 397)


class BrowserPreparationTests(unittest.TestCase):
    def test_existing_browser_avoids_download_and_uses_temporary_profile(self):
        from desktop.browser import prepare, launch_channel
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'WATCHER_BROWSER_CHANNEL': 'msedge'}), \
                patch('desktop.browser.probe') as probe, patch('desktop.browser.install_chromium') as download:
            self.assertEqual(prepare(data)['phase'], 'ready')
            probe.assert_called_once_with('msedge')
            download.assert_not_called()
            self.assertEqual(launch_channel(data), 'msedge')

    def test_missing_browser_downloads_one_full_browser_and_retry_recovers(self):
        from desktop.browser import prepare, launch_channel, read_status
        with tempfile.TemporaryDirectory() as data, patch.dict(os.environ, {'WATCHER_BROWSER_CHANNEL': 'chromium'}), \
                patch('desktop.browser.probe', side_effect=[RuntimeError('missing'), None]), \
                patch('desktop.browser.install_chromium') as download:
            self.assertEqual(prepare(data)['phase'], 'ready')
            download.assert_called_once()
            self.assertEqual(launch_channel(data), 'chromium')
        with tempfile.TemporaryDirectory() as data, patch('desktop.browser.probe', side_effect=RuntimeError('missing')), \
                patch('desktop.browser.install_chromium', side_effect=OSError('offline')):
            self.assertEqual(prepare(data, managed=True)['phase'], 'error')
            self.assertIsNone(launch_channel(data))
            self.assertEqual(read_status(data)['phase'], 'error')
            with patch('desktop.browser.probe'):
                self.assertEqual(prepare(data, managed=True)['phase'], 'ready')


if __name__ == '__main__':
    unittest.main()
