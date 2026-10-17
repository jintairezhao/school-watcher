"""Reuse a proven self-home alias without merging actual publishing paths."""
import unittest

from backend.database.db import db
from backend.database.models import Department, Announcement, Subscription, User, UserAnnouncementState
from backend.database.source_governance_models import SourceConfigVersion
from backend.services import source_governance as governance
from backend.services.source_onboarding import _install


class SourceHomeAliasTests(unittest.TestCase):
    def setUp(self):
        from tests.test_source_install_identity import SourceInstallIdentityTests as Fixture
        self.fixture = Fixture()
        self.fixture.setUp()
        self.config = dict(self.fixture.config, list_url='https://example.edu.cn/')
        self.html = self.fixture.html.replace('<body>', '<body><a href="index.htm">首页</a>')

    def tearDown(self):
        self.fixture.tearDown()

    def install(self, config, html=None, status='full', passed=True):
        ref = governance._snapshot(self.html if html is None else html, config['list_url'], role='list')
        records, errors, region = governance._column_scope(config, ref,
            {'container_selector': '#notices', 'heading_selector': '#notices h2'})
        self.assertEqual(errors, [])
        validation = {'status': status, 'list_snapshot': ref, 'region': region,
                      'articles': {'passed': passed, 'samples': []}}
        return _install(self.fixture.school_id, config, records, validation=validation)[0]

    def test_both_observed_home_addresses_reuse_stable_source_and_personal_state(self):
        first = self.install(self.config, status='limited')
        sub = Subscription.query.one()
        sub.department_ids = [first]
        ann = Announcement.query.first()
        db.session.add(UserAnnouncementState(user_id=User.query.one().id, announcement_id=ann.id, starred=True))
        db.session.commit()
        second = self.install(dict(self.config, list_url='https://example.edu.cn/index.htm'), status='limited')
        self.assertEqual(second, first)
        self.assertEqual(Department.query.count(), 1)
        self.assertEqual(Subscription.query.one().department_ids, [first])
        self.assertTrue(UserAnnouncementState.query.one().starred)
        self.assertEqual(SourceConfigVersion.query.count(), 1)
        self.assertEqual(Department.query.one().list_url, self.config['list_url'])

    def test_alias_is_also_recognized_in_reverse_order(self):
        first = self.install(dict(self.config, list_url='https://example.edu.cn/index.htm'))
        self.assertEqual(self.install(self.config), first)

    def test_changed_full_page_cannot_prove_alias(self):
        first = self.install(self.config)
        changed = self.html.replace('</body>', '<p>另一处实际发布页面</p></body>')
        second = self.install(dict(self.config, list_url='https://example.edu.cn/index.htm'), changed)
        self.assertNotEqual(second, first)

    def test_identical_html_without_an_observed_self_home_link_is_not_an_alias(self):
        html = self.fixture.html
        first = self.install(self.config, html)
        self.assertNotEqual(self.install(dict(self.config, list_url='https://example.edu.cn/index.htm'), html), first)

    def test_identical_material_at_different_directories_or_query_addresses_is_separate(self):
        first = self.install(self.config)
        for url in ('https://example.edu.cn/another/', 'https://example.edu.cn/index.htm?campus=west'):
            self.assertNotEqual(self.install(dict(self.config, list_url=url)), first)

    def test_two_same_named_widgets_on_home_aliases_stay_separate(self):
        html = self.html.replace('</body>', self.fixture.html.split('<body>')[1].split('</body>')[0].replace(
            'id="notices"', 'id="other"') + '</body>')
        first = self.install(self.config, html)
        config = dict(self.config, list_url='https://example.edu.cn/index.htm', list_selector='#other li')
        ref = governance._snapshot(html, config['list_url'], role='list')
        records, errors, region = governance._column_scope(config, ref,
            {'container_selector': '#other', 'heading_selector': '#other h2'})
        self.assertEqual(errors, [])
        second = _install(self.fixture.school_id, config, records,
            validation={'status': 'full', 'list_snapshot': ref, 'region': region,
                        'articles': {'passed': True, 'samples': []}})[0]
        self.assertNotEqual(second, first)

    def test_unconfirmed_prior_validation_does_not_prove_an_alias(self):
        first = self.install(self.config, passed=False)
        self.assertNotEqual(self.install(dict(self.config, list_url='https://example.edu.cn/index.htm')), first)

    def test_distinct_departments_never_share_a_home_alias_source(self):
        first = self.install(self.config)
        self.assertNotEqual(self.install(dict(self.config, list_url='https://example.edu.cn/index.htm', group_name='另一学院')), first)


class ReviewedHomeAliasTests(unittest.TestCase):
    def test_validated_proposal_uses_the_same_observed_alias_check(self):
        from tests.test_source_governance import SourceGovernanceTests
        fixture = SourceGovernanceTests()
        fixture.setUp()
        try:
            html = '<main><a href="index.htm">首页</a>' + fixture.html() + '</main>'
            original = governance.source_config(fixture.dept)
            from backend.scraper.discovery.inventory_crawler import inspect_page
            from backend.services.source_inventory import site_key
            key = site_key(fixture.school.url)
            report = fixture.identity.report(key)
            unit = next(page for page in report['pages'] if page['url'] == 'https://college.example.edu.cn/')
            inspect_page(fixture.identity, report['site'], unit, fetcher=lambda url: {
                'url': url, 'status': 200, 'html': '<title>示例学院</title><h1>示例学院</h1><nav>'
                '<a href="/notices/">通知公告</a><a href="/notices/index.htm">通知公告</a></nav>'})
            ids = []
            for config in (original, dict(original, list_url=original['list_url'] + 'index.htm')):
                evidence = governance.capture_source_evidence(fixture.school.id, config,
                    seed_html=html, fetcher=fixture.fetcher(html), inventory=fixture.identity)
                proposal = governance.propose_source(fixture.school.id, config, evidence)
                result = governance.validate_proposal(proposal.id)
                self.assertTrue(result['passed'], result)
                ids.append(governance.activate_proposal(proposal.id).id)
            self.assertEqual(ids[0], ids[1])
            self.assertEqual(Department.query.count(), 1)
            self.assertEqual(SourceConfigVersion.query.count(), 1)
        finally:
            fixture.tearDown()
            fixture.doCleanups()


if __name__ == '__main__':
    unittest.main()
