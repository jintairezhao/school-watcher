"""A real heading remains usable when navigation repeats its literal name."""
from pathlib import Path
from copy import deepcopy
from datetime import datetime
import hashlib
import unittest
from unittest.mock import patch

from backend.services.source_governance import _column_scope
from backend.services.source_scope import recover_observed_scope

URL='https://grs.ruc.edu.cn/xwgl/zysz.htm'
CONFIG=dict(name='专业设置',list_url=URL,list_selector='ul.m-txtlist > li',title_selector='h4',
 link_selector='a',date_selector='span.date',content_selector='',group_name='')
WRONG=dict(container_selector='ul.m-txtlist',heading_selector='h4')

class SemanticScopeRecoveryTests(unittest.TestCase):
 def setUp(self):
  self.html=(Path(__file__).parent/'fixtures/acquisition/retained-ruc-grs-degree-roster.html').read_text('utf-8')

 def test_real_title_sidebar_and_breadcrumb_do_not_need_another_paid_rule(self):
  config=deepcopy(CONFIG)
  records,errors,_=_column_scope(config,{'url':URL},WRONG,html=self.html)
  self.assertEqual(errors,['column_identity_or_scope_unconfirmed'])
  self.assertEqual(len(records),2)
  fixed=recover_observed_scope(config,{'url':URL},WRONG,html=self.html)
  self.assertIsNotNone(fixed)
  after,errors,region=_column_scope(config,{'url':URL},fixed['scope'],html=self.html)
  self.assertEqual(errors,[]);self.assertEqual(after,records);self.assertEqual(region['name'],'专业设置')
  self.assertEqual(config,CONFIG)

 def test_two_actual_same_named_sections_remain_ambiguous(self):
  html=self.html.replace('</body>','<section><h3>专业设置</h3><p>其他独立区域</p></section></body>')
  self.assertIsNone(recover_observed_scope(CONFIG,{'url':URL},WRONG,html=html))

 def test_another_publication_region_cannot_be_hidden_as_navigation(self):
  html=self.html.replace('<ul class="m-txtlist">','<div>另一个栏目</div><ul class="m-txtlist">')
  self.assertIsNone(recover_observed_scope(CONFIG,{'url':URL},WRONG,html=html))

 def test_only_a_subset_of_the_actual_list_cannot_be_repaired(self):
  config=dict(CONFIG,list_selector='ul.m-txtlist > li:first-child')
  self.assertIsNone(recover_observed_scope(config,{'url':URL},WRONG,html=self.html))

 def test_a_page_class_does_not_hide_another_visible_column_name(self):
  html=self.html.replace('<span class="p_no_d">1</span>','<span class="p_no_d">其他栏目</span>')
  self.assertIsNone(recover_observed_scope(CONFIG,{'url':URL},WRONG,html=html))

class SemanticScopeUpgradeTests(unittest.TestCase):
 def setUp(self):
  from tests.test_direct_onboarding import DirectOnboardingTests
  from backend.database.db import db
  from backend.database.models import School
  from backend.services.source_governance import _snapshot
  from backend.services.discovery_policy import progress_state
  from backend.services import tasks
  self.fixture=DirectOnboardingTests();self.fixture.setUp()
  db.session.get(School,self.fixture.school_id).url='https://www.ruc.edu.cn/'
  self.html=(Path(__file__).parent/'fixtures/acquisition/retained-ruc-grs-degree-roster.html').read_text('utf-8')
  self.column=dict(CONFIG,**WRONG)
  self.job=tasks.enqueue('onboard','retained-semantic-scope',{'school_id':self.fixture.school_id,'url':URL})
  self.job.checkpoint={'onboarding_page':_snapshot(self.html,URL,role='list'),'column_validation':{
   'validation_version':7,'stage':'repair','page_hash':hashlib.sha256((URL+r'\0'+self.html).encode()).hexdigest(),
   'failures':[{'issue':{'name':'专业设置','codes':['column_identity_or_scope_unconfirmed']},
    'config':self.column,'repairable':True,'observation':self.column}],
   'trial_columns':[self.column],'department_ids':[],'completed':[],'new_count':0,
   'progress':progress_state(),'progress_ids':[],'material_pending':False}}
  self.job.state,self.job.phase,self.job.error_code='waiting','ai_budget','budget_exhausted'
  db.session.commit()

 def tearDown(self):self.fixture.tearDown()

 def test_proved_old_rule_wakes_under_budget_pause_and_keeps_paid_history(self):
  from backend.database.db import db
  from backend.services import tasks
  from backend.services.source_governance import _extract
  from backend.services.source_onboarding import resume_rule_discovery,onboard_page
  from tests.test_pagination_checkpoint_upgrade import PaginationCheckpointUpgradeTests
  ledger=PaginationCheckpointUpgradeTests();ledger.fixture=self.fixture;before=ledger.paid_ledger()
  generation=self.job.generation
  records=_extract(CONFIG,{'url':URL},html=self.html)[2]
  def fetch(url,purpose):
   if url==URL:return self.html
   row=next((r for r in records if r['url']==url),None)
   self.assertIsNotNone(row,url)
   return f'<article><h1>{row["title"]}</h1><p>发布时间：{row["date"]}</p><p>请各学院按照研究生培养管理要求核对本学期学科设置和专业学位信息，具体办理安排详见说明。</p></article>'
  with patch('backend.ai.runtime.run_skill',side_effect=AssertionError('Retained rule must not buy another decision')):
   resume_rule_discovery();db.session.refresh(self.job)
   self.assertEqual((self.job.state,self.job.phase),('pending','validation_replay'))
   handle=tasks.claim(capabilities=['http']);self.assertEqual(handle['id'],self.job.id)
   with tasks.execution_scope(handle):result=onboard_page(handle['payload'],fetcher=fetch)
   tasks.finish(handle,result)
  self.assertEqual(result['state'],'connected',result)
  self.assertEqual(self.job.generation,generation);self.assertEqual(ledger.ledger(),before)

 def test_unknown_paid_decision_still_stays_parked(self):
  from backend.database.db import db
  from backend.services.source_onboarding import _resume_retained_validation
  self.job.checkpoint={**self.job.checkpoint,'column_pending_decision':{'blocked':'usage'}}
  db.session.commit()
  self.assertFalse(_resume_retained_validation(self.job,datetime.utcnow()))
  self.assertEqual(self.job.phase,'ai_budget')

 def test_unproved_scope_does_not_wake_a_budget_waiter(self):
  from backend.database.db import db
  from backend.services.source_onboarding import _resume_retained_validation
  value=deepcopy(self.job.checkpoint);value['column_validation']['page_hash']='wrong'
  self.job.checkpoint=value;db.session.commit()
  self.assertFalse(_resume_retained_validation(self.job,datetime.utcnow()))
  self.assertEqual(self.job.phase,'ai_budget')

 def test_known_rule_replay_keeps_unfinished_other_material(self):
  from backend.database.db import db
  from backend.services.source_onboarding import _resume_retained_validation
  value=deepcopy(self.job.checkpoint);value['column_validation']['material_pending']=True
  self.job.checkpoint=value;db.session.commit()
  self.assertTrue(_resume_retained_validation(self.job,datetime.utcnow()))
  self.assertTrue(self.job.checkpoint['column_validation']['material_pending'])

if __name__=='__main__':unittest.main()
