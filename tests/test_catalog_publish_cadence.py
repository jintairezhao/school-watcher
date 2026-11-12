"""Progress stays live while immutable catalogue publication is coalesced."""
from contextlib import ExitStack
from pathlib import Path
import unittest
from unittest.mock import patch

from backend.services.discovery_cache import adapt_site
from backend.services.runtime_catalog import RuntimeCatalog
from backend.services import tasks

ROOT = 'https://example.edu.cn/'


class CatalogPublishCadenceTests(unittest.TestCase):
    def setUp(self):
        from tests.test_direct_onboarding import DirectOnboardingTests
        self.fixture = DirectOnboardingTests(); self.fixture.setUp()
        self.app = self.fixture.app
        self.catalog = RuntimeCatalog(self.app.config['SOURCE_CATALOG_PATH'], versioned=False)
        self.clock = 0.0
        self.published = []
        self.synced = []
        self.queued = []
        self.handle = {'id': 999, 'generation': 1, 'kind': 'discover', 'capability': 'directory',
            'phase': 'crawl', 'payload': {}, 'checkpoint': {}}
        original_publish = self.catalog.publish
        def publish(inventory, key, **kwargs):
            value = original_publish(inventory, key, **kwargs)
            self.published.append((self.clock, self.catalog.get_page(key, ROOT)['title']))
            return value
        def checkpoint(data):
            self.handle['checkpoint'] = dict(data)
        def sync(school, catalog):
            self.synced.append(catalog.report(self.key)['pages'][0]['title'])
        def queue(school_id, inventory, key):
            current = inventory.get_page(key, ROOT)['title']
            self.assertEqual(self.catalog.get_page(key, ROOT)['title'], current,
                'Onboarding must follow the actually published state')
            self.queued.append(current)
            return {'onboarding_ids': [len(self.queued)], 'proposal_ids': [],
                'activated_ids': [], 'remaining_candidates': 0}
        self.stack = ExitStack()
        self.stack.enter_context(patch('backend.scraper.discovery.layered.prepare', return_value=False))
        self.stack.enter_context(patch('backend.services.runtime_catalog.RuntimeCatalog', return_value=self.catalog))
        self.stack.enter_context(patch('backend.services.discovery_cache.monotonic', side_effect=lambda:self.clock, create=True))
        self.stack.enter_context(patch('backend.services.tasks.current_execution', return_value=self.handle))
        self.checkpoint = self.stack.enter_context(patch('backend.services.tasks.checkpoint', side_effect=checkpoint))
        self.assert_owned = self.stack.enter_context(patch('backend.services.tasks.assert_owned'))
        self.publish = self.stack.enter_context(patch.object(self.catalog, 'publish', side_effect=publish))
        self.stack.enter_context(patch('backend.services.school_structure.sync_official_structure', side_effect=sync))
        self.queue = self.stack.enter_context(patch('backend.services.source_onboarding.queue_columns', side_effect=queue))
        self.record = self.stack.enter_context(patch('backend.services.onboarding_progress.record_progress'))
        self.stack.enter_context(patch('backend.services.source_governance.record_onboarding_slice'))
        self.pause = self.stack.enter_context(patch('backend.services.discovery_control.pause_if_requested'))

    def tearDown(self):
        self.stack.close(); self.fixture.tearDown()

    def crawl(self, updates, *, error=None, before_return=None):
        def run(inventory, key, **kwargs):
            self.key = key
            for processed, clock in enumerate(updates, 1):
                self.clock = clock
                inventory.finish(key, ROOT, html='<p>Revision ' + str(processed) + '</p>',
                    title='Revision ' + str(processed), state='fetched')
                kwargs['progress'](processed, inventory.progress_snapshot(key))
            if error:
                raise error
            if before_return:
                before_return(inventory, key)
            result = inventory.report(key); result['processed_this_run'] = len(updates)
            return result
        return patch('backend.scraper.discovery.inventory_crawler.crawl_site', side_effect=run)

    def test_fast_callbacks_keep_live_progress_but_coalesce_full_publications(self):
        with self.crawl(list(range(1, 11))):
            result = adapt_site('测试大学', ROOT)
        self.assertEqual([title for _, title in self.published], ['Revision 1', 'Revision 10'])
        # This cadence fixture uses versioned=False. Typed membership sync now
        # belongs to the atomic versioned publication, not adapt_site callbacks.
        # Its commit/failure behavior is covered by test_structure_publication_atomic.
        self.assertEqual(self.synced, [])
        self.assertEqual(self.queued, ['Revision 1', 'Revision 10'])
        self.assertEqual(self.record.call_count, 11)  # Every callback plus final status.
        self.assertEqual(self.handle['checkpoint']['student_scan_pages'], 10)
        self.assertEqual(len([c for c in self.checkpoint.call_args_list if 'student_scan_pages' in c.args[0]]), 11)
        self.assertEqual(result['onboarding_ids'], [1, 2])

    def test_long_crawl_publishes_at_most_once_each_fifteen_seconds(self):
        with self.crawl([0, 1, 14.9, 15, 16, 29.9, 30, 31]):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.published, [(0, 'Revision 1'), (15, 'Revision 4'),
            (30, 'Revision 7'), (31, 'Revision 8')])

    def test_final_flush_includes_settlement_after_last_callback(self):
        def settle(inventory, key):
            inventory.finish(key, ROOT, state='fetched', title='Settled final evidence')
        with self.crawl([0, 1], before_return=settle):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.published[-1][1], 'Settled final evidence')
        self.assertEqual(self.queued[-1], 'Settled final evidence')

    def test_zero_page_slice_still_publishes_real_saved_state_at_completion(self):
        with self.crawl([]):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.publish.call_count, 1)

    def test_deferred_pause_flushes_completed_pages_without_new_onboarding(self):
        original = tasks.TaskDeferred(reason='User requested pause', phase='user_paused')
        with self.crawl([0, 1, 2], error=original), self.assertRaises(tasks.TaskDeferred) as raised:
            adapt_site('测试大学', ROOT)
        self.assertIs(raised.exception, original)
        self.assertEqual(self.published[-1][1], 'Revision 3')
        self.assertEqual(self.queued, ['Revision 1'])

    def test_normal_return_flushes_before_pause_at_slice_boundary(self):
        original = tasks.TaskDeferred(reason='Pause at slice boundary', phase='user_paused')
        self.pause.side_effect = original
        with self.crawl([0, 1, 2]), self.assertRaises(tasks.TaskDeferred):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.published[-1][1], 'Revision 3')
        self.assertEqual(self.queued, ['Revision 1'])

    def test_crawl_failure_flushes_saved_pages_and_preserves_original_error(self):
        original = RuntimeError('original crawl failure')
        with self.crawl([0, 1], error=original), self.assertRaises(RuntimeError) as raised:
            adapt_site('测试大学', ROOT)
        self.assertIs(raised.exception, original)
        self.assertEqual(self.published[-1][1], 'Revision 2')
        self.assertEqual(self.queued, ['Revision 1'])

    def test_failed_best_effort_publication_never_masks_original_error(self):
        original = RuntimeError('original crawl failure')
        real_side_effect = self.publish.side_effect
        def fail_second(*args, **kwargs):
            if self.publish.call_count >= 2:
                raise OSError('snapshot disk unavailable')
            return real_side_effect(*args, **kwargs)
        self.publish.side_effect = fail_second
        with self.crawl([0, 1], error=original), self.assertLogs(self.app.logger, 'ERROR'), self.assertRaises(RuntimeError) as raised:
            adapt_site('测试大学', ROOT)
        self.assertIs(raised.exception, original)
        self.assertEqual(self.published, [(0, 'Revision 1')])

    def test_lost_lease_never_attempts_late_publication(self):
        original = tasks.LeaseLost('lost task ownership')
        with self.crawl([0, 1], error=original), self.assertRaises(tasks.LeaseLost) as raised:
            adapt_site('测试大学', ROOT)
        self.assertIs(raised.exception, original)
        self.assertEqual(self.published, [(0, 'Revision 1')])

    def test_final_publication_checks_ownership_before_writing(self):
        self.assert_owned.side_effect = [None, tasks.LeaseLost('lost before final flush')]
        with self.crawl([0, 1]), self.assertRaises(tasks.LeaseLost):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.published, [(0, 'Revision 1')])

    def test_parser_replacement_does_not_publish_outdated_completed_batch(self):
        from backend.scraper.discovery.parser_revision import ParserRevisionChanged
        with self.crawl([0, 1], error=ParserRevisionChanged('parser replaced')), self.assertRaises(ParserRevisionChanged):
            adapt_site('测试大学', ROOT)
        self.assertEqual(self.published, [(0, 'Revision 1')])


if __name__ == '__main__':
    unittest.main()
