"""Apply a saved navigation slice before requesting another paid decision."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from backend.database.db import db
from backend.scraper.discovery.ai_navigation import process_navigation
from backend.scraper.discovery.layered import navigation_candidates, route_structure
from backend.scraper.discovery.structure import extract_structure
from backend.ai.skill_loader import digest
from backend.services import tasks
from backend.services.runtime_catalog import RuntimeCatalog
from tests import test_navigation_execution_events as fixture_module


HTML = ('<title>测试大学</title><nav><a href="/entrance/">本科生培养</a>'
        '<a href="/next/">下一入口</a></nav>')


class SavedNavigationApplicationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture_module.NavigationExecutionEventTests()
        with patch.object(fixture_module, 'HTML', HTML):
            self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.handle = self.f.handle
        self.job = self.f.job
        self.root = fixture_module.ROOT
        payload = self.handle['payload']
        page, site = payload['page'], payload['site']
        parsed = extract_structure(HTML, page['url'], site['root_url'],
                                   page['kind'], page['label'], [])
        route_structure(parsed, page, HTML, site['root_url'], False, policy='valuable')
        candidates = navigation_candidates(HTML, parsed, page)
        self.assertEqual(len(candidates), 2)
        self.saved = {'url': self.root + 'entrance/', 'kind': 'unit', 'decision': 'follow',
                      'ai_reviewed': True, 'ai_role': 'unit', 'ai_value': 'relevant'}
        self.checkpoint = dict(self.handle['checkpoint'],
            route_material_hash=digest([(link['url'], link['label'], context)
                                        for link, context in candidates]),
            route_candidate_order=[[link['url'], link['label']] for link, _ in candidates],
            route_cursor=1, admitted_routes=[self.saved], unresolved_routes=[],
            navigation_unrecorded_decisions=['routes:previous-paid-decision'],
            unrelated_progress={'cursor': 7})
        with tasks.execution_scope(self.handle):
            tasks.checkpoint(self.checkpoint)
        for name in ('socket.getaddrinfo', 'socket.socket.connect',
                     'backend.ai.configuration.get_model_binding', 'backend.ai.runtime.run_skill'):
            guard = patch(name, side_effect=AssertionError('Saved application needs no new request or binding'))
            guard.start()
            self.addCleanup(guard.stop)

    def apply_saved_slice(self):
        with tasks.execution_scope(self.handle), self.assertRaises(tasks.TaskDeferred) as caught:
            process_navigation(self.handle['payload'])
        tasks.handoff(self.handle, caught.exception)
        db.session.refresh(self.job)

    def test_saved_roles_are_applied_before_another_model_binding_or_dispatch(self):
        self.assertIsNone(self.f.store.get_page(self.f.key, self.saved['url']))
        original = (self.job.id, self.job.identity, self.job.generation)
        self.apply_saved_slice()
        page = self.f.store.get_page(self.f.key, self.saved['url'])
        self.assertEqual((page['state'], page['kind']), ('pending', 'unit'))
        self.assertTrue(self.job.checkpoint.get('route_application_key'))
        self.assertEqual(self.job.checkpoint['route_cursor'], 1)
        self.assertEqual(self.job.checkpoint['admitted_routes'], [self.saved])
        self.assertEqual(self.job.checkpoint['unrelated_progress'], {'cursor': 7})
        self.assertEqual((self.job.id, self.job.identity, self.job.generation), original)

    def test_failed_publication_does_not_mark_application_complete_or_duplicate_the_page(self):
        with tasks.execution_scope(self.handle), patch.object(
                RuntimeCatalog, 'publish', side_effect=RuntimeError('Synthetic publication failure')):
            with self.assertRaisesRegex(RuntimeError, 'Synthetic publication failure'):
                process_navigation(self.handle['payload'])
        tasks.finish(self.handle, error=RuntimeError('Synthetic publication failure'))
        db.session.refresh(self.job)
        self.assertFalse(self.job.checkpoint.get('route_application_key'))
        self.job.available_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        self.handle = tasks.claim(capabilities=['directory'], exclude_kinds=('discover', 'collect'))
        self.assertEqual(self.handle['id'], self.job.id)
        self.apply_saved_slice()
        self.assertTrue(self.job.checkpoint.get('route_application_key'))
        pages = self.f.store.report(self.f.key)['pages']
        self.assertEqual(sum(p['url'] == self.saved['url'] for p in pages), 1)
        self.assertEqual(self.job.attempts, 1)

    def test_saved_role_for_an_unobserved_url_is_not_applied_or_marked_complete(self):
        checkpoint = deepcopy(self.checkpoint)
        checkpoint['admitted_routes'][0]['url'] = self.root + 'unobserved/'
        with tasks.execution_scope(self.handle):
            tasks.checkpoint(checkpoint)
            result = process_navigation(self.handle['payload'])
        self.assertEqual(result['error_code'], 'execution_conflict')
        self.assertIsNone(self.f.store.get_page(self.f.key, self.root + 'unobserved/'))
        self.assertFalse(self.handle['checkpoint'].get('route_application_key'))

    def test_material_context_change_keeps_the_saved_cursor_without_new_dispatch(self):
        checkpoint = deepcopy(self.checkpoint)
        checkpoint['route_material_hash'] = 'different-observation-context'
        with tasks.execution_scope(self.handle):
            tasks.checkpoint(checkpoint)
            result = process_navigation(self.handle['payload'])
        self.assertEqual(result['error_code'], 'execution_conflict')
        self.assertEqual(self.handle['checkpoint']['route_cursor'], 1)
        self.assertEqual(self.handle['checkpoint']['admitted_routes'], [self.saved])
        self.assertFalse(self.handle['checkpoint'].get('route_application_key'))

    def test_next_slice_can_continue_the_original_decision_sequence(self):
        self.apply_saved_slice()
        self.job.available_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.session.commit()
        self.handle = tasks.claim(capabilities=['directory'], exclude_kinds=('discover', 'collect'))
        self.assertEqual(self.handle['id'], self.job.id)
        with tasks.execution_scope(self.handle), \
                patch('backend.ai.configuration.get_model_binding', return_value={'id': 1, 'version': 1}), \
                patch('backend.ai.runtime.run_skill', return_value={'status': 'pending'}) as decision, \
                self.assertRaises(tasks.TaskDeferred) as caught:
            process_navigation(self.handle['payload'])
        tasks.handoff(self.handle, caught.exception)
        self.assertEqual(decision.call_count, 1)
        self.assertEqual(self.handle['checkpoint']['route_cursor'], 1)
        self.assertIn('route_pending_decision', self.handle['checkpoint'])
        self.assertEqual(sum(p['url'] == self.saved['url']
                             for p in self.f.store.report(self.f.key)['pages']), 1)


if __name__ == '__main__':
    unittest.main()
