"""School isolation of a sampling worker; no real university or model requests."""
import unittest

from scripts.checks import run_native_branch_pilot as pilot
import test_native_pilot_continuation as continuation


class NativePilotSchoolScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture=continuation.NativePilotContinuationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.db=self.fixture.db

    def test_add_school_preserves_prior_tasks_executions_and_cumulative_usage(self):
        from backend.database.models import AppConfig,BackgroundTask,School
        from backend.ai.models import AIExecution
        old=BackgroundTask.query.one()
        old_state=(old.id,old.generation,dict(old.checkpoint),old.state)
        self.owner()
        school_id=pilot.initialize_native_state(dict(new_school_in_existing_instance=True,existing_school_ids=[1],
            school=dict(name='独立测试学校',root_url='https://other.example.edu.cn/')))
        self.assertNotEqual(school_id,1)
        self.assertEqual(School.query.count(),2)
        old=self.db.session.get(BackgroundTask,old_state[0])
        self.assertEqual((old.id,old.generation,dict(old.checkpoint),old.state),old_state)
        self.assertEqual(AIExecution.query.one().output,{'preserved':True})
        self.assertEqual(AppConfig.get('ai_request_limit_school_discovery'),'2')
        self.assertEqual(pilot.read(self.fixture.root/'control/model-attempts.json')['used'],1)
        self.assertEqual(pilot.school_tasks(school_id)[0].payload['school_id'],school_id)
        with self.assertRaisesRegex(AssertionError,'inventory changed'):
            pilot.initialize_native_state(dict(new_school_in_existing_instance=True,existing_school_ids=[1],
                school=dict(name='独立测试学校',root_url='https://other.example.edu.cn/')))

    def owner(self):
        from backend.database.models import AppConfig,User
        user=User(username='sampling-owner',password_hash='synthetic')
        self.db.session.add(user);self.db.session.flush()
        AppConfig.set('desktop_owner_id',str(user.id));self.db.session.commit()

    def test_filter_precedes_native_limit_and_preserves_order_and_unrelated_rows(self):
        from backend.database.models import School,BackgroundTask
        from backend.services import tasks
        other=School(name='独立测试学校',url='https://other.example.edu.cn/')
        self.db.session.add(other);self.db.session.flush()
        for n in range(20):tasks.enqueue('navigation_review','old-'+str(n),{'school_id':1},commit=False)
        first=tasks.enqueue('navigation_review','target-first',{'school_id':other.id},commit=False)
        second=tasks.enqueue('navigation_review','target-second',{'school_id':other.id},commit=False)
        self.db.session.commit()
        original=[(row.id,row.state,row.claim_count,dict(row.checkpoint)) for row in pilot.school_tasks(1)]
        scope=dict(new_school_in_existing_instance=True)
        with pilot.school_queue(scope,other.id):
            a=tasks.claim(capabilities=['directory'])
            self.assertTrue(tasks.finish(a,{'synthetic_completed':True}))
            b=tasks.claim(capabilities=['directory'])
            self.assertEqual([a['id'],b['id']],[first.id,second.id])
            self.assertTrue(tasks.finish(b,{'synthetic_completed':True}))
        self.assertEqual([(row.id,row.state,row.claim_count,dict(row.checkpoint)) for row in pilot.school_tasks(1)],original)
        self.assertEqual(tasks.claim(capabilities=['directory'])['payload']['school_id'],1)

    def test_changed_instance_refuses_before_native_initialization(self):
        self.db.session.remove();self.db.engine.dispose()
        manifest=self.fixture.root/'prepared-code.json';pilot.save(manifest,{})
        scope=dict(instance_path=self.fixture.root.relative_to(pilot.ROOT).as_posix(),
            existing_business_sha256=pilot.sha(self.fixture.root/'school_watcher.db'),retained_control_files={},
            code_manifest=manifest.relative_to(pilot.ROOT).as_posix(),code_manifest_sha256=pilot.sha(manifest),
            existing_school_ids=[1],school=dict(name='独立测试学校',root_url='https://other.example.edu.cn/'))
        pilot.check_added_school(scope)
        scope['existing_business_sha256']='0'*64
        with self.assertRaisesRegex(AssertionError,'instance changed'):
            pilot.check_added_school(scope)


if __name__=='__main__':unittest.main()
