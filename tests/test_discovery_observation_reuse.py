"""Shared chrome observations must not buy recognition for every roster page."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from backend.scraper.discovery.observation_reuse import repeated_navigation_observations


ROOT = 'https://example.edu.cn/'
PAGE = ROOT + 'units/index.html'
WIDGET = ('<aside><section id="shared"><h2>学校新闻</h2><ul>'
          '<li><a href="/news/1.htm">关于学校近期活动安排的通知</a><time>2026-10-16</time></li>'
          '</ul></section></aside>')


class ObservationReuseTests(unittest.TestCase):
    def reuse(self, html, reference=None, url=PAGE):
        return repeated_navigation_observations(url, html,
            [{'container_locator': '#shared'}], reference_url=ROOT,
            reference_html=reference or WIDGET + '<main><h2>学校首页</h2></main>',
            reference_candidates=[{'container_locator': '#shared'}])

    def test_exact_shared_chrome_has_real_reference_and_keeps_all_candidate_facts(self):
        result = self.reuse(WIDGET + '<main><a href="/faculty/">真实院系入口</a></main>')
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['reference_url'], ROOT)
        self.assertEqual(result[0]['reference_container_locator'],
                         'html:nth-of-type(1) > body:nth-of-type(1) > aside:nth-of-type(1) > section:nth-of-type(1)')

    def test_main_publication_is_independent_even_with_identical_titles_and_links(self):
        self.assertEqual(self.reuse('<main>' + WIDGET + '</main>'), [])

    def test_missing_main_different_host_or_reference_page_cannot_be_deduplicated(self):
        self.assertEqual(self.reuse(WIDGET), [])
        self.assertEqual(self.reuse(WIDGET + '<main/>', url=ROOT), [])
        self.assertEqual(self.reuse(WIDGET + '<main/>', url='https://faculty.example.edu.cn/'), [])

    def test_changed_link_date_item_or_structure_requires_a_fresh_observation(self):
        for old, new in (('/news/1.htm', '/news/2.htm'), ('2026-10-16', '2026-10-17'),
                         ('学校近期活动', '学校奖学金'), ('<ul>', '<ol>')):
            self.assertEqual(self.reuse(WIDGET.replace(old, new) + '<main/>'), [])

    def test_relative_addresses_are_resolved_in_each_real_page(self):
        relative = WIDGET.replace('/news/1.htm', 'news/1.htm')
        self.assertEqual(self.reuse(relative + '<main/>', reference=relative + '<main/>'), [])

    def test_different_observed_dynamic_configuration_cannot_share_an_observation(self):
        for old, new in (('onclick="openColumn(1)"', 'onclick="openColumn(2)"'),
                         ('<script>column=1</script>', '<script>column=2</script>'),
                         ('<script>label="甲  乙"</script>', '<script>label="甲 乙"</script>')):
            first = (WIDGET.replace('id="shared"', 'id="shared" ' + old) if old.startswith('onclick')
                     else WIDGET.replace('</section>', old + '</section>'))
            second = first.replace(old, new)
            with self.subTest(change=old):
                self.assertEqual(self.reuse(second + '<main/>', reference=first + '<main/>'), [])


class ObservationQueueTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests()
        self.fixture.setUp()

    def tearDown(self):
        self.fixture.tearDown()

    def queue(self, changed=False):
        from backend.services.source_onboarding import queue_columns
        pages = [{'url': ROOT, 'final_url': ROOT, 'kind': 'channel', 'state': 'fetched', 'label': '首页'},
                 {'url': PAGE, 'final_url': PAGE, 'kind': 'directory', 'state': 'fetched', 'label': '院系设置'}]
        candidate = {'container_locator': '#shared', 'list_selector': '#shared li',
                     'name': '学校新闻', 'group_name': ''}
        configs = [dict(candidate, list_url=p['url']) for p in pages]
        html = {ROOT: WIDGET + '<main>学校首页</main>', PAGE:
                (WIDGET.replace('/news/1.htm', '/news/2.htm') if changed else WIDGET) +
                '<main><a href="/faculty/">真实院系入口</a></main>'}
        inventory = SimpleNamespace(report=lambda _: {'pages': pages, 'site': {'root_url': ROOT}},
            structure=lambda _: [], snapshot=lambda _, url: html[url])
        with patch('backend.services.source_catalog.publication_candidates', return_value=configs), \
             patch('backend.services.discovery_changes.baseline', return_value={'hash': 'observed'}):
            result = queue_columns(self.fixture.school_id, inventory, 'observed')
        return result, configs

    def test_shared_roster_chrome_does_not_create_a_duplicate_paid_task(self):
        from backend.database.models import BackgroundTask
        result, original = self.queue()
        self.assertEqual([r.payload['url'] for r in BackgroundTask.query.filter_by(kind='onboard')], [ROOT])
        self.assertEqual(len(result['reused_navigation_observations']), 1)
        self.assertEqual(len(original), 2)
        self.assertEqual(original[1]['list_url'], PAGE)

    def test_changed_widget_remains_independent_work(self):
        from backend.database.models import BackgroundTask
        self.queue(changed=True)
        self.assertEqual(BackgroundTask.query.filter_by(kind='onboard').count(), 2)

    def test_existing_channel_identity_and_config_are_not_removed_or_merged(self):
        from backend.database.models import BackgroundTask, Department
        from backend.database.db import db
        source = Department(school_id=self.fixture.school_id, name='原有真实栏目',
                            list_url=PAGE, list_selector='#shared li')
        db.session.add(source)
        db.session.commit()
        ident = source.id
        self.queue()
        db.session.refresh(source)
        self.assertEqual(source.id, ident)
        self.assertEqual(source.name, '原有真实栏目')
        self.assertEqual(source.list_url, PAGE)
        self.assertEqual(Department.query.count(), 1)
        # Its valid local rule continues to be reused without buying discovery
        # again; it is not migrated to the homepage source's identity.
        self.assertFalse(any(row.payload['url'] == PAGE for row in BackgroundTask.query.filter_by(kind='onboard')))


if __name__ == '__main__':
    unittest.main()
