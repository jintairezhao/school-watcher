"""Source identity, independent proof and atomic publication regressions."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import create_app
from backend.database.db import db
from backend.database.models import Department, School, User, Announcement, Subscription
from backend.database.source_governance_models import SourceProposal, SourceConfigVersion, SourceReviewEvent
from backend.services import source_governance as governance


class SourceGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'source-governance-test',
            'SQLALCHEMY_DATABASE_URI': 'sqlite://',
            'SOURCE_INVENTORY_PATH': Path(self.tmp.name) / 'inventory.sqlite3',
            'SOURCE_GOVERNANCE_EVIDENCE_PATH': Path(self.tmp.name) / 'evidence',
            'SOURCE_CATALOG_PATH': Path(self.tmp.name) / 'catalog.sqlite3'})
        self.ctx = self.app.app_context(); self.ctx.push()
        db.create_all()
        self.school = School(name='示例大学', url='https://www.example.edu.cn/')
        self.admin = User(username='admin', password_hash='unused', role='admin')
        self.user = User(username='reader', password_hash='unused')
        db.session.add_all([self.school, self.admin, self.user]); db.session.flush()
        self.dept = Department(school_id=self.school.id, name='通知公告', group_name='示例学院',
            list_url='https://college.example.edu.cn/notices/', list_selector='#notices li',
            title_selector='a', link_selector='a', date_selector='time')
        db.session.add(self.dept); db.session.commit()
        # Independent official roster plus the actual unit website identity and
        # its observed notice link. Legacy selectors themselves prove nothing.
        from backend.services.source_inventory import Inventory, site_key
        from backend.scraper.discovery.inventory_crawler import inspect_page
        self.identity = Inventory(Path(self.tmp.name) / 'identity.sqlite3')
        identity_key = self.identity.ensure_site(self.school.name, self.school.url)
        with self.identity.connect() as connection:
            connection.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?", (identity_key,))
        report = self.identity.report(identity_key)
        inspect_page(self.identity, report['site'], report['pages'][0], fetcher=lambda url: {
            'url': url, 'status': 200,
            'html': '<main><h4><a href="https://college.example.edu.cn/">示例学院</a></h4></main>'})
        report = self.identity.report(identity_key)
        unit = next(page for page in report['pages'] if page['url'] == 'https://college.example.edu.cn/')
        inspect_page(self.identity, report['site'], unit, fetcher=lambda url: {'url': url, 'status': 200,
            'html': '<title>示例学院</title><h1>示例学院</h1><nav><a href="/notices/">通知公告</a></nav>'})

    def tearDown(self):
        db.session.remove(); db.drop_all(); self.ctx.pop()

    def html(self, count=3, extra=''):
        return '<section id="notices"><h2>通知公告</h2><ul>' + ''.join(
            f'<li><a href="article/{i}.htm">关于研究生报名事项的通知{i}</a><time>2026-09-24</time></li>'
            for i in range(count)) + '</ul></section>' + extra

    def fetcher(self, html):
        def fetch(url, purpose):
            if purpose == 'body':
                index = url.rsplit('/', 1)[-1].split('.')[0]
                return '<article><h1>关于研究生报名事项的通知' + index + '</h1><p>请符合条件的同学认真阅读本通知，并按照学校要求在指定时间提交报名材料。</p></article>'
            return html
        return fetch

    def proposal(self, config=None, html=None):
        config = config or governance.source_config(self.dept)
        html = html or self.html()
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
                                                     seed_html=html, fetcher=self.fetcher(html), inventory=self.identity)
        return governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)

    def test_valid_short_list_does_not_require_three_nonexistent_notices(self):
        proposal = self.proposal(html=self.html(1))
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        self.assertEqual(result['item_count'], 1)
        self.assertEqual(governance.activate_proposal(proposal.id).id, self.dept.id)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(governance.activate_proposal(proposal.id).id, self.dept.id)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_adjacent_news_widget_cannot_replace_notices(self):
        html = self.html(2, '<section id="news"><h2>学院新闻</h2><ul><li><a href="article/9.htm">关于研究生报名事项的通知9</a></li></ul></section>')
        config = governance.source_config(self.dept); config['list_selector'] = '#news li'
        proposal = self.proposal(config, html)
        self.assertFalse(governance.validate_proposal(proposal.id)['passed'])
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)
        self.assertEqual(self.dept.list_selector, '#notices li')

    def test_broad_selector_leaking_across_widgets_is_rejected(self):
        html = self.html(2, '<section><h2>学术活动</h2><ul><li><a href="article/7.htm">关于研究生报名事项的通知7</a></li></ul></section>')
        config = governance.source_config(self.dept); config['list_selector'] = 'li'
        proposal = self.proposal(config, html)
        self.assertFalse(governance.validate_proposal(proposal.id)['passed'])

    def test_wrong_publisher_goes_to_review_even_with_real_articles(self):
        config = governance.source_config(self.dept); config['group_name'] = '外国语学院'
        proposal = self.proposal(config)
        result = governance.validate_proposal(proposal.id)
        self.assertIn('publisher_conflict', result['errors'])
        self.assertEqual(proposal.state, 'needs_review')

    def test_client_verified_flag_and_arbitrary_evidence_cannot_install(self):
        config = governance.source_config(self.dept)
        with self.assertRaises(ValueError):
            governance.propose_source(self.school.id, config, {'verified': True})
        from backend.services.source_catalog import apply_source_configs
        config.update(name='另一个通知栏目', source_verified=True)
        self.assertEqual(apply_source_configs(self.school.id, [config]), 0)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(SourceProposal.query.one().state, 'proposed')

    def test_stale_proposal_preserves_current_config_and_history(self):
        db.session.add(Announcement(school_id=self.school.id, department_id=self.dept.id, title='历史通知'))
        db.session.commit()
        proposal = self.proposal()
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        self.dept.name = '已经核实的新名称'; db.session.commit()
        with self.assertRaisesRegex(ValueError, '已经更新'):
            governance.activate_proposal(proposal.id)
        self.assertEqual(self.dept.name, '已经核实的新名称')
        self.assertEqual(Announcement.query.count(), 1)
        self.assertEqual(proposal.state, 'stale')

    def test_lease_loss_prevents_source_publication(self):
        from backend.services.tasks import LeaseLost
        proposal = self.proposal()
        governance.validate_proposal(proposal.id)
        with patch('backend.services.tasks.assert_owned', side_effect=LeaseLost()), self.assertRaises(LeaseLost):
            governance.activate_proposal(proposal.id)
        db.session.rollback()
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_admin_confirmation_does_not_bypass_extraction(self):
        config = governance.source_config(self.dept); config['list_selector'] = 'missing'
        proposal = self.proposal(config)
        governance.review_proposal(proposal.id, 'confirm_identity', self.admin.id, '已核实官网发布单位')
        self.assertFalse(governance.validate_proposal(proposal.id)['passed'])
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)

    def test_changed_evidence_rejected_after_validation(self):
        proposal = self.proposal()
        self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
        digest = json.loads(proposal.evidence_json)['list']['hash']
        (Path(self.tmp.name) / 'evidence' / (digest + '.html.gz')).write_bytes(b'corrupted')
        with self.assertRaises(ValueError):
            governance.activate_proposal(proposal.id)

    def test_independent_page_must_still_expose_same_column(self):
        config = governance.source_config(self.dept)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=self.html(1), fetcher=self.fetcher('<h2>校园新闻</h2><p>没有原栏目</p>'))
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        self.assertIn('independent_list_missing', governance.validate_proposal(proposal.id)['errors'])

    def test_pagination_cannot_silently_repeat_first_page(self):
        html = self.html(2, '<a rel="next" href="page2.htm">下一页</a>')
        proposal = self.proposal(html=html)
        self.assertIn('pagination_repeats_first_page', governance.validate_proposal(proposal.id)['errors'])

    def test_roster_denominator_keeps_unchecked_units_and_pending_pages(self):
        status = governance.record_onboarding_slice(self.school.id, {'states': {'fetched': 20, 'pending': 30},
            'official_units': [{'node_key': 'a', 'name': '工程学院', 'url': 'https://eng.example.edu.cn/'},
                               {'node_key': 'b', 'name': '文学院', 'url': ''}]})
        self.assertEqual(status['state'], 'discovering')
        self.assertFalse(status['coverage_verified'])
        self.assertEqual(len(status['official_units']), 2)
        self.assertTrue(all(unit['state'] == 'not_checked' for unit in status['official_units']))

    def test_selector_free_entry_is_pending_and_subscription_intent_is_deferred(self):
        sub = Subscription(school_id=self.school.id, user_id=self.user.id, department_ids=[])
        db.session.add(sub); db.session.commit()
        result = governance.queue_source_review(self.school.id, {'name': '通知公告', 'list_url': self.school.url},
                                               requested_by=self.user.id, subscribe=True)
        proposal = db.session.get(SourceProposal, result['proposal_id'])
        self.assertEqual(json.loads(proposal.candidate_json)['list_selector'], '')
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(sub.department_ids, [])
        self.assertEqual(SourceReviewEvent.query.filter_by(action='subscribe_on_activation').count(), 1)

    def register_roster(self):
        from backend.services.source_inventory import Inventory, site_key
        from backend.scraper.discovery.inventory_crawler import crawl_site
        import hashlib
        inv = Inventory(Path(self.tmp.name) / 'roster.sqlite3')
        key = inv.ensure_site(self.school.name, self.school.url)
        with inv.connect() as connection:
            connection.execute("UPDATE pages SET kind='directory',label='院系设置' WHERE site_key=?", (key,))
        html = '<main><h4><a href="https://college.example.edu.cn/">示例学院</a></h4></main>'
        crawl_site(inv, key, max_pages=1, fetcher=lambda url: {'html': html, 'url': url, 'status': 200})
        baseline = {'id': 'official-roster', 'root_url': self.school.url, 'reference_url': self.school.url,
            'reference_hash': hashlib.sha256(html.encode()).hexdigest(), 'scope': '完整院系名录',
            'scope_selector': 'main', 'category': 'academic_units', 'reviewed_at': '2026-09-24',
            'entries': [{'id': 'college', 'name': '示例学院', 'url': 'https://college.example.edu.cn/'}]}
        status = governance.register_school_baselines(self.school.id, [baseline], inv, self.admin.id, complete_roster=True)
        return inv, key, status['official_units'][0]['key']

    def test_full_roster_readiness_requires_scope_proof_and_invalidates_on_reference_failure(self):
        inv, key, unit_key = self.register_roster()
        proposal = self.proposal(html=self.html(1))
        governance.validate_proposal(proposal.id); governance.activate_proposal(proposal.id)
        proof = json.loads(proposal.evidence_json)['list']
        priorities = {category: {'state': 'not_applicable', 'evidence_reference': proof,
                                'note': '已人工检查此官方入口及适用范围'} for category in governance.PRIORITY_SCOPES}
        priorities['general_notices'].update(state='verified', source_ids=[self.dept.id])
        status = governance.review_unit_scope(self.school.id, unit_key, 'verified', self.admin.id,
            evidence_reference=proof, priority_scopes=priorities, note='当前完整院系范围已复核')
        self.assertTrue(status['coverage_verified'])
        inv.finish(key, self.school.url, state='failed', error='network failure')
        status = governance.record_onboarding_slice(self.school.id, inv.report(key))
        self.assertFalse(status['coverage_verified'])

    def test_roster_review_rejects_unrelated_article_as_absence_evidence(self):
        _inv, _key, unit_key = self.register_roster()
        proposal = self.proposal()
        article_proof = json.loads(proposal.evidence_json)['articles'][0]
        priorities = {category: {'state': 'not_applicable', 'note': 'claimed',
                                'evidence_reference': article_proof} for category in governance.PRIORITY_SCOPES}
        with self.assertRaisesRegex(ValueError, '本机构栏目或导航'):
            governance.review_unit_scope(self.school.id, unit_key, 'no_independent_column', self.admin.id,
                evidence_reference=article_proof, priority_scopes=priorities, note='claimed')

    def test_ai_suggestion_is_revalidated_before_it_can_be_activated(self):
        proposal = self.proposal(html=self.html(1))
        config = json.loads(proposal.candidate_json)
        response = {'status': 'succeeded', 'output': {'proposals': [{'candidate_id': 'source-' + str(proposal.id),
            'decision': 'propose', 'config': config}]}, 'provenance': {'skill_version': 'test'}}
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 'test'}), \
             patch('backend.ai.runtime.run_skill', return_value=response) as skill, \
             patch('backend.services.source_governance._fetch', side_effect=self.fetcher(self.html(1))):
            result = governance.run_source_skill_for_proposal(proposal.id, inventory=self.identity)
        self.assertEqual(result['status'], 'validated')
        self.assertEqual(SourceConfigVersion.query.count(), 0)
        self.assertEqual(skill.call_args.kwargs['expected_version'], 'test')
        governance.activate_proposal(proposal.id)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_unknown_ai_result_is_not_automatically_retried(self):
        proposal = self.proposal()
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 'test'}), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'uncertain'}) as skill:
            self.assertEqual(governance.run_source_skill_for_proposal(proposal.id)['status'], 'uncertain')
            self.assertEqual(governance.run_source_skill_for_proposal(proposal.id)['status'], 'result_pending_review')
        skill.assert_called_once()

    def test_rollback_requests_new_verification_without_mutating_active_source(self):
        proposal = self.proposal(html=self.html(1))
        governance.validate_proposal(proposal.id); governance.activate_proposal(proposal.id)
        self.dept.name = '已更新的栏目名称'; db.session.commit()
        result = governance.rollback_source_version(self.dept.id, 1, self.admin.id)
        self.assertEqual(result['state'], 'proposed')
        self.assertEqual(self.dept.name, '已更新的栏目名称')
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_cleanup_preserves_pending_proof_and_recent_capture_but_removes_expired_orphan(self):
        import os
        import time
        proposal = self.proposal()
        digest = json.loads(proposal.evidence_json)['list']['hash']
        protected = Path(self.tmp.name) / 'evidence' / (digest + '.html.gz')
        orphan = governance._snapshot('<p>unused old evidence</p>', self.school.url)
        orphan_path = Path(self.tmp.name) / 'evidence' / (orphan['hash'] + '.html.gz')
        recent = governance._snapshot('<p>currently being checked</p>', self.school.url)
        old = time.time() - 40 * 86400
        os.utime(protected, (old, old)); os.utime(orphan_path, (old, old))
        result = governance.prune_governance_evidence(7, all_cache=True)
        self.assertEqual(result['removed'], 1)
        self.assertTrue(protected.exists())
        self.assertFalse(orphan_path.exists())
        self.assertTrue((Path(self.tmp.name) / 'evidence' / (recent['hash'] + '.html.gz')).exists())

    def test_explicit_empty_column_is_valid_but_does_not_invent_articles(self):
        from backend.scraper.acquisition import FetchedHTML, FetchResult
        config = governance.source_config(self.dept)
        empty = FetchedHTML(FetchResult(self.dept.list_url, status=200, outcome='empty',
                                      html='<h2>通知公告</h2><p>暂无通知</p>'))
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
                                                     seed_html=empty, fetcher=lambda url, purpose: empty, inventory=self.identity)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        validation = governance.validate_proposal(proposal.id)
        self.assertTrue(validation['passed'], validation)
        self.assertEqual(validation['item_count'], 0)
        self.assertEqual(Announcement.query.count(), 0)

    def test_yaml_seed_does_not_install_unverified_selectors(self):
        from backend.core.config import load_config_yaml
        import yaml
        config = Path(self.tmp.name) / 'config.yaml'
        config.write_text(yaml.safe_dump({'schools': [{'name': self.school.name, 'url': self.school.url,
            'departments': [{'name': '学校通知', 'list_url': self.school.url,
                             'list_selector': '#unverified li', 'title_selector': 'a', 'link_selector': 'a'}]}]},
            allow_unicode=True), encoding='utf-8')
        with patch('backend.core.config.CONFIG_YAML_PATH', config):
            load_config_yaml()
            load_config_yaml()
        seeded = Department.query.filter_by(name='学校通知').one()
        self.assertFalse(seeded.list_selector)
        self.assertEqual(SourceProposal.query.filter_by(department_id=seeded.id).count(), 1)
        self.assertEqual(self.dept.list_selector, '#notices li')

    def test_discovery_slice_continues_and_does_not_claim_completeness(self):
        from backend.services.discovery_cache import adapt_site
        self.app.config['DISCOVERY_CACHE_PATH'] = Path(self.tmp.name) / 'scratch.sqlite3'
        with patch('backend.scraper.discovery.inventory_crawler.crawl_site', return_value={
                'states': {'fetched': 20, 'pending': 7}, 'pages': [], 'reference_checks': []}) as crawl:
            result = adapt_site(self.school.name, self.school.url)
        self.assertEqual(crawl.call_args.kwargs['max_pages'], 20)
        self.assertEqual(crawl.call_args.kwargs['focus'], 'all')
        self.assertTrue(result['continuation_required'])
        self.assertFalse(result['coverage_verified'])
        self.assertEqual(governance.school_governance_status(self.school.id)['pending_pages'], 7)

    def test_explicit_department_delete_retains_shared_article_and_nulls_review_target(self):
        from backend.database.models import AnnouncementSource
        proposal = self.proposal(html=self.html(1))
        governance.validate_proposal(proposal.id); governance.activate_proposal(proposal.id)
        other = Department(school_id=self.school.id, name='另一栏目', list_url=self.school.url)
        db.session.add(other); db.session.flush()
        article = Announcement(school_id=self.school.id, department_id=self.dept.id,
                               title='两栏共享的历史通知', url=self.school.url + 'shared.htm')
        db.session.add(article); db.session.flush()
        db.session.add(AnnouncementSource(announcement_id=article.id, department_id=other.id,
                                         article_url=article.url))
        db.session.commit()
        ident, proposal_id, article_id = self.dept.id, proposal.id, article.id
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.admin.id; session['_csrf_token'] = 'test-token'
        response = client.delete(f'/api/departments/{ident}', headers={'X-CSRF-Token': 'test-token'})
        self.assertEqual(response.status_code, 200, response.text)
        db.session.expire_all()
        self.assertIsNone(db.session.get(Department, ident))
        self.assertEqual(db.session.get(Announcement, article_id).department_id, other.id)
        self.assertIsNone(db.session.get(SourceProposal, proposal_id).department_id)
        self.assertEqual(SourceConfigVersion.query.count(), 0)

    def test_explicit_school_delete_cascades_governance_only_for_that_school(self):
        proposal = self.proposal()
        governance.validate_proposal(proposal.id); governance.activate_proposal(proposal.id)
        governance.record_onboarding_slice(self.school.id, {'states': {}})
        foreign = School(name='另一大学', url='https://other.edu.cn/')
        db.session.add(foreign); db.session.flush()
        source = Department(school_id=foreign.id, name='通知公告', list_url=foreign.url)
        db.session.add(source); db.session.flush()
        article = Announcement(school_id=foreign.id, department_id=source.id, title='其他学校通知')
        db.session.add(article); db.session.commit()
        article_id = article.id
        client = self.app.test_client()
        with client.session_transaction() as session:
            session['user_id'] = self.admin.id; session['_csrf_token'] = 'test-token'
        response = client.delete(f'/api/schools/{self.school.id}', headers={'X-CSRF-Token': 'test-token'})
        self.assertEqual(response.status_code, 200, response.text)
        db.session.expire_all()
        self.assertEqual(SourceProposal.query.count(), 0)
        self.assertEqual(SourceReviewEvent.query.count(), 0)
        self.assertIsNotNone(db.session.get(Announcement, article_id))

    def test_later_subscriber_reuses_activated_entry_without_new_review(self):
        from backend.database.models import BackgroundTask
        first = Subscription(school_id=self.school.id, user_id=self.admin.id, department_ids=[])
        later = Subscription(school_id=self.school.id, user_id=self.user.id, department_ids=[])
        db.session.add_all([first, later]); db.session.commit()
        candidate = {'name': '通知公告', 'list_url': self.school.url, 'group_name': ''}
        queued = governance.queue_source_review(self.school.id, candidate, requested_by=self.admin.id, subscribe=True)
        with patch('backend.services.source_governance._fetch', side_effect=self.fetcher(self.html())):
            activated = governance.process_source_review({'proposal_id': queued['proposal_id']})
        self.assertEqual(activated['state'], 'activated')
        self.assertEqual(first.department_ids, [activated['department_id']])
        task = db.session.get(BackgroundTask, queued['task_id']); task.state = 'done'; db.session.commit()
        with patch('backend.services.tasks.enqueue') as enqueue, patch('backend.services.source_governance._fetch') as fetch:
            second = governance.queue_source_review(self.school.id, candidate, requested_by=self.user.id, subscribe=True)
            repeated = governance.queue_source_review(self.school.id, candidate, requested_by=self.user.id, subscribe=True)
        self.assertEqual(second['state'], 'activated')
        self.assertEqual(repeated['proposal_id'], queued['proposal_id'])
        self.assertEqual(later.department_ids, [activated['department_id']])
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(SourceReviewEvent.query.filter_by(proposal_id=queued['proposal_id'], actor_id=self.user.id,
                                                          action='subscribe_on_activation').count(), 1)
        enqueue.assert_not_called(); fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
