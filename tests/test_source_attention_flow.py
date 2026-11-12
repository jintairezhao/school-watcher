"""Demand order is a hint, never ownership or a successful source receipt."""
from copy import deepcopy
import json
import re
from pathlib import Path
import unittest
from unittest.mock import patch

from tests import test_direct_onboarding as fixture
from backend.database.db import db
from backend.database.models import BackgroundTask, Department
from backend.database.models import School
from backend.database.models import Subscription
from backend.database.source_governance_models import SourceValidation
from backend.services import tasks
from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page

ROOT = 'https://example.edu.cn/'
TEACHER = ROOT + 'teacher/'
OTHER = ROOT + 'platform/'
TEACHER_TITLES = ['关于教师岗位聘任材料申报的通知', '关于开展教师职称评审工作的通知', '关于教职工发展培训安排的通知']
OTHER_TITLES = ['关于平台系统版本切换安排的公告', '关于开展平台维护工作的公告', '关于平台公共接口切换安排的公告']
POLICY = {'profiles':['teacher'], 'weights':{'personnel':4.0, 'teaching':1.0},
          'profile_context':{'source':'local_test','groups':[]}}


def document(label, titles, directory):
    return '<title>'+label+' - 测试大学</title><section id="notices"><h2>'+label+'</h2><ul>'+''.join(
        f'<li><a href="/{directory}/info/{i}.htm">{title}</a><span>2026-10-01</span></li>'
        for i,title in enumerate(titles))+'</ul></section>'


class SourceAttentionFlowTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.DirectOnboardingTests(); self.f.setUp()
        self.addCleanup(self.f.tearDown)
        self.inv = Inventory(Path(self.f.temp.name) / 'inventory.db')
        self.key = self.inv.ensure_site('测试大学', ROOT)
        self.pages = {ROOT:'<title>测试大学</title><nav><a href="/platform/">平台更新</a><a href="/teacher/">教师发展</a></nav>',
            TEACHER:document('教师发展',TEACHER_TITLES,'teacher'), OTHER:document('平台更新',OTHER_TITLES,'platform')}
        for url in (ROOT,OTHER,TEACHER):
            inspect_page(self.inv, self.inv.report(self.key)['site'], self.inv.get_page(self.key,url),
                fetcher=lambda address:dict(html=self.pages[address],url=address,status=200), ai_assist=False)
        self.policy = patch('backend.services.discovery_policy.get_attention_policy',return_value=POLICY)
        self.policy_mock=self.policy.start();self.addCleanup(self.policy.stop)

    def queue(self):
        from backend.services.source_onboarding import queue_columns
        return queue_columns(self.f.school_id,self.inv,self.key)

    def test_new_onboarding_batch_and_actual_first_claim_follow_existing_audience(self):
        jobs = self.queue()['onboarding_ids']
        rows = [db.session.get(BackgroundTask,ident) for ident in jobs]
        self.assertEqual([row.payload['url'] for row in rows],[TEACHER,OTHER])
        for row in rows:
            context = row.payload['discovery_attention']
            self.assertEqual(context['scope'],'page_onboarding_order')
            self.assertEqual(context['policy']['profiles'],['teacher'])
            self.assertTrue(context['material']['content_hash'])
            self.assertTrue(context['inspection_event_id'])
            self.assertFalse(context['establishes_ownership'])
        self.assertGreater(rows[0].payload['discovery_attention']['score'],rows[1].payload['discovery_attention']['score'])
        self.assertEqual(tasks.queue_ahead(rows[0],capability='http')[0],0)
        self.assertEqual(tasks.claim(capabilities=['http'])['id'],rows[0].id)

    def test_existing_queued_or_paid_work_is_not_recreated_or_reordered(self):
        self.queue()
        row = BackgroundTask.query.filter_by(kind='onboard').order_by(BackgroundTask.id).first()
        row.checkpoint={'paid_result':{'execution_id':'retained-paid-result','cursor':3}}
        db.session.commit()
        before = (row.id,row.generation,row.queued_at,deepcopy(row.payload),deepcopy(row.checkpoint))
        self.policy_mock.return_value=dict(POLICY,profiles=['undergraduate'],weights={'funding':9.0})
        self.assertEqual(self.queue()['onboarding_ids'],[])
        db.session.refresh(row)
        self.assertEqual((row.id,row.generation,row.queued_at,row.payload,row.checkpoint),before)

    def test_retry_and_availability_still_yield_to_untried_work(self):
        self.queue()
        preferred = BackgroundTask.query.filter(BackgroundTask.payload['url'].as_string()==TEACHER).one()
        other = BackgroundTask.query.filter(BackgroundTask.payload['url'].as_string()==OTHER).one()
        handle = tasks.claim(capabilities=['http'])
        self.assertEqual(handle['id'],preferred.id)
        tasks.handoff(handle,tasks.TaskDeferred(capability='http',phase='fetch',delay=0))
        self.assertEqual(tasks.queue_ahead(other,capability='http')[0],0)
        self.assertEqual(tasks.claim(capabilities=['http'])['id'],other.id)

    def test_validation_keeps_scheduling_evidence_without_granting_ownership(self):
        from backend.services.source_onboarding import onboard_page
        self.queue();handle=tasks.claim(capabilities=['http'])
        original = deepcopy(handle['payload']['discovery_attention'])
        def fetch(url,purpose):
            if url in self.pages:return self.pages[url]
            titles=TEACHER_TITLES if '/teacher/' in url else OTHER_TITLES
            title=titles[int(url.rsplit('/',1)[-1].split('.')[0])]
            return '<article><h1>'+title+'</h1><p>请有关单位按照学校公布的具体安排组织开展相关活动，详细事项和时间见学校官方网站通知正文。</p></article>'
        with tasks.execution_scope(handle),patch('backend.ai.runtime.run_skill',side_effect=AssertionError('No model call')):
            result=onboard_page(handle['payload'],fetcher=fetch)
        self.assertEqual(result['state'],'connected')
        self.assertEqual(result['discovery_attention']['policy']['version'],original['policy']['version'])
        saved=json.loads(SourceValidation.query.filter(SourceValidation.department_id.is_not(None)).order_by(SourceValidation.id.desc()).first().validation_json)
        self.assertEqual(saved['discovery_attention']['policy'],original['policy'])
        self.assertTrue(saved['discovery_attention']['scheduling_material_matches_validation'])
        self.assertFalse(saved['discovery_attention']['establishes_ownership'])
        from backend.services.structure_snapshot import read_structure
        school=db.session.get(School,self.f.school_id)
        capture=read_structure(school)
        self.assertFalse(any(edge['type']=='source_ownership' for edge in capture['facts']['relations']))

    def test_material_change_marks_old_scheduling_context_without_rewriting_it(self):
        from backend.services.source_attention import validation_context
        self.queue()
        row=BackgroundTask.query.filter_by(kind='onboard').first()
        before=deepcopy(row.payload['discovery_attention'])
        context=validation_context(row.payload,'<p>different retained page</p>')
        self.assertFalse(context['scheduling_material_matches_validation'])
        self.assertEqual(context['material'],before['material'])
        self.assertEqual(row.payload['discovery_attention'],before)

    def test_actual_claim_policy_is_immutable_in_the_inspection_event(self):
        with self.inv.connect() as connection:
            connection.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?",(self.key,ROOT))
        with patch('backend.services.source_path_receipts.load_source_receipts',return_value=[]), \
             patch('backend.services.discovery_costs.load_discovery_costs',return_value=None):
            page=self.inv.claim(self.key,focus='valuable')
        inspect_page(self.inv,self.inv.report(self.key)['site'],page,
            fetcher=lambda url:dict(html=self.pages[url],url=url,status=200),ai_assist=False)
        events=self.inv.report(self.key)['exploration_events']
        selected=events[-2]['selection_context']
        self.assertEqual(selected['reason'],'school_root')
        self.assertEqual(selected['policy']['profiles'],['teacher'])
        with self.inv.connect() as connection:
            connection.execute("UPDATE frontier_choices SET choice_json=? WHERE site_key=? AND url=?",
                (json.dumps({'reason':'later_choice'}),self.key,ROOT))
        self.assertEqual(self.inv.report(self.key)['exploration_events'][-2]['selection_context'],selected)

    def test_source_choices_and_graph_use_one_policy_without_changing_facts(self):
        from backend.services.structure_snapshot import read_structure, source_choices, graph_projection
        # Insert the lower interest first, so ID order alone would be wrong.
        low=Department(school_id=self.f.school_id,name='平台更新',list_url=OTHER,list_selector='#notices li')
        high=Department(school_id=self.f.school_id,name='教师发展',list_url=TEACHER,list_selector='#notices li')
        db.session.add_all([low,high]);db.session.commit()
        school=db.session.get(School,self.f.school_id)
        capture=read_structure(school);before=deepcopy(capture['facts'])
        groups=source_choices(capture)
        columns=[column for entries in groups.values() for entry in entries for column in entry['own_columns']]
        self.assertEqual([column['department'].id for column in columns],[high.id,low.id])
        connected_graph=graph_projection(capture,school)
        self.assertFalse(any(node['entity_type']=='source' for node in connected_graph['nodes']),
            'Configured candidates without validation stay in the exploration view')
        graph=graph_projection(capture,school,scope='exploration')
        self.assertEqual({column['attention_policy_version'] for column in columns},{graph['attention_policy_version']})
        self.assertEqual({node['source_id'] for node in graph['nodes'] if node['entity_type']=='source'},{high.id,low.id})
        self.assertEqual(before,capture['facts'],'Ordering never rewrites stable identity or relationship evidence')


