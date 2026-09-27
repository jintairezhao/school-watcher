"""Administrator log history is complete, bounded by date, and retained by policy."""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import AppConfig, School, Department, User, ScrapeLog, Announcement, Subscription


class ScrapeLogManagementTests(unittest.TestCase):
    def setUp(self):
        self.app=create_app({'TESTING':True,'SECRET_KEY':'log-test','SQLALCHEMY_DATABASE_URI':'sqlite://'})
        self.ctx=self.app.app_context();self.ctx.push();db.create_all()
        self.admin=User(username='admin',password_hash='unused',role='admin')
        self.reader=User(username='reader',password_hash='unused')
        self.school=School(name='甲大学',url='https://example.edu.cn/')
        self.other=School(name='乙大学',url='https://other.edu.cn/')
        db.session.add_all([self.admin,self.reader,self.school,self.other]);db.session.commit()
        self.now=datetime(2026,9,21,12)
        self.client=self.client_as(self.admin.id)

    def tearDown(self):
        db.session.remove();db.drop_all();self.ctx.pop()

    def client_as(self, ident):
        c=self.app.test_client()
        with c.session_transaction() as s:s.update(user_id=ident,_csrf_token='token')
        return c

    def log(self, days=0, **kwargs):
        row=ScrapeLog(school_id=kwargs.pop('school_id',self.school.id),
                      started_at=self.now-timedelta(days=days),
                      status=kwargs.pop('status','success'),**kwargs)
        db.session.add(row);db.session.commit();return row

    def test_recent_week_is_complete_across_pages_and_school_names_are_explicit(self):
        for i in range(12):self.log(i/3,school_id=self.school.id if i%2 else self.other.id)
        boundary=self.log(7)
        old=self.log(7.001,error_message='一周之前，不应提前加载')
        ids=[];cursor={}
        with patch('backend.services.scrape_logs.PAGE_SIZE',4):
            while True:
                r=self.client.get('/api/admin/scrape-logs',query_string={'period':'recent','as_of':self.now.isoformat(),**cursor})
                self.assertEqual(r.status_code,200)
                data=r.get_json();ids.extend(row['id'] for row in data['rows'])
                self.assertEqual(data['total'],13)
                self.assertNotIn('一周之前',r.text)
                self.assertTrue(all(row['school_name'] in ('甲大学','乙大学') for row in data['rows']))
                if not data['next_cursor']:break
                cursor=data['next_cursor']
        self.assertEqual(len(set(ids)),13);self.assertIn(boundary.id,ids);self.assertNotIn(old.id,ids)

    def test_archive_school_filter_full_error_and_unknown_historical_scope(self):
        error='官网返回的完整错误说明。'*35+'<script>unsafe()</script>'
        row=self.log(9,error_message=error,status='failed')
        self.log(10,school_id=self.other.id)
        response=self.client.get('/api/admin/scrape-logs',query_string={'period':'archive','as_of':self.now.isoformat(),'school_id':self.school.id})
        data=response.get_json()
        self.assertEqual([r['id'] for r in data['rows']],[row.id])
        self.assertEqual(data['rows'][0]['error_message'],error)
        self.assertIn('未记录',data['rows'][0]['source_name'])
        self.assertTrue(data['rows'][0]['started_at'].endswith('Z'))

    def test_retention_is_admin_only_validated_and_does_not_delete_on_save(self):
        self.log(90)
        endpoint='/api/admin/scrape-logs/retention'
        self.assertEqual(self.client.get(endpoint).get_json()['days'],30)
        reader=self.client_as(self.reader.id)
        self.assertEqual(reader.get('/api/admin/scrape-logs').status_code,403)
        self.assertEqual(reader.put(endpoint,json={'days':7},headers={'X-CSRF-Token':'token'}).status_code,403)
        self.assertEqual(self.client.put(endpoint,json={'days':7}).status_code,403)
        for value in [-1,1,8,'30',True,1.5,None]:
            self.assertEqual(self.client.put(endpoint,json={'days':value},headers={'X-CSRF-Token':'token'}).status_code,400)
        for value in [7,30,90,180,365,0]:
            self.assertEqual(self.client.put(endpoint,json={'days':value},headers={'X-CSRF-Token':'token'}).status_code,200)
            self.assertEqual(self.client.get(endpoint).get_json()['days'],value)
        self.assertEqual(ScrapeLog.query.count(),1)

    def test_cleanup_uses_saved_retention_preserves_active_jobs_and_notices(self):
        from backend.services.scrape_logs import prune_scrape_logs
        keep=self.log(6);boundary=self.log(7);expired=self.log(8)
        running=self.log(80,status='running')
        dept=Department(school_id=self.school.id,name='教务处');db.session.add(dept);db.session.flush()
        db.session.add(Announcement(school_id=self.school.id,department_id=dept.id,title='已保存通知'))
        db.session.commit();AppConfig.set('scrape_log_retention_days','7')
        self.assertEqual(prune_scrape_logs(now=self.now),1)
        self.assertEqual({r.id for r in ScrapeLog.query.all()},{keep.id,boundary.id,running.id})
        self.assertEqual(Announcement.query.count(),1)
        AppConfig.set('scrape_log_retention_days','0')
        self.log(1000);self.assertEqual(prune_scrape_logs(now=self.now),0)

    def test_new_source_logs_include_scope_and_runtime_details(self):
        row=self.log(source_name='医学院',finished_at=self.now+timedelta(seconds=12),new_count=3,total_count=15)
        data=self.client.get('/api/admin/scrape-logs',query_string={'as_of':self.now.isoformat()}).get_json()['rows'][0]
        self.assertEqual(data['source_name'],'医学院');self.assertEqual(data['duration_seconds'],12)
        self.assertEqual(data['new_count'],3);self.assertEqual(data['total_count'],15)

    def test_worker_records_source_partial_results_and_complete_failure(self):
        from backend.worker import dispatch
        self.school.subscriber_count=1
        dept=Department(school_id=self.school.id,name='医学院',list_url=self.school.url)
        db.session.add(dept)
        db.session.add(Subscription(user_id=self.reader.id,school_id=self.school.id))
        db.session.commit()
        payload={'school_id':self.school.id,'department_id':dept.id}
        with patch('backend.services.source_collection.collect_source',return_value={
                'new_count':3,'checked':15,'partial':True,'message':'第二个栏目暂时无法读取'}):
            dispatch('collect',payload)
        row=ScrapeLog.query.one()
        self.assertEqual((row.source_name,row.status,row.new_count,row.total_count),('医学院','partial',3,15))
        self.assertEqual(row.error_message,'第二个栏目暂时无法读取')
        error='完整错误说明。'*100+'结尾原因'
        with patch('backend.services.source_collection.collect_source',side_effect=RuntimeError(error)):
            with self.assertRaises(RuntimeError):dispatch('collect',payload)
        row=ScrapeLog.query.order_by(ScrapeLog.id.desc()).first()
        self.assertEqual(row.status,'failed');self.assertEqual(row.error_message,'医学院：'+error)
        self.assertIsNotNone(row.finished_at)

    def test_invalid_history_query_is_rejected(self):
        for query in [{'period':'all'},{'school_id':'invalid'},{'school_id':'-1'},
                      {'before_id':'2'},{'before_time':'2026-09-21'},{'as_of':'not-a-time'}]:
            self.assertEqual(self.client.get('/api/admin/scrape-logs',query_string=query).status_code,400)

    def test_worker_restart_closes_interrupted_attempts_without_losing_details(self):
        from backend.services.scrape_logs import recover_interrupted_logs
        pending=self.log(status='running',new_count=4,error_message='第一批已保存')
        done=self.log(status='success',finished_at=self.now)
        self.assertEqual(recover_interrupted_logs(now=self.now),1)
        self.assertEqual((pending.status,pending.finished_at,pending.new_count),('interrupted',self.now,4))
        self.assertIn('第一批已保存',pending.error_message);self.assertIn('后台恢复',pending.error_message)
        self.assertEqual(done.status,'success')
        self.assertEqual(recover_interrupted_logs(now=self.now),0)

    def test_overview_success_rate_excludes_waiting_and_running_records(self):
        for status, count in [('success',5),('failed',2),('partial',1),('interrupted',1),('waiting',60),('running',1)]:
            for _ in range(count):self.log(status=status, new_count=0)
        self.log(7,status='success')
        self.log(8,status='failed')
        with patch('backend.routes.admin.datetime') as clock:
            clock.utcnow.return_value=self.now
            data=self.client.get('/api/admin/stats').get_json()
        self.assertEqual(data['scrapes_7d'],10)
        self.assertEqual(data['scrape_success_rate'],0.6)
        self.assertEqual(data['scrape_outcomes_7d'],{'success':6,'failed':2,'partial':1,'interrupted':1})
        self.assertTrue(all(row['status'] not in ('waiting','running') for row in data['recent_logs']))
        self.assertEqual(ScrapeLog.query.filter_by(status='waiting').count(),60)

    def test_overview_without_ended_records_has_no_success_rate(self):
        self.log(status='waiting');self.log(status='running')
        with patch('backend.routes.admin.datetime') as clock:
            clock.utcnow.return_value=self.now
            data=self.client.get('/api/admin/stats').get_json()
        self.assertEqual(data['scrapes_7d'],0)
        self.assertIsNone(data['scrape_success_rate'])
        self.assertEqual(data['recent_logs'],[])
        self.assertEqual(self.client_as(self.reader.id).get('/api/admin/stats').status_code,403)


if __name__=='__main__':unittest.main()
