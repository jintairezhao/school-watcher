"""A failing entrance yields to healthy work and keeps retry timing across restart."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.services.source_inventory import Inventory
from backend.scraper.discovery.inventory_crawler import inspect_page
from backend.scraper.acquisition import FetchResult
from backend.scraper.http_client import retry_direct_for


class InventoryNetworkRecoveryTests(unittest.TestCase):
    def test_page_retry_can_change_route_without_parent_task_failure(self):
        """Discovery catches page errors, so the enclosing task stays at attempt 0."""
        from copy import deepcopy
        from backend.services import tasks
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            url = 'http://example.edu.cn/'
            key = inventory.ensure_site('测试大学', url)
            site = inventory.all_sites()[0]
            handle = {'attempts': 0, 'checkpoint': {
                'http_retry_direct': {'example.edu.cn': 0}}}
            clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
            routes = []
            def reader(address):
                routes.append(retry_direct_for(address))
                result = FetchResult(address, outcome='network_error', error_code='http_serverdisconnectederror')
                return dict(url=address, status=0, html='', outcome=result.outcome, result=result)
            with patch('backend.services.source_inventory.now', side_effect=lambda: clock.isoformat(timespec='seconds')), \
                    patch('random.uniform', return_value=1):
                with tasks.execution_scope(handle):
                    inspect_page(inventory, site, inventory.claim(key), reader)
                clock += timedelta(seconds=30)
                inventory = Inventory(inventory.path)
                with tasks.execution_scope(deepcopy(handle)):
                    inspect_page(inventory, site, inventory.claim(key), reader)
                    # Context cannot leak into a fresh read or another hostname.
                    self.assertFalse(retry_direct_for(url))
                    self.assertFalse(retry_direct_for('http://other.edu.cn/'))
            self.assertEqual(routes, [False, True])

    def test_three_real_attempts_survive_restart_and_do_not_delay_other_pages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'inventory.db'
            clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
            url = 'https://example.edu.cn/'
            def stamp():
                return clock.isoformat(timespec='seconds')
            with patch('backend.services.source_inventory.now', side_effect=stamp), \
                    patch('random.uniform', return_value=1):
                inventory = Inventory(path)
                key = inventory.ensure_site('测试大学', url)
                site = inventory.all_sites()[0]
                inventory.enqueue(key, url + 'working/', '另一栏目', 'channel', 1, [], 'school_domain')
                calls = []
                def failing(address):
                    calls.append(address)
                    result = FetchResult(address, outcome='network_error', error_code='http_idle_timeout', message='timeout')
                    return dict(url=address, status=0, html='', outcome=result.outcome, error=result.message, result=result)
                for index, delay in enumerate((30, 120, 21600), 1):
                    page = inventory.claim(key)
                    self.assertEqual(page['url'], url)
                    inspect_page(inventory, site, page, failing)
                    inventory = Inventory(path)  # A new process sees the same deadline and count.
                    failed = inventory.get_page(key, url)
                    self.assertEqual(failed['network_failures'], index)
                    self.assertEqual(datetime.fromisoformat(failed['retry_at']) - clock, timedelta(seconds=delay))
                    if index == 1:
                        other = inventory.claim(key)
                        self.assertEqual(other['url'], url + 'working/')
                        inventory.finish(key, other['url'], state='fetched')
                    self.assertIsNone(inventory.claim(key))
                    inventory.retry(key)
                    self.assertIsNone(inventory.claim(key))
                    self.assertEqual(inventory.next_pending_delay(key), delay if index < 3 else None)
                    clock += timedelta(seconds=delay)
                self.assertEqual(len(calls), 3)
                inventory.retry(key)
                self.assertEqual(inventory.get_page(key, url)['network_failures'], 0)
                self.assertEqual(inventory.claim(key)['url'], url)

    def test_server_wait_is_preserved_and_nonretryable_denial_is_not_retried(self):
        with tempfile.TemporaryDirectory() as folder:
            inventory = Inventory(Path(folder) / 'inventory.db')
            url = 'https://example.edu.cn/'
            key = inventory.ensure_site('测试大学', url)
            site = inventory.all_sites()[0]
            clock = datetime(2026, 10, 1, tzinfo=timezone.utc)
            with patch('backend.services.source_inventory.now', return_value=clock.isoformat(timespec='seconds')):
                page = inventory.claim(key)
                result = FetchResult(url, status=429, outcome='network_error', retry_after=900, error_code='http_429')
                inspect_page(inventory, site, page, lambda _: dict(url=url, status=429, html='', outcome=result.outcome, result=result))
                self.assertEqual(inventory.next_pending_delay(key), 900)
                other = url + 'gone/'
                inventory.enqueue(key, other, '已撤下的栏目', 'channel', 1, [], 'school_domain')
                page = inventory.claim(key)
                denied = FetchResult(other, status=404, outcome='denied', error_code='http_404')
                inspect_page(inventory, site, page, lambda _: dict(url=other, status=404, html='', outcome=denied.outcome, result=denied))
                self.assertEqual(inventory.get_page(key, other)['state'], 'failed')
                inventory.retry(key)
                self.assertIsNone(inventory.claim(key))
