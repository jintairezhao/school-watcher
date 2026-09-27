"""Private admission, retention budget, and final-commit regression coverage."""
from datetime import datetime, timedelta
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from backend import create_app
from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask, Department, School, User
from backend.services import tasks
from backend.services.task_fetch import cache_store, prune_fetch_evidence
from backend.scraper.acquisition import FetchRequest, FetchResult


class RuntimeOperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(self.root / 'main.db'),
            'BROWSER_SERVICE_TOKEN': 't' * 64, 'FETCH_EVIDENCE_DIR': str(self.root / 'evidence'),
            'FETCH_EVIDENCE_BYTES': 2048})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        school = School(name='test', url='https://example.edu/')
        db.session.add(school); db.session.flush()
        dept = Department(school_id=school.id, name='notices', list_url=school.url)
        db.session.add(dept); db.session.commit()
        self.dept = dept.id
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def test_private_admission_authentication_and_shared_host_gate(self):
        endpoint = '/internal/browser-origin/reserve'
        data = {'url': 'https://example.edu/a', 'token': 'a' * 64, 'ttl': 10}
        self.assertEqual(self.client.post(endpoint, json=data).status_code, 401)
        headers = {'X-Watcher-Token': 't' * 64}
        with patch('backend.routes.browser_origin.validate_public_url'):
            first = self.client.post(endpoint, json=data, headers=headers)
            self.assertTrue(first.json['allowed'])
            self.assertTrue(self.client.post(endpoint, json=data, headers=headers).json['allowed'])
            second = self.client.post(endpoint, json={**data, 'token': 'b' * 64}, headers=headers)
            self.assertFalse(second.json['allowed'])
        self.assertEqual(self.client.post('/api/storage/cleanup', headers=headers, json={'mode': 'expired'}).status_code, 403)

    def test_metrics_are_admin_only_and_contain_queue_delay(self):
        user = User(username='operator', role='admin', password_hash='test')
        db.session.add(user); db.session.commit()
        self.assertEqual(self.client.get('/api/admin/runtime/metrics').status_code, 401)
        with self.client.session_transaction() as session:
            session['user_id'] = user.id
        tasks.enqueue('collect', self.dept, {'department_id': self.dept})
        response = self.client.get('/api/admin/runtime/metrics')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['tasks'][0]['count'], 1)
        self.assertGreaterEqual(response.json['oldest_pending_seconds'], 0)

    def test_zero_retention_disables_age_expiry_but_not_capacity(self):
        root = self.root / 'evidence'; root.mkdir()
        old = root / 'old.json'; old.write_text('retained')
        stamp = (datetime.utcnow() - timedelta(days=100)).timestamp()
        os.utime(old, (stamp, stamp))
        AppConfig.set('discovery_cache_days', '0')
        self.assertEqual(prune_fetch_evidence()['removed'], 0)
        (root / 'large.json').write_text('x' * 3000)
        self.assertGreater(prune_fetch_evidence()['removed'], 0)
        self.assertLessEqual(sum(p.stat().st_size for p in root.iterdir()), 2048)

    def test_active_checkpoint_evidence_never_exceeds_hard_limit(self):
        tasks.enqueue('collect', self.dept, {'department_id': self.dept})
        handle = tasks.claim()
        with tasks.execution_scope(handle):
            for index in range(10):
                request = FetchRequest(url=f'https://example.edu/{index}', purpose='list')
                cache_store(request, FetchResult(request.url, html='x' * 500, outcome='usable'))
        paths = list((self.root / 'evidence').glob('*.json'))
        self.assertTrue(paths)
        self.assertLessEqual(sum(p.stat().st_size for p in paths), 2048)
        self.assertEqual(prune_fetch_evidence(all_cache=True)['removed'], 0)

    def test_finish_rejects_changed_profile_before_business_commit(self):
        task = tasks.enqueue('collect', self.dept, {'department_id': self.dept})
        handle = tasks.claim()
        handle['policy_validator'] = lambda: False
        db.session.get(Department, self.dept).name = 'stale write'
        with self.assertRaises(tasks.PolicyChanged):
            tasks.finish(handle)
        self.assertEqual(db.session.get(Department, self.dept).name, 'notices')
        self.assertEqual(db.session.get(BackgroundTask, task.id).state, 'running')
