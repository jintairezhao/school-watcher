"""Two tabs retain their independent active scopes in a single durable pointer."""
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import AppConfig, BackgroundTask, Department, School, Subscription, User
from backend.services import inbox_refresh


class ConcurrentRefreshTests(unittest.TestCase):
    def test_simultaneous_first_refreshes_merge_without_losing_a_source(self):
        with tempfile.TemporaryDirectory(prefix='refresh-scope-') as folder:
            app = create_app({'TESTING': True, 'SECRET_KEY': 'refresh-race',
                'SOURCE_CATALOG_PATH': str(Path(folder) / 'catalog.db'),
                'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(folder) / 'test.db')})
            with app.app_context():
                db.create_all()
                school = School(name='并发测试学校', url='https://example.edu.cn/')
                user = User(username='two-tabs', password_hash='unused', role='admin')
                db.session.add_all([school, user]); db.session.flush()
                a = Department(school_id=school.id, name='甲学院', list_url='https://a.example.edu.cn/')
                b = Department(school_id=school.id, name='乙学院', list_url='https://b.example.edu.cn/')
                db.session.add_all([a, b]); db.session.flush()
                db.session.add(Subscription(user_id=user.id, school_id=school.id)); db.session.commit()
                school_id, user_id, a_id, b_id = school.id, user.id, a.id, b.id
            barrier = threading.Barrier(2)
            local = threading.local()
            original = inbox_refresh._read_pointer

            def collide(ident):
                result = original(ident)
                local.count = getattr(local, 'count', 0) + 1
                # First read is the pre-enqueue baseline; second is the CAS read.
                if local.count == 2:
                    barrier.wait(timeout=10)
                return result

            def post(ident):
                client = app.test_client()
                with client.session_transaction() as session:
                    session.update(user_id=user_id, _csrf_token='test')
                response = client.post('/api/inbox/refresh', json={'scope': 'current',
                    'school_id': school_id, 'department_ids': [ident]}, headers={'X-CSRF-Token': 'test'})
                return response.status_code

            try:
                with patch.object(inbox_refresh, '_read_pointer', side_effect=collide):
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        self.assertEqual(list(pool.map(post, [a_id, b_id])), [202, 202])
                with app.app_context():
                    pointer = json.loads(AppConfig.query.filter_by(key=f'inbox_refresh:user:{user_id}').one().value)
                    self.assertEqual(set(pointer['department_ids']), {a_id, b_id})
                    self.assertEqual(BackgroundTask.query.count(), 2)
                    self.assertEqual(inbox_refresh.restore_refresh_status(user_id)['active'], 2)
            finally:
                with app.app_context():
                    db.session.remove(); db.engine.dispose()


if __name__ == '__main__':
    unittest.main()
