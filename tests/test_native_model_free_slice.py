"""A zero-model HTTP slice preserves the old ledger and still blocks dispatch."""
from datetime import datetime, timedelta, timezone
import tempfile
from pathlib import Path
import time
import unittest

from scripts.checks import run_native_branch_pilot as driver


def scope():
    return dict(model_free_native_slice=True, maximum_task_invocations=1,
        resume_expired_task_ids=[77],existing_max_task_id=80,
        original_target_deadline='2026-11-04T07:13:33.861895',
        expires_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),
        website=dict(allowed_hosts=['www.example.edu.cn'], maximum_documents=1,
                     maximum_http_attempts=1, maximum_response_bytes=1024, browser_calls=0),
        model=dict(existing_dispatch_attempts=89, maximum_dispatch_attempts=89,
                   maximum_new_dispatch_attempts=0, declared_cost_ceiling_cny='0'))


def reader_window():
    value = scope()
    value.update(model_free_reader_window=True,maximum_task_invocations=8,maximum_wall_seconds=480,
                 continuation_school_id=5)
    value['website'].update(existing_document_attempts=10,maximum_new_documents=24,maximum_documents=34,
                            existing_http_attempts=20,maximum_new_http_attempts=48,maximum_http_attempts=68)
    return value


class NativeModelFreeSliceTests(unittest.TestCase):
    def test_web_work_can_continue_while_the_next_model_dispatch_is_rejected(self):
        from backend.ai.providers import ProviderError
        from backend.scraper.acquisition import FetchRequest
        with tempfile.TemporaryDirectory(prefix='zero-model-') as folder:
            root=Path(folder);s=scope()
            bounds=driver.Bounds(s,root,root/'observations',time.monotonic()+30)
            driver.save(root/'control/model-attempts.json',dict(used=89,attempts=[{'old':'ledger'}]))
            self.assertFalse(driver.model_budget_reached(s,89))
            bounds.new_document(FetchRequest('https://www.example.edu.cn/'))
            with self.assertRaises(ProviderError):
                bounds.reserve_model(dict(provider='deepseek',model='deepseek-flash',region='default'))
            self.assertEqual(driver.read(root/'control/model-attempts.json'),
                             dict(used=89,attempts=[{'old':'ledger'}]))

    def test_zero_mode_cannot_hide_a_new_call_cost_browser_or_second_task(self):
        for section,key,value in [('model','maximum_new_dispatch_attempts',1),
                                  ('model','maximum_dispatch_attempts',90),
                                  ('model','declared_cost_ceiling_cny','1'),
                                  ('website','browser_calls',1),(None,'maximum_task_invocations',2)]:
            s=scope();(s[section]if section else s)[key]=value
            with self.subTest(section=section,key=key),self.assertRaises(ValueError):
                driver.model_budget_reached(s,89)

    def test_prior_ledger_change_is_not_treated_as_a_new_zero_allowance(self):
        for used in (88,90):
            with self.subTest(used=used),self.assertRaises(ValueError):
                driver.model_budget_reached(scope(),used)

    def test_ordinary_paid_scope_keeps_its_existing_budget_stop(self):
        s=scope();s.pop('model_free_native_slice')
        self.assertTrue(driver.model_budget_reached(s,89))
        self.assertFalse(driver.model_budget_reached(s,88))

    def test_only_the_declared_target_is_inspected_even_if_other_expired_jobs_exist(self):
        from tests import test_direct_onboarding as fixture_module
        from backend.database.db import db
        from backend.database.models import BackgroundTask
        from backend.services import tasks
        fixture=fixture_module.DirectOnboardingTests();fixture.setUp()
        try:
            old=[]
            for index in range(16):
                job=tasks.enqueue('navigation_review','expired-zero-'+str(index),{'school_id':fixture.school_id})
                job.deadline_at=datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(days=1)
                old.append(job.id)
            target=tasks.enqueue('navigation_review','zero-target',{'school_id':fixture.school_id})
            db.session.commit()
            def snapshot():
                return [(r.id,r.state,r.error_code,r.checked_at,r.updated_at,r.finished_at,r.claim_count)
                        for r in BackgroundTask.query.filter(BackgroundTask.id.in_(old)).order_by(BackgroundTask.id)]
            before=snapshot();s=dict(scope(),continuation_school_id=fixture.school_id,
                                    resume_expired_task_ids=[target.id],existing_max_task_id=target.id)
            with driver.school_queue(s,fixture.school_id,preserved_task_ids=set()):
                handle=tasks.claim(capabilities=['directory'])
            self.assertEqual(handle['id'],target.id)
            db.session.expire_all();self.assertEqual(snapshot(),before)
        finally:fixture.tearDown()

    def test_an_explicit_repeated_reader_window_still_refuses_every_new_model_dispatch(self):
        from backend.ai.providers import ProviderError
        with tempfile.TemporaryDirectory(prefix='zero-reader-window-') as folder:
            root=Path(folder);value=reader_window()
            bounds=driver.Bounds(value,root,root/'observations',time.monotonic()+30)
            old=dict(used=89,attempts=[{'preserved':'old ledger'}])
            driver.save(root/'control/model-attempts.json',old)
            self.assertFalse(driver.model_budget_reached(value,89))
            with self.assertRaises(ProviderError):
                bounds.reserve_model(dict(provider='deepseek',model='deepseek-flash',region='default'))
            self.assertEqual(driver.read(root/'control/model-attempts.json'),old)

    def test_repeated_reader_window_requires_both_a_slice_cap_and_consistent_web_caps(self):
        for changes in (dict(maximum_task_invocations=9),dict(maximum_task_invocations=True),
                        dict(maximum_wall_seconds=481),dict(continuation_school_id=None),
                        dict(model_free_reader_window=False)):
            with self.subTest(changes=changes),self.assertRaises(ValueError):
                driver.validate_model_free_scope(dict(reader_window(),**changes))
        for key,value in [('maximum_new_documents',25),('maximum_new_http_attempts',49),
                          ('maximum_documents',35),('maximum_http_attempts',69)]:
            revised=reader_window();revised['website'][key]=value
            with self.subTest(key=key),self.assertRaises(ValueError):
                driver.validate_model_free_scope(revised)


if __name__=='__main__':unittest.main()
