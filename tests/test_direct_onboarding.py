"""Automatic onboarding must produce notices, not another review queue."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import create_app
from backend.database.db import db
from backend.database.models import Announcement, BackgroundTask, Department, School, Subscription, User
from backend.database.source_governance_models import SourceConfigVersion, SourceProposal

URL = 'https://example.edu.cn/notices/'
TITLES = ['关于开展本科生课程选课工作的通知', '关于开展研究生教学安排工作的通知', '关于开展学生暑期实践工作的通知']
HTML = '<html><title>通知公告 - 测试大学</title><body><section id="notices"><h2>通知公告</h2><ul>' + ''.join(
    f'<li><a href="/info/{i}.htm">{title}</a><span>2026-10-01</span></li>'
    for i, title in enumerate(TITLES)) + '</ul></section></body></html>'


def ai_column(**changes):
    return dict({'name': '通知公告', 'list_selector': '#notices li', 'title_selector': 'a',
                 'link_selector': 'a', 'date_selector': 'span', 'content_selector': 'article',
                 'container_selector': '#notices', 'heading_selector': '#notices h2'}, **changes)


class DirectOnboardingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='direct-onboarding-')
        root = Path(self.temp.name)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'direct-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite:///' + str(root / 'test.db'),
            'SOURCE_CATALOG_PATH': str(root / 'catalog.db'),
            'DISCOVERY_CACHE_PATH': str(root / 'discovery.db'),
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': str(root / 'evidence')})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        school = School(name='测试大学', url='https://example.edu.cn/', subscriber_count=1)
        user = User(username='reader', password_hash='unused')
        db.session.add_all([school, user]); db.session.flush()
        db.session.add(Subscription(user_id=user.id, school_id=school.id))
        db.session.commit()
        self.school_id = school.id
        self.reads = []

    def tearDown(self):
        db.session.remove(); db.engine.dispose(); self.ctx.pop(); self.temp.cleanup()

    def fetch(self, url, purpose):
        self.reads.append((url, purpose))
        if url == URL:
            return HTML
        title = TITLES[int(url.rsplit('/', 1)[-1].split('.')[0])]
        return f'<html><body><article><h1>{title}</h1><p>请各学院按照学校公布的工作安排组织开展相关教学活动，具体事项和时间详见附件。</p></article></body></html>'

    def test_first_fetch_installs_notices_without_proposals_or_duplicate_list_read(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Rules need no AI')):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(SourceProposal.query.count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(self.reads.count((URL, 'directory')), 1)
        self.assertEqual([t.kind for t in BackgroundTask.query.all()], ['collect'])

    def test_attachment_only_notices_can_validate_a_column_without_paid_ai(self):
        from backend.services.source_onboarding import onboard_page
        def fetch(url, purpose):
            if url == URL:
                return HTML
            title = TITLES[int(url.rsplit('/', 1)[-1].split('.')[0])]
            return (f'<html><h1>{title}</h1><p>发布时间：2026-10-01 发布单位：测试大学</p>'
                    '<article><span pdfsrc="/files/notice.pdf"></span></article></html>')
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Attachment notices need no AI')):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)

    def test_unreadable_pinned_items_do_not_block_valid_later_sample(self):
        from backend.services.source_onboarding import onboard_page
        title = '关于开展大学生创新创业项目申报的通知'
        html = HTML.replace('</ul>', f'<li><a href="/info/3.htm">{title}</a><span>2026-10-01</span></li></ul>')
        def fetch(url, purpose):
            if url == URL:
                return html
            if url in (URL.replace('/notices/', '/info/0.htm'), URL.replace('/notices/', '/info/1.htm')):
                return '<p>该内容暂不可见</p>'
            if url.endswith('/info/3.htm'):
                return f'<article><h1>{title}</h1><p>请各学院组织学生参加创新创业项目申报，具体申报办法和时间见通知。</p></article>'
            return self.fetch(url, purpose)
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Later public sample is sufficient')):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 4)

    def test_navigation_images_and_private_attachment_urls_cannot_validate_empty_body(self):
        from backend.services.source_onboarding import _sample_matches
        config = {'content_selector': 'article'}
        record = {'url': URL + '1.htm', 'title': TITLES[0]}
        for resource in ('<span pdfsrc="http://127.0.0.1/file.pdf"></span>',
                         '<img src="/pixel.png" width="1" height="1">', ''):
            html = (f'<html><h1>{TITLES[0]}</h1><nav><img src="/logo.png"></nav>'
                    '<p>发布时间：2026-10-01 发布单位：测试大学</p><article>' + resource + '</article></html>')
            self.assertFalse(_sample_matches(config, [record], lambda *_: html))

    def test_replay_preserves_ids_and_does_not_duplicate_notices_or_versions(self):
        from backend.services.source_onboarding import onboard_page
        payload = {'school_id': self.school_id, 'url': URL}
        first = onboard_page(payload, fetcher=self.fetch)
        second = onboard_page(payload, fetcher=self.fetch)
        self.assertEqual(first['department_ids'], second['department_ids'])
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_explicit_retry_reads_repaired_page_and_only_reopens_once_per_run(self):
        from backend.services import tasks
        from backend.services.source_governance import _snapshot
        from backend.services.source_onboarding import retry_unconnected_columns
        from backend.worker import dispatch
        stale = '<html><h1>网站维护中</h1></html>'
        row = tasks.enqueue('onboard', 'repair', {'school_id': self.school_id, 'url': URL,
            'snapshot': _snapshot(stale, URL, role='list')})
        row.state, row.result = 'done', {'state': 'unsupported'}
        row.checkpoint = {'onboarding_page': row.payload['snapshot']}; db.session.commit()
        retry_unconnected_columns(self.school_id, 'refresh-1')
        handle = tasks.claim(capabilities=['http'])
        self.assertIsNotNone(handle)
        with tasks.execution_scope(handle), patch('backend.services.source_onboarding._read', side_effect=self.fetch):
            result = dispatch(handle['kind'], handle['payload'])
        tasks.finish(handle, result)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(self.reads.count((URL, 'directory')), 1)
        self.assertEqual(Announcement.query.count(), 3)
        db.session.refresh(row)
        generation = row.generation
        row.result = {'state': 'unsupported'}; db.session.commit()
        retry_unconnected_columns(self.school_id, 'refresh-1')
        db.session.refresh(row)
        self.assertEqual((row.state, row.generation), ('done', generation))

    def test_explicit_retry_preserves_connected_columns_and_active_work(self):
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page, retry_unconnected_columns
        result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        done = tasks.enqueue('onboard', 'connected', {'school_id': self.school_id, 'url': URL})
        done.state, done.result = 'done', result
        busy = tasks.enqueue('onboard', 'active', {'school_id': self.school_id, 'url': URL + 'more/'})
        busy.checkpoint = {'onboarding_page': 'saved'}; db.session.commit()
        retry_unconnected_columns(self.school_id, 'refresh-2')
        db.session.refresh(done); db.session.refresh(busy)
        self.assertEqual((done.state, done.generation), ('done', 1))
        self.assertEqual(busy.checkpoint, {'onboarding_page': 'saved'})
        self.assertEqual(busy.generation, 1)
        self.assertEqual(Announcement.query.count(), 3)

    def test_discovery_refresh_does_not_replay_unprobed_failed_column(self):
        from backend.services import tasks
        from backend.worker import dispatch
        child = tasks.enqueue('onboard', 'failed-column', {'school_id': self.school_id, 'url': URL,
            'snapshot': {'old': 'page'}})
        child.state, child.result = 'done', {'state': 'unsupported'}; db.session.commit()
        tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'refresh': True, 'trigger': 'manual_changes'})
        handle = tasks.claim(capabilities=['directory'])
        with tasks.execution_scope(handle), patch('backend.scraper.discovery.inventory_crawler.crawl_site',
                side_effect=RuntimeError('Homepage unavailable')):
            with self.assertRaisesRegex(RuntimeError, 'Homepage unavailable'):
                dispatch(handle['kind'], handle['payload'])
        db.session.rollback()
        tasks.finish(handle, error='Homepage unavailable')
        db.session.refresh(child)
        self.assertEqual((child.state, child.generation), ('done', 1))
        self.assertIn('snapshot', child.payload)
        self.assertIsNone(tasks.claim(capabilities=['http']))

    def test_directory_then_known_columns_precede_unit_expansion_without_dropping_units(self):
        from backend.services.source_inventory import Inventory
        inventory = Inventory(Path(self.temp.name) / 'priority.db')
        root = 'https://example.edu.cn/'
        key = inventory.ensure_site('测试大学', root)
        inventory.finish(key, root, state='fetched')
        inventory.enqueue(key, root + 'units/', '院系设置', 'directory', 1, [], 'school_domain')
        for i in range(30):
            inventory.enqueue(key, root + f'unit-{i}/', f'工程学院{i}学院', 'unit', 2, [], 'school_domain')
        inventory.enqueue(key, URL, '通知公告', 'channel', 1, [], 'school_domain')
        first = inventory.claim(key, focus='all')
        self.assertEqual(first['kind'], 'directory')
        inventory.finish(key, first['url'], state='fetched')
        self.assertEqual(inventory.claim(key, focus='all')['url'], URL)
        inventory.finish(key, URL, state='fetched')
        pending = inventory.report(key)['states']['pending']
        self.assertEqual(pending, 30)
        self.assertEqual(inventory.claim(key, focus='all')['kind'], 'unit')

    def test_ai_protocol_rejects_missing_fields_and_wrong_candidate(self):
        from backend.ai.skill_loader import load_skill, validate_output, SkillValidationError
        skill = load_skill('university-source-onboarding', 'column')
        evidence = {'school_id': self.school_id, 'candidates': [{'candidate_id': 'page'}],
                    'evidence': [{'evidence_id': 'page', 'url': URL, 'html': HTML}]}
        for output in ({'status': 'ready'}, {'candidate_id': 'other', 'status': 'not_column', 'columns': [], 'reason': '无列表'}):
            with self.assertRaises(SkillValidationError):
                validate_output(skill, output, evidence)

    def test_legal_json_with_empty_selector_match_never_installs(self):
        from backend.services.source_onboarding import onboard_page
        output = {'candidate_id': 'page', 'status': 'ready', 'reason': '', 'columns': [{
            'name': '通知公告', 'list_selector': '#missing li', 'title_selector': 'a',
            'link_selector': 'a', 'date_selector': '', 'content_selector': '',
            'container_selector': '#notices', 'heading_selector': '#notices h2'}]}
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value=output):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(Announcement.query.count(), 0)

    def test_ai_rules_are_executed_and_create_readable_notices(self):
        from backend.services.source_onboarding import onboard_page
        output = {'candidate_id': 'page', 'status': 'ready', 'reason': '', 'columns': [{
            'name': '通知公告', 'list_selector': '#notices li', 'title_selector': 'a',
            'link_selector': 'a', 'date_selector': 'span', 'content_selector': 'article',
            'container_selector': '#notices', 'heading_selector': '#notices h2'}]}
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value=output):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual({a.title for a in Announcement.query.all()}, set(TITLES))
        self.assertEqual(SourceProposal.query.count(), 0)

    def test_ai_cannot_join_adjacent_publication_regions_under_one_heading(self):
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('<section id="notices">', '<main id="content"><section id="notices">').replace(
            '</body>', '<section id="news"><h2>校园新闻</h2><ul><li><a href="/info/9.htm">学校举办校园开放日活动</a></li></ul></section></main></body>')
        for heading_html in (html, html.replace('<h2>', '<div class="column-label">').replace('</h2>', '</div>')):
            with self.subTest(custom_heading='column-label' in heading_html):
                output = {'status': 'ready', 'columns': [{
                    'name': '通知公告', 'list_selector': '#content li', 'title_selector': 'a',
                    'link_selector': 'a', 'date_selector': '', 'content_selector': 'article',
                    'container_selector': '#content',
                    'heading_selector': '#notices .column-label' if 'column-label' in heading_html else '#notices h2'}]}
                with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
                     patch('backend.services.source_onboarding.recognize_column', return_value=output):
                    result = onboard_page({'school_id': self.school_id, 'url': URL},
                        fetcher=lambda url, purpose: heading_html if url == URL else self.fetch(url, purpose))
                self.assertEqual(result['state'], 'unsupported')
                self.assertEqual(Department.query.count(), 0)
                self.assertEqual(Announcement.query.count(), 0)

    def test_ai_column_uses_observed_heading_without_invented_publisher_prefix(self):
        from backend.services.source_onboarding import onboard_page
        output = {'status': 'ready', 'columns': [{
            'name': '虚构部门 / 通知公告', 'list_selector': '#notices li', 'title_selector': 'a',
            'link_selector': 'a', 'date_selector': 'span', 'content_selector': 'article',
            'container_selector': '#notices', 'heading_selector': '#notices h2'}]}
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value=output):
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Department.query.one().name, '通知公告')
        self.assertEqual(json.loads(SourceConfigVersion.query.one().config_json)['name'], '通知公告')

    def test_legacy_parent_name_without_matching_website_never_claims_column(self):
        from backend.database.models import DepartmentDirectoryEntry
        from backend.services.source_onboarding import onboard_page
        legacy = Department(school_id=self.school_id, name='学生工作部',
            list_url='https://other.example.edu.cn/', group_name='组织机构')
        db.session.add(legacy); db.session.commit()
        result = onboard_page({'school_id': self.school_id, 'url': URL, 'group_name': legacy.name}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(DepartmentDirectoryEntry.query.count(), 0)

    def test_legacy_parent_with_matching_website_retains_column_membership(self):
        from backend.database.models import DepartmentDirectoryEntry
        from backend.services.source_onboarding import onboard_page
        legacy = Department(school_id=self.school_id, name='学生工作部', list_url=URL, group_name='组织机构')
        db.session.add(legacy); db.session.commit()
        result = onboard_page({'school_id': self.school_id, 'url': URL, 'group_name': legacy.name}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        membership = DepartmentDirectoryEntry.query.one()
        self.assertEqual(membership.parent_id, legacy.id)
        self.assertEqual(membership.department_id, result['department_ids'][0])

    def test_ai_budget_exhaustion_keeps_already_recognized_columns(self):
        from backend.services.source_onboarding import recognize_column
        from backend.ai.configuration import AIConfigError
        column = {'name':'通知公告','list_selector':'#notices li'}
        with patch('backend.ai.configuration.get_model_binding',return_value={'id':1,'version':1}), \
             patch('backend.scraper.discovery.column_regions.materials',return_value=[{'id':1},{'id':2}]), \
             patch('backend.ai.runtime.run_skill',side_effect=[
                 {'status':'succeeded','output':{'status':'ready','columns':[column]}},
                 AIConfigError('budget exhausted','budget_exhausted')]):
            result = recognize_column(self.school_id, URL, HTML)
        self.assertEqual(result['status'],'ready')
        self.assertEqual(result['columns'],[column])
        self.assertEqual(result['unresolved_regions'],1)

    def test_failed_ai_trial_gets_one_evidence_based_correction(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=[
                 {'status': 'ready', 'columns': [ai_column(list_selector='#missing li')]},
                 {'status': 'ready', 'columns': [ai_column()]}]) as recognize:
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(result['new_count'], 3)
        self.assertFalse(result['issues'])
        self.assertEqual(recognize.call_count, 2)
        feedback = recognize.call_args.kwargs['feedback']
        self.assertEqual(feedback[0]['list_selector'], '#missing li')
        self.assertIn('list_missing', feedback[0]['codes'])
        self.assertEqual(SourceProposal.query.count(), 0)

    def test_failed_ai_correction_cannot_repeat_forever(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.services.source_onboarding.publication_lists', return_value=[]), \
             patch('backend.services.source_onboarding.recognize_column', return_value={
                 'status': 'ready', 'columns': [ai_column(list_selector='#missing li')]}) as recognize:
            result = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(recognize.call_count, 2)
        self.assertEqual(Department.query.count(), 0)

    def test_partial_success_survives_feedback_deferral_and_restart(self):
        from backend.services import tasks
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('</body>', '<section id="news"><h2>校园新闻</h2><ul><li><a href="/info/9.htm">'
                            '学校举办校园开放日活动</a></li></ul></section></body>')
        def fetch(url, purpose):
            if url == URL:
                return html
            if url.endswith('/9.htm'):
                return '<article><h1>学校举办校园开放日活动</h1><p>学校邀请在校大学生参加本次校园开放日，具体参与办法详见通知。</p></article>'
            return self.fetch(url, purpose)
        detected = [ai_column(), ai_column(name='校园新闻', list_selector='#missing li',
                    container_selector='#news', heading_selector='#news h2')]
        task = tasks.enqueue('onboard', 'partial-repair', {'school_id': self.school_id, 'url': URL})
        handle = tasks.claim(capabilities=['http'])
        def pending(*args, **kwargs):
            tasks.defer(capability='directory', delay=0, reason='等待栏目识别')
        with tasks.execution_scope(handle), \
             patch('backend.services.source_onboarding.publication_lists', return_value=detected), \
             patch('backend.services.source_onboarding.recognize_column', side_effect=pending) as recognize:
            with self.assertRaises(tasks.TaskDeferred) as deferred:
                onboard_page(task.payload, fetcher=fetch)
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(recognize.call_count, 1)
        tasks.handoff(handle, deferred.exception)
        resumed = tasks.claim(capabilities=['directory'])
        self.assertIsNotNone(resumed)
        with tasks.execution_scope(resumed), \
             patch('backend.services.source_onboarding.publication_lists', side_effect=AssertionError('Rules already executed')), \
             patch('backend.services.source_onboarding.recognize_column', return_value={
                 'status': 'ready', 'columns': [ai_column(name='校园新闻', list_selector='#news li',
                     container_selector='#news', heading_selector='#news h2', date_selector='')]}) as recognize:
            result = onboard_page(task.payload, fetcher=fetch)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(len(result['department_ids']), 2)
        self.assertEqual(result['new_count'], 4)
        self.assertFalse(result['issues'])
        self.assertEqual(Announcement.query.count(), 4)
        self.assertEqual(Department.query.count(), 2)
        self.assertEqual(recognize.call_args.kwargs['feedback'][0]['name'], '校园新闻')
        tasks.finish(resumed, result)

    def test_unrelated_article_is_not_accepted(self):
        from backend.services.source_onboarding import onboard_page
        result = onboard_page({'school_id': self.school_id, 'url': URL},
            fetcher=lambda url, purpose: HTML if purpose == 'directory' else '<h1>用户登录</h1><p>请先登录</p>')
        self.assertEqual(result['state'], 'unsupported')
        self.assertEqual(Department.query.count(), 0)

    def test_provider_json_passes_runtime_contract_execution_and_ingestion(self):
        import os
        from backend.ai.configuration import save_profile, bind_profile
        from backend.ai.models import AIProfile, AIExecution
        from backend.ai.providers import ProviderResult
        from backend.services.source_onboarding import onboard_page
        output = {'candidate_id': 'page', 'status': 'ready', 'reason': '', 'columns': [{
            'name': '通知公告', 'list_selector': '#notices li', 'title_selector': 'a',
            'link_selector': 'a', 'date_selector': 'span', 'content_selector': '',
            'container_selector': '#notices', 'heading_selector': '#notices h2'}]}
        with patch.dict(os.environ, {'FIELD_ENC_KEY': 'isolated-onboarding-test-key'}):
            profile = save_profile({'name': 'test', 'provider': 'deepseek', 'model': 'deepseek-chat', 'api_key': 'not-real'})
            row = db.session.get(AIProfile, profile['id'])
            row.enabled, row.tested_version = True, row.version
            db.session.commit(); bind_profile('directory', row.id)
            response = ProviderResult(json.dumps(output, ensure_ascii=False), 'stop',
                {'known': True, 'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150}, 'test-request')
            with patch('backend.ai.providers.complete', return_value=response) as provider, \
                 patch('backend.services.source_onboarding.publication_lists', return_value=[]):
                first = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
                second = onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
            self.assertEqual(first['state'], 'connected')
            self.assertEqual(first['department_ids'], second['department_ids'])
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(AIExecution.query.one().status, 'succeeded')
            self.assertEqual(Announcement.query.count(), 3)
            messages = provider.call_args.args[2]
            self.assertNotIn('coverage_gaps', json.dumps(messages, ensure_ascii=False))
            self.assertNotIn('actions', json.dumps(messages, ensure_ascii=False))

    def test_retry_is_waiting_not_running_and_does_not_expose_internal_error(self):
        from datetime import datetime, timedelta
        from backend.services import tasks
        from backend.services.onboarding_progress import status
        school = db.session.get(School, self.school_id)
        task = tasks.enqueue('discover', school.id, {'school_id': school.id})
        task.phase = 'retry'; task.available_at = datetime.utcnow() + timedelta(seconds=60)
        task.error = 'private local path'; db.session.commit()
        result = status(school)
        self.assertEqual(result['state'], 'retry_wait')
        self.assertTrue(result['active']); self.assertFalse(result['busy'])
        self.assertNotIn('private local path', str(result))

    def test_official_navigation_flows_through_worker_without_ai_or_review(self):
        from backend.scraper.discovery.inventory_crawler import crawl_site
        from backend.services import tasks
        from backend.worker import dispatch
        root = 'https://example.edu.cn/'
        unit = 'https://cs.example.edu.cn/'
        pages = {root: '<a href="/yxsz.htm">院系设置</a>',
            root + 'yxsz.htm': '<h1>院系设置</h1><a href="' + unit + '">计算机学院</a>',
            unit: '<title>计算机学院</title><a href="/notices/">通知公告</a>', unit + 'notices/': HTML}
        def crawl(*args, **kwargs):
            return crawl_site(*args, **kwargs, fetcher=lambda url: {'html': pages[url], 'url': url, 'status': 200})
        with patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=crawl), \
             patch('backend.ai.providers.complete', side_effect=AssertionError('No AI for ordinary navigation')):
            tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'ai_assist': False})
            for _ in range(4):
                handle = tasks.claim(capabilities=['directory'])
                try:
                    with tasks.execution_scope(handle):
                        result = dispatch('discover', handle['payload'])
                except tasks.TaskDeferred as deferred:
                    tasks.handoff(handle, deferred)
                    from datetime import datetime
                    row = db.session.get(BackgroundTask, handle['id'])
                    row.available_at = datetime.utcnow(); db.session.commit()
                else:
                    tasks.finish(handle, result)
                    break
            self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 1)
            self.assertEqual(SourceProposal.query.count(), 0)
            handle = tasks.claim(capabilities=['http'])
            with tasks.execution_scope(handle), patch('backend.services.source_onboarding._read', side_effect=self.fetch):
                result = dispatch(handle['kind'], handle['payload'])
            tasks.finish(handle, result)
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)
        self.assertTrue(Department.query.filter_by(kind='column').one().list_url.startswith(unit))
        self.assertEqual(Department.query.filter_by(kind='unit').one().name, '计算机学院')

    def test_bad_page_does_not_prevent_next_page_from_connecting(self):
        from backend.services import tasks
        from backend.worker import dispatch
        bad = 'https://example.edu.cn/bad/'
        for i, url in enumerate((bad, URL)):
            tasks.enqueue('onboard', i, {'school_id': self.school_id, 'url': url})
        def fetch(url, purpose):
            return '<html><h1>栏目暂未开放</h1></html>' if url == bad else self.fetch(url, purpose)
        results = []
        with patch('backend.services.source_onboarding._read', side_effect=fetch):
            for _ in range(2):
                handle = tasks.claim(capabilities=['http'])
                with tasks.execution_scope(handle):
                    result = dispatch(handle['kind'], handle['payload'])
                tasks.finish(handle, result); results.append(result['state'])
        self.assertEqual(results, ['unsupported', 'connected'])
        self.assertEqual(Announcement.query.count(), 3)

    def test_single_list_can_use_its_plain_document_title(self):
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('通知公告 - 测试大学', '通知公告').replace('<h2>通知公告</h2>', '')
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Common list needs no AI')):
            result = onboard_page({'school_id': self.school_id, 'url': URL},
                fetcher=lambda url, purpose: html if url == URL else self.fetch(url, purpose))
        self.assertEqual(result['state'], 'connected')
        self.assertEqual(Announcement.query.count(), 3)

    def test_failed_notice_write_rolls_back_source_and_version(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.services.announcement_sources.record_source', side_effect=RuntimeError('write failed')):
            with self.assertRaisesRegex(RuntimeError, 'write failed'):
                onboard_page({'school_id': self.school_id, 'url': URL}, fetcher=self.fetch)
        db.session.rollback()
        self.assertEqual(Department.query.count(), 0)
        self.assertEqual(Announcement.query.count(), 0)
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_upgrade_resumes_missing_ai_but_preserves_user_pause(self):
        from backend.services import tasks
        from backend.services.source_onboarding import resume_rule_discovery
        row = tasks.enqueue('discover', self.school_id, {'school_id': self.school_id, 'require_ai': True})
        row.state, row.phase = 'waiting', 'ai_setup'; db.session.commit()
        resume_rule_discovery(); db.session.refresh(row)
        self.assertEqual((row.state, row.phase), ('pending', 'fetch'))
        row.state, row.phase = 'waiting', 'user_paused'
        row.payload = dict(row.payload, discovery_pause_requested=True); db.session.commit()
        resume_rule_discovery(); db.session.refresh(row)
        self.assertEqual((row.state, row.phase), ('waiting', 'user_paused'))

    def test_capacity_upgrade_resumes_only_matching_unpaused_jobs(self):
        from backend.services import tasks
        from backend.services.source_onboarding import resume_rule_discovery
        jobs = []
        for key, error, paused in [('capacity', '调查缓存已达到容量上限，已保存进度，请先清理后继续', False),
                                   ('network', 'HTTP 503', False),
                                   ('paused', '调查缓存已达到容量上限，已保存进度，请先清理后继续', True)]:
            row = tasks.enqueue('discover', key, {'school_id': self.school_id, 'discovery_pause_requested': paused})
            row.state, row.error, row.attempts = 'failed', error, 3
            row.checkpoint = {'pending_pages': 17}
            jobs.append(row)
        db.session.commit()
        resume_rule_discovery()
        for row in jobs: db.session.refresh(row)
        self.assertEqual([r.state for r in jobs], ['pending', 'failed', 'failed'])
        self.assertEqual(jobs[0].attempts, 0)
        self.assertEqual(jobs[0].checkpoint, {'pending_pages': 17})
        self.assertEqual(jobs[0].generation, 1)

    def test_disabled_ai_never_blocks_or_calls_paid_fallback(self):
        from backend.services.source_onboarding import onboard_page
        with patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('AI disabled')):
            result = onboard_page({'school_id': self.school_id, 'url': URL, 'ai_assist': False},
                                  fetcher=lambda url, purpose: '<html><title>暂未识别</title></html>')
        self.assertEqual(result['state'], 'unsupported')
        with patch('backend.services.source_onboarding.recognize_column', side_effect=AssertionError('Rules need no AI')):
            result = onboard_page({'school_id': self.school_id, 'url': URL, 'ai_assist': False}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected')

    def test_new_column_inherits_existing_unit_subscription(self):
        from backend.services.source_onboarding import onboard_page
        from backend.services.inbox_refresh import subscribed_sources
        unit = Department(school_id=self.school_id, name='计算机学院', list_url='https://example.edu.cn/cs/')
        db.session.add(unit); db.session.flush()
        sub = Subscription.query.one(); sub.department_ids = [unit.id]; db.session.commit()
        column_url = unit.list_url + 'notices/'
        result = onboard_page({'school_id': self.school_id, 'url': column_url, 'group_name': unit.name},
            fetcher=lambda url, purpose: HTML if url == column_url else self.fetch(url, purpose))
        self.assertEqual(result['state'], 'connected')
        self.assertEqual([d.id for d in subscribed_sources(unit.school, sub.department_ids)], result['department_ids'])
        self.assertEqual(BackgroundTask.query.filter_by(kind='collect').count(), 1)

    def test_waiting_column_has_access_verification_action(self):
        from backend.services import tasks
        from backend.services.onboarding_progress import status
        job = tasks.enqueue('onboard', 'waiting', {'school_id': self.school_id, 'url': URL})
        job.state, job.phase = 'waiting', 'verification'; db.session.commit()
        result = status(db.session.get(School, self.school_id))
        self.assertEqual(result['state'], 'waiting')
        self.assertTrue(result['needs_verification'])
        self.assertFalse(result['busy'])

    def test_template_dates_split_into_day_and_year_month_are_read_correctly(self):
        from backend.services.source_onboarding import onboard_page
        html = HTML.replace('<ul>', '<ul class="news_list">').replace('<li>', '<li class="news">').replace(
            '<span>2026-10-01</span>', '<div class="news_date"><div class="news_year">01</div><div class="news_days">2026.10</div></div>')
        for i, title in enumerate(TITLES):
            html = html.replace(f'<a href="/info/{i}.htm">', f'<a title="{title}" href="/info/{i}.htm">')
        from bs4 import BeautifulSoup
        from backend.scraper.cms_registry import load_selector_profiles
        from backend.scraper.date_elements import publication_date_text
        from backend.scraper.change_detector import parse_date
        from backend.scraper.discovery.publication_lists import select_node
        profile = next(p for p in load_selector_profiles() if p['name'] == '苏迪列表 (news_list 属性标题)')
        item = BeautifulSoup(html, 'lxml').select_one(profile['list_selector'])
        self.assertIsNotNone(parse_date(publication_date_text(select_node(item, profile['date_selector']))))
        result = onboard_page({'school_id': self.school_id, 'url': URL},
            fetcher=lambda url, purpose: html if url == URL else self.fetch(url, purpose))
        self.assertEqual(result['state'], 'connected')
        self.assertTrue(all((a.published_at.year, a.published_at.month, a.published_at.day) == (2026, 10, 1)
                            for a in Announcement.query.all()))


if __name__ == '__main__':
    unittest.main()
