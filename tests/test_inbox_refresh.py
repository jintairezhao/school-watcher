"""Reader refreshes enqueue only their selected sources and share incremental work."""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import School, Department, User, Subscription, BackgroundTask, Announcement


class InboxRefreshTests(unittest.TestCase):
    def test_reopen_uses_persisted_completion_not_task_metadata_touch(self):
        from datetime import timedelta
        from backend.services.inbox_refresh import queue_sources
        from backend.database.models import AppConfig
        AppConfig.set('scrape_interval','30')
        self.post({'scope':'all'})
        task=BackgroundTask.query.one()
        task.state='done'; task.finished_at=datetime.utcnow()-timedelta(minutes=31)
        task.checked_at=None; task.updated_at=datetime.utcnow()
        self.a.last_scraped_at=task.finished_at
        ident=self.a.id; db.session.commit(); db.session.remove()
        self.assertTrue(queue_sources([db.session.get(Department,ident)]))
        self.assertEqual(BackgroundTask.query.one().state,'pending')

    def test_reopen_skips_recent_completion_and_obeys_changed_interval(self):
        from datetime import timedelta
        from backend.services.inbox_refresh import queue_sources
        from backend.database.models import AppConfig
        self.post({'scope':'all'})
        task=BackgroundTask.query.one()
        task.state='done';task.finished_at=datetime.utcnow()-timedelta(minutes=20)
        task.checked_at=task.finished_at;self.a.last_scraped_at=task.finished_at
        ident=self.a.id;db.session.commit();db.session.remove()
        AppConfig.set('scrape_interval','30')
        self.assertEqual(queue_sources([db.session.get(Department,ident)]),[])
        AppConfig.set('scrape_interval','10')
        self.assertTrue(queue_sources([db.session.get(Department,ident)]))

    def test_scheduler_uses_current_interval_instead_of_old_next_run_estimate(self):
        from datetime import timedelta
        from backend.services import tasks
        from backend.worker import _schedule_due
        from backend.database.models import AppConfig
        self.school.subscriber_count=1
        task=tasks.enqueue('scrape',self.school.id,{'school_id':self.school.id})
        task.state='done';task.finished_at=datetime.utcnow()-timedelta(minutes=20)
        task.checked_at=task.finished_at;task.next_run_at=datetime.utcnow()+timedelta(hours=1)
        db.session.commit();ident=task.id
        AppConfig.set('scrape_interval','10')
        _schedule_due()
        self.assertEqual(db.session.get(BackgroundTask,ident).state,'pending')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'refresh-test', 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
                              'SOURCE_CATALOG_PATH': str(Path(self.temp.name) / 'catalog.db')})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        self.school = School(name='例校', url='https://example.edu.cn/', subscriber_count=2)
        self.other_school = School(name='其他学校', url='https://other.edu.cn/', subscriber_count=1)
        self.user = User(username='reader', password_hash='unused', role='admin')
        self.other = User(username='other', password_hash='unused', role='admin')
        db.session.add_all([self.school,self.other_school,self.user,self.other]); db.session.flush()
        self.a = Department(school_id=self.school.id, name='医学院', list_url='https://med.example.edu.cn/')
        self.b = Department(school_id=self.school.id, name='物理学院', list_url='https://physics.example.edu.cn/')
        self.c = Department(school_id=self.other_school.id, name='教务处', list_url='https://other.edu.cn/notice/')
        db.session.add_all([self.a,self.b,self.c]); db.session.flush()
        self.sub = Subscription(user_id=self.user.id, school_id=self.school.id, department_ids=[self.a.id])
        db.session.add_all([self.sub, Subscription(user_id=self.other.id, school_id=self.school.id),
                           Subscription(user_id=self.other.id, school_id=self.other_school.id)])
        db.session.commit()
        self.client = self.client_as(self.user.id)

    def tearDown(self):
        db.session.remove();db.drop_all();self.ctx.pop();self.temp.cleanup()

    def client_as(self, ident):
        c = self.app.test_client()
        with c.session_transaction() as s: s.update(user_id=ident, _csrf_token='token')
        return c

    def post(self, data, client=None):
        return (client or self.client).post('/api/inbox/refresh',json=data,headers={'X-CSRF-Token':'token'})

    def test_scoped_refresh_only_enqueues_without_network_and_shares_work(self):
        payload = {'scope':'current','school_id':self.school.id,'department_ids':[self.a.id]}
        with patch('backend.scraper.engine._fetch_html',side_effect=AssertionError('No collection in request')):
            a = self.post(payload); b = self.post(payload,self.client_as(self.other.id))
        self.assertEqual(a.status_code,202); self.assertEqual(b.status_code,202)
        jobs = BackgroundTask.query.all()
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0].payload['department_id'],self.a.id)

    def test_global_refresh_cannot_include_another_users_sources(self):
        r = self.post({'scope':'all'})
        self.assertEqual(r.status_code,202)
        self.assertEqual([t.payload['department_id'] for t in BackgroundTask.query.all()],[self.a.id])
        for school, ident in [(self.school.id,self.b.id),(self.other_school.id,self.c.id)]:
            self.assertEqual(self.post({'scope':'current','school_id':school,'department_ids':[ident]}).status_code,403)
        self.assertEqual(self.client.get(f'/api/inbox/refresh?school={self.other_school.id}').status_code,403)

    @patch('backend.services.source_placements.official_source_placements')
    def test_official_group_filters_and_refresh_share_scope_without_widening_subscription(self, placements):
        self.a.group_name = '医学院'
        self.b.group_name = '物理学院'
        db.session.add_all([
            Announcement(school_id=self.school.id, department_id=self.a.id,
                         title='已订阅学院的通知', published_at=datetime.utcnow()),
            Announcement(school_id=self.school.id, department_id=self.b.id,
                         title='未订阅学院的通知', published_at=datetime.utcnow())])
        db.session.commit()
        placements.return_value = {d.id: [{'group': '院系设置', 'nodes': [
            {'key': str(d.id), 'name': d.name}], 'label': '本部门通知'}] for d in (self.a, self.b)}
        response = self.client.get(f'/?school={self.school.id}&group=院系设置&period=all')
        self.assertEqual(response.status_code, 200)
        self.assertIn('已订阅学院的通知', response.text)
        self.assertNotIn('未订阅学院的通知', response.text)
        self.assertNotIn(f'value="{self.b.id}" data-department', response.text)
        queued = self.post({'scope': 'current', 'school_id': self.school.id, 'group': '院系设置'})
        self.assertEqual(queued.status_code, 202)
        self.assertEqual([t.payload['department_id'] for t in BackgroundTask.query.all()], [self.a.id])
        self.assertEqual(self.sub.department_ids, [self.a.id])

    def test_invalid_scope_does_not_widen_to_all_and_csrf_still_applies(self):
        for payload in [{'scope':'unexpected'}, {'school_id':'oops'}, {'department_ids':[self.a.id]},
                        {'school_id':self.school.id,'department_ids':['bad']}]:
            self.assertEqual(self.post(payload).status_code,400)
        self.assertEqual(self.client.post('/api/inbox/refresh',json={'scope':'all'}).status_code,403)
        self.assertEqual(BackgroundTask.query.count(),0)

    def test_recent_completion_is_reused_and_failures_are_visible(self):
        data={'school_id':self.school.id,'department_ids':[self.a.id]}
        self.post(data)
        task=BackgroundTask.query.one();task.state='done';task.finished_at=datetime.utcnow();task.result={'new_count':2}
        db.session.commit(); self.post(data)
        self.assertEqual(task.state,'done')
        task.state='failed';task.error='官网暂时无法读取';db.session.commit()
        r=self.client.get(f'/api/inbox/refresh?school={self.school.id}&dept={self.a.id}')
        self.assertEqual(r.get_json()['sources'][0]['state'],'failed')
        self.assertIn('无法读取',r.get_json()['sources'][0]['message'])

    def test_refresh_tracking_survives_new_client_without_enqueuing_on_resume(self):
        self.post({'scope': 'all'})
        with patch('backend.services.inbox_refresh.queue_sources', side_effect=AssertionError('Resume must only read')):
            resumed = self.client_as(self.user.id).get('/api/inbox/refresh?resume=1').get_json()
        self.assertTrue(resumed['tracked'])
        self.assertEqual(resumed['tracking']['scope'], 'all')
        self.assertEqual([s['id'] for s in resumed['sources']], [self.a.id])
        self.assertEqual(resumed['active'], 1)
        self.assertEqual(BackgroundTask.query.count(), 1)

    def test_refresh_tracking_is_per_user_and_revalidates_subscription_changes(self):
        self.post({'scope': 'all'}, self.client_as(self.other.id))
        own = self.client.get('/api/inbox/refresh?resume=1').get_json()
        self.assertEqual([s['id'] for s in own['sources']], [self.a.id])
        self.sub.department_ids = []; db.session.commit()
        resumed = self.client.get('/api/inbox/refresh?resume=1').get_json()
        self.assertEqual(resumed['sources'], [])
        self.assertEqual(resumed['active'], 0)

    def test_local_refresh_keeps_the_running_global_scope_and_real_completion(self):
        other = self.client_as(self.other.id)
        self.post({'scope': 'all'}, other)
        local = self.post({'scope': 'current', 'school_id': self.school.id,
                           'department_ids': [self.a.id]}, other).get_json()
        self.assertEqual(local['tracking']['scope'], 'all')
        self.assertEqual({s['id'] for s in local['sources']}, {self.a.id, self.b.id, self.c.id})
        for task in BackgroundTask.query.all():
            task.state = 'done'; task.result = {'new_count': 2}
        db.session.commit()
        resumed = other.get('/api/inbox/refresh?resume=1').get_json()
        self.assertEqual((resumed['active'], resumed['done'], resumed['new_count']), (0, 3, 6))

    def test_resume_recovers_active_tasks_if_response_or_tracking_write_was_lost(self):
        from backend.services.inbox_refresh import queue_sources
        queue_sources([self.a])
        resumed = self.client.get('/api/inbox/refresh?resume=1').get_json()
        self.assertTrue(resumed['tracked'])
        self.assertEqual(resumed['active'], 1)
        self.assertEqual(resumed['tracking']['scope_label'], '本次更新')

    def test_new_local_update_does_not_restore_a_finished_global_scope(self):
        from datetime import timedelta
        other = self.client_as(self.other.id)
        self.post({'scope': 'all'}, other)
        for task in BackgroundTask.query.all():
            task.state = 'done'; task.updated_at = datetime.utcnow() - timedelta(minutes=2)
        db.session.commit()
        local = self.post({'scope': 'current', 'school_id': self.school.id,
                           'department_ids': [self.a.id]}, other).get_json()
        self.assertEqual(local['tracking']['scope'], 'current')
        self.assertEqual([s['id'] for s in local['sources']], [self.a.id])

    def test_unconfigured_unit_queues_shared_discovery_and_keeps_history(self):
        from backend.services.source_collection import collect_source
        self.a.list_selector = None
        db.session.add(Announcement(school_id=self.school.id, department_id=self.a.id,
                                   title='已有教学通知', url=self.a.list_url + 'old.htm'))
        db.session.commit()
        ids = [item.id for item in Announcement.query.all()]
        with patch('backend.services.source_collection.fetch_source_page') as fetcher:
            first = collect_source(self.a)
            second = collect_source(self.a)
        fetcher.assert_not_called()
        self.assertEqual(first['state'], 'discovering')
        self.assertTrue(first['partial'])
        self.assertEqual(first['new_count'], 0)
        self.assertEqual(second['checked'], 0)
        self.assertEqual(BackgroundTask.query.filter_by(kind='discover').count(), 1)
        self.assertEqual(ids, [item.id for item in Announcement.query.all()])
        self.assertEqual(self.a.list_url, 'https://med.example.edu.cn/')
        self.assertIsNone(self.a.last_scraped_at)

    def test_unreadable_configured_site_is_not_reported_as_successful_empty_collection(self):
        from backend.services.source_collection import collect_source
        from backend.scraper.fetch_errors import SourceAccessError
        self.a.list_selector = 'ul.notices li'
        db.session.commit()
        with patch('backend.scraper.engine._fetch_html', side_effect=SourceAccessError('官网无法读取')):
            with self.assertRaisesRegex(RuntimeError, '无法读取'):
                collect_source(self.a)
        self.assertIsNone(self.a.last_scraped_at)

    def test_source_rows_distinguish_unknown_counts_from_empty_results(self):
        from bs4 import BeautifulSoup

        def row(fragment=False):
            response = self.client.get(f'/?school={self.school.id}',
                headers={'X-Inbox-Fragment': '1'} if fragment else {})
            self.assertEqual(response.status_code, 200)
            soup = BeautifulSoup(response.data, 'html.parser')
            self.assertIsNone(soup.select_one(f'[data-department][value="{self.b.id}"]'))
            return soup.select_one(f'[data-department][value="{self.a.id}"]').parent

        initial = row()
        self.assertEqual(initial.select_one('.department-count').text.strip(), '—')
        self.assertEqual(initial.select_one('[data-source-status]').text.strip(), '待采集')
        self.post({'school_id': self.school.id, 'department_ids': [self.a.id]})
        task = BackgroundTask.query.one()
        task.state = 'failed'; task.error = '官网当前返回访问校验页面，暂时无法读取通知'
        db.session.commit()
        failed = row(fragment=True)
        self.assertEqual(failed.select_one('.department-count').text.strip(), '—')
        self.assertEqual(failed.select_one('[data-source-status]').text.strip(), '访问受限')
        response = self.client.get(f'/api/inbox/refresh?school={self.school.id}')
        self.assertEqual(response.get_json()['sources'][0]['status_label'], '访问受限')
        task.state = 'done'; task.error = ''; db.session.commit()
        complete = row()
        self.assertEqual(complete.select_one('.department-count').text.strip(), '0')
        self.assertTrue(complete.select_one('[data-source-status]').has_attr('hidden'))


if __name__=='__main__': unittest.main()
