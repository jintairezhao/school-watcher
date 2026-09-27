"""Admin permissions and persisted verification state in an isolated temporary database."""
from datetime import datetime, timedelta
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import VerificationSession, User
from backend.services.browser_sessions import BrowserSessionError


class BrowserVerificationRoutesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'verify-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'main.db'),
            'SOURCE_CATALOG_PATH': str(root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(root / 'discovery.db'),
            'STORAGE_ROOT': str(root), 'BACKUP_DIR': str(root / 'backups'), 'BACKUP_COPY_DIR': ''})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        admin = User(username='administrator', password_hash='test', role='admin')
        reader = User(username='reader', password_hash='test')
        db.session.add_all([admin, reader]); db.session.flush()
        self.admin_id, self.reader_id = admin.id, reader.id
        self.row = VerificationSession(id='verification', source_id='7', origin='https://example.edu',
            url='https://example.edu/notices', generation='private-generation', status='required',
            request_payload={'purpose': 'article', 'readiness_selector': '.article-content'})
        db.session.add(self.row); db.session.commit()
        self.client = self.as_user(self.admin_id)
        self.headers = {'X-CSRF-Token': 'test-token'}

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def as_user(self, ident):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session.update(user_id=ident, _csrf_token='test-token')
        return client

    def endpoint(self, suffix=''):
        return '/api/admin/browser-sessions/verification' + suffix

    def test_readers_and_anonymous_cannot_access_verification(self):
        self.assertEqual(self.as_user(self.reader_id).get('/api/admin/browser-sessions').status_code, 403)
        self.assertEqual(self.app.test_client().get('/api/admin/browser-sessions').status_code, 401)
        self.assertEqual(self.as_user(self.reader_id).get('/api/admin/browser-access/auth').status_code, 403)

    def test_csrf_required_and_secrets_never_serialized(self):
        self.assertEqual(self.client.post(self.endpoint('/open')).status_code, 403)
        data = self.client.get('/api/admin/browser-sessions').get_json()
        self.assertNotIn('private-generation', str(data))
        self.assertNotIn('request_payload', str(data))
        self.assertEqual(self.client.get('/admin/access-verification').status_code, 200)

    def test_open_preserves_validated_source_purpose_and_policy(self):
        with patch('backend.services.browser_sessions.runtime_request', return_value={
            'runtime_id': 'runtime-one', 'state': 'active', 'remote': False, 'remaining_seconds': 599}) as call:
            response = self.client.post(self.endpoint('/open'), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(call.call_args.args[2]['purpose'], 'article')
        self.assertEqual(call.call_args.args[2]['readiness_selector'], '.article-content')
        self.assertNotIn('generation', response.get_json())
        db.session.refresh(self.row)
        self.assertEqual(self.row.status, 'active')

    def test_concurrent_opening_cannot_replace_generation(self):
        self.row.status = 'opening'; self.row.expires_at = datetime.utcnow() + timedelta(minutes=5)
        db.session.commit()
        with patch('backend.services.browser_sessions.runtime_request') as call:
            response = self.client.post(self.endpoint('/open'), headers=self.headers)
        self.assertEqual(response.status_code, 409)
        call.assert_not_called()
        self.assertEqual(self.row.generation, 'private-generation')

    def test_runtime_restart_expires_active_link(self):
        self.row.status = 'active'; self.row.runtime_id = 'runtime-one'
        self.row.expires_at = datetime.utcnow() + timedelta(minutes=5); db.session.commit()
        with patch('backend.services.browser_sessions.runtime_request', side_effect=BrowserSessionError(
                '已关闭', 404, 'session_missing')):
            response = self.client.get(self.endpoint())
        self.assertEqual(response.get_json()['status'], 'expired')

    def test_only_successful_browser_validation_resumes_source(self):
        self.row.status = 'active'; self.row.runtime_id = 'runtime-one'
        self.row.expires_at = datetime.utcnow() + timedelta(minutes=5); db.session.commit()
        with patch('backend.services.browser_sessions.runtime_request', side_effect=[
                {'runtime_id': 'runtime-one', 'state': 'active', 'remote': False, 'remaining_seconds': 99},
                {'state': 'verified'}]), patch('backend.services.tasks.resume_verification') as resume:
            response = self.client.post(self.endpoint('/verify'), headers=self.headers)
        self.assertEqual(response.status_code, 200)
        resume.assert_called_once_with('7')


if __name__ == '__main__': unittest.main()
