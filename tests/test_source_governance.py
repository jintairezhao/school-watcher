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
    def test_school_column_requires_observed_home_link_and_target_branding(self):
        from backend.services.source_workflow import school_publisher_proven
        root = self.school.url
        target = root + 'notices/'
        home = governance._snapshot('<a href="/notices/">通知公告</a>', root)
        body = governance._snapshot('<title>示例大学通知公告</title>' + self.html(), target)
        bundle = {'root_url': root, 'school_name': self.school.name, 'list': body,
                  'publisher_material': {'home': home}}
        config = dict(governance.source_config(self.dept), list_url=target, group_name=self.school.name)
        self.assertTrue(school_publisher_proven(bundle, config))
        for different in (root+'unobserved/', 'https://college.example.edu.cn/notices/'):
            self.assertFalse(school_publisher_proven(bundle, dict(config, list_url=different)))
        self.assertFalse(school_publisher_proven(bundle, dict(config, group_name='某学院')))
        bundle['list'] = governance._snapshot(self.html(), target)
        self.assertFalse(school_publisher_proven(bundle, config))

    def test_ai_declared_scope_executes_without_template_and_checks_independent_read(self):
        config = governance.source_config(self.dept)
        html = self.html().replace('<h2>', '<div class="column-label">').replace('</h2>', '</div>')
        scope = {'container_selector': '#notices', 'heading_selector': '#notices .column-label'}
        with patch('backend.scraper.discovery.publication_lists.publication_lists', return_value=[]):
            for bad, expected in ((None, True), ('body', False), ('#nonexistent', False)):
                proof = scope if bad is None else dict(scope, container_selector=bad)
                evidence = governance.capture_source_evidence(self.school.id, config, inventory=self.identity,
                    fetcher=self.fetcher(html), scope_evidence=proof)
                proposal = governance.propose_source(self.school.id, config, evidence)
                self.assertEqual(governance.validate_proposal(proposal.id)['passed'], expected)
            def changed(url, purpose):
                document = html.replace('通知公告', '新闻动态') if purpose == 'independent_list' else html
                return self.fetcher(document)(url, purpose)
            evidence = governance.capture_source_evidence(self.school.id, config, inventory=self.identity,
                fetcher=changed, scope_evidence=scope)
            proposal = governance.propose_source(self.school.id, config, evidence)
            self.assertIn('independent_column_identity_or_scope_unconfirmed', governance.validate_proposal(proposal.id)['errors'])

    def test_explicit_official_publisher_link_can_cross_school_subdomains(self):
        from backend.services.source_workflow import publisher_proven
        directory_url = 'https://www.example.edu.cn/departments/'
        home_url = 'https://college.example.edu.cn/'
        column = 'https://admission.example.edu.cn/notices/'
        refs = {
            'root': governance._snapshot('<a href="/departments/">机构设置</a>', self.school.url),
            'directory': governance._snapshot(f'<a href="{home_url}">示例学院</a>', directory_url),
            'home': governance._snapshot(f'<title>示例学院</title><a href="{column}">通知公告</a>', home_url),
        }
        bundle = {'root_url': self.school.url, 'publisher_material': refs,
                  'list': governance._snapshot('<title>示例学院通知公告</title>', column),
                  'publisher_evidence': {'name': '示例学院', 'directory_evidence_id': 'directory', 'homepage_evidence_id': 'home'}}
        config = dict(governance.source_config(self.dept), list_url=column)
        self.assertTrue(publisher_proven(bundle, config))
        self.assertFalse(publisher_proven(bundle, dict(config, list_url='https://other.example.edu.cn/notices/')))
        self.assertFalse(publisher_proven(bundle, dict(config, group_name='未经证实的学院')))
        bundle['list'] = governance._snapshot('<title>研究生院通知公告</title>', column)
        self.assertFalse(publisher_proven(bundle, config))

    def test_declared_regions_reach_real_acquisition_and_body_validation(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        requests = []
        config = dict(governance.source_config(self.dept), content_selector='.publication-copy')
        def transport(request):
            requests.append(request)
            html = self.fetcher(self.html())(request.url, 'body' if request.purpose == 'article' else 'list')
            html = html.replace('<article>', '<div class="publication-copy">').replace('</article>', '</div>')
            return FetchResult(request.url, status=200, html=html, outcome='usable')
        # Only replace HTTP I/O: coordinator, request validation and content
        # classifier execute the same contract as the packaged application.
        with patch('backend.scraper.acquisition.coordinator.http_fetch', side_effect=transport):
            evidence = governance.capture_source_evidence(self.school.id, config, inventory=self.identity)
            proposal = governance.propose_source(self.school.id, config, evidence)
            result = governance.validate_proposal(proposal.id)
            self.assertTrue(result['passed'], result)
        self.assertEqual({r.readiness_selector for r in requests if r.purpose == 'article'}, {'.publication-copy'})
        self.assertEqual({r.readiness_selector for r in requests if r.purpose == 'list'}, {'#notices li'})
        wrong = dict(config, content_selector='.missing-body')
        evidence = governance.capture_source_evidence(self.school.id, wrong, inventory=self.identity,
                                                     fetcher=self.fetcher(self.html()))
        proposal = governance.propose_source(self.school.id, wrong, evidence)
        self.assertIn('article_body_missing', governance.validate_proposal(proposal.id)['errors'])
        with patch('backend.scraper.acquisition.coordinator.http_fetch', return_value=FetchResult(
                'https://id.example.edu.cn/cas/login?service=https://college.example.edu.cn/article/1.htm',
                status=200, html=self.IDENTITY, outcome='usable')):
            with self.assertRaises(FetchFailure):
                governance._fetch('https://college.example.edu.cn/article/1.htm', 'body',
                                  config=dict(config, content_selector='body'))

    def test_readable_unrecognized_list_reaches_config_validation(self):
        from backend.scraper.acquisition import FetchResult, FetchFailure
        def transport(request):
            html = self.fetcher(self.html())(request.url, 'body' if request.purpose == 'article' else 'list')
            if request.purpose == 'list':
                raise FetchFailure(FetchResult(request.url, status=200, html=html,
                    outcome='needs_adapter', error_code='content_not_recognized'))
            return FetchResult(request.url, status=200, html=html, outcome='usable')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            config = governance.source_config(self.dept)
            captured = governance.capture_source_evidence(self.school.id, config,
                department_id=self.dept.id, inventory=self.identity)
            proposal = governance.propose_source(self.school.id, config, captured, department_id=self.dept.id)
            self.assertTrue(governance.validate_proposal(proposal.id)['passed'])
            bad = dict(config, list_selector='body', name='无依据的栏目')
            captured = governance.capture_source_evidence(self.school.id, bad,
                department_id=self.dept.id, inventory=self.identity)
            proposal = governance.propose_source(self.school.id, bad, captured, department_id=self.dept.id)
            self.assertFalse(governance.validate_proposal(proposal.id)['passed'])

    def test_complete_onboarding_through_actual_acquisition_request_contract(self):
        from backend.scraper.acquisition import FetchResult
        from backend.database.models import BackgroundTask
        from backend.worker import dispatch
        db.session.add(Subscription(school_id=self.school.id, user_id=self.user.id))
        self.school.subscriber_count = 1
        db.session.commit()
        requests = []
        def transport(request):
            requests.append(request)
            html = self.fetcher(self.html())(request.url, 'body' if request.purpose == 'article' else 'list')
            return FetchResult(request.url, status=200, html=html, outcome='usable')
        with patch('backend.scraper.acquisition.fetch_or_raise', side_effect=transport):
            config = governance.source_config(self.dept)
            evidence = governance.capture_source_evidence(self.school.id, config,
                department_id=self.dept.id, inventory=self.identity)
            proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
            validation = governance.validate_proposal(proposal.id)
            self.assertTrue(validation['passed'], validation)
            self.assertEqual(governance.activate_proposal(proposal.id).id, self.dept.id)
            task = BackgroundTask.query.filter_by(identity=f'collect:{self.dept.id}').one()
            self.assertEqual(task.payload['school_id'], self.school.id)
            self.assertEqual(task.phase, 'onboarding_collection')
            collected = dispatch(task.kind, task.payload)
            self.assertEqual(collected['new_count'], 3)
            self.assertEqual(dispatch(task.kind, task.payload)['new_count'], 0)
            self.assertEqual(dispatch(task.kind, {'department_id': self.dept.id})['new_count'], 0)
            self.assertEqual(Announcement.query.filter_by(department_id=self.dept.id).count(), 3)
        self.assertEqual(sum(r.purpose == 'article' for r in requests), 3)
        self.assertTrue(any(r.policy.get('verification_pass') == 'independent' for r in requests))

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
        def fetch(url, purpose, **kwargs):
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

    def test_unconfirmed_column_still_yields_body_and_independent_evidence(self):
        """An ambiguous region must gather more evidence, not stop gathering it.

        The preflight finding used to gate every body and independent read, so the
        one check that could settle an unclear column was permanently starved of
        the evidence it needed.
        """
        config = dict(governance.source_config(self.dept), name='研究生通知')
        calls = []
        base = self.fetcher(self.html())
        def counting(url, purpose):
            calls.append((url, purpose))
            return base(url, purpose)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=self.html(), fetcher=counting, inventory=self.identity)
        bundle = governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id).evidence_json
        bundle = json.loads(bundle)
        self.assertIn('column_identity_or_scope_unconfirmed', bundle['preflight_errors'])
        self.assertEqual(len(bundle['articles']), 3)
        self.assertTrue(bundle.get('independent'), bundle.get('samples'))
        self.assertEqual(bundle['samples']['independent'], 'obtained')
        # Still bounded: three bodies and one independent read, as when clean.
        self.assertEqual([purpose for _, purpose in calls].count('body'), 3)
        self.assertEqual([purpose for _, purpose in calls].count('independent_list'), 1)

    def test_non_column_page_still_costs_only_the_initial_read(self):
        """The cost guard survives: a page that is not a column is never sampled."""
        config = governance.source_config(self.dept)
        article_page = ('<html><body class="single-post"><article class="type-post" id="post-1">'
                        '<h1><a href="/notices/">2024年秋季学期本科生选课通知</a></h1>'
                        '<p>' + '请同学们按时完成选课并核对课表。' * 20 + '</p></article></body></html>')
        calls = []
        def counting(url, purpose):
            calls.append((url, purpose))
            return article_page
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=article_page, fetcher=counting, inventory=self.identity)
        bundle = json.loads(governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id).evidence_json)
        self.assertEqual(bundle['preflight_errors'], ['article_instead_of_column'])
        self.assertEqual(calls, [])
        self.assertEqual(bundle['articles'], [])
        self.assertIsNone(bundle.get('independent'))

    def test_a_failed_sample_does_not_cancel_the_others(self):
        """One unreachable article must not discard the independent proof."""
        from backend.scraper.acquisition import FetchFailure, FetchResult
        config = governance.source_config(self.dept)
        calls = []
        base = self.fetcher(self.html())
        def flaky(url, purpose):
            calls.append((url, purpose))
            if purpose == 'body':
                raise FetchFailure(FetchResult(url, status=503, outcome='network_error',
                                               error_code='http_503', message='官网服务暂时不可用'))
            return base(url, purpose)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=self.html(), fetcher=flaky, inventory=self.identity)
        bundle = json.loads(governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id).evidence_json)
        self.assertEqual(bundle['articles'], [])
        self.assertTrue(bundle.get('independent'), bundle.get('samples'))
        # The failure is recorded per sample instead of escaping as a task crash.
        self.assertTrue(any(value.startswith('failed:') for value in bundle['samples'].values()),
                        bundle['samples'])

    def unnamed_candidate(self):
        # Spelled out rather than imported, so the assertions below still fail for
        # the behavioural reason on a build that has no notion of the sentinel.
        return dict(governance.source_config(self.dept), name='栏目名称待核实')

    def test_unnamed_column_adopts_the_official_page_heading(self):
        """A column discovery could not name is still a real, installable column.

        Discovery marks it 栏目名称待核实; requiring that sentinel to equal the page
        heading could only ever fail, so 63 real columns were recorded as scope
        unconfirmed without a single sample ever being read.
        """
        config = self.unnamed_candidate()
        db.session.delete(self.dept); db.session.commit()
        proposal = governance.propose_source(self.school.id,
            {field: config[field] for field in governance.FIELDS}, origin='submitted_entry')
        self.assertEqual(json.loads(proposal.candidate_json)['name'], '栏目名称待核实')
        with patch('backend.services.runtime_catalog.RuntimeCatalog', return_value=self.identity), \
                patch.object(governance, '_fetch', side_effect=self.fetcher(self.html())), \
                patch.object(governance, 'run_source_skill_for_proposal') as ai:
            result = governance.process_source_review({'proposal_id': proposal.id})
        self.assertEqual(result['state'], 'activated', result['validation'])
        self.assertNotIn('column_identity_or_scope_unconfirmed', result['validation']['errors'])
        self.assertNotIn('article_sample_missing', result['validation']['errors'])
        # The name comes from the official page, not from the sentinel.
        self.assertEqual(result['candidate']['name'], '通知公告')
        self.assertEqual(db.session.get(Department, result['department_id']).name, '通知公告')
        ai.assert_not_called()
        # The rename is traceable, with the observed heading as its basis.
        event = SourceReviewEvent.query.filter_by(proposal_id=proposal.id,
                                                 action='column_named_from_page').one()
        detail = json.loads(event.detail_json)
        self.assertEqual(detail['observed_name'], '通知公告')
        self.assertEqual(detail['previous_name'], '栏目名称待核实')

    def test_an_action_label_is_never_adopted_as_a_column_name(self):
        """A "更多" heading is a link caption, not a column identity."""
        config = self.unnamed_candidate()
        html = ('<section id="notices"><h2>更多</h2><ul>' + ''.join(
            f'<li><a href="article/{i}.htm">关于研究生报名事项的通知{i}</a><time>2026-09-24</time></li>'
            for i in range(3)) + '</ul></section>')
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=html, fetcher=self.fetcher(html), inventory=self.identity)
        bundle = json.loads(governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id).evidence_json)
        self.assertEqual(bundle['config']['name'], governance.UNVERIFIED_COLUMN_NAME)
        self.assertIn('column_identity_or_scope_unconfirmed', bundle['preflight_errors'])

    def test_a_named_candidate_is_never_renamed_by_the_page(self):
        """Adoption is only for candidates that had no name; a claim is not overwritten."""
        config = dict(governance.source_config(self.dept), name='研究生通知')
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
            seed_html=self.html(), fetcher=self.fetcher(self.html()), inventory=self.identity)
        bundle = json.loads(governance.propose_source(self.school.id, config, evidence,
            department_id=self.dept.id).evidence_json)
        self.assertEqual(bundle['config']['name'], '研究生通知')
        self.assertIn('column_identity_or_scope_unconfirmed', bundle['preflight_errors'])

    def test_snapshot_preserves_mixed_newlines_and_still_detects_changes(self):
        import gzip
        html = '<main>通知\r\n第一行\r第二行\n</main>'
        reference = governance._snapshot(html, self.school.url)
        self.assertEqual(governance.read_snapshot(reference), html)
        path = Path(self.app.config['SOURCE_GOVERNANCE_EVIDENCE_PATH']) / (reference['hash'] + '.html.gz')
        path.write_bytes(gzip.compress((html + 'changed').encode()))
        with self.assertRaisesRegex(ValueError, '网页证据已变化'):
            governance.read_snapshot(reference)

    def test_observed_column_can_activate_without_user_filling_publisher(self):
        config = governance.source_config(self.dept)
        db.session.delete(self.dept); db.session.commit()
        proposal = governance.propose_source(self.school.id,
            {'name': config['name'], 'list_url': config['list_url']}, origin='submitted_entry')
        with patch('backend.services.runtime_catalog.RuntimeCatalog', return_value=self.identity), \
                patch.object(governance, '_fetch', side_effect=self.fetcher(self.html())), \
                patch.object(governance, 'run_source_skill_for_proposal') as ai:
            result = governance.process_source_review({'proposal_id': proposal.id})
        self.assertEqual(result['state'], 'activated', result)
        self.assertEqual(result['candidate']['group_name'], '示例学院')
        ai.assert_not_called()

    def test_crlf_notice_can_pass_validation_and_activate(self):
        html = self.html(2).replace('><', '>\r\n<')
        proposal = self.proposal(html=html)
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        self.assertEqual(governance.activate_proposal(proposal.id).id, self.dept.id)

    def test_old_snapshot_failure_is_rechecked_once_without_revisiting_other_reviews(self):
        from backend.services.source_inventory import site_key
        config = {**governance.source_config(self.dept), 'name': '新通知'}
        proposal = governance.propose_source(self.school.id, config)
        proposal.state = 'needs_review'
        proposal.validation_json = json.dumps({'passed': False, 'errors': ['网页证据已变化，请重新检查'],
                                               'validator_version': 'source-governance-1'})
        db.session.commit()
        def validate(ident, **kwargs):
            proposal.validation_json = json.dumps({'passed': False, 'errors': ['publisher_unconfirmed'],
                                                   'validator_version': governance.VERSION})
            db.session.commit()
            return {'passed': False}
        with patch('backend.services.source_catalog.publication_candidates', return_value=[config]), \
                patch.object(governance, 'capture_source_evidence') as capture, \
                patch.object(governance, 'validate_proposal', side_effect=validate), \
                patch.object(governance, 'run_source_skill_for_proposal', return_value={'changed': False}):
            first = governance.process_discovered_candidates(self.school.id, self.identity, site_key(self.school.url))
            self.assertIn(proposal.id, first['proposal_ids'])
            self.assertEqual(capture.call_count, 1)
            second = governance.process_discovered_candidates(self.school.id, self.identity, site_key(self.school.url))
            self.assertNotIn(proposal.id, second['proposal_ids'])
            self.assertEqual(capture.call_count, 1)

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

    def test_unknown_result_is_saved_before_scheduled_retry(self):
        proposal = self.proposal()
        with patch('backend.ai.configuration.get_model_binding', return_value={'version': 'test'}), \
             patch('backend.ai.runtime.run_skill', return_value={'status': 'uncertain'}) as skill:
            self.assertEqual(governance.run_source_skill_for_proposal(proposal.id)['status'], 'pending')
            self.assertEqual(governance.run_source_skill_for_proposal(proposal.id)['status'], 'pending')
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
        self.assertEqual(crawl.call_args.kwargs['max_pages'], 3)
        self.assertEqual(crawl.call_args.kwargs['focus'], 'valuable')
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

    IDENTITY = ('<html><head><title>示例大学统一身份认证</title></head><body>'
                '<div>统一身份认证 UsernamePassword</div></body></html>')

    def refusal(self, url, purpose, *, html=None, landed=None):
        """What the real chain reports for a page the site will not serve.

        The page is driven through the production path — evidence role, transport
        purpose, coordinator, classifier — rather than a hand-written
        ``FetchResult``. A change in how a refusal is reported then shows up here
        instead of being masked by the test's own copy of the verdict.
        """
        from backend.scraper.acquisition import FetchFailure, FetchResult
        landed = landed or ('https://id.example.edu.cn/cas/login?service=' + url)
        def transport(request):
            # The first field is the address the content actually came from.
            return FetchResult(landed, status=200, html=html or self.IDENTITY, outcome='usable')
        # Patch the socket, not the verdict: classification still runs for real.
        with patch('backend.scraper.acquisition.coordinator.http_fetch', side_effect=transport):
            try:
                governance._fetch(url, purpose)
            except FetchFailure as failure:
                return failure
        self.fail('the response was not reported as a failure: ' + url)

    def gated(self, base, gated_urls):
        """A site that serves one set of entries publicly and gates another."""
        def fetch(url, purpose):
            if url in gated_urls:
                raise self.refusal(url, purpose)
            return base(url, purpose)
        return fetch

    def test_a_gated_entry_is_a_limit_not_a_missing_sample(self):
        """A mixed column installs and records what the site withheld.

        Official columns mix public entries with ones only their own members may
        open. Reading that gate as "not a publication column" kept a real public
        column from ever being installed, and reported the site's access rule as
        something the user had to fix.
        """
        html = self.html(5)
        config = governance.source_config(self.dept)
        withheld = {f'https://college.example.edu.cn/notices/article/{i}.htm' for i in range(3)}
        calls = []
        def fetch(url, purpose):
            calls.append((url, purpose))
            return self.gated(self.fetcher(html), withheld)(url, purpose)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
                                                     seed_html=html, fetcher=fetch, inventory=self.identity)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        bundle = json.loads(proposal.evidence_json)
        self.assertEqual(sorted(bundle['access_limited']), sorted(withheld))
        for url in withheld:
            self.assertEqual(bundle['samples']['article:' + url], 'access_limited:source_login_required')
        # The sample reaches past the target only as far as it must, and stops at
        # the first body it can actually read.
        self.assertEqual([p for _, p in calls].count('body'), 4)
        result = governance.validate_proposal(proposal.id)
        self.assertTrue(result['passed'], result)
        self.assertNotIn('article_sample_missing', result['errors'])
        self.assertEqual(len(result['limitations']), 3)
        self.assertEqual(governance.activate_proposal(proposal.id).id, self.dept.id)

    def test_a_column_whose_entries_are_all_gated_is_still_refused(self):
        """Nothing could be confirmed as an article, so the gate still holds.

        The refusal does not change; only the reason does. Reporting this as a
        missing body sample told the reader to re-check a column that is simply
        not public, and no amount of re-checking can sign anyone in.
        """
        html = self.html(6)
        config = governance.source_config(self.dept)
        everything = {f'https://college.example.edu.cn/notices/article/{i}.htm' for i in range(6)}
        calls = []
        def fetch(url, purpose):
            calls.append((url, purpose))
            return self.gated(self.fetcher(html), everything)(url, purpose)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
                                                     seed_html=html, fetcher=fetch, inventory=self.identity)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        result = governance.validate_proposal(proposal.id)
        self.assertFalse(result['passed'])
        self.assertIn('source_login_required', result['errors'])
        self.assertNotIn('article_sample_missing', result['errors'])
        # And it reads to the reader as an access limit, not as work for them.
        from backend.services.inbox_refresh import source_status_kind, source_status_label
        self.assertEqual((source_status_kind('failed', '', 'source_login_required'),
                          source_status_label('failed', '', 'source_login_required')),
                         ('access_limited', '访问受限'))
        # Bounded by the ceiling, never the whole list.
        self.assertEqual([p for _, p in calls].count('body'), governance.ARTICLE_SAMPLE_LIMIT)

    def test_a_failure_that_is_not_an_access_limit_stays_fatal(self):
        """Only the site's own refusal is tolerated; an unreadable page is not."""
        html = self.html(5)
        config = governance.source_config(self.dept)
        base = self.fetcher(html)
        login = {f'https://college.example.edu.cn/notices/article/{i}.htm' for i in (0, 1)}
        unparsed = 'https://college.example.edu.cn/notices/article/2.htm'
        def fetch(url, purpose):
            if url == unparsed:
                # An ordinary page the program could not read: no gate, no notice.
                raise self.refusal(url, purpose, landed=url,
                                   html='<html><body><p>普通页面</p></body></html>')
            return self.gated(base, login)(url, purpose)
        evidence = governance.capture_source_evidence(self.school.id, config, department_id=self.dept.id,
                                                     seed_html=html, fetcher=fetch, inventory=self.identity)
        proposal = governance.propose_source(self.school.id, config, evidence, department_id=self.dept.id)
        result = governance.validate_proposal(proposal.id)
        self.assertFalse(result['passed'])
        self.assertIn('article_sample_missing', result['errors'])
        self.assertEqual(len(result['limitations']), 2)


if __name__ == '__main__':
    unittest.main()
