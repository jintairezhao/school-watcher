"""Correct old pager-generated rules using retained material and the same paid task."""
from copy import deepcopy
from datetime import datetime
import hashlib
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Department,Subscription,Announcement,UserRead,UserAnnouncementState
from backend.services import tasks

URL='https://example.edu.cn/notices/'
TITLES=['关于本学期面向全体学生组织专业实验课程补选报名及教学安排调整的通知'+str(i) for i in range(3)]
HTML='<section id="notices"><h2>通知公告</h2><table id="rows">'+''.join(
    '<tr><td><a href="/info/'+str(i)+'.htm">'+title+'</a></td><td><span>2026-10-01</span></td></tr>'
    for i,title in enumerate(TITLES))+'<tr><td colspan="2">共3条 1/1 首页 上页 下页 尾页</td></tr></table></section>'


class PagerRuleRecoveryTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture=DirectOnboardingTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.no_model=patch('backend.ai.providers.complete',side_effect=AssertionError('No new model call'))
        self.no_model.start();self.addCleanup(self.no_model.stop)

    def retained(self, *, state='done', html=HTML):
        from backend.services.source_governance import _snapshot
        column=dict(name='通知公告',list_selector='#rows > tbody > tr',title_selector='a',
            link_selector='a',date_selector='span',content_selector='',
            container_selector='#notices',heading_selector='#notices h2')
        failure=dict(issue=dict(name='通知公告',reason='旧列表选择器匹配为空',codes=['list_missing']),repairable=True,
            config=dict(column,list_url=URL,group_name=''),observation=dict(column,codes=['list_missing']))
        job=tasks.enqueue('onboard','retained-generated-pager',dict(school_id=self.fixture.school_id,url=URL,ai_assist=True))
        job.checkpoint=dict(onboarding_page=_snapshot(html,URL,role='list'),school_round='original-round',
            column_regions=dict(columns=[column],decision_ids=['original-paid-decision'],cursor=1),
            column_validation=dict(validation_version=8,stage='done' if state=='done' else 'repair',
                page_hash=hashlib.sha256((URL+r'\0'+html).encode()).hexdigest(),
                department_ids=[],new_count=0,completed=[],failures=[failure],
                trial_columns=[column],trial_origin_stage='repair',trial_decision_ids=['original-paid-decision'],
                trial_request_key='old-input',progress=dict(stopped=True),progress_ids=['old-evidence'],
                result=dict(state='unsupported',department_ids=[])))
        job.state=state;job.phase='complete' if state=='done' else 'ai_budget'
        job.error_code='' if state=='done' else 'budget_exhausted'
        job.result=dict(state='unsupported');job.finished_at=datetime.utcnow()
        db.session.commit();return job

    def ledger(self):
        from tests.test_pagination_checkpoint_upgrade import PaginationCheckpointUpgradeTests
        helper=PaginationCheckpointUpgradeTests();helper.fixture=self.fixture
        return helper,helper.paid_ledger()

    def wake(self, job):
        from backend.services.source_onboarding import _resume_retained_validation
        return _resume_retained_validation(job,datetime.utcnow())

    def read_article(self, url, purpose):
        self.assertNotEqual(url,URL,'Use the already retained list instead of fetching it again')
        i=int(url.rsplit('/',1)[-1].split('.')[0])
        return '<article><h1>'+TITLES[i]+'</h1><p>发布时间：2026-10-01</p><p>请各学院按照学校公布的工作安排组织开展相关教学活动，具体事项和办理时间详见附件。</p></article>'

    def test_completed_failed_rule_replays_without_replacing_paid_cursor(self):
        from backend.services.source_onboarding import onboard_page
        job=self.retained();old=deepcopy(job.checkpoint);identity,generation=job.identity,job.generation
        ledger,before=self.ledger();subscription=Subscription.query.one()
        existing=Department(school_id=self.fixture.school_id,name='原有栏目',list_url=URL+'old/',list_selector='li')
        db.session.add(existing);db.session.flush();subscription.department_ids=[existing.id]
        notice=Announcement(school_id=self.fixture.school_id,department_id=existing.id,
            title='保留的旧通知',url=URL+'old/item.htm',content_html='旧正文')
        db.session.add(notice);db.session.flush()
        db.session.add_all([UserRead(user_id=subscription.user_id,announcement_id=notice.id),
            UserAnnouncementState(user_id=subscription.user_id,announcement_id=notice.id,starred=True)])
        db.session.commit();sub_ids=deepcopy(subscription.department_ids)
        existing_identity=(existing.id,existing.name,existing.list_url,existing.list_selector)
        with patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('No paid re-recognition')):
            self.assertTrue(self.wake(job))
            handle=tasks.claim(capabilities=['http']);self.assertEqual(handle['id'],job.id)
            with tasks.execution_scope(handle):result=onboard_page(handle['payload'],fetcher=self.read_article)
            tasks.finish(handle,result)
        self.assertEqual(result['state'],'connected',result)
        self.assertEqual((job.identity,job.generation),(identity,generation))
        self.assertEqual(ledger.ledger(),before)
        self.assertEqual(job.checkpoint['column_regions'],old['column_regions'])
        self.assertEqual(job.checkpoint['school_round'],old['school_round'])
        self.assertEqual(job.checkpoint['column_validation']['trial_decision_ids'],['original-paid-decision'])
        self.assertEqual(Subscription.query.one().department_ids,sub_ids)
        self.assertEqual(Department.query.filter_by(kind='column').count(),2)
        self.assertEqual((existing.id,existing.name,existing.list_url,existing.list_selector),existing_identity)
        self.assertEqual(UserRead.query.count(),1);self.assertTrue(UserAnnouncementState.query.one().starred)
        self.assertEqual(notice.content_html,'旧正文')

    def test_budget_waiter_resumes_only_the_proved_unpaid_rule_trial(self):
        job=self.retained(state='waiting');ledger,before=self.ledger();original=deepcopy(job.checkpoint)
        self.assertTrue(self.wake(job))
        self.assertEqual((job.state,job.phase,job.generation),('pending','validation_replay',1))
        self.assertEqual(job.checkpoint,original);self.assertEqual(ledger.ledger(),before)

    def test_missing_bodies_keep_corrected_trial_and_paid_ledger_for_resume(self):
        from backend.services.source_onboarding import onboard_page
        from backend.scraper.acquisition import FetchFailure,FetchResult
        job=self.retained();ledger,before=self.ledger();self.assertTrue(self.wake(job))
        handle=tasks.claim(capabilities=['http'])
        def missing(url,purpose):
            self.assertNotEqual(url,URL)
            raise FetchFailure(FetchResult(url,outcome='network_error',error_code='http_timeout'))
        with tasks.execution_scope(handle),patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('No model')):
            with self.assertRaises(FetchFailure) as raised:onboard_page(handle['payload'],fetcher=missing)
        tasks.finish(handle,error=raised.exception);db.session.refresh(job)
        self.assertEqual(job.checkpoint['column_validation']['stage'],'trial')
        self.assertTrue(job.checkpoint['column_validation']['validation_replay']['columns'])
        self.assertEqual(Department.query.filter_by(kind='column').count(),0)
        self.assertEqual(ledger.ledger(),before)
        job.available_at=datetime.utcnow();db.session.commit()
        resumed=tasks.claim(capabilities=['http']);self.assertEqual(resumed['id'],job.id)
        with tasks.execution_scope(resumed),patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('Keep the saved replay')):
            result=onboard_page(resumed['payload'],fetcher=self.read_article)
        tasks.finish(resumed,result)
        self.assertEqual(result['state'],'connected');self.assertEqual(job.generation,1)
        self.assertEqual(ledger.ledger(),before)

    def test_changed_material_does_not_reopen_saved_failure(self):
        job=self.retained();value=deepcopy(job.checkpoint);value['column_validation']['page_hash']='wrong'
        job.checkpoint=value;db.session.commit();self.assertFalse(self.wake(job))

    def test_ambiguous_same_named_regions_do_not_reopen_saved_failure(self):
        job=self.retained(html=HTML+HTML);self.assertFalse(self.wake(job))

    def test_pause_and_unknown_usage_remain_parked(self):
        job=self.retained(state='waiting');original=deepcopy(job.checkpoint)
        job.payload={**job.payload,'discovery_pause_requested':True};db.session.commit()
        self.assertFalse(self.wake(job))
        job.payload={**job.payload,'discovery_pause_requested':False}
        job.checkpoint={**original,'column_pending_decision':{'blocked':'usage'}};db.session.commit()
        self.assertFalse(self.wake(job));self.assertEqual(job.state,'waiting')

    def test_current_version_failure_is_not_reopened_forever(self):
        job=self.retained();value=deepcopy(job.checkpoint);value['column_validation']['validation_version']=9
        job.checkpoint=value;db.session.commit();self.assertFalse(self.wake(job))

    def test_malformed_retained_selector_does_not_crash_recovery(self):
        job=self.retained();value=deepcopy(job.checkpoint)
        value['column_validation']['failures'][0]['config']['list_selector']='['
        job.checkpoint=value;db.session.commit();self.assertFalse(self.wake(job))

    def test_unrelated_failure_survives_the_specific_pager_replay(self):
        from backend.services.source_onboarding import onboard_page
        job=self.retained();value=deepcopy(job.checkpoint)
        other=deepcopy(value['column_validation']['failures'][0])
        other['issue']['name']='未确认栏目';other['config']['name']='未确认栏目'
        other['repairable']=False;other['failure_kind']='access_restricted'
        value['column_validation']['failures'].append(other);job.checkpoint=value;db.session.commit()
        self.assertTrue(self.wake(job));handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle),patch('backend.services.source_onboarding.recognize_column',side_effect=AssertionError('No model')):
            result=onboard_page(handle['payload'],fetcher=self.read_article)
        tasks.finish(handle,result)
        self.assertEqual(result['state'],'connected')
        self.assertIn('未确认栏目',[x['name'] for x in result['issues']])

    def test_same_named_pager_correction_cannot_switch_to_another_region(self):
        pager='<tr><td colspan="2">共3条 1/1 首页 上页 下页 尾页</td></tr>'
        other=HTML.replace('id="notices"','id="other"').replace('id="rows"','id="other-rows"').replace('/info/','/info/20')
        html='<div id="whole">'+HTML.replace(pager,'')+other+'</div>'
        job=self.retained(html=html);value=deepcopy(job.checkpoint)
        value['column_validation']['failures'][0]['config']['container_selector']='#whole'
        job.checkpoint=value;db.session.commit();self.assertFalse(self.wake(job))

    def test_original_generated_rows_replay_only_with_the_same_concrete_parent(self):
        job=self.retained();value=deepcopy(job.checkpoint);failure=value['column_validation']['failures'][0]
        failure['config']['list_selector']='#rows > tr';failure['issue']['codes']=['invalid_list_item']
        job.checkpoint=value;db.session.commit();self.assertTrue(self.wake(job))
