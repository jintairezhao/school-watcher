"""Finished column verdicts survive a later column's acquisition handoff."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.database.models import Announcement, Department
from backend.database.source_governance_models import SourceValidation
from backend.services import tasks
from tests import test_direct_onboarding as fixture_module

URL, HTML, TITLES = fixture_module.URL, fixture_module.HTML, fixture_module.TITLES


class ColumnTrialCheckpointTests(unittest.TestCase):
    def setUp(self):
        for target in ('socket.getaddrinfo', 'socket.socket.connect', 'backend.ai.providers.complete'):
            guard=patch(target, side_effect=AssertionError('Offline fixture only'))
            guard.start();self.addCleanup(guard.stop)
        self.fixture=fixture_module.DirectOnboardingTests();self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)

    def exercise_handoff(self, *, empty=False, repair=False):
        from backend.services.source_onboarding import onboard_page
        first=('<section id="notices"><h2>通知公告</h2><ul></ul><p>暂无内容</p></section>'
               if empty else HTML[HTML.index('<section'):HTML.index('</section>')+len('</section>')])
        other='<section id="other"><h2>资助通知</h2><ul>'+''.join(
            f'<li><a href="/other/{i}.htm">{title}</a><span>2026-10-01</span></li>'
            for i,title in enumerate(TITLES))+'</ul></section>'
        html='<html><title>测试栏目</title><body>'+first+other+'</body></html>'
        columns=[fixture_module.ai_column(list_selector='#notices li' if empty else '#missing li'),
            fixture_module.ai_column(name='资助通知',list_selector='#other li',
                container_selector='#other',heading_selector='#other h2')]
        job=tasks.enqueue('onboard', 'two-column-verdict',
            {'school_id':self.fixture.school_id,'url':URL,'ai_assist':repair,'observed_columns':columns})
        identity=(job.id,job.identity,job.generation);handle=tasks.claim(capabilities=['http'])
        with tasks.execution_scope(handle):
            tasks.checkpoint({'school_round':'original-round','saved_paid_plan':{'cursor':4,'decisions':['old-paid']}})
        original_plan=deepcopy(handle['checkpoint']['saved_paid_plan'])
        yielding=True
        def fetch(url,purpose):
            if url==URL:return html
            if yielding and '/other/' in url:
                tasks.defer(capability='http',phase='fetch',delay=0,reason='Synthetic later-column origin wait')
            return self.fixture.fetch(url,purpose)
        with tasks.execution_scope(handle), self.assertRaises(tasks.TaskDeferred) as caught:
            onboard_page(handle['payload'],fetcher=fetch)
        tasks.handoff(handle,caught.exception);db.session.refresh(job)
        first_records=[json.loads(v.validation_json) for v in SourceValidation.query.all()]
        self.assertEqual(len(first_records),1)
        checkpoint=deepcopy(job.checkpoint)
        if empty:
            self.assertEqual(first_records[0]['status'],'empty')
        else:
            self.assertEqual(len(checkpoint['column_validation']['failures']),1)
        yielding=False;resumed=tasks.claim(capabilities=['http'],exclude_kinds=('collect',))
        self.assertEqual(resumed['id'],identity[0])
        def recognize(*args,**kwargs):
            self.assertTrue(repair,'Unpaid continuation must not buy a repair')
            self.assertEqual(kwargs['feedback'][0]['name'],'通知公告')
            return {'status':'ready','columns':[fixture_module.ai_column()],
                'decision_ids':['new-synthetic-repair'],'material_complete':True}
        with tasks.execution_scope(resumed), \
                patch('backend.services.source_onboarding.recognize_column',side_effect=recognize) as model:
            result=onboard_page(resumed['payload'],fetcher=fetch)
        tasks.finish(resumed,result);db.session.refresh(job)
        self.assertEqual((job.id,job.identity,job.generation),identity)
        self.assertEqual(job.checkpoint['school_round'],'original-round')
        self.assertEqual(job.checkpoint['saved_paid_plan'],original_plan)
        self.assertEqual(model.call_count,1 if repair else 0)
        old_verdicts=[json.loads(v.validation_json) for v in SourceValidation.query.all()
                      if json.loads(v.validation_json)['name']=='通知公告'
                      and json.loads(v.validation_json)['status'] in ('empty','rejected')]
        self.assertEqual(len(old_verdicts),1,'First completed verdict must not be executed again')
        self.assertEqual(result['state'],'connected')
        self.assertEqual(Department.query.count(),2 if repair else 1)
        self.assertEqual(Announcement.query.count(),6 if repair else 3)
        return result

    def test_rule_failure_is_saved_and_skipped_after_later_column_handoff(self):
        result=self.exercise_handoff()
        self.assertEqual(result['issues'][0]['codes'],['list_missing'])

    def test_explicit_empty_column_is_not_revalidated_after_later_handoff(self):
        result=self.exercise_handoff(empty=True)
        self.assertEqual(result['empty_columns'],['通知公告'])
        self.assertFalse(result['issues'])

    def test_changed_repair_plan_executes_instead_of_reusing_old_failed_verdict(self):
        result=self.exercise_handoff(repair=True)
        self.assertFalse(result['issues'])


if __name__=='__main__':unittest.main()
