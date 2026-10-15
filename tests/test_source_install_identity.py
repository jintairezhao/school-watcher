"""Executed widget identity survives equivalent CSS without merging neighbours."""
import json
from pathlib import Path
import unittest

from backend.database.db import db
from backend.database.models import (Announcement, Department, School, Subscription,
                                     User, UserRead, UserAnnouncementState)
from backend.database.source_governance_models import SourceConfigVersion
from backend.services import source_governance as governance
from backend.services.source_onboarding import _install


class SourceInstallIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()
        self.school_id = self.fixture.school_id
        self.config = dict(name='通知公告', list_url='https://example.edu.cn/notices/',
                           group_name='', list_selector='#notices li', title_selector='a',
                           link_selector='a', date_selector='time', content_selector='')
        self.html = ('<html><body><section id="notices"><h2><span>通知公告</span></h2><ul>' +
                     ''.join(f'<li><a href="/info/{i}.htm">关于开展学生实践活动的通知{i}</a>'
                             '<time>2026-10-01</time></li>' for i in range(3)) +
                     '</ul></section></body></html>')

    def tearDown(self):
        self.fixture.tearDown()

    def install(self, config=None, html=None, **kwargs):
        config = config or self.config
        reference = governance._snapshot(html or self.html, config['list_url'], role='list')
        scope = kwargs.pop('scope', {'container_selector': '#notices', 'heading_selector': '#notices h2'})
        records, errors, region = governance._column_scope(config, reference, scope)
        self.assertEqual(errors, [])
        validation = {'status': 'full', 'list_snapshot': reference, 'region': region,
                      'articles': {'passed': True, 'samples': []}}
        return _install(self.school_id, config, records, validation=validation, **kwargs)[0]

    def test_equivalent_css_keeps_original_id_rule_history_and_personal_state(self):
        ident = self.install()
        user = User.query.one()
        subscription = Subscription.query.one()
        subscription.department_ids = [ident]
        article = Announcement.query.first()
        db.session.add(UserRead(user_id=user.id, announcement_id=article.id))
        db.session.add(UserAnnouncementState(user_id=user.id, announcement_id=article.id, starred=True))
        db.session.commit()
        replay = self.install(dict(self.config, list_selector='section#notices > ul > li'),
                              scope={'container_selector': 'section#notices',
                                     'heading_selector': '#notices h2 > span'})
        self.assertEqual(replay, ident)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Department.query.one().list_selector, '#notices li')
        self.assertEqual(Subscription.query.one().department_ids, [ident])
        self.assertEqual(Announcement.query.count(), 3)
        self.assertEqual(UserRead.query.one().announcement_id, article.id)
        self.assertTrue(UserAnnouncementState.query.one().starred)
        self.assertEqual(SourceConfigVersion.query.count(), 1)

    def test_actual_full_cupk_snapshot_has_same_four_item_nodes_and_nested_heading(self):
        root = Path(__file__).parent / 'fixtures'
        html = (root / 'retained-cupk-zhb.html').read_text('utf-8')
        original = json.loads((root / 'retained-cupk-zhb-columns.json').read_text('utf-8'))
        school = db.session.get(School, self.school_id)
        school.url = 'https://www.cupk.edu.cn/'
        db.session.commit()
        identifiers = []
        for key in ('54', '85'):
            row = original[key]
            identifiers.append(self.install(row['config'], html,
                scope={'container_selector': row['region']['container'],
                       'heading_selector': row['region']['heading']}))
        self.assertEqual(identifiers[0], identifiers[1])
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Announcement.query.count(), 4)

    def test_same_named_neighbour_with_identical_article_urls_remains_separate(self):
        html = self.html.replace('</body>', self.html.split('<body>')[1].split('</body>')[0].replace(
            'id="notices"', 'id="other"') + '</body>')
        first = self.install(html=html)
        second = self.install(dict(self.config, list_selector='#other li'), html,
                              scope={'container_selector': '#other', 'heading_selector': '#other h2'})
        self.assertNotEqual(first, second)

    def test_same_named_different_department_is_not_reused(self):
        first = self.install()
        second = self.install(dict(self.config, group_name='另一个学院',
                                   list_selector='section#notices > ul > li'))
        self.assertNotEqual(first, second)

    def test_preexisting_duplicate_rows_are_retained_without_a_third_install(self):
        first = self.install()
        duplicate = Department(school_id=self.school_id,
            **dict(self.config, list_selector='section#notices > ul > li'))
        db.session.add(duplicate)
        db.session.commit()
        duplicate_id = duplicate.id
        replay = self.install(dict(self.config, list_selector='section[id="notices"] ul > li'))
        self.assertEqual(replay, first)
        self.assertEqual({row.id for row in Department.query.all()}, {first, duplicate_id})

    def test_missing_full_snapshot_cannot_make_changed_css_equivalent(self):
        from backend.services.source_identity import same_observed_column
        ident = self.install()
        source = db.session.get(Department, ident)
        changed = dict(self.config, list_selector='section#notices > ul > li')
        self.assertFalse(same_observed_column(source, changed, {}))
        self.assertFalse(same_observed_column(source, changed, {'status': 'full',
            'list_snapshot': {'hash': 'missing', 'url': self.config['list_url']},
            'region': {'container': '#notices', 'heading': '#notices h2'}}))

    def test_matching_first_two_samples_cannot_merge_different_whole_item_sets(self):
        from backend.services.source_identity import same_observed_column
        ident = self.install()
        changed = dict(self.config, list_selector='#notices li:nth-child(-n+2)')
        reference = governance._snapshot(self.html, self.config['list_url'], role='list')
        records, errors, region = governance._column_scope(changed, reference,
            {'container_selector': '#notices', 'heading_selector': '#notices h2'})
        self.assertEqual(errors, [])
        self.assertEqual(len(records), 2)
        self.assertFalse(same_observed_column(db.session.get(Department, ident), changed,
            {'status': 'full', 'list_snapshot': reference, 'region': region}))

    def test_new_address_requires_existing_explicit_repair_continuity(self):
        first = self.install()
        moved = dict(self.config, list_url='https://example.edu.cn/new-notices/',
                     list_selector='section#notices > ul > li')
        second = self.install(moved, previous_url=self.config['list_url'])
        self.assertNotEqual(first, second)
        # Existing explicit relocation uses article continuity and retains ID.
        source = db.session.get(Department, second)
        db.session.delete(source)
        db.session.commit()
        repaired = self.install(moved, previous_url=self.config['list_url'], repair=True,
                                repair_department_id=first)
        self.assertEqual(repaired, first)


class PublicSourceInstallIdentityTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_governance import SourceGovernanceTests
        self.fixture = SourceGovernanceTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()
        self.fixture.doCleanups()

    def test_validated_equivalent_proposal_reuses_original_source_and_history(self):
        fixture = self.fixture
        first = fixture.proposal()
        self.assertTrue(governance.validate_proposal(first.id)['passed'])
        source = governance.activate_proposal(first.id)
        ident = source.id
        config = dict(governance.source_config(source), list_selector='section#notices > ul > li')
        evidence = governance.capture_source_evidence(fixture.school.id, config,
            seed_html=fixture.html(), fetcher=fixture.fetcher(fixture.html()), inventory=fixture.identity)
        second = governance.propose_source(fixture.school.id, config, evidence)
        self.assertTrue(governance.validate_proposal(second.id)['passed'])
        replay = governance.activate_proposal(second.id)
        self.assertEqual(replay.id, ident)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(replay.list_selector, '#notices li')
        self.assertEqual(SourceConfigVersion.query.count(), 1)


if __name__ == '__main__':
    unittest.main()
