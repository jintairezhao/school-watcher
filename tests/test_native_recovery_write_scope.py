"""Replay the recorded task27 correction through native scheduling offline."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime,timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import gzip,hashlib,json,time,unittest

from backend.database.db import db
from backend.database.models import BackgroundTask,School
from backend.services import source_onboarding as onboarding,tasks
from scripts.checks import run_native_branch_pilot as pilot
from tests import test_native_pilot_continuation as continuation


class NativeRecoveryWriteScopeTests(unittest.TestCase):
    def setUp(self):
        self.f=continuation.NativePilotContinuationTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        fixtures=Path(__file__).parent/'fixtures/structure'
        saved=json.loads((fixtures/'swpu-completed-task27-scope-20261101.json').read_text(encoding='utf-8'))
        row,school=deepcopy(saved['task']),saved['school']
        self.snapshot=row['checkpoint']['onboarding_page']
        material=fixtures/saved['body_file']
        self.raw=gzip.decompress(material.read_bytes()).decode()
        self.assertEqual(hashlib.sha256(self.raw.encode()).hexdigest(),self.snapshot['hash'])
        self.assertEqual(row['checkpoint']['column_validation']['validation_version'],8)
        db.session.add(School(**school));db.session.flush()
        self.parent=BackgroundTask(id=23,kind='discover',identity='discover:3',payload={'school_id':3},
            state='pending',generation=1,capability='directory',deadline_at=datetime.utcnow()+timedelta(minutes=10))
        db.session.add(self.parent)
        self.job=BackgroundTask(**row,state='done',phase='complete',deadline_at=datetime.utcnow()-timedelta(days=1))
        db.session.add(self.job);db.session.commit()
        page=dict(url=self.snapshot['url'],kind='channel',state='fetched',label=row['payload']['label'],
            content_hash=self.snapshot['hash'],checked_at=self.snapshot['captured_at'])
        self.inventory=SimpleNamespace(report=lambda _:dict(site=dict(name=school['name'],root_url=school['url'],site_key='offline'),pages=[page]),
            structure=lambda _:[],snapshot=lambda *_:self.raw)
        self.scope=dict(self.f.scope,continuation_school_id=3,resume_expired_task_ids=[],
            school=dict(name=school['name'],root_url=school['url']),maximum_wall_seconds=20)
        self.anchor()

    def anchor(self):
        pilot.save(self.f.root/'anchor.json',pilot.continuation_state(self.scope))
        self.scope['continuation_anchor_sha256']=pilot.sha(self.f.root/'anchor.json')

    def state(self):
        db.session.refresh(self.job)
        return {c.name:deepcopy(getattr(self.job,c.name)) for c in self.job.__table__.columns}

    def guards(self):
        return patch.multiple('backend.services.source_governance',read_snapshot=lambda ref:
            self.raw if ref['hash']==self.snapshot['hash'] else (_ for _ in ()).throw(AssertionError('Unknown saved material')))

    def queue(self):
        with self.guards(),patch('backend.services.source_catalog.publication_candidates',return_value=[]):
            return onboarding.queue_columns(3,self.inventory,'offline')

    def preserve(self,ids):
        # The fallback permits an actual before-fix reproduction, not an import failure.
        return getattr(onboarding,'preserve_onboarding_tasks',lambda _:nullcontext())(ids)

    def test_recorded_task27_reopens_without_explicit_sampling_preservation(self):
        result=self.queue()
        self.assertIn(27,result['onboarding_ids'])
        self.assertEqual((self.job.state,self.job.phase),('pending','validation_replay'))
        self.assertEqual(self.job.generation,1)

    def test_protected_task27_stays_unchanged_through_queue_and_global_recovery(self):
        before=self.state()
        with self.preserve({27}):
            self.assertEqual(self.queue()['onboarding_ids'],[])
            onboarding.resume_rule_discovery()
        self.assertEqual(self.state(),before)

    def test_changed_page_cannot_requeue_protected_task_via_enqueue_fallback(self):
        from backend.services.source_validation import VALIDATION_VERSION
        checkpoint=deepcopy(self.job.checkpoint)
        checkpoint['column_validation'].update(validation_version=VALIDATION_VERSION,failures=[])
        self.job.checkpoint=checkpoint;db.session.commit();before=self.state()
        with self.preserve({27}),patch('backend.services.discovery_changes.baseline',return_value={'change':'changed','hash':'new'}):
            self.assertEqual(self.queue()['onboarding_ids'],[])
        self.assertEqual(self.state(),before)

    def test_requeued_protected_task_cannot_enter_the_sampling_worker(self):
        self.job.state='pending';self.job.phase='validation_replay';db.session.commit()
        before=self.state()
        with pilot.school_queue(self.scope,3):
            handle=tasks.claim(capabilities=['http','directory'])
            self.assertEqual(handle['id'],23)
            self.assertTrue(tasks.finish(handle,{'synthetic':True}))
            self.assertIsNone(tasks.claim(capabilities=['http','directory']))
        self.assertEqual(self.state(),before)

    def test_new_task_is_not_confused_with_preserved_existing_work(self):
        preserved=pilot.preserved_native_task_ids(self.scope)
        new=tasks.enqueue('onboard','offline-new',{'school_id':3,'url':'https://www.swpu.edu.cn/offline-fixture/'})
        new_id=new.id
        with onboarding.preserve_onboarding_tasks(preserved),pilot.school_queue(self.scope,3,preserved_task_ids=preserved):
            for _ in range(2):
                handle=tasks.claim(capabilities=['http','directory'])
                self.assertIsNotNone(handle)
                self.assertIn(handle['id'],{23,new_id})
                self.assertTrue(tasks.finish(handle,{'synthetic':True}))
            self.assertIsNone(tasks.claim(capabilities=['http','directory']))
        self.assertEqual(self.job.state,'done')

    def test_direct_recovery_and_nested_exception_cannot_relax_outer_preservation(self):
        before=self.state()
        with self.guards(),self.preserve({27}):
            with self.assertRaisesRegex(RuntimeError,'nested'):
                with self.preserve(set()):
                    self.assertFalse(onboarding._resume_retained_validation(self.job,datetime.utcnow()))
                    raise RuntimeError('nested')
            self.assertFalse(onboarding._resume_retained_validation(self.job,datetime.utcnow()))
        self.assertEqual(self.state(),before)
        with self.guards():self.assertTrue(onboarding._resume_retained_validation(self.job,datetime.utcnow()))

    def test_shared_enqueue_cannot_replace_a_protected_completed_task(self):
        before=self.state()
        with self.preserve({27}):
            job=tasks.enqueue('onboard',self.job.identity.split(':',1)[1],{'school_id':3,'new':True},replace_finished=True)
            self.assertEqual(job.id,27)
        self.assertEqual(self.state(),before)

    def test_retry_picker_excludes_protected_completed_work(self):
        self.job.updated_at=self.job.checked_at=self.job.finished_at=datetime.utcnow()-timedelta(days=1)
        db.session.commit()
        self.assertTrue(onboarding.pending_onboarding_recovery(3))
        before=self.state()
        with self.preserve({27}):
            self.assertEqual(onboarding.retry_unconnected_columns(3,'offline-new-run',automatic=True),[])
            self.assertFalse(onboarding.pending_onboarding_recovery(3))
        self.assertEqual(self.state(),before)

    def test_native_child_installs_preservation_before_native_scheduling(self):
        self.scope.update(expires_at=(datetime.utcnow()+timedelta(hours=1)).isoformat()+'+00:00',
            maximum_task_invocations=1,website=dict(allowed_hosts=['www.swpu.edu.cn'],allowed_school_domain='swpu.edu.cn',
                maximum_documents=1,maximum_http_attempts=1,maximum_response_bytes=1024),model=dict(maximum_dispatch_attempts=2))
        self.anchor();scope_path=self.f.root/'native-scope.json';pilot.save(scope_path,self.scope)
        before=self.state();called=[]
        def execute(app,handle):
            called.append(handle['id'])
            with app.app_context(),tasks.execution_scope(handle):
                self.assertEqual(self.queue()['onboarding_ids'],[])
            self.assertTrue(tasks.finish(handle,{'offline_scope_checked':True}))
        with patch.object(pilot,'require_model_admission',return_value={'ready':True}), \
                patch.dict('os.environ',{}),patch('backend.create_app',return_value=self.f.app), \
                patch('backend.ai.configuration.get_model_binding',return_value={'model':'deepseek-flash'}), \
                patch('backend.worker.execute',side_effect=execute), \
                patch('backend.ai.providers.complete',side_effect=AssertionError('No paid requests')), \
                patch('backend.scraper.acquisition.http_transport.fetch_http',side_effect=AssertionError('No website')):
            pilot.native_child(str(scope_path),str(self.f.root/'offline-run'),time.monotonic()+20)
        self.assertEqual(called,[23]);self.assertEqual(self.state(),before)
        self.assertEqual(pilot.read(self.f.root/'control/model-attempts.json')['used'],1)


if __name__=='__main__':unittest.main()
