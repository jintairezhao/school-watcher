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

    def test_already_attempted_direct_directory_waits_for_the_ordinary_rotation(self):
        directory = self.add('layout', '院系设置', 'directory', 1, attempts=1)
        channel = self.add('notices', '通知公告', 'channel', 1)
        self.edge(ROOT, directory, 'directory', content_hash='root-current')
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], channel)

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


if __name__ == '__main__':
    unittest.main()
