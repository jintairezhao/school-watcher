"""Continuation admission and native state preservation, with no paid dispatch."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from scripts.checks import run_native_branch_pilot as pilot


class NativePilotContinuationTests(unittest.TestCase):
    def setUp(self):
        from backend import create_app
        from backend.database.db import db
        from backend.database.models import AppConfig, School
        from backend.ai.models import AIBudget, AIExecution
        from backend.services import tasks
        pilot.BASE.mkdir(parents=True, exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='native-continuation-',dir=pilot.BASE)
        self.root=Path(self.temp.name)
        self.app=create_app(dict(TESTING=True,SECRET_KEY='synthetic-continuation',
            SQLALCHEMY_DATABASE_URI='sqlite:///'+str(self.root/'school_watcher.db'),
            DISCOVERY_CACHE_PATH=str(self.root/'discovery.sqlite3'),SOURCE_CATALOG_PATH=str(self.root/'catalog.sqlite3')))
        self.context=self.app.app_context();self.context.push();db.create_all()
        self.db=db
        school=School(name='测试大学',url='https://example.edu.cn/')
        db.session.add(school);db.session.flush()
        job=tasks.enqueue('discover',school.id,dict(school_id=school.id))
        job.checkpoint={'route_cursor':1,'route_candidate_order':['first','second']}
        db.session.add(AIBudget(key='school-discovery:test',used_tokens=100,used_requests=1))
        db.session.add(AIExecution(execution_id='synthetic-prior-request',purpose='directory',
            config_version='test',provider='deepseek',model='deepseek-flash',skill_id='test',
            skill_version='1',skill_digest='a'*64,input_digest='b'*64,mode='navigation',
            status='succeeded',request_sent=True,usage={'known':True,'total_tokens':100},output={'preserved':True}))
        AppConfig.set('ai_request_limit_school_discovery','2');db.session.commit()
        pilot.save(self.root/'control-placeholder.json',{})
        (self.root/'control').mkdir()
        pilot.save(self.root/'control/model-attempts.json',dict(used=1,attempts=[dict(origin='synthetic_prior_call')]))
        pilot.save(self.root/'control/pilot-web-ledger.json',dict(documents=[],http_attempts=[],boundary=None))
        self.scope=dict(instance_path=self.root.relative_to(pilot.ROOT).as_posix(),
            school=dict(name=school.name,root_url=school.url),native_school_request_limit=3,
            continuation_anchor=(self.root/'anchor.json').relative_to(pilot.ROOT).as_posix(),
            retained_control_files={},approval_record=(self.root/'approval.json').relative_to(pilot.ROOT).as_posix())
        self.original=pilot.continuation_state(self.scope)
        pilot.save(self.root/'anchor.json',self.original)
        self.scope['continuation_anchor_sha256']=pilot.sha(self.root/'anchor.json')
        pilot.save(self.root/'code.json',{})
        self.scope.update(code_manifest=(self.root/'code.json').relative_to(pilot.ROOT).as_posix(),
            code_manifest_sha256=pilot.sha(self.root/'code.json'))
        self.scope_file=self.root/'scope.json';pilot.save(self.scope_file,self.scope)

    def tearDown(self):
        self.db.session.remove();self.db.engine.dispose();self.context.pop();self.temp.cleanup()

    def test_approval_must_match_the_exact_additional_scope(self):
        with self.assertRaisesRegex(AssertionError,'Explicit approval'):
            pilot.check_continuation_approval(self.scope_file,self.scope)
        pilot.save(self.root/'approval.json',dict(approved=True,scope_sha256='wrong'))
        with self.assertRaisesRegex(AssertionError,'does not match'):
            pilot.check_continuation_approval(self.scope_file,self.scope)
        pilot.save(self.root/'approval.json',dict(approved=True,scope_sha256=pilot.sha(self.scope_file)))
        pilot.check_continuation_approval(self.scope_file,self.scope)
        self.assertEqual(pilot.continuation_state(self.scope),self.original)

    def test_initialization_preserves_native_cursors_generations_history_and_usage(self):
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('Never recreate a task')):
            pilot.initialize_native_state(self.scope)
        current=pilot.continuation_state(self.scope)['state']
        old=self.original['state']
        for key in old:
            if key!='native_school_request_limit':self.assertEqual(current[key],old[key],key)
        self.assertEqual(current['native_school_request_limit'],'3')
        self.assertEqual(current['model_counter']['used'],1)

    def test_changed_checkpoint_refuses_before_raising_the_budget(self):
        from backend.database.models import AppConfig, BackgroundTask
        job=BackgroundTask.query.one();job.checkpoint={'route_cursor':2};self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'state changed'):
            pilot.initialize_native_state(self.scope)
        self.assertEqual(AppConfig.get('ai_request_limit_school_discovery'),'2')

    def test_explicit_domain_boundary_release_preserves_the_task_and_cursor(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        job=BackgroundTask.query.one();identity, generation=job.id,job.generation
        original=dict(job.checkpoint)
        job.phase='sampling_boundary';job.error_code='sampling_outside_declared_host'
        job.available_at=datetime.utcnow()+timedelta(days=1);self.db.session.commit()
        self.scope['resume_boundary_task_ids']=[job.id]
        pilot.save(self.root/'anchor.json',pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256']=pilot.sha(self.root/'anchor.json')
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('Never recreate a task')):
            pilot.initialize_native_state(self.scope)
        self.assertEqual(self.db.session.get(BackgroundTask,identity).checkpoint,original)
        handle=tasks.claim(capabilities=['directory'])
        self.assertIsNotNone(handle)
        self.assertEqual((handle['id'],handle['generation']),(identity,generation))

    def test_unsettled_request_or_expired_task_refuses_admission(self):
        from backend.ai.models import AIExecution
        from backend.database.models import BackgroundTask
        row=AIExecution.query.one();row.status='uncertain';self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'unsettled'):
            pilot.continuation_state(self.scope)
        row.status='succeeded'
        BackgroundTask.query.one().deadline_at=datetime.utcnow()-timedelta(seconds=1)
        self.db.session.commit()
        with self.assertRaisesRegex(AssertionError,'deadline expired'):
            pilot.continuation_state(self.scope)

    def test_finished_native_tasks_are_preserved_without_being_restarted(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        first=BackgroundTask.query.one();first.state='done';first.deadline_at=datetime.utcnow()-timedelta(days=1)
        second=tasks.enqueue('navigation_review','synthetic-remaining',{'school_id':first.payload['school_id']})
        self.db.session.commit()
        first_id,second_id=first.id,second.id
        original=pilot.continuation_state(self.scope)
        pilot.save(self.root/'anchor.json',original)
        self.scope['continuation_anchor_sha256']=pilot.sha(self.root/'anchor.json')
        with patch('backend.services.tasks.enqueue',side_effect=AssertionError('Never recreate a task')):
            pilot.initialize_native_state(self.scope)
        self.assertEqual(self.db.session.get(BackgroundTask,first_id).state,'done')
        self.assertEqual(self.db.session.get(BackgroundTask,second_id).state,'pending')

    def test_http_only_validation_uses_native_worker_without_new_model_or_directory_work(self):
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        school_id=BackgroundTask.query.one().payload['school_id']
        directory_id=BackgroundTask.query.one().id
        http=tasks.enqueue('onboard','synthetic-http-only',{'school_id':school_id,'url':'https://example.edu.cn/notices/'})
        http_id=http.id
        self.scope.update(http_only_validation=True,complete_existing_http_task=http_id,initial_failure_count=0,
            expires_at=(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
            website=dict(allowed_hosts=['example.edu.cn'],maximum_documents=1,maximum_http_attempts=1,maximum_response_bytes=1024),
            model=dict(maximum_dispatch_attempts=1))
        pilot.save(self.root/'anchor.json',pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256']=pilot.sha(self.root/'anchor.json')
        pilot.save(self.scope_file,self.scope)
        seen=[]
        def dispatch(kind,payload):
            seen.append(kind)
            if len(seen)==1:
                tasks.defer(phase='origin_wait',delay=0,reason='Synthetic admission wait',keep_place=True)
            return {'status':'checked'}
        with patch.dict('os.environ',{}),patch('backend.create_app',return_value=self.app), \
                patch('backend.ai.configuration.get_model_binding',return_value={'model':'deepseek-flash'}), \
                patch('backend.worker.dispatch',side_effect=dispatch), \
                patch('backend.ai.providers.complete',side_effect=AssertionError('No paid dispatch')):
            pilot.native_child(str(self.scope_file),str(self.root/'http-output'),time.monotonic()+30)
        self.assertEqual(seen,['onboard','onboard'])
        self.assertEqual(self.db.session.get(BackgroundTask,http_id,populate_existing=True).state,'done')
        self.assertEqual(self.db.session.get(BackgroundTask,directory_id,populate_existing=True).state,'pending')
        self.assertEqual(pilot.read(self.root/'control/model-attempts.json')['used'],1)


if __name__=='__main__':unittest.main()
