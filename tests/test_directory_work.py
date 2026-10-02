import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from backend import create_app
from backend.database.db import db
from backend.database.models import School
from backend.database.source_governance_models import DiscoveryWorkItem
from backend.services.directory_work import fragments, partition, run_batch


class DirectoryWorkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test',
            'SOURCE_CATALOG_PATH': str(Path(self.temp.name) / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(Path(self.temp.name) / 'inventory.db'),
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(Path(self.temp.name) / 'test.db')})
        self.context = self.app.app_context(); self.context.push(); db.create_all()
        school = School(name='示例大学', url='https://www.example.edu.cn/'); db.session.add(school); db.session.commit()
        self.school = school.id
        self.evidence = {'school_id': self.school, 'candidates': [{'candidate_id': str(i)} for i in range(3)],
            'evidence': [{'evidence_id': 'page', 'url': school.url, 'html': '<nav>学院</nav>'}]}

    def tearDown(self):
        db.session.remove(); db.drop_all(); db.engine.dispose(); self.context.pop(); self.temp.cleanup()

    def call(self):
        return run_batch(self.school, 1, 'test', 'classify', self.evidence, {'version': 1})

    def due(self):
        for row in DiscoveryWorkItem.query.all():
            row.next_run_at = datetime.utcnow() - timedelta(seconds=1)
        db.session.commit()

    def test_finished_grouping_with_gaps_cannot_report_complete_coverage(self):
        from backend.database.models import BackgroundTask
        from backend.database.source_governance_models import SchoolOnboarding
        from backend.services.directory_work import coverage
        db.session.add(SchoolOnboarding(school_id=self.school))
        db.session.add(BackgroundTask(identity=f'discover:{self.school}', kind='discover',
            payload={'school_id':self.school}, state='done', generation=1))
        db.session.add(DiscoveryWorkItem(identity='roster', school_id=self.school, generation=1,
            group_key='roster', candidate_id='roster', kind='roster', material_hash='snapshot',
            input_json={}, state='succeeded'))
        db.session.commit()
        self.assertTrue(coverage(self.school)['complete'])
        grouping = BackgroundTask(identity=f'source_grouping:{self.school}', kind='source_grouping',
            payload={'school_id':self.school}, state='done', result={'gaps':[
                {'source_id':123, 'url':'https://www.example.edu.cn/column', 'reason':'尚无足够官网证据确定归属'}]})
        db.session.add(grouping); db.session.commit()
        result = coverage(self.school)
        self.assertFalse(result['complete'])
        self.assertEqual(result['placement_gap_count'], 1)
        self.assertTrue(any(g['state'] == 'placement_unverified' for g in result['gaps']))
        grouping.result = {'gaps':[]}; db.session.commit()
        self.assertTrue(coverage(self.school)['complete'])

    def test_grouping_checkpoint_keeps_gaps_visible_after_restart_without_counting_as_running(self):
        from backend.database.models import BackgroundTask
        from backend.services.directory_work import coverage
        db.session.add(BackgroundTask(identity=f'source_grouping:{self.school}', kind='source_grouping',
            payload={'school_id':self.school}, state='waiting', checkpoint={'grouping_gaps':[
                {'url':'https://www.example.edu.cn/units/', 'reason':'官网目录访问失败'}]}))
        db.session.commit(); db.session.remove()
        result = coverage(self.school)
        self.assertEqual(result['active_task_count'], 0)
        self.assertEqual(result['placement_gap_count'], 1)
        self.assertTrue(any(g['reason'] == '官网目录访问失败' for g in result['gaps']))

    def test_upgrade_restores_native_checklist_once_without_model_calls(self):
        from backend.database.source_governance_models import SchoolOnboarding
        from backend.services.directory_recovery import restore_inventory
        db.session.add(SchoolOnboarding(school_id=self.school)); db.session.commit()
        page = {'url': 'https://www.example.edu.cn/units', 'state': 'fetched', 'kind': 'directory',
            'content_hash': 'hash', 'notes_json': '[]'}
        unit = {'node_key': 'unit-1', 'name': '学院', 'url': page['url'], 'kind': 'unit',
            'relation': 'academic_unit', 'reference_url': page['url'], 'content_hash': 'hash'}
        from backend.services.source_relationships import ROSTER_RELATIONS
        unit['relation'] = next(iter(ROSTER_RELATIONS))
        with patch('backend.services.runtime_catalog.RuntimeCatalog') as catalog, \
             patch('backend.ai.runtime.run_skill') as paid:
            catalog.return_value.report.return_value = {'pages': [page]}
            catalog.return_value.structure.return_value = [unit]
            self.assertEqual(restore_inventory(), 1)
            self.assertEqual(restore_inventory(), 0)
            paid.assert_not_called()
        self.assertEqual({r.kind for r in DiscoveryWorkItem.query.all()}, {'page', 'department', 'roster'})

    def test_cms_attachment_does_not_stop_other_structural_entrances(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.scraper.discovery.structure import extract_structure, document_reference
        url = 'https://www.example.edu.cn/system/_content/download.jsp?owner=123&urltype=news.DownloadAttachUrl&wbfileid=abc'
        self.assertTrue(document_reference(url))
        self.assertFalse(document_reference('https://www.example.edu.cn/downloads.htm', '下载中心'))
        parsed = extract_structure(f'<a href="{url}">培养方案.pdf</a>', self.evidence['evidence'][0]['url'],
            self.evidence['evidence'][0]['url'], 'root', '示例大学', [])
        self.assertEqual(parsed['links'][0]['decision'], 'document_reference')
        inventory = Inventory(Path(self.temp.name) / 'attachments.db')
        key = inventory.ensure_site('示例大学', 'https://www.example.edu.cn/')
        inventory.enqueue(key, url, '培养方案', 'channel', 1, [], 'school_domain')
        roster = 'https://www.example.edu.cn/units.pdf'
        inventory.enqueue(key, roster, '机构设置.pdf', 'directory', 1, [], 'school_domain')
        called = []
        def fetch(address):
            called.append(address)
            return {'html': '<main>学校首页</main>', 'url': address, 'status': 200}
        report = crawl_site(inventory, key, max_pages=5, workers=1, fetcher=fetch)
        self.assertNotIn(url, called)
        self.assertNotIn(roster, called)
        self.assertEqual(inventory.get_page(key, url)['state'], 'reference_only')
        self.assertEqual(inventory.get_page(key, roster)['state'], 'blocked')
        self.assertEqual(report['states'].get('pending', 0), 0)

    def test_large_frontier_keeps_all_entrances_and_full_department_path(self):
        from backend.services.discovery_cache import DiscoveryCache
        inventory = DiscoveryCache(Path(self.temp.name) / 'large-frontier.db')
        key = inventory.ensure_site('示例大学', 'https://www.example.edu.cn/')
        trail = [f'第 {i} 层部门' for i in range(10)]
        for index in range(1001):
            inventory.enqueue(key, f'https://www.example.edu.cn/dept/{index}/', '学院', 'unit', 10, trail, 'school_domain')
        report = inventory.report(key)
        self.assertEqual(len(report['pages']), 1002)
        self.assertEqual(json.loads(inventory.get_page(key, 'https://www.example.edu.cn/dept/1000/')['path_json']), trail)

    def test_one_department_needing_access_verification_keeps_other_pages_running(self):
        from backend.services.source_inventory import Inventory
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.scraper.acquisition import FetchResult, FetchFailure
        inventory = Inventory(Path(self.temp.name) / 'access-gap.db')
        root = 'https://www.example.edu.cn/'
        key = inventory.ensure_site('示例大学', root)
        blocked = root + 'blocked/'
        inventory.enqueue(key, blocked, '需验证学院', 'unit', 1, [], 'school_domain')
        inventory.enqueue(key, root + 'open/', '公开学院', 'unit', 1, [], 'school_domain')
        def fetch(address):
            if address == blocked:
                raise FetchFailure(FetchResult(address, outcome='needs_manual', status=403))
            return {'html': '<main>公开主页</main>', 'url': address, 'status': 200}
        report = crawl_site(inventory, key, workers=1, max_pages=5, fetcher=fetch)
        self.assertEqual(inventory.get_page(key, blocked)['state'], 'blocked')
        self.assertEqual(inventory.get_page(key, root + 'open/')['state'], 'fetched')
        self.assertEqual(report['states'].get('pending', 0), 0)

    def test_upgrade_resumes_only_document_waiters_and_keeps_verification_history(self):
        from backend.database.models import VerificationSession, VerificationWaiter, BackgroundTask
        from backend.services import tasks
        from backend.services.directory_recovery import resume_document_waiters
        task = tasks.enqueue('discover', self.school, {'school_id': self.school})
        session = VerificationSession(id='document-check', source_id=f'discover:{self.school}', origin='https://www.example.edu.cn',
            url='https://www.example.edu.cn/system/_content/download.jsp?wbfileid=abc', status='required', task_id=task.id)
        db.session.add(session); db.session.flush()
        db.session.add(VerificationWaiter(task_id=task.id, session_id=session.id))
        task.state, task.phase, task.checkpoint = 'waiting', 'verification', {'verification_id': session.id}
        generation = task.generation
        db.session.commit()
        self.assertEqual(resume_document_waiters(), 1)
        db.session.refresh(task); db.session.refresh(session)
        self.assertEqual((task.state, task.generation, session.status), ('pending', generation, 'required'))
        self.assertIsNone(db.session.get(VerificationWaiter, task.id))
        task.state, task.phase, task.checkpoint = 'waiting', 'verification', {'verification_id': session.id}
        session.url = 'https://www.example.edu.cn/units'; db.session.commit()
        self.assertEqual(resume_document_waiters(), 0)

    def test_validated_column_resolves_insufficient_fragments_without_erasing_results(self):
        from backend.database.source_governance_models import SourceProposal
        from backend.services.directory_work import coverage
        proposal = SourceProposal(school_id=self.school, origin='test', candidate_json='{}',
            identity_key='1' * 64, proposal_key='2' * 64,
            expected_config_hash='0' * 64, evidence_hash='0' * 64, state='proposed')
        db.session.add(proposal); db.session.flush()
        result = {'candidate_id': 'column', 'decision': 'review', 'reason': '仅有页头，需列表区域'}
        row = DiscoveryWorkItem(identity='resolved-fragment', school_id=self.school, generation=1,
            group_key=f'source:{proposal.id}:fragment:0', candidate_id='column', kind='extraction',
            material_hash='0' * 64, input_json={}, result_json=result, state='succeeded', attempts=1)
        db.session.add(row); db.session.commit()
        self.assertEqual(coverage(self.school)['unresolved_count'], 2)
        proposal.state = 'activated'; db.session.commit()
        self.assertEqual(coverage(self.school)['unresolved_count'], 0)
        self.assertEqual(row.result_json, result)

    def test_identified_directory_is_a_gap_until_its_material_is_processed(self):
        from backend.services.directory_work import coverage
        db.session.add(DiscoveryWorkItem(identity='directory-gap', school_id=self.school, generation=1,
            group_key='page:test', candidate_id='roster-link', kind='classify', material_hash='0' * 64,
            input_json={'candidates': [{'candidate_id': 'roster-link', 'url': 'https://www.example.edu.cn/units.pdf'}]},
            result_json={'candidate_id': 'roster-link', 'kind': 'directory', 'decision': 'propose'}, state='succeeded', attempts=1))
        db.session.commit()
        gaps = coverage(self.school)['gaps']
        self.assertTrue(any(g['state'] == 'directory_material_unprocessed' and g['url'].endswith('units.pdf') for g in gaps))

    def test_partition_preserves_verified_relative_links_and_rejects_unobserved_targets(self):
        from backend.ai.skill_loader import canonical
        evidence = {'school_id': self.school, 'candidates': [{'candidate_id': 'column', 'url': 'https://www.example.edu.cn/news/index.htm'}],
            'observed_urls': ['https://www.example.edu.cn/publisher/'],
            'evidence': [{'evidence_id': 'page', 'url': 'https://www.example.edu.cn/news/index.htm',
                'html': '<main>' + '<p>正文材料</p>' * 4000 + '<a href="../publisher/">部门主页</a><a href="../invented/">无核对地址</a></main>'}]}
        batches = partition(evidence, 'extraction')
        linked = next(b for b in batches if 'href="../publisher/"' in b['evidence'][0].get('html', ''))
        self.assertIn('https://www.example.edu.cn/publisher/', linked['observed_urls'])
        self.assertNotIn('https://www.example.edu.cn/invented/', linked['observed_urls'])
        self.assertTrue(all(len(canonical(b).encode()) <= 24 * 1024 for b in batches))

    def test_partition_keeps_page_address_and_shared_feedback_without_metadata_only_calls(self):
        evidence = {'school_id': self.school, 'candidates': [{'candidate_id': 'column', 'config': {'list_url': 'https://www.example.edu.cn/'}}],
            'observed_urls': [], 'limits': {'calls': 3}, 'available_references': ['identity'],
            'operation_results': [{'url': 'https://www.example.edu.cn/old', 'status': 'fetch_failed'}],
            'evidence': [{'evidence_id': 'page', 'url': 'https://www.example.edu.cn/', 'html': '<main>' + '<p>正文材料</p>' * 4000 + '</main>'}]}
        batches = partition(evidence, 'extraction')
        self.assertTrue(all(b['evidence'][0].get('html') for b in batches))
        self.assertTrue(all('https://www.example.edu.cn/' in b['observed_urls'] for b in batches))
        self.assertTrue(all(b['operation_results'] == evidence['operation_results'] for b in batches))

    def test_incomplete_fragment_absence_never_excludes_the_whole_column(self):
        from backend.services.directory_work import extraction_suggestion
        result, error = extraction_suggestion([{'candidate_id': 'column', 'decision': 'not_column'}], self.evidence)
        self.assertIsNone(result)
        self.assertEqual(error, '')

    def test_saved_proposal_recovery_is_once_and_never_resets_fragment_budget(self):
        from backend.services import source_governance as g
        from backend.services.directory_recovery import recover_saved_results
        from backend.services.source_exploration import save_event, STEP_ACTION
        from backend.database.models import BackgroundTask
        proposal = g.propose_source(self.school, {'name': '通知', 'list_url': 'https://www.example.edu.cn/'}, origin='submitted_entry')
        suggestion = {'candidate_id': f'source-{proposal.id}', 'decision': 'propose', 'config': {'name': '通知'}}
        save_event(proposal.id, STEP_ACTION, {'revision': proposal.revision, 'applied': False,
            'result': {'status': 'needs_recovery', 'output': {'proposals': [suggestion]}}})
        self.assertEqual(recover_saved_results(), 1)
        self.assertEqual(recover_saved_results(), 0)
        task = BackgroundTask.query.filter_by(identity=f'source_review:{proposal.id}').one()
        self.assertTrue(task.payload['saved_results_only'])
        self.assertEqual(DiscoveryWorkItem.query.count(), 0)

    def test_format_retry_passes_feedback_inside_the_same_three_call_budget(self):
        from backend.services.directory_work import run_batch
        with patch('backend.ai.runtime.run_skill', return_value={'status': 'failed', 'error_code': 'invalid_json'}) as invoke:
            self.call(); self.due(); self.call()
            self.assertIsNone(invoke.call_args_list[0].kwargs['repair_feedback'])
            self.assertEqual(invoke.call_args_list[1].kwargs['repair_feedback'], 'invalid_json')
            self.assertTrue(all(r.attempts == 2 for r in DiscoveryWorkItem.query.all()))

    def test_partial_is_saved_and_only_missing_ids_are_retried(self):
        calls = []
        def invoke(*args, **kwargs):
            ids = [r['candidate_id'] for r in args[2]['candidates']]; calls.append(ids)
            keep = ids[:1] if len(calls) == 1 else ids
            return {'status': 'partial', 'error_code': 'connection_lost', 'output': {'results': [
                {'candidate_id': ident, 'kind': 'unknown'} for ident in keep]}}
        with patch('backend.ai.runtime.run_skill', side_effect=invoke):
            first = self.call(); self.assertEqual(first['next_delay'], 30)
            self.assertEqual(DiscoveryWorkItem.query.filter_by(state='succeeded').count(), 1)
            self.due(); self.assertEqual(self.call()['status'], 'succeeded')
            self.assertEqual(self.call()['status'], 'succeeded')
        self.assertEqual(calls, [['0','1','2'], ['1','2']])

    def test_three_failures_exhaust_shared_budget_and_keep_execution_ids(self):
        ids = []
        def invoke(*args, **kwargs):
            ids.append(args[4]); return {'status': 'failed', 'error_code': 'invalid_json'}
        with patch('backend.ai.runtime.run_skill', side_effect=invoke):
            self.assertEqual(self.call()['next_delay'], 30)
            self.due(); self.assertEqual(self.call()['next_delay'], 120)
            self.due(); self.assertEqual(self.call()['status'], 'needs_recovery')
            self.evidence['feedback'] = 'format repair'
            self.assertEqual(self.call()['status'], 'needs_recovery')
        self.assertEqual(len(set(ids)), 3)
        self.assertTrue(all(r.attempts == 3 for r in DiscoveryWorkItem.query.all()))

    def test_quota_pauses_without_automatic_replay(self):
        with patch('backend.ai.runtime.run_skill', return_value={'status': 'failed', 'error_code': 'budget_exhausted'}) as invoke:
            self.assertEqual(self.call()['status'], 'needs_recovery'); self.due(); self.call()
        self.assertEqual(invoke.call_count, 1)
        self.assertEqual(DiscoveryWorkItem.query.filter_by(state='paused').count(), 3)

    def test_late_old_result_cannot_overwrite_revised_material(self):
        def invoke(*args, **kwargs):
            row = DiscoveryWorkItem.query.filter_by(candidate_id='0').first()
            row.material_hash = 'new-version'; row.execution_id = 'new-call'; db.session.commit()
            return {'status': 'succeeded', 'output': {'results': [{'candidate_id': '0', 'kind': 'unit'}]}}
        with patch('backend.ai.runtime.run_skill', side_effect=invoke): self.call()
        self.assertIsNone(DiscoveryWorkItem.query.filter_by(candidate_id='0').first().result_json)

    def test_long_unicode_document_and_more_than_24_candidates_are_all_partitioned(self):
        html = '<main>' + ''.join(f'<section>部门{i}：' + '完整材料' * 1500 + '</section>' for i in range(15)) + '<footer>最后一个部门</footer></main>'
        parts = fragments(html)
        self.assertGreater(len(parts), 12)
        self.assertIn('最后一个部门', ''.join(parts))
        for i in range(15): self.assertIn(f'部门{i}：', ''.join(parts))
        self.evidence['candidates'] = [{'candidate_id': str(i), 'url': f'https://external.example.org/{i}'} for i in range(35)]
        self.evidence['evidence'][0]['html'] = html
        batches = partition(self.evidence, 'classify')
        self.assertEqual({c['candidate_id'] for b in batches for c in b['candidates']}, {str(i) for i in range(35)})
        self.assertTrue(all(len(b['candidates']) <= 8 and len(json.dumps(b, ensure_ascii=False, separators=(',', ':')).encode()) <= 24 * 1024 for b in batches))

    def test_reference_school_tracks_every_department_column_and_cross_domain_entry(self):
        from backend.services import tasks
        from backend.services.source_inventory import Inventory
        from backend.services.source_relationships import ROSTER_RELATIONS
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.scraper.discovery.ai_navigation import process_navigation
        from backend.database.models import BackgroundTask
        from backend.services.source_governance import record_onboarding_slice
        from backend.services.directory_work import coverage
        from backend.ai.skill_loader import load_skill, validate_output
        root = 'https://www.example.edu.cn/'
        expected = {root + f'unit/{i}/': f'第{i}学院' for i in range(30)}
        expected['https://public.example.org/institute/'] = '跨域研究院'
        columns = {url + 'notices/' for url in expected}
        pages = {root: '<title>示例大学</title><nav><a href="/units/">机构设置</a></nav>',
            root + 'units/': '<title>示例大学机构设置</title><main><h1>机构设置</h1><p>' + '公开说明材料。' * 20000 + '</p><ul>' + ''.join(
            f'<li><a href="{url}">{name}</a></li>' for url, name in expected.items()) + '</ul><footer>目录末项已保留</footer></main>'}
        for url, name in expected.items():
            pages[url] = f'<title>示例大学 {name}</title><h1>{name}</h1><nav><a href="{url}notices/">通知公告</a><a href="{root}">示例大学官网</a></nav>'
            pages[url + 'notices/'] = '<title>示例大学通知公告</title><main><h1>通知公告</h1><ul>' + ''.join(
                f'<li><a href="{url}article-{i}.html">学生交流活动相关通知第{i}项</a><time>2026-09-20</time></li>' for i in range(3)) + '</ul></main>'
            for i in range(3): pages[url + f'article-{i}.html'] = '<article>示例大学通知正文</article>'
        inventory = Inventory(self.app.config['DISCOVERY_CACHE_PATH'])
        key = inventory.ensure_site('示例大学', root)
        tasks.enqueue('discover', self.school, {'school_id': self.school, 'ai_assist': True})
        handle = tasks.claim(capabilities=['directory'])
        def fetch(url): return {'html': pages[url], 'status': 200, 'url': url}
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), tasks.execution_scope(handle):
            report = crawl_site(inventory, key, fetcher=fetch)
            # Exercise the complete legacy classification adapter explicitly.
            # Production now skips paid classification for executable routes.
            from backend.scraper.discovery.ai_navigation import queue_navigation
            for page in report['pages']:
                if page['state'] == 'fetched':
                    queue_navigation(report['site'], page)
        report['official_units'] = [u for u in inventory.structure(key) if u['kind'] == 'unit' and u['relation'] in ROSTER_RELATIONS]
        self.assertTrue(set(expected) <= {u['url'] for u in report['official_units']},
            str(set(expected) - {u['url'] for u in report['official_units']}) + str([(u['name'],u['relation']) for u in inventory.structure(key) if u['kind']=='unit']))
        record_onboarding_slice(self.school, report)
        tasks.finish(handle, {'status': 'native_complete'})
        self.assertGreater(BackgroundTask.query.filter_by(kind='navigation_review').count(), 12)
        seen = set()
        def classify(*args, **kwargs):
            request = args[2]
            self.assertLessEqual(len(request['candidates']), 8)
            self.assertLessEqual(len(json.dumps(request, ensure_ascii=False, separators=(',', ':')).encode()), 24 * 1024)
            results = []
            for candidate in request['candidates']:
                url = candidate['url']; seen.add(url)
                kind = 'unit' if url in expected else 'channel' if url in columns else 'non_source'
                propose = kind != 'non_source'
                results.append({'candidate_id': candidate['candidate_id'], 'kind': kind,
                    'name': candidate['name'] if propose else None,
                    'parent_entity_id': 'school' if kind == 'unit' else None,
                    'publisher_entity_id': 'school' if kind == 'channel' else None,
                    'topics': [], 'decision': 'propose' if propose else 'exclude',
                    'evidence': {'identity': ['page'], 'name': ['page'] if propose else [],
                        'parent': ['page'] if kind == 'unit' else [], 'publisher': ['page'] if kind == 'channel' else []},
                    'reason': '固定官网样本', 'needed_evidence': []})
            output = {'school_id': self.school, 'results': results, 'coverage_gaps': []}
            validate_output(load_skill('university-source-onboarding', 'classify'), output, request)
            return {'status': 'succeeded', 'output': output}
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), patch('backend.ai.runtime.run_skill', side_effect=classify):
            for _ in range(300):
                job = tasks.claim(capabilities=['directory'])
                if not job: break
                try:
                    with tasks.execution_scope(job):
                        if job['kind'] == 'discover':
                            result = crawl_site(inventory, key, fetcher=fetch)
                            result['official_units'] = report['official_units']
                            record_onboarding_slice(self.school, result)
                        else:
                            result = process_navigation(job['payload'])
                except tasks.TaskDeferred as deferred:
                    tasks.handoff(job, deferred)
                    db.session.get(BackgroundTask, job['id']).available_at = datetime.utcnow() - timedelta(seconds=1)
                    db.session.commit()
                else: tasks.finish(job, result)
            else: self.fail('fixed reference school did not settle')
        self.assertTrue(set(expected) <= seen, str(set(expected) - seen))
        self.assertTrue(columns <= seen)
        self.assertFalse(coverage(self.school)['complete'], 'Unvalidated columns must remain gaps')
        self.assertTrue(any(g['state'] == 'column_validation_missing' for g in coverage(self.school)['gaps']))

    def test_model_coverage_gap_prevents_complete_even_when_all_ids_return(self):
        result = {'status': 'succeeded', 'output': {'results': [{'candidate_id': str(i), 'kind': 'unit'} for i in range(3)],
            'coverage_gaps': [{'entity_id': None, 'topic': None, 'reason': '还有无法核对的机构入口', 'needed_evidence': ['官方名录']}]}}
        with patch('backend.ai.runtime.run_skill', return_value=result): self.call()
        self.assertEqual(DiscoveryWorkItem.query.filter_by(kind='coverage_gap', state='needs_recovery').count(), 1)
