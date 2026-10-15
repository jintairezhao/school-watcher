"""A paid CSS typo can be corrected without inventing a column or widening it."""
import unittest
from pathlib import Path
import hashlib
import json
from unittest.mock import patch

from backend.services.source_governance import _column_scope
from backend.services.source_scope import recover_observed_scope

URL = 'https://example.edu.cn/announcements/'
CONFIG = {'name': '通知公告', 'list_url': URL, 'group_name': '',
          'list_selector': 'div.cards > div.card', 'title_selector': 'h3',
          'link_selector': 'a', 'date_selector': '.date', 'content_selector': ''}
WRONG_SCOPE = {'container_selector': 'div.card', 'heading_selector': 'div.selected > a'}


def cards(prefix='info', class_name='cards'):
    return f'<div class="{class_name}">' + ''.join(
        f'<div class="card"><a href="/{prefix}/10/{10001+i}.htm">'
        f'<h3>关于本学期组织学术交流活动的通知{i}</h3></a><span class="date">2026-10-15</span></div>'
        for i in range(3)) + '</div>'


def page(extra='', *, heading='通知公告', target='/announcements/'):
    return ('<html><title>通知公告 - 测试大学</title><body><header><nav>'
            '<a href="/announcements/">通知公告</a></nav></header><main id="content">'
            f'<div class="section-nav"><ul><li class="selected"><a href="{target}">{heading}</a>'
            '</li><li><a href="/news/">学校新闻</a></li></ul></div>' + cards() + extra + '</main></body></html>')


class ObservedScopeRecoveryTests(unittest.TestCase):
    def recover(self, html, config=None):
        return recover_observed_scope(config or CONFIG, {'url': URL}, WRONG_SCOPE, html=html)

    def test_wrong_tag_and_item_container_use_exact_observed_heading_and_common_region(self):
        # Real public response captured on 2026-10-15 from cupk.edu.cn/tzgg/.
        # Model output is retained verbatim; this is not a school-specific rule.
        html = (Path(__file__).parent / 'fixtures/acquisition/retained-cupk-announcements.html').read_text('utf-8')
        config = dict(CONFIG, list_url='https://www.cupk.edu.cn/tzgg/',
                      list_selector='div.SecondArticle__art--art', title_selector='h3.SecondArticle__art--titles',
                      link_selector='a.SecondArticle__art--link', date_selector='div.SecondArticle__dates')
        wrong = {'container_selector': 'div.SecondArticle__art--art',
                 'heading_selector': 'div.SecondLeft-nav__left--navClick > a'}
        ref = {'url': config['list_url']}
        records, errors, _ = _column_scope(config, ref, wrong, html=html)
        self.assertEqual(errors, ['column_identity_or_scope_unconfirmed'])
        fixed = recover_observed_scope(config, ref, wrong, html=html)
        self.assertIsNotNone(fixed)
        verified, errors, region = _column_scope(config, ref, fixed['scope'], html=html)
        self.assertEqual(errors, [])
        self.assertEqual(verified, records)
        self.assertEqual(region['name'], '通知公告')
        self.assertEqual(fixed['proposed_scope'], wrong)
        self.assertEqual(fixed['basis'], 'executed_observed_heading')

    def test_neighbouring_column_does_not_supply_a_heading_for_selected_cards(self):
        html = ('<main><section><nav><a href="/announcements/">通知公告</a></nav></section>'
                '<section><h2>学校新闻</h2>' + cards() + '</section></main>')
        self.assertIsNone(self.recover(html))

    def test_other_list_or_article_outside_selected_group_is_ambiguous(self):
        for extra in (cards('other', 'other-cards'),
                      '<a href="/info/20/99999.htm">关于另一栏目发布活动安排的通知</a>',
                      '<h2>校园新闻</h2>'):
            with self.subTest(extra=extra):
                self.assertIsNone(self.recover(page(extra)))

    def test_broad_selector_spanning_two_list_parents_cannot_be_repaired(self):
        html = page(cards('other', 'cards'))
        self.assertIsNone(self.recover(html))

    def test_name_absent_or_not_a_self_link_cannot_be_invented(self):
        for html in (page(heading='学校新闻'), page(target='/other/')):
            with self.subTest(html=html):
                self.assertIsNone(self.recover(html))

    def test_different_regions_with_same_literal_name_remain_ambiguous(self):
        html = ('<main><nav><a href="/announcements/">通知公告</a></nav>' + cards() + '</main>'
                '<aside><nav><a href="/announcements/">通知公告</a></nav>' + cards('other', 'other-cards') + '</aside>')
        self.assertIsNone(self.recover(html))

    def test_partial_list_and_invalid_records_do_not_get_scope_only_approval(self):
        for changes in ({'list_selector': '.cards .card:nth-child(-n+2)'},
                        {'title_selector': '.does-not-exist'}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.recover(page(), dict(CONFIG, **changes)))

    def test_already_valid_scope_has_no_automatic_rewrite(self):
        valid = {'container_selector': '#content', 'heading_selector': '.selected > a'}
        self.assertIsNone(recover_observed_scope(CONFIG, {'url': URL}, valid, html=page()))


class ObservedScopeOnboardingTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        from backend.database.db import db
        from backend.database.models import School
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.url = 'https://www.cupk.edu.cn/tzgg/'
        db.session.get(School, self.fixture.school_id).url = 'https://www.cupk.edu.cn/'
        db.session.commit()
        self.html = (Path(__file__).parent / 'fixtures/acquisition/retained-cupk-announcements.html').read_text('utf-8')
        self.column = dict(CONFIG, list_url=self.url,
            list_selector='div.SecondArticle__art--art', title_selector='h3.SecondArticle__art--titles',
            link_selector='a.SecondArticle__art--link', date_selector='div.SecondArticle__dates',
            container_selector='div.SecondArticle__art--art',
            heading_selector='div.SecondLeft-nav__left--navClick > a')
        from backend.services.source_governance import _extract
        self.records = _extract(self.column, {'url': self.url}, html=self.html)[2]
        self.reads = []

    def tearDown(self):
        self.fixture.tearDown()

    def fetch(self, url, purpose):
        self.reads.append((url, purpose))
        if url == self.url:
            return self.html
        if url.endswith('index_2.shtml'):
            html = self.html
            for i, row in enumerate(self.records):
                from urllib.parse import urlsplit
                html = html.replace(row['url'], f'https://www.cupk.edu.cn/info/99/{90000+i}.htm')
                html = html.replace(urlsplit(row['url']).path, f'/info/99/{90000+i}.htm')
            return html
        row = next((r for r in self.records if r['url'] == url), None)
        self.assertIsNotNone(row, url)
        return (f'<article><h1>{row["title"]}</h1><p>发布时间：{row["date"]}</p><p>'
                '请各学院按照通知要求组织本学期相关工作，具体事项、办理时间和联系方式详见附件。</p></article>')

    def test_scope_correction_still_executes_pagination_and_article_identity_checks(self):
        from backend.database.db import db
        from backend.database.models import Announcement, Department
        from backend.database.source_governance_models import SourceValidation
        from backend.services.source_onboarding import onboard_page
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('No extra paid decision')):
            result = onboard_page({'school_id': self.fixture.school_id, 'url': self.url,
                                   'ai_assist': False, 'observed_columns': [self.column]}, fetcher=self.fetch)
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 10)
        detail = json.loads(SourceValidation.query.filter(SourceValidation.department_id.is_not(None)).first().validation_json)
        self.assertEqual(detail['scope_correction']['basis'], 'executed_observed_heading')
        self.assertTrue(detail['articles']['passed'])
        self.assertEqual(detail['pagination']['errors'], [])
        self.assertTrue(any(url.endswith('index_2.shtml') for url, _ in self.reads))
        db.session.commit()

    def test_saved_failed_rule_replays_same_task_and_paid_ledger_without_new_decision(self):
        from backend.database.db import db
        from backend.services import tasks
        from backend.services.source_governance import _snapshot
        from backend.services.source_onboarding import onboard_page, resume_rule_discovery
        from tests.test_pagination_checkpoint_upgrade import PaginationCheckpointUpgradeTests
        ledger = PaginationCheckpointUpgradeTests()
        ledger.fixture = self.fixture
        before = ledger.paid_ledger()
        failure = {'issue': {'name': self.column['name'], 'codes': ['column_identity_or_scope_unconfirmed']},
                   'config': self.column, 'repairable': True, 'observation': self.column}
        job = tasks.enqueue('onboard', 'retained-scope', {'school_id': self.fixture.school_id, 'url': self.url})
        job.checkpoint = {'onboarding_page': _snapshot(self.html, self.url, role='list'),
            'column_validation': {'validation_version': 5, 'stage': 'done', 'page_hash': hashlib.sha256(
                (self.url + '\\0' + self.html).encode()).hexdigest(), 'failures': [failure],
                'trial_columns': [self.column], 'department_ids': [], 'completed': [], 'new_count': 0,
                'progress': {'decisions': 8, 'stopped': True}, 'progress_ids': ['original'],
                'result': {'state': 'unsupported', 'issues': [failure['issue']]}}}
        job.state = 'done'
        db.session.commit()
        generation = job.generation
        with patch('backend.ai.runtime.run_skill', side_effect=AssertionError('Paid result already exists')):
            resume_rule_discovery()
            handle = tasks.claim(capabilities=['http'])
            self.assertIsNotNone(handle)
            self.assertEqual(handle['id'], job.id)
            with tasks.execution_scope(handle):
                result = onboard_page(handle['payload'], fetcher=self.fetch)
            tasks.finish(handle, result)
        self.assertEqual(result['state'], 'connected', result)
        self.assertEqual(job.generation, generation)
        self.assertEqual(ledger.ledger(), before)
        self.assertEqual(job.checkpoint['column_validation']['progress_ids'][0], 'original')


if __name__ == '__main__':
    unittest.main()
