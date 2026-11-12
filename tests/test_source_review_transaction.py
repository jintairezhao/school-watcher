"""A review request and its continuation task are one committed operation."""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import OperationalError

from backend import create_app
from backend.database.db import db
from backend.database.models import BackgroundTask, Department, School, User
from backend.database.source_governance_models import SourceProposal, SourceReviewEvent, SourceConfigVersion
from backend.services.source_governance import source_config
from backend.services.structure_versions import current_version


class SourceReviewTransactionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='review-transaction-')
        root=Path(self.tmp.name)
        self.app=create_app(dict(TESTING=True,PROPAGATE_EXCEPTIONS=False,SECRET_KEY='review-transaction',
            SQLALCHEMY_DATABASE_URI='sqlite://',SOURCE_CATALOG_PATH=str(root/'catalog.sqlite3'),
            SOURCE_GOVERNANCE_EVIDENCE_PATH=str(root/'evidence'),DISCOVERY_CACHE_PATH=str(root/'discovery.sqlite3')))
        self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.school=School(name='测试大学',url='https://example.edu.cn/')
        self.admin=User(username='admin',password_hash='unused',role='admin')
        db.session.add_all([self.school,self.admin]);db.session.flush()
        self.source=Department(school_id=self.school.id,name='通知公告',group_name='原归组',
            list_url='https://example.edu.cn/notices/',list_selector='li',title_selector='a',link_selector='a')
        db.session.add(self.source);db.session.flush()
        self.proposal=SourceProposal(school_id=self.school.id,department_id=self.source.id,
            identity_key='i'*64,proposal_key='p'*64,origin='manual_review',state='needs_review',
            candidate_json=json.dumps(source_config(self.source)),expected_config_hash='c'*64,
            evidence_hash='e'*64,validated_hash='v'*64,validation_json=json.dumps(dict(workflow=dict(status='user_choice',
                question=dict(text='选择订阅栏目',purpose='subscription_preference',options=[dict(id='graduate',
                    field='list_url',value='https://example.edu.cn/graduate/',label='研究生通知')])))))
        db.session.add(self.proposal);db.session.commit()
        self.ident=self.proposal.id;self.client=self.app.test_client()
        with self.client.session_transaction() as session:
            session['user_id']=self.admin.id;session['_csrf_token']='transaction-test'
        self.url=f'/api/admin/source-proposals/{self.ident}/review'
        self.original=self.state()
        self.guards=ExitStack()
        self.guards.enter_context(patch('socket.getaddrinfo',side_effect=AssertionError('No DNS')))
        self.guards.enter_context(patch('socket.socket.connect',side_effect=AssertionError('No website')))
        self.guards.enter_context(patch('backend.ai.providers.complete',side_effect=AssertionError('No model')))

    def tearDown(self):
        self.guards.close();db.session.remove();db.engine.dispose();self.ctx.pop();self.tmp.cleanup()

    def state(self):
        proposal=db.session.get(SourceProposal,self.ident if hasattr(self,'ident') else self.proposal.id)
        return dict(proposal={k:getattr(proposal,k) for k in ('state','revision','candidate_json','validated_hash','validation_json')},
            events=SourceReviewEvent.query.count(),tasks=BackgroundTask.query.count(),
            config=source_config(self.source),versions=SourceConfigVersion.query.count(),structure_version=current_version(self.school.id))

    def post(self,action,**extra):
        return self.client.post(self.url,json=dict(action=action,**extra),headers={'X-CSRF-Token':'transaction-test'})

    def assert_failed_unchanged(self,response):
        self.assertEqual(response.status_code,500)
        self.assertEqual(self.state(),self.original)
        self.assertEqual(response.json,{'error':'操作未完成，请稍后重试。'})

    def test_recheck_enqueue_failure_rolls_back_review(self):
        with patch('backend.services.tasks.enqueue',side_effect=OperationalError('injected queue failure',{},None)):
            response=self.post('recheck')
        self.assert_failed_unchanged(response)

    def test_selection_enqueue_failure_rolls_back_choice_and_revision(self):
        with patch('backend.services.tasks.enqueue',side_effect=OperationalError('injected queue failure',{},None)):
            response=self.post('select_option',choice='graduate')
        self.assert_failed_unchanged(response)

    def test_commit_failure_after_task_staging_rolls_back_both(self):
        session=db.session();commit=session.commit
        def fail_when_task_exists():
            if BackgroundTask.query.count():raise OperationalError('injected final commit',{},None)
            return commit()
        with patch.object(session,'commit',side_effect=fail_when_task_exists):
            response=self.post('recheck')
        self.assert_failed_unchanged(response)

    def test_success_commits_choice_and_task_together_and_returns_pending_state(self):
        commits=[];session=db.session()
        def observed_commit(_):
            commits.append((self.proposal.state,self.proposal.revision,BackgroundTask.query.count()))
        event.listen(session,'before_commit',observed_commit)
        try:response=self.post('select_option',choice='graduate')
        finally:event.remove(session,'before_commit',observed_commit)
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(commits,[('proposed',2,1)])
        self.assertEqual(response.json['workflow']['status'],'checking')
        self.assertEqual(response.json['state'],'proposed')
        task=db.session.get(BackgroundTask,response.json['task_id'])
        self.assertEqual((task.identity,task.state),(f'source_review:{self.ident}','pending'))
        self.assertEqual(task.payload,{'proposal_id':self.ident})
        self.assertEqual(SourceReviewEvent.query.count(),1)
        candidate=json.loads(self.proposal.candidate_json)
        self.assertEqual((candidate['name'],candidate['list_url']),('研究生通知','https://example.edu.cn/graduate/'))
        self.assertEqual(candidate['group_name'],self.original['config']['group_name'])
        self.assertEqual(candidate['list_selector'],'')
        self.assertEqual(source_config(self.source),self.original['config'])
        self.assertEqual(current_version(self.school.id),dict(self.original['structure_version'],
            revision=self.original['structure_version']['revision']+1))
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def test_rejection_has_no_continuation_task(self):
        response=self.post('reject')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json['state'],'rejected')
        self.assertEqual(response.json['workflow']['status'],'rejected')
        self.assertNotIn('task_id',response.json)
        self.assertEqual(BackgroundTask.query.count(),0)
        self.assertEqual(SourceReviewEvent.query.count(),1)
        self.assertEqual(source_config(self.source),self.original['config'])
        self.assertEqual(current_version(self.school.id),dict(self.original['structure_version'],
            revision=self.original['structure_version']['revision']+1))

    def test_recheck_and_repeat_reuse_one_pending_task_without_changing_active_rules(self):
        response=self.post('recheck')
        self.assertEqual(response.status_code,202,response.text)
        self.assertEqual(response.json['workflow']['status'],'checking')
        self.assertEqual(response.json['state'],'proposed')
        first_version=current_version(self.school.id)
        self.assertEqual(first_version,dict(self.original['structure_version'],
            revision=self.original['structure_version']['revision']+1))
        again=self.post('recheck')
        self.assertEqual(again.status_code,202,again.text)
        self.assertEqual(again.json['task_id'],response.json['task_id'])
        self.assertEqual(BackgroundTask.query.count(),1)
        self.assertEqual(current_version(self.school.id),first_version)
        self.assertEqual(source_config(self.source),self.original['config'])
        self.assertEqual(SourceConfigVersion.query.count(),0)

    def test_invalid_choice_is_rejected_without_any_mutation(self):
        response=self.post('select_option',choice='unknown')
        self.assertEqual(response.status_code,400,response.text)
        self.assertEqual(self.state(),self.original)


if __name__=='__main__':unittest.main()
