"""Monitor reads genuine saved state without provider calls, tasks or scratch reads."""
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import test_shared_summaries as fixture_module
from backend.database.db import db
from backend.database.models import BackgroundTask, Department, DepartmentDirectoryEntry, School, Subscription
from backend.services.runtime_catalog import RuntimeCatalog, pack
from backend.services.source_inventory import site_key


class DiscoveryMonitorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture_module.SharedSummaryTests(); self.fixture.setUp()
        self.app = self.fixture.app
        self.school = db.session.get(School, self.fixture.ann.school_id)
        self.school.name = '演示大学'
        db.session.add(Subscription(user_id=self.fixture.reader.id, school_id=self.school.id))
        db.session.commit()
        self.client = self.fixture.client(self.fixture.admin)
        self.endpoint = f'/api/schools/{self.school.id}/discovery-monitor'
        self.page = f'/subscriptions/{self.school.id}/discovery-monitor'
        self.catalog = RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'], versioned=False)

    def tearDown(self):
        self.fixture.tearDown()

    def publish(self, pages):
        with self.catalog.connect(write=True) as c:
            c.execute('INSERT OR REPLACE INTO catalog_sites VALUES(?,?,?,?,?,?,?,?)',
                (1, site_key(self.school.url), self.school.name, self.school.url, '2026-10-17T04:00:00Z',
                 pack({'catalogue_published_at':'2026-10-17T04:00:00Z'}), pack({}), pack([])))
            c.execute('DELETE FROM catalog_pages')
            for index, page in enumerate(pages):
                c.execute('INSERT INTO catalog_pages VALUES(?,?,?,?,?,?)',
                    (index + 1, 1, page['url'], pack(page), pack([]), b'HTML MUST NOT BE READ'))

    def task(self, name='discover', **values):
        row = BackgroundTask(identity=f'{name}:{self.school.id}', kind=name,
            payload={'school_id':self.school.id}, **values)
        db.session.add(row); db.session.commit(); return row

    def test_empty_passive_reads_create_nothing_and_reveal_no_keys(self):
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')), \
             patch('backend.services.source_inventory.Inventory.__init__', side_effect=AssertionError('No scratch reads')):
            for _ in range(3):
                response = self.client.get(self.endpoint)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Cache-Control'], 'no-store')
                self.assertFalse(response.json['coverage_complete'])
                self.assertEqual(response.json['nodes'], [])
            page = self.client.get(self.page)
            self.assertEqual(page.status_code, 200)
            self.assertIn('官网探索路径', page.text)
        self.assertEqual(BackgroundTask.query.count(), 0)
        self.assertFalse(Path(self.app.config['SOURCE_CATALOG_PATH']).exists())

    def test_subscriber_scope_and_disabled_schools(self):
        reader = self.fixture.client(self.fixture.reader)
        self.assertEqual(reader.get(self.endpoint).status_code, 200)
        Subscription.query.delete(); db.session.commit()
        self.assertEqual(reader.get(self.endpoint).status_code, 403)
        self.assertEqual(reader.get(self.page).status_code, 403)
        self.assertEqual(self.client.get(self.endpoint).status_code, 200)
        self.assertIn(self.fixture.client().get(self.endpoint).status_code, (401, 403))
        self.school.enabled = False; db.session.commit()
        self.assertEqual(self.client.get(self.endpoint).status_code, 404)

    def test_published_pages_current_work_and_installed_sources_stay_distinct(self):
        url = self.school.url + '/funding'
        self.publish([{'url':self.school.url, 'label':'学校官网', 'kind':'root', 'state':'fetched'},
                      {'url':url, 'label':'学生资助', 'path_json':json.dumps(['学校官网','组织机构','学生工作处']),
                       'kind':'channel', 'state':'fetched', 'checked_at':'2026-10-17T04:01:00Z',
                       'status_code':200, 'feed_json':'SECRET HTML', 'content_hash':'a' * 64}])
        source = Department(school_id=self.school.id, name='奖助通知', list_url=url, list_selector='li', group_name='学生工作处')
        db.session.add(source); db.session.commit()
        row = self.task(state='running', lease_until=datetime.utcnow() + timedelta(minutes=2), checkpoint={
            'discovery_progress':{'checked_pages':23,'pending_pages':7,'updated_at':'2026-10-17T04:02:00Z',
                'current_pages':[{'url':self.school.url + '/colleges','label':'院系设置','path_json':['学校官网']}]}})
        before = row.checkpoint.copy()
        value = self.client.get(self.endpoint).json
        by_url = {node['url']:node for node in value['nodes']}
        self.assertEqual(by_url[url]['state'], 'connected')
        self.assertEqual(by_url[url]['path'], ['学校官网','组织机构','学生工作处'])
        self.assertEqual(by_url[self.school.url + '/']['state'], 'fetched')
        self.assertEqual(by_url[self.school.url + '/colleges']['state'], 'running')
        self.assertEqual(value['progress']['checked_pages'], 23)
        self.assertIn('funding', by_url[url]['categories'])
        self.assertNotIn('SECRET HTML', json.dumps(value))
        self.assertNotIn('HTML MUST NOT BE READ', json.dumps(value))
        self.assertFalse(value['coverage_complete'])
        db.session.refresh(row); self.assertEqual(row.checkpoint, before)

    def test_stale_lease_is_not_presented_as_running_and_old_generation_is_history(self):
        self.task(state='running', generation=3, lease_until=datetime.utcnow() - timedelta(seconds=1))
        old = BackgroundTask(identity='navigation_review:old', kind='navigation_review', state='running',
            payload={'school_id':self.school.id,'school_generation':2,'page':{'url':self.school.url + '/old','label':'旧任务'}},
            lease_until=datetime.utcnow() + timedelta(minutes=5))
        new = BackgroundTask(identity='onboard:new', kind='onboard', state='running',
            payload={'school_id':self.school.id,'url':self.school.url + '/new'},
            lease_until=datetime.utcnow() - timedelta(seconds=1))
        db.session.add_all([old, new]); db.session.commit()
        value = self.client.get(self.endpoint).json
        self.assertFalse(any(node['state'] == 'running' for node in value['nodes']))
        self.assertNotIn(old.id, [row['id'] for row in value['tasks']])
        self.assertEqual(next(row for row in value['tasks'] if row['id'] == new.id)['state'], 'waiting')
        self.assertIn('租约已失效', next(row for row in value['tasks'] if row['id'] == new.id)['reason'])

    def college(self):
        group = Department(school_id=self.school.id, name='院系设置', kind='group',
                           list_url=self.school.url + '/colleges/')
        unit = Department(school_id=self.school.id, name='化工与环境学院', kind='unit',
                          list_url=self.school.url + '/gxy/')
        source = Department(school_id=self.school.id, name=unit.name, kind='column',
                            group_name=group.name, list_url=unit.list_url + 'tzgg/', list_selector='li')
        db.session.add_all([group, unit, source]); db.session.flush()
        db.session.add_all([DepartmentDirectoryEntry(parent_id=group.id, department_id=unit.id),
                            DepartmentDirectoryEntry(parent_id=unit.id, department_id=source.id)])
        db.session.commit()
        return group, unit, source

    def publish_college(self, group, unit, source):
        from test_source_placements import route
        self.publish([{'url':unit.list_url, 'label':'工学院', 'kind':'unit', 'path_json':[]},
                      {'url':source.list_url, 'label':source.name, 'kind':'channel', 'path_json':[]}])
        evidence = route(group=group.name, names=(unit.name,), urls=(unit.list_url,), reference=group.list_url)
        evidence['entry_names'] = ['通知公告']
        with self.catalog.connect(write=True) as c:
            c.execute('UPDATE catalog_sites SET paths_gzip=?', (pack({source.list_url:[evidence]}),))

    def test_saved_directory_membership_repairs_orphan_without_changing_raw_records(self):
        group, unit, source = self.college()
        self.publish_college(group, unit, source)
        before = (source.name, source.group_name, source.list_url, source.list_selector,
                  DepartmentDirectoryEntry.query.count(), BackgroundTask.query.count())
        with patch('backend.ai.providers.complete', side_effect=AssertionError('No paid request')):
            nodes = {n['url']:n for n in self.client.get(self.endpoint).json['nodes']}
        self.assertEqual(nodes[source.list_url]['label'], '通知公告')
        self.assertEqual(nodes[source.list_url]['path'], [self.school.name, group.name, unit.name])
        self.assertEqual(nodes[source.list_url]['navigation_path'], [])
        self.assertEqual(nodes[source.list_url]['path_basis'], '已保存的目录归属')
        self.assertEqual(nodes[unit.list_url]['label'], unit.name)
        self.assertEqual(nodes[unit.list_url]['path'], [self.school.name, group.name])
        self.assertEqual(nodes[group.list_url]['kind'], 'directory')
        self.assertEqual(nodes[source.list_url]['sources'][0]['name'], source.name)
        self.assertEqual((source.name, source.group_name, source.list_url, source.list_selector,
                          DepartmentDirectoryEntry.query.count(), BackgroundTask.query.count()), before)
        raw = self.catalog.monitor_snapshot(site_key(self.school.url))['pages']
        self.assertEqual(next(p for p in raw if p['url']==source.list_url)['path_json'], [])

    def test_published_entry_proof_recovers_source_missing_membership_but_name_alone_cannot(self):
        group, unit, source = self.college()
        self.publish_college(group, unit, source)
        DepartmentDirectoryEntry.query.filter_by(department_id=source.id).delete()
        stranger = Department(school_id=self.school.id, name=source.name, group_name=group.name,
                             list_url='https://unrelated.edu.cn/tzgg/', list_selector='li')
        db.session.add(stranger); db.session.commit()
        nodes = {n['url']:n for n in self.client.get(self.endpoint).json['nodes']}
        self.assertEqual(nodes[source.list_url]['path'], [self.school.name, group.name, unit.name])
        self.assertEqual(nodes[source.list_url]['path_basis'], '官网目录与栏目入口记录')
        self.assertEqual(nodes[stranger.list_url]['path'], [])

    def test_conflicting_saved_owners_and_directory_cycles_do_not_invent_edges(self):
        group, unit, source = self.college()
        other = Department(school_id=self.school.id, name='另一学院', kind='unit',
                           list_url=self.school.url + '/other/')
        db.session.add(other); db.session.flush()
        db.session.add(DepartmentDirectoryEntry(parent_id=other.id, department_id=source.id))
        db.session.commit()
        self.publish([{'url':source.list_url, 'label':source.name, 'kind':'channel', 'path_json':[]}])
        nodes = {n['url']:n for n in self.client.get(self.endpoint).json['nodes']}
        self.assertEqual(nodes[source.list_url]['path'], [])
        DepartmentDirectoryEntry.query.filter_by(parent_id=other.id).delete()
        db.session.add(DepartmentDirectoryEntry(parent_id=unit.id, department_id=group.id))
        db.session.commit()
        nodes = {n['url']:n for n in self.client.get(self.endpoint).json['nodes']}
        self.assertEqual(nodes[source.list_url]['path'], [])

    def test_failed_catalog_keeps_last_work_metadata_and_never_reads_scratch(self):
        self.task(state='waiting', phase='ai_result', error_code='ai_result_unknown')
        with patch.object(RuntimeCatalog, 'monitor_snapshot', side_effect=sqlite3.OperationalError('fixture unavailable')):
            value = self.client.get(self.endpoint).json
        self.assertTrue(value['catalog']['unavailable'])
        self.assertEqual(value['tasks'][0]['reason'], '付费请求结果尚未确认，未自动重发')

    def test_secrets_and_raw_errors_are_excluded(self):
        self.task(state='failed', error='sk-private-secret', checkpoint={'api_key':'sk-private-secret'},
            result={'output':'sk-private-secret'})
        row = BackgroundTask(identity='onboard:secret', kind='onboard', state='failed',
            payload={'school_id':self.school.id,'url':self.school.url + '/x?api_key=sk-private-secret&id=1'})
        db.session.add(row); db.session.commit()
        raw = self.client.get(self.endpoint).text
        self.assertNotIn('sk-private-secret', raw)
        self.assertIn('id=1', raw)

    def test_control_is_admin_only_explicit_and_preserves_checkpoint(self):
        row = self.task(state='pending', checkpoint={'saved':'keep'})
        endpoint = self.endpoint + '/control'
        headers = {'X-CSRF-Token':'token'}
        reader = self.fixture.client(self.fixture.reader)
        self.assertEqual(reader.post(endpoint, json={'action':'pause'}, headers=headers).status_code, 403)
        self.assertEqual(self.client.post(endpoint, json={'action':'retry'}, headers=headers).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json={'action':'pause'}).status_code, 403)
        self.assertEqual(self.client.post(endpoint, json={'action':'pause'}, headers=headers).status_code, 200)
        db.session.refresh(row)
        self.assertEqual((row.state,row.checkpoint), ('waiting',{'saved':'keep'}))
        self.assertEqual(self.client.post(endpoint, json={'action':'resume'}, headers=headers).status_code, 200)
        db.session.refresh(row)
        self.assertEqual((row.state,row.checkpoint), ('pending',{'saved':'keep'}))
        self.assertEqual(BackgroundTask.query.count(), 1)

    def test_confirmed_usage_reservations_and_other_school_are_separate(self):
        from backend.ai.models import AIExecution, AISchoolRound
        key = f'school-discovery:{self.school.id}:1:1'
        db.session.add(AISchoolRound(school_id=self.school.id, budget_key=key))
        for ident, budget, sent, usage, reserve, state in (
            ('confirmed',key,True,{'known':True,'total_tokens':250},0,'succeeded'),
            ('unknown',key,True,{'known':False},80,'uncertain'),
            ('unsent',key,False,{},30,'reserved'),
            ('another-school','school-discovery:999:1:1',True,{'known':True,'total_tokens':99999},0,'succeeded')):
            db.session.add(AIExecution(execution_id=ident,purpose='directory',config_version='1',
                provider='deepseek',model='not-a-real-request',skill_id='directory',skill_version='1',
                skill_digest='fixture',input_digest='fixture',mode='navigation',request_sent=sent,
                status=state,usage=usage,reserved_tokens=reserve,budget_keys=[budget]))
        db.session.commit()
        value = self.client.get(self.endpoint).json
        self.assertEqual(value['cost']['totals']['confirmed_tokens'], 250)
        self.assertEqual(value['cost']['totals']['reserved_tokens'], 110)
        self.assertEqual(value['cost']['totals']['actual_requests'], 2)
        self.assertEqual(value['cost']['totals']['unknown_requests'], 1)

    def test_legacy_address_labels_are_not_used_as_human_names(self):
        self.publish([{'url':self.school.url + '/legacy','label':'news.example.edu.cn/x',
                       'kind':'channel','state':'fetched','path_json':['www.example.edu.cn','组织机构']}])
        value = self.client.get(self.endpoint).json
        node = value['nodes'][0]
        self.assertEqual(node['label'], '未命名官网入口')
        self.assertNotIn('www.example.edu.cn', node['path'])

    def test_pending_and_done_tasks_do_not_claim_to_be_reading(self):
        rows = []
        for name, state in (('queued','pending'), ('finished','done')):
            row = BackgroundTask(identity='onboard:' + name,kind='onboard',state=state,phase='fetch',
                payload={'school_id':self.school.id,'url':self.school.url + '/' + name})
            db.session.add(row); rows.append(row)
        db.session.commit()
        value = self.client.get(self.endpoint).json
        task_by_id = {row['id']:row for row in value['tasks']}
        node_by_url = {row['url']:row for row in value['nodes']}
        self.assertEqual(node_by_url[self.school.url + '/queued']['state'], 'pending')
        self.assertEqual(task_by_id[rows[0].id]['reason'], '等待调度')
        self.assertEqual(task_by_id[rows[1].id]['reason'], '本项处理结束')
        self.assertEqual(value['progress']['column_tasks'], 1)


if __name__ == '__main__':
    unittest.main()
