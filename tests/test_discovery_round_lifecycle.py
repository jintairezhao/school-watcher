"""Only a completed round followed by observed redesign can renew its allowance."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import threading
import unittest

from backend.database.db import db
from backend.ai.models import AIBudget, AISchoolRound
from backend.ai.school_budget import school_budget_key


ROOT = 'https://example.edu.cn/'


class DiscoveryRoundLifecycleTests(unittest.TestCase):
    def setUp(self):
        from tests.test_ai_runtime import AIRuntimeTests
        from backend.database import source_governance_models
        from backend.database.models import School
        from backend.services.source_inventory import Inventory
        self.fixture = AIRuntimeTests(); self.fixture.setUp()
        self.fixture.app.config['DISCOVERY_CACHE_PATH'] = str(Path(self.fixture.tmp.name) / 'inventory.db')
        db.session.add(School(id=3, name='测试大学', url=ROOT)); db.session.commit()
        self.store = Inventory(self.fixture.app.config['DISCOVERY_CACHE_PATH'])
        self.site = self.store.ensure_site('测试大学', ROOT)
        self.store.finish(self.site, ROOT, state='fetched', html='<nav>旧入口</nav>',
            notes_json=json.dumps(['discovery_shape:' + json.dumps({'hash': 'old-shape'})]))
        self.original_hash = self.store.get_page(self.site, ROOT)['content_hash']
        self.key = school_budget_key(3)
        db.session.add(AIBudget(key=self.key, used_requests=40, used_tokens=200000)); db.session.commit()

    def tearDown(self):
        self.fixture.tearDown()

    def test_new_material_does_not_reset_unfinished_or_budget_paused_round(self):
        from backend.services import tasks
        from backend.services.discovery_round_lifecycle import mark_completed, observe_structure
        self.assertFalse(observe_structure(3, ROOT, 'new-shape', 'new-html'))
        job = tasks.enqueue('navigation_review', 'paused', {'school_id': 3})
        job.state, job.phase = 'waiting', 'ai_budget'; db.session.commit()
        self.assertFalse(mark_completed(3))
        self.assertFalse(observe_structure(3, ROOT, 'new-shape', 'new-html'))
        self.assertEqual(school_budget_key(3), self.key)

    def test_restart_same_shape_or_only_code_change_keeps_completed_budget(self):
        from backend.services.discovery_round_lifecycle import mark_completed, observe_structure
        self.assertTrue(mark_completed(3))
        self.assertFalse(observe_structure(3, ROOT, 'old-shape', 'new-article-html'))
        self.assertFalse(observe_structure(3, ROOT, 'new-policy-shape', self.original_hash))
        self.assertEqual(school_budget_key(3), self.key)
        self.assertTrue(observe_structure(3, ROOT, 'new-shape', 'new-html'))
        self.assertNotEqual(school_budget_key(3), self.key)
        self.assertEqual(db.session.get(AIBudget, self.key).used_requests, 40)
        self.assertIsNone(db.session.get(AISchoolRound, 3).completed_at)

    def test_open_frontier_active_reservation_and_old_child_prevent_completion(self):
        from backend.services import tasks
        from backend.services.discovery_round_lifecycle import mark_completed
        self.store.enqueue(self.site, ROOT + 'later/', '未读入口', 'unit', 1, [], 'school_domain')
        self.assertFalse(mark_completed(3))
        self.store.finish(self.site, ROOT + 'later/', state='reference_only')
        budget = db.session.get(AIBudget, self.key); budget.reserved_tokens = 1; db.session.commit()
        self.assertFalse(mark_completed(3))
        budget.reserved_tokens = 0; db.session.commit()
        job = tasks.enqueue('navigation_review', 'child', {'school_id': 3})
        self.assertFalse(mark_completed(3))
        job.state = 'done'; db.session.commit()
        self.assertTrue(mark_completed(3))

    def test_finishing_last_child_marks_completed_but_parent_alone_does_not(self):
        from backend.services import tasks
        parent = tasks.enqueue('discover', 3, {'school_id': 3})
        child = tasks.enqueue('navigation_review', 'child', {'school_id': 3})
        handle = tasks.claim(capabilities=['directory']); tasks.finish(handle, {})
        self.assertIsNone(db.session.get(AISchoolRound, 3).completed_at)
        handle = tasks.claim(capabilities=['directory']); tasks.finish(handle, {})
        self.assertIsNotNone(db.session.get(AISchoolRound, 3).completed_at)

    def test_two_school_workers_can_open_only_one_new_round(self):
        from backend.services.discovery_round_lifecycle import mark_completed, observe_structure
        self.assertTrue(mark_completed(3))
        db.session.remove()
        gate = threading.Barrier(2)
        def worker():
            with self.fixture.app.app_context():
                gate.wait(timeout=5)
                changed = observe_structure(3, ROOT, 'new-shape', 'new-html')
                key = school_budget_key(3)
                db.session.remove()
                return changed, key
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        self.assertEqual(sum(changed for changed, _ in results), 1)
        self.assertEqual(len({key for _, key in results}), 1)
        self.assertNotEqual(results[0][1], self.key)
        self.assertEqual(db.session.get(AIBudget, self.key).used_tokens, 200000)

    def test_failed_or_blocked_network_page_keeps_round_open_without_child_task(self):
        from backend.services.discovery_round_lifecycle import mark_completed
        self.store.enqueue(self.site, ROOT + 'later/', '未读入口', 'unit', 1, [], 'school_domain')
        for state, health, error in [('failed', 'unreachable', 'http_connecttimeout'),
                                     ('blocked', 'dynamic_content', 'access_verification_required'),
                                     ('blocked', 'unreachable', 'network_error')]:
            with self.subTest(state=state, error=error):
                self.store.finish(self.site, ROOT + 'later/', state=state, health=health, error=error)
                self.assertFalse(mark_completed(3))
        self.store.finish(self.site, ROOT + 'later/', state='reference_only', health='unverified',
                          error='document_reference_only')
        self.assertTrue(mark_completed(3))


if __name__ == '__main__':
    unittest.main()
