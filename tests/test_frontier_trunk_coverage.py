"""Observed school entrances get a first pass before one broad branch grows."""
from pathlib import Path
import tempfile
import unittest

from backend.services.source_inventory import Inventory

ROOT = 'https://example.edu.cn/'


class FrontierTrunkCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.store = Inventory(Path(self.temp.name) / 'inventory.sqlite3')
        self.key = self.store.ensure_site('测试大学', ROOT)
        self.fetched(ROOT, 'a' * 64)
        self.layout = self.add('layout', '院系设置', 'directory', 1)
        self.fetched(self.layout, 'b' * 64)
        self.busy = self.add('branch/many', '学科 教学 招生 奖助 就业 国际交流 教师发展', 'channel', 3)

    def add(self, path, label, kind, depth):
        url = ROOT + path
        self.store.enqueue(self.key, url, label, kind, depth, [], 'school_domain')
        return url

    def fetched(self, url, digest):
        self.store.finish(self.key, url, state='fetched', status_code=200)
        with self.store.connect() as c:
            c.execute('UPDATE pages SET content_hash=? WHERE site_key=? AND url=?', (digest, self.key, url))

    def roster(self, *, digest='b' * 64, node_locator='#faculty a', relation='directory_entry'):
        unit = self.add('college', '工学院', 'unit', 2)
        self.store.record_edges(self.key, self.layout, [{'url': unit, 'label': '工学院', 'kind': 'unit',
            'path': [], 'locator': '#faculty a', 'decision': 'follow'}], 'b' * 64)
        self.store.record_structure(self.key, self.layout, [{'key': 'college', 'name': '工学院',
            'kind': 'unit', 'url': unit, 'parent': '', 'locator': node_locator, 'relation': relation}], digest)
        return unit

    def test_unread_real_roster_member_precedes_multi_need_descendant(self):
        unit = self.roster()
        chosen = self.store.claim(self.key, focus='valuable')
        self.assertEqual(chosen['url'], unit)
        self.assertEqual(self.store.get_page(self.key, self.busy)['state'], 'pending')
        self.assertEqual(self.store.exploration_choices(self.key)[0]['reason'], 'unread_school_trunk')

    def test_school_funding_entrance_also_gets_first_pass(self):
        funding = self.add('aid', '学生资助', 'gateway', 1)
        self.store.record_edges(self.key, ROOT, [{'url': funding, 'label': '学生资助', 'kind': 'gateway',
            'path': [], 'locator': '#main-navigation a', 'decision': 'follow'}], 'a' * 64)
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], funding)

    def test_stale_or_unbound_roster_facts_do_not_create_trunk_priority(self):
        for changes in ({'digest': 'c' * 64}, {'node_locator': '#unrelated a'}, {'relation': 'navigation_entry'}):
            with self.subTest(changes=changes):
                self.roster(**changes)
                chosen = self.store.claim(self.key, focus='valuable')
                self.assertEqual(chosen['url'], self.busy)
                with self.store.connect() as c:
                    c.execute("UPDATE pages SET state='pending',attempts=0 WHERE site_key=? AND url=?", (self.key, self.busy))

    def test_an_already_observed_member_does_not_restart_the_initial_pass(self):
        unit = self.roster(); self.fetched(unit, 'd' * 64)
        with self.store.connect() as c:
            c.execute("UPDATE pages SET state='pending' WHERE site_key=? AND url=?", (self.key, unit))
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], self.busy)

    def test_network_wait_is_still_authoritative_for_a_primary_entrance(self):
        unit = self.roster()
        with self.store.connect() as c:
            c.execute("UPDATE pages SET retry_at='2999-01-01T00:00:00+00:00' WHERE url=?", (unit,))
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], self.busy)

    def test_oldest_pending_turn_is_kept_for_other_observed_entrances(self):
        self.roster()
        with self.store.connect() as c:
            c.execute('UPDATE pages SET attempts=4 WHERE site_key=? AND url=?', (self.key, ROOT))
        self.assertEqual(self.store.claim(self.key, focus='valuable')['url'], self.busy)
