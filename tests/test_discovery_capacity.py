"""Cache pressure must never discard progress or prevent the next college."""
import hashlib
import random
from pathlib import Path
import tempfile
import unittest

from backend.services.discovery_cache import DiscoveryCache
from backend.services.source_inventory import Inventory


class DiscoveryCapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'discovery.db'
        self.root = 'https://example.edu.cn/'

    def tearDown(self):
        self.temp.cleanup()

    def test_large_progress_does_not_consume_temporary_cache_budget(self):
        cache = DiscoveryCache(self.path)
        key = cache.ensure_site('Example', self.root)
        cache.finish(key, self.root, state='fetched')
        cache.record_edges(key, self.root, [
            dict(url=self.root + str(i), label='College', kind='unit', path=[],
                 locator='x' * 2048 + str(i), decision='follow') for i in range(150)], 'proof')
        cache.enqueue(key, self.root + 'college/', 'College', 'unit', 1, [], 'school_domain')
        cache.max_bytes = 256 * 1024
        cache.trim()
        self.assertEqual(cache.get_page(key, self.root)['state'], 'fetched')
        self.assertEqual(cache.report(key)['edge_count'], 150)
        self.assertEqual(cache.claim(key)['url'], self.root + 'college/')
        self.assertGreater(self.path.stat().st_size, cache.max_bytes)

    def test_legacy_snapshots_migrate_then_evict_without_losing_progress(self):
        old = Inventory(self.path)
        key = old.ensure_site('Example', self.root)
        html = random.Random(7).randbytes(700000).hex()
        old.finish(key, self.root, html=html, state='fetched')
        cache = DiscoveryCache(self.path)
        self.assertEqual(cache.snapshot(key, self.root), html)
        cache.max_bytes = 256 * 1024
        self.assertEqual(cache.trim(), 1)
        self.assertIsNone(cache.snapshot(key, self.root))
        self.assertEqual(cache.get_page(key, self.root)['content_hash'], hashlib.sha256(html.encode()).hexdigest())
        self.assertIsNone(cache.claim(key))
        with cache.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)
        self.assertLessEqual(cache.snapshots.path.stat().st_size, cache.max_bytes)
        reopened = DiscoveryCache(self.path)
        self.assertEqual(reopened.get_page(key, self.root)['state'], 'fetched')

    def test_inventory_maintenance_reads_and_updates_migrated_snapshots(self):
        cache = DiscoveryCache(self.path)
        key = cache.ensure_site('Example', self.root)
        cache.finish(key, self.root, html='first page', state='fetched')
        maintenance = Inventory(self.path)
        self.assertEqual(maintenance.snapshot(key, self.root), 'first page')
        maintenance.finish(key, self.root, html='updated page', state='fetched')
        self.assertEqual(cache.snapshot(key, self.root), 'updated page')
        with maintenance.connect() as c:
            self.assertEqual(c.execute('SELECT count(*) FROM snapshots').fetchone()[0], 0)

    def test_clear_temporary_pages_preserves_route_and_next_college(self):
        cache = DiscoveryCache(self.path)
        key = cache.ensure_site('Example', self.root)
        cache.finish(key, self.root, html='<html>Directory</html>', state='fetched')
        cache.enqueue(key, self.root + 'college/', 'College', 'unit', 1, [], 'school_domain')
        cache.record_structure(key, self.root, [dict(key='college', name='College', kind='unit',
            url=self.root + 'college/', parent='', locator='a', relation='directory_entry')], 'proof')
        self.assertEqual(cache.trim(all_cache=True), 1)
        self.assertIsNone(cache.snapshot(key, self.root))
        self.assertEqual(len(cache.structure(key)), 1)
        self.assertEqual(cache.claim(key)['url'], self.root + 'college/')