class PureAttentionContextTests(unittest.TestCase):
    def test_policy_snapshot_is_deterministic_and_validation_is_shared(self):
        from backend.services.source_attention import policy_snapshot
        self.assertEqual(policy_snapshot(POLICY),policy_snapshot(deepcopy(POLICY)))
        from backend.scraper.discovery.attention import CATEGORY_KEYS
        self.assertEqual(policy_snapshot(POLICY),policy_snapshot(dict(POLICY,weights=dict.fromkeys(CATEGORY_KEYS,1.0)|POLICY['weights'])))
        with self.assertRaises(ValueError):policy_snapshot({'profiles':['unrecognised-person']})

    def test_neighbouring_widgets_do_not_lend_demand_to_a_source(self):
        from backend.services.source_attention import source_attention
        page={'url':ROOT,'label':'教师发展','title':'奖助和教师发展','kind':'root','state':'fetched','checked_at':'2026-10-22T00:00:00Z','content_hash':'raw-hash',
            'feed_json':json.dumps({'lists':[{'name':'教师发展','list_selector':'#teachers li','container_locator':'#teachers','samples':[]},
                {'name':'平台更新','list_selector':'#platform li','container_locator':'#platform','samples':[]}]})}
        report={'pages':[page]}
        source={'label':'平台更新','url':ROOT}
        result=source_attention(source,{'list_selector':'#platform li'},report,POLICY)
        self.assertEqual(result['score'],0)
        self.assertEqual(result['needs'],[])

    def test_missing_public_url_does_not_bind_to_another_invalid_page(self):
        from backend.services.source_attention import source_attention
        report={'pages':[{'url':'https://user:secret@example.edu.cn/','state':'fetched',
            'checked_at':'2026-10-22T00:00:00Z','content_hash':'raw-hash',
            'feed_json':json.dumps({'name':'教师发展','list_selector':'#same li','samples':[]})}]}
        result=source_attention({'label':'平台更新','url':''},{'list_selector':'#same li'},report,POLICY)
        self.assertEqual(result['score'],0,'An empty public identity cannot bind a source to hidden page material')


class ReaderAttentionContractTests(unittest.TestCase):
    def test_rendered_choices_api_and_graph_share_policy_but_keep_structural_version(self):
        from tests.test_structure_snapshot import StructureSnapshotTests
        f=StructureSnapshotTests();f.setUp();self.addCleanup(f.tearDown)
        platform=f.source('平台更新',f.school.url+'/platform/')
        teacher=f.source('教师发展',f.school.url+'/teacher/')
        funding=f.source('学生资助',f.school.url+'/funding/')
        admin=f.fixture.fixture.admin
        db.session.add(Subscription(user_id=admin.id,school_id=f.school.id));db.session.commit()
        with patch('backend.services.discovery_policy.get_attention_policy',return_value=POLICY) as policy:
            first=f.get()
            response=f.client.get(f'/subscriptions/{f.school.id}')
            self.assertEqual(response.status_code,200)
            html=response.get_data(as_text=True)
            self.assertEqual([int(value) for value in re.findall(r'name="department" value="(\d+)"',html)],
                [teacher.id,platform.id,funding.id])
            choices_response=f.client.get(f'/api/subscriptions/{f.school.id}/discovery')
            self.assertEqual(choices_response.status_code,200)
            choices=choices_response.json
            self.assertEqual(choices['attention_policy_version'],first['attention_policy_version'])
            self.assertIn(first['attention_policy_version'],html)
            policy.return_value=dict(POLICY,profiles=['undergraduate'],weights={'funding':6.0})
            updated=f.get(since=first['structure_token'])
            choices_response=f.client.get(f'/api/subscriptions/{f.school.id}/discovery')
            self.assertEqual(choices_response.status_code,200)
            choices=choices_response.json
            self.assertEqual(updated['structure_version'],first['structure_version'])
            self.assertEqual(choices['structure_version'],first['structure_version'])
            self.assertNotEqual(updated['attention_policy_version'],first['attention_policy_version'])
            self.assertEqual(choices['attention_policy_version'],updated['attention_policy_version'])
            self.assertEqual([int(value) for value in re.findall(r'name="department" value="(\d+)"',choices['choices_html'])],
                [funding.id,platform.id,teacher.id])
            self.assertEqual(updated['update_mode'],'delta')
            merged=f.apply_delta(first,updated)
            self.assertEqual([(node['id'],node['state']) for node in merged['nodes']],[(node['id'],node['state']) for node in first['nodes']])


if __name__=='__main__':unittest.main()
