"""Offline crash windows: only a claimed dispatch can consume request budget."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from backend.ai import runtime
from backend.ai.models import AIExecution, AIBudget
from backend.database.db import db


class AIBudgetRecoveryTests(unittest.TestCase):
    def setUp(self):
        from backend.database import source_governance_models
        from tests.test_ai_runtime import AIRuntimeTests
        self.fixture = AIRuntimeTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def reserve(self, ident):
        return runtime._reserve(ident, 'directory', self.fixture.binding,
            'university-source-onboarding', '1', 'digest', ident, 'column', 100, school_id=3)[0]

    def test_crash_before_dispatch_releases_tokens_requests_and_active_slots_once(self):
        row = self.reserve('not-sent')
        row.created_at = datetime.utcnow() - timedelta(minutes=5)
        db.session.commit()
        self.assertEqual(runtime.recover_uncertain_executions(), 1)
        row = db.session.get(AIExecution, 'not-sent', populate_existing=True)
        self.assertEqual(row.status, 'failed')
        self.assertFalse(row.request_sent)
        self.assertEqual(row.usage['total_tokens'], 0)
        for budget in AIBudget.query.all():
            self.assertEqual((budget.used_tokens, budget.reserved_tokens,
                budget.used_requests, budget.reserved_requests, budget.active_count), (0, 0, 0, 0, 0))
        self.assertEqual(runtime.recover_uncertain_executions(), 0)
        self.assertIsNotNone(self.reserve('another-operation'))

    def test_unexpected_failure_before_dispatch_is_known_zero_spend(self):
        with patch.object(runtime, 'credential_for', side_effect=['fixture-key', RuntimeError('storage failure')]), \
             patch('backend.ai.providers.complete') as provider:
            result = self.fixture.invoke('credential-failure')
        provider.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['usage']['total_tokens'], 0)
        self.assertTrue(all(b.reserved_tokens == b.used_tokens == b.used_requests ==
            b.reserved_requests == b.active_count == 0 for b in AIBudget.query.all()))

    def test_recovery_fences_worker_that_resumes_before_provider_dispatch(self):
        calls = 0
        def credential(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                row = db.session.get(AIExecution, 'resumed-worker')
                row.created_at = datetime.utcnow() - timedelta(minutes=5)
                db.session.commit()
                self.assertEqual(runtime.recover_uncertain_executions(), 1)
            return 'fixture-key'
        with patch.object(runtime, 'credential_for', side_effect=credential), \
             patch('backend.ai.providers.complete', return_value=self.fixture.response()) as provider:
            result = self.fixture.invoke('resumed-worker')
        provider.assert_not_called()
        self.assertEqual(result['status'], 'failed')
        self.assertFalse(db.session.get(AIExecution, 'resumed-worker').request_sent)
        self.assertTrue(all(b.used_requests == b.reserved_requests == b.reserved_tokens == 0
                            for b in AIBudget.query.all()))

    def test_source_review_without_school_payload_uses_proposal_school_budget(self):
        from backend.database.models import School
        from backend.database.source_governance_models import SourceProposal
        from backend.services import tasks
        from backend.ai.school_budget import school_usage
        db.session.add(School(id=3, name='Budget audit', url='https://example.edu.cn/'))
        proposal = SourceProposal(school_id=3, identity_key='identity', proposal_key='proposal',
            origin='test', candidate_json='{}', expected_config_hash='config', evidence_hash='evidence')
        db.session.add(proposal); db.session.commit()
        tasks.enqueue('source_review', proposal.id, {'proposal_id': proposal.id})
        handle = tasks.claim(capabilities=['directory'])
        self.assertNotIn('school_id', handle['payload'])
        with tasks.execution_scope(handle):
            row, _ = runtime._reserve('proposal-review', 'directory', self.fixture.binding,
                'university-source-onboarding', '1', 'digest', 'proposal', 'column', 100)
        self.assertIn(school_usage(3)['budget_key'], row.budget_keys)
        self.assertEqual(school_usage(3)['reserved_requests'], 1)

    def test_global_and_school_limits_are_shared_by_separate_processes(self):
        self.reserve('parent')
        code = '''import json,sys
from flask import Flask
from backend.database.db import db
from backend.ai.runtime import _reserve,AIBudgetError
app=Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI']=sys.argv[1]
db.init_app(app)
with app.app_context():
 try:
  _reserve(sys.argv[2],'directory',json.loads(sys.argv[3]),'university-source-onboarding',
           '1','digest',sys.argv[2],'column',100,school_id=int(sys.argv[4]))
  print('reserved')
 except AIBudgetError as exc:
  print(exc.code)
'''
        for ident, school, expected in [('same-school', 3, 'concurrency_limit'),
                                        ('other-school', 4, 'reserved'), ('third-school', 5, 'concurrency_limit')]:
            result = subprocess.run([sys.executable, '-c', code,
                self.fixture.app.config['SQLALCHEMY_DATABASE_URI'], ident,
                json.dumps(self.fixture.binding), str(school)], cwd=Path(__file__).resolve().parents[1],
                capture_output=True, text=True, check=True, timeout=20)
            self.assertEqual(result.stdout.strip(), expected)


if __name__ == '__main__':
    unittest.main()
