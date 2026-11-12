"""Recorded execution behavior on local HTML, never evidence of website yield."""
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.services.source_inventory import Inventory
from backend.services.discovery_cache import DiscoveryCache
from backend.services.runtime_catalog import RuntimeCatalog
from backend.scraper.discovery.inventory_crawler import inspect_page

ROOT_URL = 'https://example.edu.cn/'
HOME = '<title>学校官网</title><nav><a href="/departments/">院系设置</a></nav>'
DEPARTMENTS = '<title>院系设置</title><nav><a href="/">学校首页</a></nav>'


class ExplorationEventTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.inv = Inventory(Path(self.temp.name) / 'inventory.db')
        self.key = self.inv.ensure_site('本地程序用例', ROOT_URL)

    def inspect(self, url=ROOT_URL, html=HOME, **response):
        page = self.inv.get_page(self.key, url)
        site = self.inv.report(self.key)['site']
        inspect_page(self.inv, site, page, fetcher=lambda _: dict(html=html, url=url, status=200, **response), ai_assist=False)

    def events(self):
        return self.inv.report(self.key)['exploration_events']

    def test_existing_rows_and_readers_never_manufacture_history(self):
        self.inv.enqueue(self.key, ROOT_URL + 'old/', '旧入口', 'directory', 1, [], 'school_domain')
        self.assertEqual(self.events(), [])
        self.assertEqual(self.events(), [])
        self.assertEqual(self.inv.report(self.key)['exploration_history']['history_before_recording'], 'unknown')

    def test_actual_inspection_and_first_enqueue_keep_causal_proof(self):
        self.inspect()
        child = ROOT_URL + 'departments/'
        self.inspect(child, DEPARTMENTS)
        events = self.events()
        self.assertEqual([row['event_type'] for row in events], [
            'exploration_started', 'page_discovered', 'page_inspection_finished',
            'page_inspection_started', 'page_inspection_finished'])
        first, discovered, ended, started, child_end = events
        self.assertEqual(discovered['parent_event_id'], first['id'])
        self.assertEqual(started['parent_event_id'], discovered['id'])
        self.assertEqual(child_end['parent_event_id'], started['id'])
        self.assertEqual(discovered['evidence'][0]['reference_url'], ROOT_URL)
        self.assertTrue(discovered['evidence'][0]['locator'])
        self.assertEqual(discovered['evidence'][0]['content_hash'], hashlib.sha256(HOME.encode()).hexdigest())
        self.assertEqual(ended['state'], 'fetched')
        self.assertIsNone(ended['cost'])
        self.assertEqual(ended['cost_source'], 'not_measured')
        self.assertEqual(ended['recorder_version'], 1)
        self.assertRegex(ended['parser_revision'], r'^[0-9a-f]{64}$')
        self.assertEqual(len({row['id'] for row in events}), len(events))
        self.assertEqual([row['sequence'] for row in events], sorted(row['sequence'] for row in events))
        self.assertEqual(Inventory(self.inv.path).report(self.key)['exploration_events'], events)

    def test_reread_and_loop_do_not_invent_new_discoveries(self):
        self.inspect()
        self.inspect()
        self.inspect(ROOT_URL + 'departments/', DEPARTMENTS)
        events = self.events()
        self.assertEqual(sum(e['event_type'] == 'page_discovered' for e in events), 1)
        self.assertEqual(sum(e['event_type'] == 'exploration_started' for e in events), 2)
        self.assertEqual(len(events), 7)

    def test_unrecorded_legacy_upstream_stays_missing(self):
        old = ROOT_URL + 'old/'
        self.inv.enqueue(self.key, old, '旧入口', 'directory', 1, ['学校官网'], 'school_domain')
        self.inspect(old, '<title>旧入口</title>')
        event = self.events()[0]
        self.assertEqual(event['event_type'], 'page_inspection_started')
        self.assertIsNone(event['parent_event_id'])
        self.assertEqual(event['upstream_status'], 'not_recorded')

    def test_failed_read_is_failure_not_semantic_low_yield(self):
        self.inspect(html='', outcome='network_error', error='transport unavailable')
        event = self.events()[-1]
        self.assertEqual(event['state'], 'pending')
        self.assertEqual(event['outcome'], 'read_failed')
        self.assertNotIn('low_yield', json.dumps(event))
        self.assertIsNone(event['cost'])

    def test_program_exception_records_interruption_without_secret_message(self):
        page = self.inv.get_page(self.key, ROOT_URL)
        site = self.inv.report(self.key)['site']
        with self.assertRaisesRegex(RuntimeError, 'private diagnostic'):
            inspect_page(self.inv, site, page, fetcher=lambda _: (_ for _ in ()).throw(RuntimeError('private diagnostic')), ai_assist=False)
        event = self.events()[-1]
        self.assertEqual(event['event_type'], 'page_inspection_interrupted')
        self.assertEqual(event['error_type'], 'RuntimeError')
        self.assertNotIn('private diagnostic', json.dumps(self.events()))

    def test_enqueue_event_failure_rolls_back_new_page(self):
        with self.inv.connect() as connection:
            connection.execute("CREATE TRIGGER reject_discovery BEFORE INSERT ON exploration_events "
                "WHEN NEW.event_type='page_discovered' BEGIN SELECT RAISE(ABORT,'fixture event failure'); END")
        with self.assertRaisesRegex(Exception, 'fixture event failure'):
            self.inspect()
        self.assertIsNone(self.inv.get_page(self.key, ROOT_URL + 'departments/'))
        self.assertEqual(self.events()[-1]['event_type'], 'page_inspection_interrupted')

    def test_event_window_keeps_parents_or_reports_gap_with_bounded_size(self):
        self.inspect()
        self.inspect(ROOT_URL + 'departments/', DEPARTMENTS)
        from backend.services.exploration_events import read_events
        with self.inv.connect() as connection:
            events, meta = read_events(connection, self.key, limit=1, maximum=8)
        self.assertEqual(meta['total_events'], 5)
        self.assertEqual(meta['selected_recent_events'], 1)
        self.assertEqual(len(events), 4)
        self.assertFalse(meta['missing_parent_ids'])
        with self.inv.connect() as connection:
            limited, meta = read_events(connection, self.key, limit=1, maximum=2)
        self.assertEqual(len(limited), 2)
        self.assertTrue(meta['missing_parent_ids'])

    def test_cache_cleanup_keeps_execution_progress(self):
        cache = DiscoveryCache(Path(self.temp.name) / 'cache.db')
        cache_key = cache.ensure_site('本地程序用例', ROOT_URL)
        inspect_page(cache, cache.report(cache_key)['site'], cache.get_page(cache_key, ROOT_URL),
                     fetcher=lambda _: dict(html=HOME, url=ROOT_URL, status=200), ai_assist=False)
        before = cache.report(cache_key)['exploration_events']
        cache.trim(all_cache=True)
        self.assertEqual(cache.report(cache_key)['exploration_events'], before)

    def test_published_history_does_not_reopen_inventory(self):
        self.inspect()
        catalog = RuntimeCatalog(Path(self.temp.name) / 'published.db')
        catalog.publish(self.inv, self.key)
        expected = self.events()
        with patch.object(Inventory, '__init__', side_effect=AssertionError('No scratch writer')):
            report = catalog.monitor_snapshot(self.key)
        self.assertEqual(report['exploration_events'], expected)
        self.assertEqual(report['exploration_history']['total_events'], len(expected))
        self.assertEqual(report['exploration_history']['history_before_recording'], 'unknown')

    def test_task_reference_cannot_copy_payload_or_lease(self):
        from backend.services.tasks import execution_scope
        with execution_scope(dict(id=17, generation=3, payload=dict(api_key='private'), token='secret lease')):
            self.inspect()
        encoded = json.dumps(self.events())
        self.assertNotIn('private', encoded)
        self.assertNotIn('secret lease', encoded)
        self.assertEqual(self.events()[0]['task_id'], 17)
        self.assertEqual(self.events()[0]['task_generation'], 3)

    def test_public_event_projection_rejects_secret_urls_and_non_page_identity(self):
        from backend.services.exploration_events import public_events
        facts = dict(nodes=[dict(id='public-home', entity_type='page', url=ROOT_URL)])
        events = [dict(id='good', sequence=1, event_type='exploration_started', url=ROOT_URL,
            observed_at='2026-10-22T00:00:00Z', parent_event_id=None, payload={'token':'secret'},
            evidence=[dict(reference_url=ROOT_URL+'?token=secret', locator='nav a', content_hash='h')]),
            dict(id='unsafe', url=ROOT_URL+'#/page?token=secret')]
        projected = public_events(events, facts)
        self.assertEqual([event['id'] for event in projected], ['good'])
        self.assertEqual(projected[0]['node_id'], 'public-home')
        self.assertEqual(projected[0]['evidence'], [])
        self.assertNotIn('secret', json.dumps(projected))


if __name__ == '__main__':
    unittest.main()
