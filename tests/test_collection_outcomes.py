"""Completed workers must not turn unfinished collection into reader success."""
from types import SimpleNamespace
import unittest

from bs4 import BeautifulSoup
from backend.database.db import db
from backend.database.models import BackgroundTask
from backend.services.source_collection import _repair_progress
from test_inbox_refresh import InboxRefreshTests


class CollectionOutcomeTests(unittest.TestCase):
    setUp = InboxRefreshTests.setUp
    tearDown = InboxRefreshTests.tearDown
    client_as = InboxRefreshTests.client_as
    post = InboxRefreshTests.post

    def stored_result(self, result, *, state='done'):
        self.a.list_selector = 'li.notice'
        db.session.commit()
        self.post({'scope': 'all'})
        task = BackgroundTask.query.filter_by(identity=f'collect:{self.a.id}').one()
        task.state, task.result = state, result
        db.session.commit()
        return task

    def status(self, client=None):
        response = (client or self.client).get('/api/inbox/refresh?resume=1')
        self.assertEqual(response.status_code, 200)
        return response.json

    def test_saved_partial_results_are_not_successful_updates_or_manual_challenges(self):
        cases = [
            (_repair_progress(SimpleNamespace(id=997, state='waiting')), 'repairing', '待恢复读取'),
            (_repair_progress(None), 'retry_wait', '等待重试'),
            ({'state':'discovering', 'partial':True, 'new_count':0, 'checked':0}, 'discovering', '待识别'),
            ({'state':'needs_check', 'partial':True, 'new_count':0, 'checked':0}, 'needs_check', '尚未读通'),
            ({'partial':True, 'new_count':2, 'checked':5}, 'partial', '部分更新'),
            ({'state':{}, 'partial':True, 'new_count':0, 'checked':0}, 'partial', '部分更新'),
        ]
        for result, state, label in cases:
            with self.subTest(state=state):
                task = self.stored_result(result)
                status = self.status()
                source = status['sources'][0]
                self.assertEqual((status['done'], status['incomplete'], status['processed']), (0, 1, 1))
                self.assertEqual((source['state'], source['status_label']), (state, label))
                self.assertEqual(source['task_state'], 'done')
                self.assertNotEqual(source['status_kind'], 'manual_verification')
                db.session.refresh(task)
                self.assertEqual(task.state, 'done', 'Reading status must not rewrite durable worker state')

    def test_new_client_and_server_render_keep_the_same_unfinished_result(self):
        self.stored_result(_repair_progress(None))
        reopened = self.client_as(self.user.id)
        self.assertEqual(self.status(reopened)['done'], 0)
        response = reopened.get(f'/?school={self.school.id}')
        self.assertEqual(response.status_code, 200)
        html = BeautifulSoup(response.text, 'html.parser')
        row = html.select_one(f'[data-department][value="{self.a.id}"]').parent
        self.assertEqual(row.select_one('[data-source-status]').text.strip(), '等待重试')
        self.assertEqual(row.select_one('.department-count').text.strip(), '—')

    def test_running_attempt_takes_precedence_over_old_partial_result(self):
        self.stored_result(_repair_progress(None), state='pending')
        status = self.status()
        self.assertEqual((status['active'], status['done'], status['incomplete']), (1, 0, 0))
        self.assertEqual(status['sources'][0]['state'], 'pending')

    def test_mixed_success_and_partial_preserve_new_notices_without_claiming_all_success(self):
        self.sub.department_ids = None
        self.a.list_selector = self.b.list_selector = 'li.notice'
        db.session.commit()
        self.post({'scope':'all'})
        jobs = {task.payload['department_id']:task for task in BackgroundTask.query.filter_by(kind='collect')}
        jobs[self.a.id].state, jobs[self.a.id].result = 'done', {'new_count':1,'checked':3}
        jobs[self.b.id].state, jobs[self.b.id].result = 'done', _repair_progress(
            SimpleNamespace(id=998,state='pending'),new=2,total=5)
        db.session.commit()
        status = self.status()
        self.assertEqual((status['total'],status['done'],status['incomplete'],status['processed']), (2,1,1,2))
        self.assertEqual(status['new_count'],3)
        partial = next(source for source in status['sources'] if source['id']==self.b.id)
        self.assertTrue(partial['partial'])

    def test_failed_task_and_real_manual_verification_keep_existing_classification(self):
        task = self.stored_result({},state='failed')
        task.error_code = 'http_403'
        db.session.commit()
        status = self.status()
        self.assertEqual((status['failed'],status['incomplete']), (1,0))
        self.assertEqual(status['sources'][0]['status_kind'],'access_limited')
        task.state,task.error_code = 'waiting','human_verification'
        db.session.commit()
        self.assertEqual(self.status()['sources'][0]['status_kind'],'manual_verification')


if __name__ == '__main__':
    unittest.main()
