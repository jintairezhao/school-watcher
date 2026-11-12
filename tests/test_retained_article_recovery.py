"""Old failed task recovery must reuse complete recorded evidence and fee identity."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.database.source_governance_models import SourceValidation
from backend.services import tasks, source_governance as g
from tests import test_direct_onboarding as fixture_module

URL, HTML, ai_column = fixture_module.URL, fixture_module.HTML, fixture_module.ai_column


class RetainedArticleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.DirectOnboardingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def seed(self, *, stage='repair', full_record=True, suffix_conflict=False, paused=False, pending=False):
        from backend.services.source_agent import validation_feedback
        config = dict(ai_column(),list_url=URL,group_name='')
        ref = g._snapshot(HTML,URL)
        samples = [
            {'url':URL+'0.htm','matched':False,'reason':'needs_adapter','error_code':'response_too_large'},
            {'url':URL+'1.htm','matched':False,'reason':'requires_render','error_code':'readiness_missing'},
            {'url':URL+'2.htm','matched':True,'reason':'matched'},
        ]
        if suffix_conflict:
            samples.append({'url':URL+'3.htm','matched':False,'reason':'article_identity_conflict'})
        articles = dict(passed=False,sample_count=len(samples),available_count=len(samples),samples=samples)
        detail = dict(validation_version=12,list_snapshot=ref,articles=articles,status='rejected',errors=['article_sample_mismatch'])
        event = None
        if full_record:
            event = SourceValidation(school_id=self.fixture.school_id,config_hash=g._hash({f:config[f] for f in g.FIELDS}),
                validation_json=g._json(detail))
            db.session.add(event);db.session.flush()
        failure = dict(issue={'name':'通知公告','codes':['article_sample_mismatch'],'reason':'旧规则错误'},
            config=config,repairable=True,failure_kind='rule_error',trial_key='same-trial',
            observation=validation_feedback(config,['article_sample_mismatch'],articles=articles))
        job = tasks.enqueue('onboard','retained-body-recovery',{'school_id':self.fixture.school_id,'url':URL})
        job.checkpoint = dict(onboarding_page=ref,school_round='same-round',
            column_regions={'columns':[ai_column()],'decision_ids':['paid-original']},
            column_validation=dict(validation_version=12,page_hash=hashlib.sha256((URL+'\\0'+HTML).encode()).hexdigest(),
                stage=stage,trial_columns=[ai_column()],department_ids=[],completed=[],new_count=0,
                failures=[failure],progress={'decisions':1,'stopped':False},progress_ids=['paid-original'],
                trial_decision_ids=['paid-original'],trial_request_key='same-input',
                result={'state':'unsupported','department_ids':[],'new_count':0}))
        if pending:
            job.checkpoint=dict(job.checkpoint,column_pending_decision={'blocked':'usage','execution_id':'original-unknown'})
        if paused:
            job.payload=dict(job.payload,discovery_pause_requested=True)
        if stage=='done':
            job.state='done';job.finished_at=datetime.utcnow()
            job.result=deepcopy(job.checkpoint['column_validation']['result'])
        db.session.commit()
        return job,event

    def test_active_old_material_failure_does_not_repeat_reads_or_model(self):
        from backend.services.source_onboarding import onboard_page
        job,event=self.seed()
        before=deepcopy(job.checkpoint)
        handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle),patch('backend.services.source_onboarding.recognize_column',
                side_effect=AssertionError('No paid repair for retained material failure')):
            result=onboard_page(handle['payload'],fetcher=lambda *_:self.fail('No replacement website read'))
        self.assertEqual(result['state'],'material_limited')
        current=handle['checkpoint']
        self.assertEqual(current['school_round'],before['school_round'])
        self.assertEqual(current['column_regions'],before['column_regions'])
        self.assertEqual(current['column_validation']['progress'],before['column_validation']['progress'])
        self.assertEqual(current['column_validation']['trial_request_key'],'same-input')
        self.assertEqual(SourceValidation.query.count(),1)
        self.assertEqual(Department.query.count(),0)
        self.assertEqual(Announcement.query.count(),0)

    def test_terminal_old_result_is_reclassified_without_requeue_or_new_generation(self):
        from backend.services.source_onboarding import resume_rule_discovery,pending_onboarding_recovery
        job,event=self.seed(stage='done')
        before=(job.state,job.phase,job.generation,job.attempts,deepcopy(job.payload))
        with patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('No models')):
            resume_rule_discovery()
        self.assertEqual((job.state,job.phase,job.generation,job.attempts,job.payload),before)
        self.assertEqual(job.result['state'],'material_limited')
        self.assertEqual(job.checkpoint['column_validation']['result']['state'],'material_limited')
        self.assertFalse(pending_onboarding_recovery(self.fixture.school_id))

    def test_complete_record_suffix_identity_conflict_is_not_hidden_by_short_feedback(self):
        from backend.services.source_onboarding import resume_rule_discovery
        job,event=self.seed(stage='done',suffix_conflict=True)
        before=deepcopy(job.checkpoint)
        resume_rule_discovery()
        self.assertEqual(job.checkpoint,before)
        self.assertTrue(job.checkpoint['column_validation']['failures'][0]['repairable'])

    def test_missing_complete_record_holds_for_evidence_instead_of_buying_repair(self):
        from backend.services.source_onboarding import onboard_page
        job,event=self.seed(full_record=False)
        handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle),patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('No model for incomplete history')):
            result=onboard_page(handle['payload'],fetcher=lambda *_:self.fail('No website read'))
        self.assertEqual(result['state'],'material_limited')
        migration=handle['checkpoint']['column_validation']['article_failure_migrations'][-1]
        self.assertEqual(migration['basis'],'incomplete_retained_validation')
        self.assertTrue(result['retained_validation_review_required'])

    def test_missing_history_is_not_a_reason_to_automatically_fetch_again(self):
        from backend.services.source_onboarding import resume_rule_discovery,pending_onboarding_recovery
        job,event=self.seed(stage='done',full_record=False)
        resume_rule_discovery()
        self.assertTrue(job.result['retained_validation_review_required'])
        self.assertFalse(pending_onboarding_recovery(self.fixture.school_id))

    def test_completed_neighbors_and_existing_source_are_preserved(self):
        from backend.services.source_onboarding import resume_rule_discovery
        existing=Department(id=77,school_id=self.fixture.school_id,kind='column',name='已接入的相邻栏目',list_url=URL+'existing/')
        db.session.add(existing);db.session.commit()
        job,event=self.seed(stage='done')
        checkpoint=deepcopy(job.checkpoint)
        checkpoint['column_validation'].update(department_ids=[77],completed=['existing'],new_count=1)
        checkpoint['column_validation']['result'].update(state='connected',department_ids=[77],new_count=1)
        job.checkpoint=checkpoint;job.result=deepcopy(checkpoint['column_validation']['result']);db.session.commit()
        before=g.source_config(existing)
        resume_rule_discovery()
        self.assertEqual(job.result['state'],'connected')
        self.assertEqual(job.result['department_ids'],[77])
        self.assertEqual(job.result['new_count'],1)
        self.assertEqual(g.source_config(db.session.get(Department,77)),before)

    def test_explicit_pause_and_unknown_usage_keep_the_original_checkpoint(self):
        from backend.services.source_onboarding import resume_rule_discovery
        for paused,pending in ((True,False),(False,True)):
            with self.subTest(paused=paused,pending=pending):
                job,event=self.seed(stage='done',paused=paused,pending=pending)
                before=deepcopy(job.checkpoint)
                resume_rule_discovery()
                self.assertEqual(job.checkpoint,before)
                db.session.delete(job);db.session.commit()
