"""Keep an early focused branch from hiding the school's observed layout."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.services.source_inventory import Inventory


ROOT = 'https://example.edu.cn/'


class InitialFrontierLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='initial-frontier-layout-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'inventory.sqlite3'
        self.store = Inventory(self.path)
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.fetched(ROOT, 'root-current')

    def fetched(self, url, content_hash):
        self.store.finish(self.key, url, state='fetched')
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET content_hash=? WHERE site_key=? AND url=?',
                               (content_hash, self.key, url))

    def add(self, suffix, label, kind, depth, *, state='pending', attempts=0):
        url = ROOT + suffix
        self.store.enqueue(self.key, url, label, kind, depth, [], 'school_domain')
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET state=?,attempts=? WHERE site_key=? AND url=?',
                               (state, attempts, self.key, url))
        return url

    def edge(self, parent, target, kind, *, content_hash, decision='follow'):
        with self.store.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                'SELECT * FROM edges WHERE site_key=? AND parent_url=?', (self.key, parent))]
        links = [{'url': row['target_url'], 'label': row['label'], 'kind': row['kind'],
                  'path': json.loads(row['path_json']), 'locator': row['locator'],
                  'decision': row['decision']} for row in rows]
        links.append({'url': target, 'label': '真实页面中的入口', 'kind': kind, 'path': [],
                      'locator': '#observed-' + str(len(links)), 'decision': decision})
        self.store.record_edges(self.key, parent, links, content_hash)

    def test_observed_direct_directory_precedes_early_channel_and_gateway(self):
        directory = self.add('opaque-layout', '组织机构', 'directory', 1)
        channel = self.add('opaque-channel', '通知公告', 'channel', 1)
        gateway = self.add('opaque-gateway', '教学入口', 'gateway', 1)
        for url, kind in ((directory, 'directory'), (channel, 'channel'), (gateway, 'gateway')):
            self.edge(ROOT, url, kind, content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], directory)

    def test_layout_preference_needs_current_followed_homepage_evidence(self):
        directory = self.add('opaque-layout', '组织机构', 'directory', 1)
        channel = self.add('opaque-channel', '通知公告', 'channel', 1)
        for content_hash, decision in [('root-old', 'follow'), ('root-current', 'route_reference')]:
            with self.subTest(content_hash=content_hash, decision=decision):
                self.store.record_edges(self.key, ROOT, [], content_hash)
                self.edge(ROOT, directory, 'directory', content_hash=content_hash, decision=decision)
                selected = self.store.claim(self.key, focus='valuable')
                self.assertEqual(selected['url'], channel)
                with self.store.connect() as connection:
                    connection.execute("UPDATE pages SET state='pending',attempts=0 WHERE url=?", (channel,))

    def test_nested_directory_does_not_displace_a_known_publishing_route(self):
        parent = self.add('unit', '真实部门', 'unit', 1, state='fetched')
        self.fetched(parent, 'unit-current')
        nested = self.add('unit/layout', '机构设置', 'directory', 2)
        channel = self.add('notice', '通知公告', 'channel', 1)
        self.edge(parent, nested, 'directory', content_hash='unit-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

    def test_unprobed_units_get_a_slice_turn_before_one_unit_expands_all_columns(self):
        layout = self.add('layout', '教学单位', 'directory', 1, state='fetched')
        self.fetched(layout, 'layout-current')
        library = self.add('opaque-unit-a', '图书馆', 'unit', 2, state='fetched', attempts=8)
        self.fetched(library, 'library-current')
        college = self.add('opaque-unit-b', '真实学院', 'unit', 2)
        self.edge(layout, library, 'unit', content_hash='layout-current')
        self.edge(layout, college, 'unit', content_hash='layout-current')
        for index in range(12):
            column = self.add(f'opaque-list-{index}', '通知公告', 'channel', 3)
            self.edge(library, column, 'channel', content_hash='library-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], college)

    def test_fair_turn_survives_restart_and_prefers_less_explored_unit(self):
        layout = self.add('layout', '教学单位', 'directory', 1, state='fetched')
        self.fetched(layout, 'layout-current')
        first = self.add('a-unit', '第一部门', 'unit', 2, state='fetched', attempts=8)
        self.fetched(first, 'first-current')
        next_unit = self.add('z-unit', '第二部门', 'unit', 2, state='fetched')
        self.fetched(next_unit, 'next-current')
        first_list = self.add('a-list', '通知公告', 'channel', 3)
        next_list = self.add('z-list', '一般信息', 'channel', 3)
        self.edge(layout, first, 'unit', content_hash='layout-current')
        self.edge(layout, next_unit, 'unit', content_hash='layout-current')
        self.edge(first, first_list, 'channel', content_hash='first-current')
        self.edge(next_unit, next_list, 'channel', content_hash='next-current')
        restored = Inventory(self.path)
        self.assertEqual(restored.claim(self.key, focus='valuable')['url'], next_list)

    def test_network_wait_is_not_removed_by_layout_or_fairness(self):
        directory = self.add('layout', '组织机构', 'directory', 1)
        channel = self.add('notices', '通知公告', 'channel', 1)
        self.edge(ROOT, directory, 'directory', content_hash='root-current')
        with self.store.connect() as connection:
            connection.execute("UPDATE pages SET retry_at='2999-01-01T00:00:00+00:00' WHERE url=?", (directory,))
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

    def test_a_directory_name_alone_cannot_trigger_the_initial_layout_pass(self):
        self.add('unproven-directory', '组织机构', 'directory', 1)
        channel = self.add('notices', '通知公告', 'channel', 1)
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

    def test_already_observed_direct_directory_waits_for_the_ordinary_rotation(self):
        directory = self.add('layout', '院系设置', 'directory', 1, attempts=1)
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET checked_at=?,content_hash=? WHERE url=?',
                               ('2026-10-01T00:00:00+00:00', 'observed-material', directory))
        # An actual observation finishes the initial pass. A claim alone does
        # not: a process can stop between claiming the URL and reading it.
        channel = self.add('notices', '教务通知', 'channel', 1)
        self.edge(ROOT, directory, 'directory', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

    def test_interrupt_before_read_keeps_layout_priority_without_inventing_a_failure(self):
        directory = self.add('layout', '院系设置', 'directory', 1)
        self.edge(ROOT, directory, 'directory', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], directory)
        self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        restored = Inventory(self.path)
        restored.recover(self.key)
        self.assertEqual(restored.claim(self.key, focus='valuable')['url'], directory)
        self.assertEqual(restored.get_page(self.key, directory)['network_failures'], 0)

    def failed_layout(self):
        directory = self.add('layout', '院系设置', 'directory', 1)
        self.edge(ROOT, directory, 'directory', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], directory)
        self.store.fail_read(self.key, directory, retryable=True,
                             error='http_connect_timeout', health='unreachable')
        return directory

    def retry_due(self, url):
        with self.store.connect() as connection:
            connection.execute("UPDATE pages SET retry_at='2000-01-01T00:00:00+00:00' WHERE url=?", (url,))

    def test_due_network_retry_keeps_unread_homepage_layout_priority_after_restart(self):
        directory = self.failed_layout()
        self.add('gateway', '教学 招生 就业 国际交流', 'gateway', 1)
        self.retry_due(directory)
        restored = Inventory(self.path)
        self.assertEqual(restored.claim(self.key, focus='valuable')['url'], directory)
        self.assertEqual(restored.get_page(self.key, directory)['network_failures'], 1)

    def test_failed_layout_respects_wait_without_consuming_its_retry(self):
        directory = self.failed_layout()
        other = self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        self.assertEqual(Inventory(self.path).claim(self.key, focus='valuable')['url'], other)
        saved = self.store.get_page(self.key, directory)
        self.assertEqual((saved['attempts'], saved['network_failures']), (1, 1))

    def test_failed_layout_needs_current_homepage_evidence_to_retain_priority(self):
        directory = self.failed_layout()
        other = self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        self.retry_due(directory)
        self.fetched(ROOT, 'changed-root')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], other)

    def test_previously_read_layout_does_not_get_initial_priority_from_later_outage(self):
        directory = self.failed_layout()
        other = self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        with self.store.connect() as connection:
            connection.execute('UPDATE pages SET content_hash=? WHERE url=?', ('retained-material', directory))
        self.retry_due(directory)
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], other)

    def test_initial_layout_retry_is_finite_and_yields_during_low_frequency_recovery(self):
        directory = self.failed_layout()
        other = self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        for expected in (2, 3):
            self.retry_due(directory)
            self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], directory)
            self.store.fail_read(self.key, directory, retryable=True,
                                 error='http_connect_timeout', health='unreachable')
            self.assertEqual(self.store.get_page(self.key, directory)['network_failures'], expected)
        self.assertEqual(self.store.get_page(self.key, directory)['state'], 'failed')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], other)

    def test_layout_still_unread_when_low_frequency_recovery_starts_a_new_network_burst(self):
        directory = self.failed_layout()
        self.add('other', '教学 招生 就业 国际交流', 'gateway', 1)
        for _ in range(2):
            self.store.fail_read(self.key, directory, retryable=True,
                                 error='http_connect_timeout', health='unreachable')
        self.retry_due(directory)
        self.store.retry(self.key)
        self.assertEqual(self.store.get_page(self.key, directory)['network_failures'], 0)
        self.assertEqual(Inventory(self.path).claim(self.key, focus='valuable')['url'], directory)

    def test_changed_homepage_role_does_not_promote_an_old_directory_classification(self):
        directory = self.add('old-layout', '组织机构', 'directory', 1)
        channel = self.add('notices', '通知公告', 'channel', 1)
        self.edge(ROOT, directory, 'channel', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

    def test_fairness_uses_the_configured_existing_slice_size(self):
        unit = self.add('unit', '真实部门', 'unit', 1, attempts=2)
        channel = self.add('notices', '通知公告', 'channel', 1)
        self.edge(ROOT, unit, 'unit', content_hash='root-current')
        from backend.services.discovery_policy import get_policy
        configured = dict(get_policy(), discovery_slice_pages=4)
        with patch('backend.services.discovery_policy.get_policy', return_value=configured):
            self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)
            self.store.finish(self.key, channel, state='fetched')
            self.assertEqual(Inventory(self.path).claim(self.key, focus='valuable')['url'], unit)

    def test_initial_layout_order_is_deterministic_after_restart(self):
        directories = []
        for suffix, label in [('z-layout', '组织机构'), ('a-layout', '教学机构')]:
            directory = self.add(suffix, label, 'directory', 1)
            self.edge(ROOT, directory, 'directory', content_hash='root-current')
            directories.append(directory)
        self.add('notice', '通知公告', 'channel', 1)
        first = self.store.claim(self.key, focus='valuable')
        self.assertEqual(first['url'], min(directories))
        self.store.finish(self.key, first['url'], state='fetched')
        self.assertEqual(Inventory(self.path).claim(self.key, focus='valuable')['url'], max(directories))

    def test_observed_academic_directory_precedes_general_organization_despite_url_order(self):
        organization = self.add('a-layout', '组织机构', 'directory', 1)
        academic = self.add('z-layout', '院系设置', 'directory', 1)
        for url in (organization, academic):
            self.edge(ROOT, url, 'directory', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], academic)
        self.store.finish(self.key, academic, state='fetched')
        self.assertEqual(Inventory(self.path).claim(self.key, focus='valuable')['url'], organization)

    def test_observed_academic_layout_wording_variants_share_priority(self):
        from backend.scraper.discovery.frontier import choose_route
        organization = {'url': ROOT + 'a-layout', 'kind': 'directory', 'label': '组织机构',
                        'depth': 1, 'priority': 0}
        parent = {'url': ROOT, 'kind': 'root', 'state': 'fetched', 'content_hash': 'current'}
        for label in ('院系设置', '教学机构', '教学科研单位', '学部设置', '学科设置',
                      '学科专业', '学科与专业', '学科、专业设置', '本科专业设置',
                      '研究生学科目录', '学科专业目录', '专业介绍', '学科建设'):
            with self.subTest(label=label):
                academic = dict(organization, url=ROOT + 'z-layout', label=label, priority=4)
                edges = [{'parent_url': ROOT, 'target_url': page['url'], 'kind': 'directory',
                          'decision': 'follow', 'content_hash': 'current'}
                         for page in (organization, academic)]
                self.assertEqual(choose_route([organization, academic], edges=edges,
                                              parents={ROOT: parent}), academic)

    def test_academic_news_or_channel_names_cannot_be_promoted_to_layout_directories(self):
        organization = self.add('z-layout', '组织机构', 'directory', 1)
        self.edge(ROOT, organization, 'directory', content_hash='root-current')
        for index, label in enumerate(('学科建设新闻', '院系设置通知', '专业设置动态')):
            channel = self.add(f'a-news-{index}', label, 'channel', 1)
            self.edge(ROOT, channel, 'channel', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], organization)

    def test_oldest_turn_overlap_only_defers_fair_unit_probe_until_next_slice(self):
        from backend.scraper.discovery.frontier import choose_route
        oldest = {'url': ROOT + 'oldest', 'kind': 'navigation', 'frontier_rowid': 1}
        unit = {'url': ROOT + 'unit', 'kind': 'unit', 'frontier_rowid': 3}
        library = {'url': ROOT + 'library', 'kind': 'unit', 'attempts': 14}
        columns = [{'url': ROOT + f'column-{i}', 'kind': 'channel', 'frontier_rowid': 4 + i}
                   for i in range(3)]
        pages = [oldest, unit, *columns]
        history = {page['url']: page for page in [*pages, library]}
        branches = {page['url']: library['url'] if page in columns else page['url'] for page in pages}
        selected = []
        for count in range(14, 18):
            choice = choose_route(pages, claim_count=count, history=history, branches=branches)
            selected.append(choice['url'])
            pages.remove(choice)
        self.assertEqual(selected[0], oldest['url'])  # Claim 15 retains its oldest-entrance turn.
        self.assertEqual(selected[3], unit['url'])  # Claim 18 resumes the existing 3-page rotation.


if __name__ == '__main__':
    unittest.main()
