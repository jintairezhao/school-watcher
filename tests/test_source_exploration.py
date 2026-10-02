"""Exercise the model/worker/FetchRequest boundary, including persisted resumption."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend import create_app
from backend.database.db import db
from backend.database.models import School
from backend.database.source_governance_models import SourceReviewEvent
from backend.services import source_governance as g
from backend.scraper.acquisition import FetchResult, FetchFailure

ROOT = 'https://www.example.edu.cn/'
LIST = '<html><main><h1>通知公告</h1><ul>' + ''.join(
    f'<li><a href="/notice/{i}.html">关于第{i}届学生交流活动的通知</a><time>2026-09-20</time></li>'
    for i in range(1, 4)) + '</ul></main></html>'


class ExplorationTests(unittest.TestCase):
    def test_publisher_reference_uses_archived_full_snapshot_when_catalogue_html_was_pruned(self):
        from unittest.mock import MagicMock
        html = '<html><title>示例学院</title><a href="/">示例学院</a></html>'
        reference = g._snapshot(html, ROOT+'units.html', role='structure')
        path = {'unit_name': '示例学院', 'references': [{'url': reference['url'], 'content_hash': reference['hash']}]}
        inventory = MagicMock()
        inventory.report.return_value = {'pages': []}
        inventory.snapshot.return_value = ''
        with patch('backend.services.source_relationships.SourceRelationships') as relation, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            relation.return_value.paths_for.return_value = [path]
            relation.return_value.publication_owners.return_value = {'示例学院'}
            evidence = g.capture_source_evidence(self.p.school_id, self.config, inventory=inventory)
        snapshots = evidence.bundle['identity_snapshots']
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(g.read_snapshot(snapshots[0]), html)

    def test_pending_journal_reuses_valid_proposal_before_repartitioning_updated_material(self):
        from backend.services.source_exploration import material, save_event, STEP_ACTION
        result = self.output()
        result['status'] = 'needs_recovery'; result['error_code'] = 'output_validation_failed'
        save_event(self.p.id, STEP_ACTION, {'attempt': 4, 'revision': self.p.revision,
            'status': 'needs_recovery', 'applied': False, 'manifest_started': True,
            'input': material(self.p), 'result': result})
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill') as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            actual = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(actual['state'], 'activated')
        model.assert_not_called()

    def test_saved_valid_column_reaches_activation_despite_failed_footer_without_new_model_call(self):
        from backend.services.source_exploration import material
        from backend.services.directory_work import prepare
        evidence = material(self.p)
        footer = dict(evidence, evidence=[{'evidence_id': 'footer', 'url': ROOT, 'html': '<footer>友情链接</footer>'}])
        good = prepare(self.p.school_id, 1, f'source:{self.p.id}:fragment:0', 'extraction', evidence)[0]
        good.state = 'succeeded'; good.result_json = self.output()['output']['proposals'][0]; good.attempts = 1
        failed = prepare(self.p.school_id, 1, f'source:{self.p.id}:fragment:1', 'extraction', footer)[0]
        failed.state = 'needs_recovery'; failed.attempts = 3; failed.error_code = 'output_validation_failed'
        db.session.commit()
        with patch('backend.services.directory_work.partition', return_value=[evidence, footer]), \
             patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill') as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            result = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(result['state'], 'activated')
        model.assert_not_called()
        self.assertEqual(failed.attempts, 3)
        self.assertEqual(good.result_json['decision'], 'propose')
        self.assertEqual(failed.state, 'covered')
        self.assertEqual(failed.error_code, 'output_validation_failed')
        self.assertTrue(SourceReviewEvent.query.filter_by(proposal_id=self.p.id, action='resolve_column_material').first())

    def test_legacy_failure_keeps_original_record_and_reloads_full_snapshot(self):
        from backend.services.source_exploration import STEP_ACTION, save_event
        bundle = json.loads(self.p.evidence_json)
        html = LIST.replace('</main>', '<footer>旧截断之后的完整目录材料</footer></main>')
        bundle['list'] = g._snapshot(html, ROOT)
        self.p.evidence_json = g._json(bundle); db.session.commit()
        old = save_event(self.p.id, STEP_ACTION, {'execution_id': 'old-paid-call', 'attempt': 1,
            'revision': self.p.revision, 'status': 'uncertain', 'applied': False,
            'input': {'truncated': True}, 'result': {'status': 'uncertain', 'error_code': 'response_limit'}})
        seen = []
        def model(*args, **kwargs):
            seen.append(args[2]); return self.output('not_column')
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=model):
            g.run_source_skill_for_proposal(self.p.id)
        original = json.loads(old.detail_json)
        self.assertEqual(original['status'], 'uncertain')
        self.assertEqual(original['result']['error_code'], 'response_limit')
        self.assertEqual(original['input'], {'truncated': True})
        self.assertTrue(any('旧截断之后的完整目录材料' in ref.get('html', '') for request in seen for ref in request['evidence']))
        self.assertEqual(SourceReviewEvent.query.filter_by(proposal_id=self.p.id, action=STEP_ACTION).count(), 2)

    def test_current_page_replaces_old_alias_and_preserves_body_failure_for_ai(self):
        from backend.services.source_exploration import material
        bundle = json.loads(self.p.evidence_json)
        bundle['publisher_material'] = {'old_alias': g._snapshot('<h1>旧首页</h1>', ROOT)}
        bundle['exploration_pages'] = [g._snapshot('<article class="notice" style="font-size:12px" onclick="evil()">正文资料</article>',
                                                  ROOT+'notice/1.html', role='article', outcome='needs_adapter')]
        bundle['samples'] = {'article:'+ROOT+'notice/1.html': 'failed: content_not_recognized'}
        self.p.evidence_json = g._json(bundle); db.session.commit()
        evidence = material(self.p)
        self.assertFalse(any('旧首页' in ref.get('html','') for ref in evidence['evidence']))
        body = next(ref for ref in evidence['evidence'] if ref.get('role')=='article')
        self.assertEqual(body['outcome'],'needs_adapter')
        self.assertIn('class="notice"',body['html'])
        self.assertNotIn('onclick',body['html']); self.assertNotIn('style=',body['html'])
        self.assertEqual(evidence['acquisition_results'],bundle['samples'])

    def test_upgrade_revalidates_saved_ai_plan_without_repaying_exhausted_calls(self):
        from backend.services.source_exploration import STEP_ACTION
        self.p.validation_json = g._json({'passed':False, 'errors':['list_missing'], 'validator_version':'old-validator'})
        for attempt in range(1,5):
            db.session.add(SourceReviewEvent(proposal_id=self.p.id, action=STEP_ACTION,
                detail_json=g._json({'status':'succeeded', 'result':self.output(), 'applied':True,
                                    'attempt':attempt, 'revision':self.p.revision})))
        db.session.commit()
        with patch('backend.ai.runtime.run_skill') as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            result = g.process_source_review({'proposal_id':self.p.id})
        self.assertEqual(result['state'],'activated')
        model.assert_not_called()
        self.assertEqual(SourceReviewEvent.query.filter_by(proposal_id=self.p.id, action=STEP_ACTION).count(),4)
        self.assertEqual(SourceReviewEvent.query.filter_by(proposal_id=self.p.id, action='revalidate_after_upgrade').count(),1)

    def test_selector_typo_returns_feedback_without_browser_or_article_reads(self):
        calls = []
        def model(*args, **kwargs):
            evidence = args[2]; calls.append(evidence)
            if len(calls)==1:
                result = self.output(); result['output']['proposals'][0]['config']['list_selector']='section.nonexistent li'
                return result
            self.assertEqual(evidence['operation_results'][-1]['matched'],0)
            return self.output()
        with patch('backend.ai.configuration.get_model_binding',return_value={'version':1}), \
             patch('backend.ai.runtime.run_skill',side_effect=model), \
             patch('backend.scraper.acquisition.fetch_or_raise',side_effect=self.transport) as wire:
            result = g.run_source_skill_for_proposal(self.p.id)
        self.assertTrue(json.loads(self.p.validation_json)['passed'])
        self.assertEqual(len(calls),2)
        self.assertEqual(sum(c.args[0].purpose=='article' for c in wire.call_args_list),3)

    def test_address_change_deferred_mid_validation_keeps_page_identity_and_paid_result(self):
        from backend.services.tasks import TaskDeferred
        from backend.services.source_exploration import references
        home = '<title>示例大学</title><a href="/notices/">通知公告</a>'
        listing = '<title>示例大学通知公告</title>' + LIST
        old_bundle = json.loads(self.p.evidence_json)
        old_bundle['list'] = g._snapshot(home, ROOT)
        self.p.evidence_json = g._json(old_bundle); self.p.evidence_hash = g._hash(old_bundle); db.session.commit()
        output = self.output()
        output['output']['proposals'][0]['config']['list_url'] = ROOT+'notices/'
        interrupted = False
        def wire(request):
            nonlocal interrupted
            if request.purpose == 'article' and not interrupted:
                interrupted = True
                raise TaskDeferred(capability='browser', reason='resume validation')
            if request.purpose == 'list':
                return FetchResult(request.url, html=listing, status=200, outcome='usable')
            return self.transport(request)
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=output) as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=wire):
            with self.assertRaises(TaskDeferred):
                g.process_source_review({'proposal_id': self.p.id})
            result = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['state'], 'activated')
        bundle = json.loads(self.p.evidence_json)
        self.assertEqual(g.read_snapshot(bundle['list']), listing)
        self.assertTrue(any(r['url']==ROOT and g.read_snapshot(r)==home for _,r in references(bundle)))

    def test_configured_ai_judges_list_before_any_guessed_body_reads(self):
        sequence = []
        def model(*args, **kwargs):
            sequence.append('ai')
            self.assertFalse(any(x['evidence_id'].startswith('articles-') for x in args[2]['evidence']))
            return self.output()
        def transport(request):
            sequence.append(request.purpose)
            return self.transport(request)
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=model), \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            result = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(sequence[0], 'ai')
        self.assertIn('article', sequence)

    def test_unrecognized_readable_entry_is_given_to_ai_before_validation(self):
        self.p.evidence_json = '{}'; self.p.evidence_hash = g._hash({}); db.session.commit()
        seen = []
        def transport(request):
            seen.append(request.purpose)
            if request.purpose == 'directory':
                raise FetchFailure(FetchResult(ROOT, html=LIST, status=200, outcome='needs_adapter',
                                               error_code='content_not_recognized'))
            return self.transport(request)
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=self.output()) as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            result = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(result['state'], 'activated')
        self.assertEqual(model.call_count, 1)
        self.assertEqual(seen[0], 'directory')

    def test_recheck_restores_obtained_pages_without_fetching_them_again(self):
        from backend.services.source_exploration import acquire
        url = ROOT+'notice/1.html'
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            acquire(self.p, {'type': 'read_page', 'url': url, 'purpose': 'article'}, {url})
        bundle = json.loads(self.p.evidence_json)
        bundle.pop('exploration_pages'); bundle.pop('operation_results')
        self.p.evidence_json = g._json(bundle); self.p.evidence_hash = g._hash(bundle); db.session.commit()
        def model(*args, **kwargs):
            self.assertTrue(any(ref.get('url') == url for ref in args[2]['evidence']))
            return self.output('not_column')
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=model), \
             patch('backend.scraper.acquisition.fetch_or_raise') as wire:
            g.run_source_skill_for_proposal(self.p.id)
        wire.assert_not_called()
        self.assertEqual(self.p.state, 'not_applicable')

    def test_invalid_output_waits_before_bounded_retry_and_keeps_diagnostics(self):
        failed = {'status': 'failed', 'error_code': 'output_validation_failed',
                  'error_detail': 'candidate_coverage_mismatch', 'usage': {'known': True, 'total_tokens': 100}}
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', return_value=failed) as model:
            g.run_source_skill_for_proposal(self.p.id)
            g.run_source_skill_for_proposal(self.p.id)
            self.assertEqual(model.call_count, 1)
            from backend.database.source_governance_models import DiscoveryWorkItem
            from datetime import datetime, timedelta
            for row in DiscoveryWorkItem.query.all():
                row.next_run_at = datetime.utcnow() - timedelta(seconds=1)
            db.session.commit()
            g.run_source_skill_for_proposal(self.p.id)
        self.assertEqual(model.call_count, 2)
        self.assertIn('output_validation_failed', json.loads(self.p.validation_json)['workflow']['reason'])
        self.assertNotEqual(self.p.state, 'activated')

    def test_known_usage_settles_reservation_but_unknown_usage_keeps_it(self):
        from backend.services.source_exploration import charged_tokens
        self.assertEqual(charged_tokens({'reservation': 10000, 'result': {
            'usage': {'known': True, 'total_tokens': 1200}}}), 1200)
        for usage in ({'known': False, 'total_tokens': 1200}, {'known': True},
                      {'known': True, 'total_tokens': -1}):
            self.assertEqual(charged_tokens({'reservation': 10000, 'result': {'usage': usage}}), 10000)

    def test_factual_uncertainty_is_not_sent_to_user_as_a_choice(self):
        from backend.services.source_exploration import material
        from backend.services.source_workflow import supported_question
        evidence = material(self.p)
        question = {'text': '希望订阅哪一个栏目？', 'purpose': 'subscription_preference', 'options': [
            {'id': str(i), 'field': 'list_url', 'value': ROOT+f'notice/{i}.html',
             'label': str(i), 'evidence_ids': ['list']} for i in (1, 2)]}
        self.assertIsNotNone(supported_question(self.p, question, evidence))
        question.pop('purpose')
        self.assertIsNone(supported_question(self.p, question, evidence))
        question['purpose'] = 'subscription_preference'
        question['options'][0].update(field='group_name', value='某部门')
        evidence['entities'] = [{'name': '某部门'}]
        self.assertIsNone(supported_question(self.p, question, evidence))

    def test_contract_rejects_code_and_accepts_observed_read(self):
        from backend.ai.skill_loader import load_skill, validate_output, SkillValidationError
        from backend.services.source_exploration import material
        evidence = material(self.p)
        output = self.output('explore', actions=[dict(type='read_page', url=ROOT+'notice/1.html', purpose='article', reason='需要正文')])['output']
        skill = load_skill('university-source-onboarding', 'extraction')
        validate_output(skill, output, evidence)
        output['proposals'][0]['actions'][0]['code'] = 'fetch(secret)'
        with self.assertRaises(SkillValidationError):
            validate_output(skill, output, evidence)

    def test_material_is_cached_and_unknown_reference_is_rejected(self):
        from backend.services.source_exploration import acquire
        action = dict(type='read_page', url=ROOT+'notice/1.html', purpose='article', reason='需要正文')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport) as wire:
            first = acquire(self.p, action, {action['url']})
            second = acquire(self.p, action, {action['url']})
        self.assertEqual((first['status'], second['status']), ('obtained', 'cached'))
        self.assertEqual(wire.call_count, 1)
        self.assertEqual(acquire(self.p, {'type': 'read_reference', 'reference': '../../.env'}, set())['status'], 'invalid_reference')

    def test_fetch_failure_is_returned_to_ai_as_operation_feedback(self):
        calls = []
        def model(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 1:
                return self.output('explore', actions=[dict(type='read_page', url=ROOT+'notice/1.html', purpose='article', reason='需要正文')])
            self.assertEqual(args[2]['operation_results'][0]['status'], 'login_required')
            return self.output('review', actions=[dict(type='read_reference', reference='identity', reason='检查归属依据')])
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=model), \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=FetchFailure(FetchResult(ROOT+'notice/1.html',
                outcome='denied', error_code='source_login_required', message='要求登录'))):
            g.run_source_skill_for_proposal(self.p.id)
        self.assertGreaterEqual(len(calls), 2)
        self.assertLessEqual(len(calls), 4)

    def test_configured_quota_pauses_without_repeated_calls(self):
        from backend.ai.configuration import AIConfigError
        from backend.database.source_governance_models import DiscoveryWorkItem
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1}), \
             patch('backend.ai.runtime.run_skill', side_effect=AIConfigError('quota', 'budget_exhausted')) as model:
            g.run_source_skill_for_proposal(self.p.id)
            g.run_source_skill_for_proposal(self.p.id)
        self.assertEqual(model.call_count, 1)
        self.assertEqual(DiscoveryWorkItem.query.filter_by(state='paused').count(), 1)
        self.assertNotEqual(self.p.state, 'activated')

    def test_generic_article_metadata_blocks_article_as_column(self):
        from backend.services.source_review_recovery import page_problem
        html = '<meta property="og:type" content="article"><div class="v_news_content">' + '正文资料' * 40 + '</div>' + LIST
        self.assertEqual(page_problem(html), 'article_instead_of_column')

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test', 'SQLALCHEMY_DATABASE_URI': 'sqlite://',
            'SOURCE_INVENTORY_PATH': Path(self.tmp.name) / 'inventory.sqlite3',
            'SOURCE_CATALOG_PATH': Path(self.tmp.name) / 'catalog.sqlite3',
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': Path(self.tmp.name) / 'evidence'})
        self.ctx = self.app.app_context(); self.ctx.push(); db.create_all()
        school = School(name='示例大学', url=ROOT); db.session.add(school); db.session.commit()
        self.config = dict(name='通知公告', list_url=ROOT, list_selector='main li', title_selector='a',
                           link_selector='a', date_selector='time', content_selector='article', group_name='')
        self.p = g.propose_source(school.id, self.config)
        bundle = dict(schema=1, school_id=school.id, school_name=school.name, root_url=ROOT,
            config_hash=g._hash(self.config), expected_config={}, list=g._snapshot(LIST, ROOT),
            articles=[], identity_paths=[], identity_snapshots=[])
        self.p.evidence_json = g._json(bundle); self.p.evidence_hash = g._hash(bundle); db.session.commit()

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def output(self, decision='propose', **extra):
        return {'status': 'succeeded', 'output': {'proposals': [dict(candidate_id=f'source-{self.p.id}',
            decision=decision, config=dict(self.config, pagination_url=None), evidence_ids=['list'],
            reason='列表与正文一致', needed_evidence=[], **extra)]}, 'usage': {'known': True, 'total_tokens': 200}}

    def transport(self, request):
        html = LIST if request.purpose != 'article' else '<article><h1>关于第' + request.url.split('/')[-1][0] + '届学生交流活动的通知</h1><p>' + '这是官网发布的学生交流通知正文。' * 20 + '</p></article>'
        return FetchResult(request.url, status=200, html=html, outcome='usable')

    def test_material_request_is_executed_then_sent_back_and_validated(self):
        calls, requests = [], []
        def model(*args, **kwargs):
            calls.append(args[2])
            if len(calls) == 1:
                return self.output('explore', actions=[dict(type='read_page', url=ROOT+'notice/1.html', purpose='article', reason='读取正文')])
            self.assertTrue(any('这是官网发布' in ref.get('html', '') for ref in args[2]['evidence']))
            return self.output()
        def wire(request):
            requests.append(request); return self.transport(request)
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1, 'id': 1, 'max_output_tokens': 4000}), \
             patch('backend.ai.runtime.run_skill', side_effect=model), \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=wire):
            result = g.run_source_skill_for_proposal(self.p.id)
        self.assertEqual(len(calls), 2)
        self.assertEqual(requests[0].purpose, 'article')
        self.assertTrue(result['changed'])
        self.assertTrue(json.loads(self.p.validation_json)['passed'])
        self.assertTrue(SourceReviewEvent.query.filter_by(proposal_id=self.p.id, action='exploration_read').count())

    def test_unobserved_link_never_reaches_transport(self):
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1, 'id': 1, 'max_output_tokens': 4000}), \
             patch('backend.ai.runtime.run_skill', return_value=self.output('explore', actions=[dict(
                 type='read_page', url='https://www.example.edu.cn/invented', purpose='article', reason='猜测')])), \
             patch('backend.scraper.acquisition.fetch_or_raise') as wire:
            g.run_source_skill_for_proposal(self.p.id)
        wire.assert_not_called()

    def test_successful_model_step_is_resumed_without_paying_again(self):
        from backend.services.source_exploration import STEP_ACTION
        suggestion = self.output('explore', actions=[dict(type='read_page', url=ROOT+'notice/1.html', purpose='article', reason='读取正文')])
        db.session.add(SourceReviewEvent(proposal_id=self.p.id, action=STEP_ACTION,
            detail_json=g._json({'status': 'succeeded', 'result': suggestion, 'applied': False, 'attempt': 1,
                                'revision': self.p.revision, 'execution_id': 'resumed'})))
        db.session.commit()
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 1, 'id': 1, 'max_output_tokens': 4000}), \
             patch('backend.ai.runtime.run_skill', return_value=self.output()) as model, \
             patch('backend.scraper.acquisition.fetch_or_raise', side_effect=self.transport):
            result = g.run_source_skill_for_proposal(self.p.id)
        self.assertEqual(model.call_count, 1)
        self.assertTrue(result['changed'])

    def test_login_and_program_failure_have_different_responsibility(self):
        # Keep the exception visible to the worker while persisting its responsibility.
        self.p.candidate_json = g._json(dict(self.config, list_selector='')); db.session.commit()
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=ValueError('Unknown acquisition purpose')):
            with self.assertRaises(ValueError):
                g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(g.serialize_proposal(self.p)['workflow']['owner'], 'developer')
        self.p.state = 'proposed'; db.session.commit()
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=FetchFailure(FetchResult(ROOT,
                outcome='denied', error_code='source_login_required', message='官网要求登录'))):
            result = g.process_source_review({'proposal_id': self.p.id})
        self.assertEqual(result['workflow']['status'], 'login_required')
        self.assertFalse(result['workflow']['requires_user'])
