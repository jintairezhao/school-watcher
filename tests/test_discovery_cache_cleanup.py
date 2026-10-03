"""Scratch eviction must remove orphan evidence without losing the frontier."""
from pathlib import Path
import tempfile
import unittest

from backend.services.discovery_cache import DiscoveryCache


class DiscoveryCacheCleanupTests(unittest.TestCase):
    def test_trim_removes_old_orphan_edges_but_keeps_live_routes_and_pending_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = DiscoveryCache(Path(folder) / 'cache.db')
            root = 'https://example.edu.cn/'
            key = cache.ensure_site('Example University', root)
            cache.enqueue(key, root + 'college/', 'College', 'unit', 1, [], 'school_domain')
            link = {'url': root + 'college/', 'label': 'College', 'kind': 'unit',
                    'path': [], 'locator': 'nav a', 'decision': 'follow'}
            cache.record_edges(key, root, [link], 'current')
            cache.record_edges(key, root + 'previously-evicted/', [link], 'old')
            cache.trim()
            report = cache.report(key)
            self.assertEqual(report['edge_count'], 1)
            self.assertEqual(report['states'], {'pending': 2})
            with cache.connect() as connection:
                self.assertEqual(connection.execute('SELECT parent_url FROM edges').fetchone()[0], root)

    def test_capacity_cleanup_preserves_current_evidence_and_visited_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = DiscoveryCache(Path(folder) / 'cache.db')
            root = 'https://example.edu.cn/'
            key = cache.ensure_site('Example University', root)
            cache.finish(key, root, state='fetched')
            cache.enqueue(key, root + 'college/', 'College', 'unit', 1, [], 'school_domain')
            cache.record_edges(key, root, [
                {'url': root + f'old/{i}/', 'label': f'Old {i}', 'kind': 'navigation',
                 'path': [], 'locator': 'x' * 2048 + str(i), 'decision': 'follow'}
                for i in range(100)], 'old')
            cache.max_bytes = 256 * 1024
            cache.trim()
            self.assertEqual(cache.get_page(key, root)['state'], 'fetched')
            self.assertEqual(cache.get_page(key, root + 'college/')['state'], 'pending')
            self.assertEqual(cache.report(key)['edge_count'], 100)
            self.assertLessEqual(cache.snapshots.path.stat().st_size, cache.max_bytes)


if __name__ == '__main__':
    unittest.main()
