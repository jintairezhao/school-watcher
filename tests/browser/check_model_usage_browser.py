"""Actual local runtime invoice -> retained source ancestry -> bilingual graph."""
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests'),str(Path(__file__).parent)]
from check_source_execution_browser import check
from test_execution_observations import ExecutionObservationTests
from backend.ai.models import AIExecution


class ModelUsageFixture(ExecutionObservationTests):
    def setUp(self):
        super().setUp();self.app=self.f.app

    def connect(self):
        self.run_review()
        self.invoice_created=True
        return self.f.connect()

    def doCleanups(self):
        try:
            if getattr(self,'invoice_created',False):
                assert AIExecution.query.count()==1,'Read-only graph must not create AI executions'
                assert AIExecution.query.one().usage['total_tokens']==70
        finally:cleaned=super().doCleanups()
        return cleaned


if __name__=='__main__':
    check(ModelUsageFixture,ROOT/'.local/structure-facts-20261021/model-use-browser',
        expected_event_text='Confirmed usage: 70 tokens',expected_zh_event_text='已确认用量：70 Token')
