"""First-run configuration must precede subscription side effects."""
from datetime import date, datetime
import unittest
from unittest.mock import patch


class CollectionSetupTests(unittest.TestCase):
    def setUp(self):
        from test_desktop_local import DesktopLocalTests
        from backend.database.db import db
        from backend.database.models import School
        self.fixture = DesktopLocalTests()
        self.fixture.setUp()
        self.fixture.open()
        self.client = self.fixture.client
        self.school = School(name='测试大学', url='https://www.example.edu.cn/')
        db.session.add(self.school); db.session.commit()
        with self.client.session_transaction() as session:
            self.headers = {'X-CSRF-Token': session['_csrf_token']}

    def tearDown(self):
        self.fixture.tearDown()

    def test_first_subscribe_and_add_are_guarded_before_any_mutation(self):
        from backend.database.models import BackgroundTask, Subscription, School
        for path, payload in [('/api/subscriptions', {'school_id': self.school.id}),
                              ('/api/catalog/subscribe', {'name': '浙江大学'}),
                              ('/api/schools', {'name': '另一所大学', 'url': self.school.url})]:
            response = self.client.post(path, json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 409, response.json)
            self.assertEqual(response.json['error_code'], 'collection_setup_required')
        self.assertEqual(Subscription.query.count(), 0)
        self.assertEqual(BackgroundTask.query.count(), 0)
        self.assertEqual(School.query.count(), 1)

    def test_viewing_or_leaving_setup_does_not_subscribe_or_call_ai(self):
        from backend.database.models import BackgroundTask, Subscription
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')):
            response = self.client.get('/collection/setup?school_id=' + str(self.school.id))
        self.assertEqual(response.status_code, 200)
        self.assertIn('从哪天的通知开始收录', response.text)
        self.assertIn('获取 API 密钥', response.text)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(BackgroundTask.query.count(), 0)
        self.assertEqual(Subscription.query.count(), 0)

    def test_save_then_subscribe_uses_exact_day_and_explicit_rules_mode(self):
        from backend.database.models import AppConfig, BackgroundTask
        from backend.services.collection_settings import since_date
        value = f'{date.today().year - 1}-06-15'
        saved = self.client.post('/api/collection/setup', json={
            'since_date': value, 'interval': 45, 'ai_mode': 'rules'}, headers=self.headers)
        self.assertEqual(saved.status_code, 200, saved.json)
        self.assertEqual(BackgroundTask.query.count(), 0, 'saving must not start work')
        result = self.client.post('/api/subscriptions', json={
            'school_id': self.school.id, 'ai_assist': False}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertEqual(since_date(), datetime.fromisoformat(value))
        self.assertEqual(AppConfig.get('scrape_interval'), '45')
        self.assertFalse(BackgroundTask.query.filter_by(kind='discover').one().payload['ai_assist'])
        self.assertEqual(BackgroundTask.query.filter_by(kind='scrape').count(), 1)
        self.client.post('/api/subscriptions', json={'school_id': self.school.id}, headers=self.headers)
        self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 1)

    def test_invalid_setup_is_atomic_and_missing_ai_cannot_be_claimed_ready(self):
        from backend.database.models import AppConfig
        for values in ({'since_date': '9999-01-01', 'interval': 45, 'ai_mode': 'rules'},
                       {'since_date': '2024-06-15', 'interval': 4, 'ai_mode': 'rules'},
                       {'since_date': '2024-06-15', 'interval': 5.5, 'ai_mode': 'rules'},
                       {'since_date': '2024-06-15', 'interval': True, 'ai_mode': 'rules'},
                       {'since_date': '2024-06-15', 'interval': 45, 'ai_mode': 'ai'}):
            response = self.client.post('/api/collection/setup', json=values, headers=self.headers)
            self.assertEqual(response.status_code, 400, response.json)
            self.assertIsNone(AppConfig.get('scrape_interval'))
            self.assertIsNone(AppConfig.get('scrape_since_date'))
            self.assertIsNone(AppConfig.get('collection_setup_completed'))

    def test_same_month_earlier_cutoff_does_not_reuse_partial_backfill(self):
        from backend.database.db import db
        from backend.database.models import Department
        from backend.services.collection_settings import record_coverage, coverage_complete
        source = Department(school_id=self.school.id, name='通知', list_url=self.school.url)
        db.session.add(source); db.session.commit()
        record_coverage(source, datetime(2024, 6, 15))
        self.assertTrue(coverage_complete(source, datetime(2024, 6, 15)))
        self.assertFalse(coverage_complete(source, datetime(2024, 6, 1)))

    def test_existing_month_settings_upgrade_and_admin_edits_stay_consistent(self):
        from backend.database.models import AppConfig
        from backend.services.collection_settings import since_date
        AppConfig.set('scrape_since_month', '2024-06')
        self.assertEqual(since_date(), datetime(2024, 6, 1))
        response = self.client.post('/api/settings', json={'since_date': '2024-06-15', 'interval': 20}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/admin/stats').json['scrape_since_date'], '2024-06-15')
        response = self.client.post('/api/settings', json={'since_month': '2024-06'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(since_date(), datetime(2024, 6, 1))

    def test_existing_installation_is_not_blocked_or_reset(self):
        from backend.database.db import db
        from backend.database.models import Subscription
        from backend.auth.desktop import ensure_local_owner
        db.session.add(Subscription(user_id=ensure_local_owner().id, school_id=self.school.id))
        self.school.subscriber_count = 1
        db.session.commit()
        response = self.client.post('/api/subscriptions', json={'school_id': self.school.id}, headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_setup_requires_csrf(self):
        self.assertEqual(self.client.post('/api/collection/setup', json={
            'since_date': '2024-06-15', 'interval': 30, 'ai_mode': 'rules'}).status_code, 403)

    def test_tested_binding_is_reused_without_test_calls_or_key_exposure(self):
        from backend.database.db import db
        from backend.ai.models import AIProfile, AIBinding
        profile = AIProfile(name='既有配置', provider='deepseek', model='existing-model', region='default',
            encrypted_key='never-render-this-secret', version=1, tested_version=1, enabled=True)
        db.session.add(profile); db.session.flush()
        db.session.add(AIBinding(purpose='directory', profile_id=profile.id)); db.session.commit()
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid test')):
            page = self.client.get('/collection/setup?school_id=' + str(self.school.id))
            result = self.client.post('/api/collection/setup', json={
                'since_date':'2024-06-15', 'interval':30, 'ai_mode':'ai'}, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.json)
        self.assertIn('既有配置', page.text)
        self.assertNotIn('never-render-this-secret', page.text)

    def test_source_ui_and_catalog_share_setup_and_escape_school_names(self):
        from backend.database.db import db
        self.school.name = '<img src=x onerror=alert(1)>'
        db.session.commit()
        page = self.client.get('/collection/setup?school_id=' + str(self.school.id))
        self.assertNotIn('<img src=x', page.text)
        self.assertIn('&lt;img', page.text)
        self.assertEqual(self.client.get('/collection/setup?name=不存在的学校').status_code, 404)

    def test_non_admin_cannot_change_instance_settings(self):
        from backend.database.db import db
        from backend.database.models import User, AppConfig
        user = User(username='reader', password_hash='unused', role='user')
        db.session.add(user); db.session.commit()
        with patch('backend.auth.desktop.local_owner', return_value=user):
            page = self.client.get('/collection/setup?school_id=' + str(self.school.id))
            self.assertEqual(page.status_code, 200)
            self.assertNotIn('id="aiKey"', page.text)
            self.assertEqual(self.client.post('/api/collection/setup', json={
                'since_date':'2024-06-15', 'interval':5, 'ai_mode':'rules'}, headers=self.headers).status_code, 403)
            self.assertIsNone(AppConfig.get('scrape_interval'))
            self.assertEqual(self.client.post('/api/collection/setup', json={'ai_mode':'rules'},
                headers=self.headers).status_code, 403)
            AppConfig.set('collection_setup_completed', '1')
            self.assertEqual(self.client.post('/api/collection/setup', json={'ai_mode':'rules'},
                headers=self.headers).status_code, 200)

    def test_day_cutoff_is_applied_by_collection_and_keeps_undated_records(self):
        from backend.database.db import db
        from backend.database.models import Department, Announcement
        from backend.services.collection_settings import save_settings
        from backend.scraper.engine import scrape_department
        save_settings('2024-06-15', 45)
        source = Department(school_id=self.school.id, name='通知', list_url=self.school.url,
            list_selector='li', title_selector='a', link_selector='a', date_selector='time')
        db.session.add(source); db.session.commit()
        html = '<h2>通知</h2><ul>' + ''.join('<li><a href="/notice/' + key + '">' + title + '</a><time>' + stamp + '</time></li>'
            for key, title, stamp in [('before','之前的公开通知','2024-06-14'),
                ('boundary','当天发布的通知','2024-06-15'), ('after','之后发布的通知','2024-06-16'),
                ('undated','日期未知的办理通知','')]) + '</ul>'
        with patch('backend.scraper.engine._fetch_html', return_value=html), \
                patch('backend.scraper.engine.save_element_signatures'), \
                patch('backend.scraper.selector_monitor.evaluate_and_repair', return_value={'action':'healthy'}):
            scrape_department(source, self.school.url)
        self.assertEqual({row.title for row in Announcement.query.all()},
            {'当天发布的通知', '之后发布的通知', '日期未知的办理通知'})

    def test_new_school_is_only_created_after_settings_and_keeps_rules_choice(self):
        from backend.database.models import BackgroundTask, School, Subscription
        self.client.post('/api/collection/setup', json={
            'since_date':'2024-06-15', 'interval':45, 'ai_mode':'rules'}, headers=self.headers)
        with patch('backend.scraper.http_client.validate_public_url'):
            response = self.client.post('/api/schools', json={
                'name':'新建大学校区', 'url':'https://campus.example.edu.cn/'}, headers=self.headers)
        self.assertEqual(response.status_code, 201, response.json)
        created = School.query.filter_by(name='新建大学校区').one()
        self.assertEqual(Subscription.query.filter_by(school_id=created.id).count(), 1)
        self.assertFalse(BackgroundTask.query.filter_by(identity=f'discover:{created.id}').one().payload['ai_assist'])
